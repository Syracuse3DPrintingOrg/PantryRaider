"""The hardware-facing controller loop.

This is the only module that imports the Stream Deck device library. It opens
the first attached deck, picks a layout for its key count, renders the pages,
and wires key presses to the action handlers. A background task polls the app
for the counts shown on status keys.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime
from typing import Optional

import httpx

from . import actions, layout, render, theme
from . import config as config_mod
from .actions import (
    KEYPAD_CANCEL,
    KEYPAD_CLEAR,
    KEYPAD_ENTER,
    ActionContext,
    ActionSpec,
    HaEntityState,
    HealthState,
    PinBuffer,
    TimerState,
    WeatherState,
)
from .config import BRIGHTNESS_STEPS, Config

log = logging.getLogger("foodassistant.streamdeck")

# Watchdog cadence while the deck is present: steady so a config change (an
# orientation rewrite from the setup page) is picked up promptly.
WATCHDOG_INTERVAL = 5.0

# Reconnect backoff for a missing deck (FoodAssistant-o29k). A brief USB glitch
# recovers almost at once (first retry is quick), then the poll eases off to a
# steady idle interval so a deck that stays unplugged, or a box with no deck
# attached at all, waits quietly instead of hammering enumerate every few
# seconds or spinning the logs.
RECONNECT_BACKOFF_START = 1.0
RECONNECT_BACKOFF_MAX = 30.0
RECONNECT_BACKOFF_FACTOR = 2.0


def _next_backoff(current: float,
                  start: float = RECONNECT_BACKOFF_START,
                  maximum: float = RECONNECT_BACKOFF_MAX,
                  factor: float = RECONNECT_BACKOFF_FACTOR) -> float:
    """Next wait in the reconnect backoff sequence, capped at ``maximum``.

    A zero or negative ``current`` seeds the sequence at ``start``; each step
    multiplies by ``factor`` up to the cap. The cap is what keeps a permanent
    absence from busy-spinning: the poll settles at ``maximum`` and stays there.
    """
    if current <= 0:
        return start
    return min(current * factor, maximum)


# Activity on another surface (the kiosk screen) counts as fresh if it happened
# within this many seconds, slightly longer than the idle-loop tick so a single
# poll cannot miss it (FoodAssistant-otiy).
_SHARED_ACTIVITY_WINDOW_SECS = 12


def _external_activity_is_fresh(last_activity, now_epoch,
                                window=_SHARED_ACTIVITY_WINDOW_SECS) -> bool:
    """True when the bridge's shared last-activity epoch is recent enough to
    treat as activity on this deck. Pure helper for the shared-activity poll."""
    if not isinstance(last_activity, (int, float)) or last_activity <= 0:
        return False
    return 0 <= (now_epoch - last_activity) <= window


def _external_wake_due(last_activity, now_epoch, prev_seen,
                       window=_SHARED_ACTIVITY_WINDOW_SECS):
    """Decide whether a bridge activity report should wake this deck.

    Returns ``(wake, seen)``: ``wake`` is True when someone touched another
    surface (the kiosk screen) since our last look, and ``seen`` is the value
    to remember for the next poll. Two signals count, so a slow or skipped
    poll tick cannot miss a touch (FoodAssistant-exuv):

    - the report is inside the freshness window, or
    - the epoch ADVANCED past the one we saw last time (any advance means a
      new touch happened between polls, however long ago the poll ran).

    The very first poll (``prev_seen`` None) only trusts the window, so a
    stale epoch from before the controller started never fires a wake. A
    malformed or future-stamped epoch is ignored entirely.
    """
    if (not isinstance(last_activity, (int, float)) or last_activity <= 0
            or last_activity > now_epoch):
        return False, prev_seen
    fresh = _external_activity_is_fresh(last_activity, now_epoch, window)
    advanced = prev_seen is not None and last_activity > prev_seen
    return (fresh or advanced), float(last_activity)


def _idle_logo_due(deck_idle_reached, enabled, overlay_active) -> bool:
    """Whether a just-idled deck should show the logo instead of going dark.

    Pure decision for the idle blanker (FoodAssistant-gic5): the deck's own
    "Blank after idle" timeout has elapsed, the toggle is on, and no full-deck
    camera overlay owns the keys. When True the deck lights the Pantry Raider
    mark across every key in place of a black panel; when False it blanks fully
    as before. The logo is tied to the DECK's idle, not the kitchen display.
    """
    return bool(deck_idle_reached and enabled and not overlay_active)


class Controller:
    def __init__(self, deck, config: Config, config_path: Optional[str] = None) -> None:
        self.deck = deck
        self.config = config
        # Path of the TOML this controller was loaded from, if any. The
        # config-change watcher reloads it (and re-inits the deck) when the web
        # setup page rewrites it, so an orientation change applies in-process
        # without depending on a clean systemd bounce.
        self.config_path = config_path
        self.client: Optional[httpx.AsyncClient] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        # Guards a re-init so the watchdog and the config watcher cannot tear
        # the deck down at the same time.
        self._reinit_lock = asyncio.Lock()
        self._config_mtime = self._read_config_mtime()

        self.key_count: int = deck.key_count()
        # Advanced per-key overrides from the web setup page. Parsed into
        # ActionSpec entries and stamped onto the default layout, replacing the
        # stock action at each configured slot. Parsed before the pages are
        # built so the keys list can be padded out to the highest override slot,
        # keeping the pagination (and slot math) identical to the editor grid.
        self.key_overrides: dict[int, ActionSpec] = actions.overrides_to_specs(
            getattr(config, "key_overrides", []) or [], self.key_count
        )
        self.pages: list[list[Optional[ActionSpec]]] = layout.build_pages(
            layout.pad_keys_for_overrides(
                config.keys, self.key_overrides.keys(), self.key_count
            ),
            self.key_count,
        )
        layout.apply_overrides(self.pages, self.key_overrides, self.key_count)
        self.page = 0
        # On-deck PIN keypad. ``keypad_mode`` swaps the visible page for the
        # numeric pad; ``pin_buffer`` accumulates the entered code and
        # ``pin_status`` carries a short transient label (e.g. an error) shown
        # while the pad is up.
        self.keypad_mode: bool = False
        self.keypad_pages: list[list[Optional[ActionSpec]]] = layout.build_keypad_pages(
            self.key_count
        )
        self.keypad_page_idx: int = 0
        self.pin_buffer: PinBuffer = PinBuffer()
        self.pin_status: str = ""
        # Dynamic shopping quick-check page (542t). ``shopping_check_mode`` swaps
        # the visible page for one whose keys are the top items on the Mealie
        # shopping list; pressing a key checks that item off. ``shopping_check_page``
        # is the built page and ``shopping_check_items`` maps each item key's name
        # to its full item dict, so a press knows what to check off. Populated
        # when the page is opened and refreshed on the poll loop while it is up.
        self.shopping_check_mode: bool = False
        self.shopping_check_page: list[Optional[ActionSpec]] = []
        self.shopping_check_items: dict[str, dict] = {}
        self.status: dict[str, int] = {
            "expiring": 0, "pending": 0, "shopping": 0, "ready": 0,
        }
        # Today's planned meal name shown on a meal_today info key. Refreshed on
        # the normal status poll; defaults to a neutral face until first fetched.
        self.meal_today: str = "No meal"
        # Active barcode scanner mode label shown on a scan_mode key (8jbk).
        # Refreshed on the poll loop; defaults to the inventory ("Stock") face.
        self.scanner_mode_label: str = "Stock"
        self.timers: dict[str, TimerState] = {}  # action name -> timer state
        # Active-recipe timer suggestions mapped onto the timer keys. Keyed by
        # the timer action name (timer_1/2/3 and any timer-override slot name),
        # each value is a {label, seconds, step_index} descriptor. Empty when no
        # recipe is active, in which case the timer keys behave exactly as the
        # stock manual countdowns. Populated by the poll loop (sbu3).
        self.recipe_timer_specs: dict[str, dict] = {}
        # Toggles each poll tick while a timer alert is active so the key blinks
        # bright/dim until the alert is dismissed.
        self._blink_phase: int = 0
        self._key_down_time: dict[int, float] = {}  # physical key -> press timestamp
        self.weather: WeatherState = WeatherState(
            location=config.weather_location, units=config.weather_units,
            base_url=config.base_url,
        )
        # Host-bridge system health shown on a health key. Refreshed on the
        # status poll; neutral grey until first fetched (and when off-Pi).
        self.health: HealthState = HealthState()
        # Build per-slot HA entity state and override the static ActionSpec
        # placeholders (ha_1..ha_5) with slot config from config.toml.
        self.ha_entities: dict[str, HaEntityState] = {}
        _slot_names = [f"ha_{i}" for i in range(1, 6)]
        for slot_name, slot_cfg in zip(_slot_names, config.ha_slots):
            entity_id = slot_cfg.get("entity_id", "")
            if not entity_id:
                continue
            color_on = slot_cfg.get("color_on", actions._HA_STATE_COLOR_ON)
            color_off = slot_cfg.get("color_off", actions._HA_STATE_COLOR_OFF)
            label = slot_cfg.get("label", entity_id.split(".", 1)[-1].replace("_", " ").title())
            svc = slot_cfg.get("service", "homeassistant.toggle")
            actions.ACTIONS[slot_name] = ActionSpec(
                name=slot_name, label=label, color=color_off,
                kind="ha_entity", ha_entity_id=entity_id, ha_service=svc,
            )
            self.ha_entities[slot_name] = HaEntityState(entity_id, color_on, color_off)

        # Register state for override keys. Weather overrides may each carry
        # their own location, so they get a dedicated WeatherState keyed by the
        # spec name rather than sharing the single global widget. HA action
        # overrides get an HaEntityState so the key reflects live entity state.
        self.override_weather: dict[str, WeatherState] = {}
        for spec in self.key_overrides.values():
            # Both the current-conditions ("weather") and high/low ("forecast")
            # override faces draw from a per-location WeatherState so each tile
            # can target its own place rather than sharing the global widget.
            if spec.kind in ("weather", "forecast"):
                self.override_weather[spec.name] = WeatherState(
                    location=spec.weather_location or config.weather_location,
                    units=spec.weather_units or config.weather_units,
                    base_url=config.base_url,
                )
            elif spec.kind == "ha_entity" and spec.ha_entity_id:
                # Honour any per-override on/off colours; empty strings fall back
                # to the stock HA palette baked into HaEntityState's defaults.
                kwargs: dict[str, str] = {}
                if spec.color_on:
                    kwargs["color_on"] = spec.color_on
                if spec.color_off:
                    kwargs["color_off"] = spec.color_off
                self.ha_entities[spec.name] = HaEntityState(spec.ha_entity_id, **kwargs)

        try:
            self._bright_idx = BRIGHTNESS_STEPS.index(
                min(BRIGHTNESS_STEPS, key=lambda s: abs(s - config.brightness))
            )
        except ValueError:
            self._bright_idx = len(BRIGHTNESS_STEPS) // 2

        # Idle-blank state. _last_activity is reset on every key press.
        # _idle_blanked is True while the deck is blanked due to inactivity.
        # _wake_keys tracks which physical keys were pressed while blanked so
        # their release events can be swallowed without triggering actions.
        self._last_activity: float = time.monotonic()
        self._idle_blanked: bool = False
        self._wake_keys: set[int] = set()
        # False when reinit() failed to recover the deck (e.g. it is physically
        # unplugged and not yet back). The watchdog uses this to keep retrying
        # even when the health probe passes on the closed handle.
        self._deck_live: bool = True
        # True once the current disconnect has been logged, so the watchdog
        # reports "lost" and "reattached" once each rather than every retry
        # tick (FoodAssistant-o29k). Cleared when the deck is healthy again.
        self._deck_lost_logged: bool = False
        # Current reconnect backoff (seconds); grows while the deck is absent
        # and resets to 0 once it is back, so the next loss recovers promptly.
        self._reconnect_delay: float = 0.0

        # Camera snapshot cache, keyed by snapshot URL so several camera keys can
        # each show a different configured camera without evicting one another.
        # Each entry is (jpeg_bytes, monotonic_fetch_time) so the draw loop reuses
        # a recent frame instead of hammering the camera on every redraw.
        self._camera_cache: dict[str, tuple[bytes, float]] = {}
        # Full-deck overlay state. While active, a dedicated task refreshes the
        # whole deck from one frame and any key press exits it. _camera_full_name
        # is the chosen camera (by name) for the active overlay; empty = first.
        self._camera_full_active: bool = False
        self._camera_full_task: Optional[asyncio.Task] = None
        self._camera_full_name: str = ""
        # Idle logo face (FoodAssistant-gic5). When the deck's OWN idle-blank
        # timeout fires and logo_when_display_off is on, the deck lights the
        # Pantry Raider mark across every key instead of going dark; the face
        # coincides with _idle_blanked and exits on any key press or a
        # cross-surface wake, which returns the deck to its page. Storage key
        # stays logo_when_display_off for config compatibility.
        self._logo_face_active: bool = False
        # The bridge last-activity epoch seen on the previous shared-activity
        # poll, so an ADVANCE (a touch between polls) wakes the deck even if
        # the poll tick ran late (FoodAssistant-exuv). None until first read.
        self._seen_external_activity: Optional[float] = None
        # Last face pushed to each physical key, keyed by physical index. The
        # value is a tuple of everything that goes into rendering that face
        # (label, colour, count, alert, icon, style, rotation, ...), so an
        # unchanged key skips both the PIL render and the USB write. On a Pi 3
        # this matters: while a timer runs, _draw_page fires once a second, and
        # without this every key on the page was re-rasterised and re-sent even
        # though only the countdown face changed. Cleared whenever something
        # paints the deck outside _draw_page (overlays, splash, re-init).
        self._face_cache: dict[int, tuple] = {}

    # -- lifecycle ---------------------------------------------------------

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        headers = {"X-API-Key": self.config.api_key} if self.config.api_key else {}
        async with httpx.AsyncClient(timeout=8.0, headers=headers) as client:
            self.client = client
            self._open_deck()
            # First frame before any network work: replace the factory logo
            # with the Pantry Raider splash while the status polls run.
            self._show_splash()
            await self._poll_once()
            await self._refresh_weather()
            await self._refresh_ha_entities()
            self._draw_page()
            log.info(
                "Connected to %s (%d keys, %d page(s))",
                self.deck.deck_type(),
                self.key_count,
                len(self.pages),
            )
            await asyncio.gather(
                self._poll_forever(),
                self._idle_loop(),
                self._watchdog_loop(),
            )

    def _open_deck(self) -> None:
        """Open the HID device and put it into the rendered, callback-wired state.

        Called both at startup and on every re-init. Re-asserting the callback
        and brightness here (not just at first open) is what makes a re-opened
        deck responsive again after a teardown.

        A deck that arrives already open carries the early boot splash painted
        by __main__ before the heavy imports (FoodAssistant-krbn); skipping the
        open+reset pair keeps that splash on screen until the first real page
        draw, where a reset would blank it back to black for the rest of
        startup. Re-inits always come through _teardown_deck (which closes the
        handle), so they still take the full open+reset path.
        """
        try:
            already_open = bool(self.deck.is_open())
        except Exception:  # noqa: BLE001 - no is_open() on this handle/fake
            already_open = False
        if not already_open:
            self.deck.open()
            self.deck.reset()
        self.deck.set_brightness(BRIGHTNESS_STEPS[self._bright_idx])
        self.deck.set_key_callback(self._on_key)
        self._idle_blanked = False
        self._logo_face_active = False
        # A freshly opened (or reset) deck shows nothing we drew, so the next
        # _draw_page must push every face regardless of what was sent before.
        self._face_cache.clear()

    def _show_splash(self) -> None:
        """Paint the Pantry Raider brand mark across every key.

        Called right after the deck opens, before the first status poll, so
        the boot gap shows the raccoon splash instead of the Elgato factory
        logo (FoodAssistant-v32r). Best-effort: any failure just leaves the
        deck as-is, and the real page replaces the splash as soon as the
        controller finishes booting.
        """
        try:
            rows, cols = self._display_grid()
            key_size = self.deck.key_image_format()["size"]
            tiles = render.splash_tiles(rows, cols, key_size)
            if tiles:
                self._set_full_deck_tiles(tiles)
        except Exception:  # noqa: BLE001 - a splash must never block boot
            pass

    def _teardown_deck(self) -> None:
        """Reset and close the HID handle, swallowing any error.

        On an orientation change or a crashed deck the old handle may already
        be in a bad state, so every step is best-effort: the goal is to release
        the USB device so a fresh open() can claim it cleanly.
        """
        try:
            self.deck.reset()
        except Exception:  # noqa: BLE001 - the handle may already be dead
            pass
        try:
            self.deck.close()
        except Exception:  # noqa: BLE001
            pass

    async def reinit(self, reload_config: bool = False) -> bool:
        """Tear down the deck and bring it back up cleanly.

        Used for two cases: an orientation/config change (reload_config=True,
        which re-reads the TOML and rebuilds the page layout for the new
        rotation) and a watchdog recovery after the deck stopped responding.
        Returns True on success. A failure here leaves self.deck closed; the
        watchdog will retry on its next tick.
        """
        async with self._reinit_lock:
            self._teardown_deck()
            if reload_config and self.config_path:
                try:
                    new_cfg = config_mod.load(self.config_path)
                    self._apply_config(new_cfg)
                except Exception as e:  # noqa: BLE001 - keep the old config
                    log.warning("config reload failed, keeping current: %s", e)
            # If the original handle is gone (USB re-enumerated, e.g. after the
            # controller chip reset on a crash), pick up the freshly attached
            # deck instead of re-opening a stale handle.
            try:
                fresh = find_deck()
                if fresh is not None:
                    self.deck = fresh
            except Exception as e:  # noqa: BLE001 - fall back to the old handle
                log.debug("re-enumerate failed, reusing handle: %s", e)
            try:
                self._open_deck()
                self._draw_page()
                self._deck_live = True
                log.info("Stream Deck re-initialised (rotation=%d)", self.config.rotation)
                return True
            except Exception as e:  # noqa: BLE001 - watchdog will retry
                self._deck_live = False
                # Kept at debug: the watchdog logs the disconnect once as a
                # transition, so a deck left unplugged does not spin the log
                # with a re-init error on every backoff tick.
                log.debug("Stream Deck re-init failed (will retry): %s", e)
                return False

    def _apply_config(self, cfg: Config) -> None:
        """Adopt a freshly loaded config, rebuilding rotation-dependent layout.

        Only the fields that a setup-page rewrite can change and that the
        running controller reads each draw are refreshed here. The page grid is
        rebuilt because rotation and the key list both change which slot maps to
        which physical key.
        """
        self.config = cfg
        self.key_count = self.deck.key_count()
        self.key_overrides = actions.overrides_to_specs(
            getattr(cfg, "key_overrides", []) or [], self.key_count
        )
        self.pages = layout.build_pages(
            layout.pad_keys_for_overrides(
                cfg.keys, self.key_overrides.keys(), self.key_count
            ),
            self.key_count,
        )
        layout.apply_overrides(self.pages, self.key_overrides, self.key_count)
        self.keypad_pages = layout.build_keypad_pages(self.key_count)
        self.page = self.page % len(self.pages)
        try:
            self._bright_idx = BRIGHTNESS_STEPS.index(
                min(BRIGHTNESS_STEPS, key=lambda s: abs(s - cfg.brightness))
            )
        except ValueError:
            self._bright_idx = len(BRIGHTNESS_STEPS) // 2

    def close(self) -> None:
        self._teardown_deck()

    # -- rendering ---------------------------------------------------------

    def _current(self) -> list[Optional[ActionSpec]]:
        if self.keypad_mode:
            return self.keypad_pages[self.keypad_page_idx % len(self.keypad_pages)]
        if self.shopping_check_mode:
            return self.shopping_check_page
        return self.pages[self.page % len(self.pages)]

    def _draw_page(self) -> None:
        # The full-deck camera overlay and the display-off logo face each own
        # every key while active, so the normal page draw stays out of the way
        # until they exit.
        if self._camera_full_active or self._logo_face_active:
            return
        from StreamDeck.ImageHelpers import PILHelper

        rotation = self.config.rotation
        for index, spec in enumerate(self._current()):
            # The page slot `index` is a visual position; it maps to the
            # physical key it occupies after the deck is turned, and the face
            # cache is keyed by that physical index.
            phys = layout.rotated_index(index, self.key_count, rotation)
            if spec is None:
                face_key: tuple = ("blank", rotation)
                if self._face_cache.get(phys) == face_key:
                    continue
                image = render.blank_key(*self._key_size())
            else:
                # Recolour the key's base to match the active web UI theme; the
                # dynamic state colours (timer running, HA on/off) are computed
                # from this base, so theming flows through them (gxl).
                base_color = theme.themed_color(spec.name, spec.color, self.config.theme)
                if spec.kind == "keypad":
                    label, color = self._keypad_face(spec)
                    alert = False
                    count = None
                elif spec.kind == "timer":
                    t = self.timers.get(spec.name)
                    # When a recipe is active its suggestion relabels the key
                    # (e.g. "Pasta"); a running/alerting TimerState still wins so
                    # the countdown and Done! states render as before.
                    base_label = self._recipe_timer_label(spec.name, spec.label)
                    label = t.label(base_label) if t else base_label
                    color = (
                        t.color(base_color, self._blink_phase) if t else base_color
                    )
                    alert = t.alert_active() if t else False
                    count = None
                elif spec.kind == "weather":
                    w = self.override_weather.get(spec.name, self.weather)
                    label = w.label(spec.label)
                    color = w.color(base_color)
                    alert = False
                    count = None
                elif spec.kind == "forecast":
                    # A forecast override draws its high/low from its own
                    # per-location WeatherState; the stock forecast key (no
                    # override registered) keeps sharing the global widget.
                    w = self.override_weather.get(spec.name, self.weather)
                    label = w.forecast_label(spec.label)
                    color = w.forecast_color(base_color)
                    alert = False
                    count = None
                elif spec.kind == "ha_entity":
                    ha = self.ha_entities.get(spec.name)
                    label = ha.label(spec.label) if ha else spec.label
                    color = ha.color(base_color) if ha else base_color
                    alert = False
                    count = None
                elif spec.kind == "clock":
                    # Computed fresh each draw so the fast loop keeps it ticking.
                    # The face follows the 12/24-hour clock_format the app stamps
                    # into config.toml; an older config falls back to 24-hour.
                    label = actions._clock_label(
                        datetime.now(), clock_format=self.config.clock_format)
                    color = base_color
                    alert = False
                    count = None
                elif spec.kind == "info":
                    # An info key shows a polled text label (e.g. today's meal)
                    # rather than a count. Falls back to its stock label when the
                    # field is unknown.
                    label = self._info_label(spec)
                    color = base_color
                    alert = False
                    count = None
                elif spec.kind == "health":
                    # The health key's colour tracks the polled bridge state:
                    # green when clear, amber on warnings, grey when unreachable.
                    label = self.health.label(spec.label)
                    color = self.health.color(base_color)
                    alert = False
                    count = None
                elif spec.kind in ("camera", "camera_full"):
                    # A camera key shows the latest snapshot when one is cached;
                    # the face is painted below from the JPEG. Until a frame is
                    # available it falls back to a normal labelled face.
                    label = spec.label
                    color = base_color
                    alert = False
                    count = None
                elif spec.kind in (
                    "shopping_add", "macro", "ha_service",
                    "shopping_check", "shopping_check_page",
                ):
                    # Quick-add, macro, media (ha_service), and the shopping
                    # quick-check keys (the entry key plus each dynamic per-item
                    # key) are stateless faces: they show their configured label
                    # and colour and do their work on press (dispatched through
                    # actions.run_action), so there is no live count or alert to
                    # compute here.
                    label = spec.label
                    color = base_color
                    alert = False
                    count = None
                elif spec.kind == "scan_mode":
                    # The scan-mode key shows the active barcode scanner mode,
                    # refreshed by the poll loop.
                    label = f"Scan\n{self.scanner_mode_label}"
                    color = base_color
                    alert = False
                    count = None
                else:
                    count = (
                        self.status.get(spec.status_field)
                        if spec.kind == "status"
                        else None
                    )
                    label = spec.label
                    # The page-cycle keys show the current page on a paginated
                    # deck so you can tell where you are (FoodAssistant-unf9),
                    # e.g. "More" over "2/3". Only when there is more than one
                    # page (otherwise the More key would not be shown at all).
                    if spec.name in ("page_next", "page_prev") and len(self.keypad_pages) > 1:
                        label = "%s\n%d/%d" % (
                            spec.label, self.keypad_page_idx + 1, len(self.keypad_pages))
                    color = base_color
                    alert = bool(count)
                # A camera key's face also changes when a fresh snapshot lands,
                # so the cached JPEG participates in the face key via its hash.
                cam_cached: Optional[bytes] = None
                if spec.kind == "camera":
                    cam_cached = self._cached_snapshot(
                        self._camera_url_for(getattr(spec, "camera_name", "") or "")
                    )
                # Everything render_key (and the camera overlay below) uses to
                # produce this face. Unchanged means the physical key already
                # shows exactly this image, so skip the render and the USB push.
                face_key = (
                    spec.name, spec.kind, label, color, count, alert, spec.icon,
                    self.config.key_style, self.config.icon_color, rotation,
                    hash(cam_cached) if cam_cached is not None else None,
                )
                if self._face_cache.get(phys) == face_key:
                    continue
                image = render.render_key(
                    *self._key_size(),
                    label=label,
                    color=color,
                    count=count,
                    alert=alert,
                    icon=spec.icon,
                    key_style=self.config.key_style,
                    icon_color=self.config.icon_color,
                    action_name=spec.name,
                    emoji=actions.emoji_for(spec.name),
                    icon_fraction=render.icon_fraction_for(spec.kind),
                    text_only=render.text_only_kind(spec.kind),
                    feature_face=render.feature_face_kind(spec.kind),
                )
                # A camera key paints the latest snapshot over the fallback face
                # when a frame is cached for its chosen camera; if decoding fails
                # it keeps the label.
                if spec.kind == "camera" and cam_cached:
                    snap = render.image_from_jpeg(cam_cached, self._key_size())
                    if snap is not None:
                        image = snap
            if rotation:
                # PIL rotates counter-clockwise, so negate to turn the face
                # clockwise (matching how a user physically turns the deck).
                # The HDMI/kiosk display rotation is handled separately at the
                # OS level (xrandr / KMS) and is out of scope here.
                image = image.rotate(-rotation, expand=True)
            self.deck.set_key_image(phys, PILHelper.to_native_format(self.deck, image))
            self._face_cache[phys] = face_key

    def _keypad_face(self, spec: ActionSpec) -> tuple[str, str]:
        """Label and colour for a keypad key.

        Digit and Clear/Cancel keys show their static label. The Enter key
        doubles as the feedback surface: it shows masked dots for the entered
        code (never the digits themselves) or a transient status such as an
        error, so a user without a screen still sees their progress.
        """
        if spec.keypad_key == KEYPAD_ENTER:
            if self.pin_status:
                return self.pin_status, "#7f1d1d"
            if self.pin_buffer.is_empty():
                return spec.label, spec.color
            return self.pin_buffer.masked(), spec.color
        return spec.label, spec.color

    def _key_size(self) -> tuple[int, int]:
        w, h = self.deck.key_image_format()["size"]
        return w, h

    def _visual_slot(self, phys: int) -> int:
        """Recover the displayed-grid slot for a pressed physical key.

        ``layout.slot_for_physical`` is the exact inverse of the draw-time
        ``rotated_index`` mapping, so a press always resolves to the slot the
        user sees, for every rotation.
        """
        return layout.slot_for_physical(phys, self.key_count, self.config.rotation)

    # -- input -------------------------------------------------------------

    def _on_key(self, deck, key: int, pressed: bool) -> None:
        if self.loop is None:
            return
        # While the full-deck camera overlay is up, ANY key press exits it and
        # is otherwise swallowed (no action runs). Handled on the press edge so
        # the deck returns to normal immediately. The matching release is
        # ignored because the overlay key state was cleared on entry.
        if self._camera_full_active:
            if pressed:
                self._last_activity = time.monotonic()
                self._key_down_time.pop(key, None)
                self.loop.call_soon_threadsafe(self._exit_camera_full)
            return
        # The idle logo face is the deck's own idle-blank showing the mark
        # instead of black. A press means someone is at the deck, so it wakes
        # back to its page, tells the bridge (waking the kitchen display too,
        # FoodAssistant-fho8), and is swallowed so it never fires the action
        # underneath. Marking it a wake key swallows the matching release too.
        if self._logo_face_active:
            if pressed:
                self._last_activity = time.monotonic()
                self._key_down_time.pop(key, None)
                self._wake_keys.add(key)
                if self.loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        self._report_activity(), self.loop
                    )
                asyncio.run_coroutine_threadsafe(
                    self._wake_from_idle(), self.loop
                )
            return
        if pressed:
            # Record when this key went down so we can measure hold duration.
            self._key_down_time[key] = time.monotonic()
            # Any press counts as activity, resetting the idle timer.
            self._last_activity = time.monotonic()
            # Tell the host bridge so the kiosk display wakes too (shared
            # activity across surfaces, FoodAssistant-otiy). Best-effort, and
            # only when the loop is live (skipped in unit tests with no loop).
            if self.loop.is_running():
                asyncio.run_coroutine_threadsafe(self._report_activity(), self.loop)
            # If the deck is blanked, mark this key as a wake key and restore.
            if self._idle_blanked:
                self._wake_keys.add(key)
                asyncio.run_coroutine_threadsafe(
                    self._wake_from_idle(), self.loop
                )
            return
        # Key released: determine short vs long press.
        down_at = self._key_down_time.pop(key, None)
        long_press = down_at is not None and (time.monotonic() - down_at) >= 0.5
        # If this key woke the deck from idle, swallow the action.
        if key in self._wake_keys:
            self._wake_keys.discard(key)
            return
        # `key` is the physical index pressed. Invert the draw-time mapping to
        # recover the visual slot, so the action matches what the user sees.
        slot = self._visual_slot(key)
        page = self._current()
        if slot >= len(page) or page[slot] is None:
            return
        spec = page[slot]
        # A camera_full key takes over the whole deck. Enter the overlay on the
        # loop thread (it creates a task) rather than running the no-op handler.
        # Pass the key's chosen camera so a per-key override targets it.
        if spec.kind == "camera_full":
            cam_name = getattr(spec, "camera_name", "") or ""
            self.loop.call_soon_threadsafe(self._enter_camera_full, cam_name)
            return
        fut = asyncio.run_coroutine_threadsafe(
            self._handle(spec, long_press=long_press), self.loop
        )
        def _on_done(f):
            try:
                f.result()
            except Exception as e:
                log.error("Action failed: %s", e)
        fut.add_done_callback(_on_done)

    def _enter_keypad(self) -> None:
        """Switch the deck into PIN keypad mode with a fresh, empty buffer."""
        self.keypad_mode = True
        self.keypad_page_idx = 0
        self.pin_buffer.clear()
        self.pin_status = ""
        self._draw_page()

    def _exit_keypad(self) -> None:
        """Leave keypad mode and return to the normal layout."""
        self.keypad_mode = False
        self.pin_buffer.clear()
        self.pin_status = ""
        self._draw_page()

    async def _keypad_press(self, keypad_key: str) -> None:
        """Handle one keypad key. Digits accumulate; controls act immediately."""
        # Any press clears a lingering error so the next attempt starts clean.
        self.pin_status = ""
        if keypad_key.isdigit():
            self.pin_buffer.digit(keypad_key)
            self._draw_page()
            return
        if keypad_key == KEYPAD_CLEAR:
            self.pin_buffer.backspace()
            self._draw_page()
            return
        if keypad_key == KEYPAD_CANCEL:
            self._exit_keypad()
            return
        if keypad_key == KEYPAD_ENTER:
            await self._submit_pin()
            return

    async def _submit_pin(self) -> None:
        """Submit the buffered PIN. Return to normal on success, else show error."""
        if self.pin_buffer.is_empty() or self.client is None:
            self.pin_status = "Empty"
            self._draw_page()
            return
        ok = await actions.submit_pin(
            self.client, self.config.base_url, self.pin_buffer.value
        )
        if ok:
            self._exit_keypad()
        else:
            self.pin_buffer.clear()
            self.pin_status = "Wrong"
            self._draw_page()

    # -- shopping quick-check page (542t) ----------------------------------

    def _build_shopping_check_page(self, payload: dict) -> None:
        """Rebuild the quick-check page and item map from a shopping payload.

        Maps the top unchecked items onto item keys (one per slot up to the
        page's capacity), records each key's full item dict so a press can check
        it off, and lays the keys out with a trailing Back key. Pure of I/O: the
        caller fetches the payload.
        """
        cap = layout.shopping_check_capacity(self.key_count)
        key_specs = actions.shopping_check_key_specs(payload, cap)
        item_specs, items = actions.shopping_check_action_specs(key_specs)
        self.shopping_check_items = items
        self.shopping_check_page = layout.build_shopping_check_page(
            item_specs, self.key_count
        )

    async def _enter_shopping_check(self) -> None:
        """Switch the deck into the shopping quick-check page.

        Fetches the current shopping list, builds the item keys, and swaps the
        visible page. An empty or unreachable list still opens the page (just a
        Back key) so the press never feels dead."""
        payload = {"items": []}
        if self.client is not None:
            payload = await actions.fetch_shopping_items(
                self.client, self.config.base_url
            )
        self._build_shopping_check_page(payload)
        self.shopping_check_mode = True
        self._draw_page()

    def _exit_shopping_check(self) -> None:
        """Leave the quick-check page and return to the normal layout."""
        self.shopping_check_mode = False
        self.shopping_check_items = {}
        self.shopping_check_page = []
        self._draw_page()

    async def _refresh_shopping_check(self) -> None:
        """Re-fetch the shopping list and rebuild the quick-check page in place.

        Called after a check-off and on the poll loop while the page is up, so
        the keys stay in step with the list (a checked item drops out, a newly
        added one appears). Best-effort: a failure leaves the last page up."""
        if not self.shopping_check_mode or self.client is None:
            return
        payload = await actions.fetch_shopping_items(
            self.client, self.config.base_url
        )
        self._build_shopping_check_page(payload)
        self._draw_page()

    async def _shopping_check_press(self, name: str) -> str:
        """Check off the item bound to a quick-check key, then refresh the page.

        Returns a short face. An unknown key (its item already gone) is a safe
        no-op. After a successful check the page is rebuilt so the item drops
        out and the rest shuffle up."""
        item = self.shopping_check_items.get(name)
        if item is None or self.client is None:
            return "Gone"
        face = await actions.check_shopping_item(
            self.client, self.config.base_url, item
        )
        await self._refresh_shopping_check()
        return face

    async def _timer_press(self, name: str, long_press: bool = False) -> None:
        """Handle a timer key press against the shared server registry.

        The server registry is the single source of truth: a press that
        starts, restarts, or stops a countdown creates or cancels the matching
        server timer, and the key face only mirrors it, so the web UI and
        satellites always agree with the deck. Short press from idle starts
        the key's duration (the active recipe's suggestion, the key's preset,
        or the first cycle stage); a short press while running restarts a
        preset, advances a plain key to its next cycle stage (stopping after
        the last), or, on a recipe key, jumps the kiosk to the recipe
        (FoodAssistant-y7ud). A long press cancels; a press on an expired
        alert dismisses it, removing the finished timer everywhere. When the
        server cannot be reached the countdown still runs on this deck alone.
        """
        if name not in self.timers:
            self.timers[name] = TimerState()
        timer = self.timers[name]
        recipe_spec = self.recipe_timer_specs.get(name)
        preset = self._override_timer_minutes(name)
        try:
            if long_press or timer.alert_active():
                # Reset or dismiss: drop the server timer so every surface clears.
                await self._cancel_deck_timer(timer)
            elif timer.is_running():
                if recipe_spec is not None:
                    # Shortcut to the recipe; leave the countdown untouched.
                    self._navigate_async("ui/current-recipe")
                    return
                if preset > 0:
                    # Press again to restart the preset from the top.
                    await self._cancel_bound_server_timer(timer)
                    await self._start_deck_timer(timer, name, preset * 60)
                else:
                    # Cycle key: replace the countdown with the next stage, or
                    # stop after the last one.
                    seconds = timer.next_cycle_seconds()
                    await self._cancel_bound_server_timer(timer)
                    if seconds > 0:
                        await self._start_deck_timer(timer, name, seconds)
                    else:
                        timer.clear()
            elif recipe_spec is not None and recipe_spec.get("seconds"):
                await self._start_deck_recipe_timer(timer, recipe_spec)
            elif preset > 0:
                await self._start_deck_timer(timer, name, preset * 60)
            else:
                await self._start_deck_timer(timer, name, timer.next_cycle_seconds())
        finally:
            # Reset the blink phase so a fresh alert starts on its bright frame.
            self._blink_phase = 0
            self._draw_page()

    async def _start_deck_timer(self, timer: TimerState, name: str,
                                seconds: float) -> None:
        """Start a shared server timer for a key and bind its face to it.

        Falls back to a deck-local countdown when the server cannot be reached
        (or there is no client, as in unit tests), so the kitchen timer keeps
        working offline. The key's cycle position is untouched: bind and
        start_local only set the countdown, so a cycling key keeps its stage.
        """
        if seconds <= 0:
            return
        created = None
        if self.client is not None:
            created = await actions.create_server_timer(
                self.client, self.config.base_url, self._timer_label(name), seconds
            )
        if created is not None:
            timer.bind(created)
        else:
            timer.start_local(seconds)
        # The screen does not change on a timer press, so confirm it on the
        # kiosk (FoodAssistant-rdlo). Best-effort; the helper no-ops offline.
        await self._confirm_timer_started(self._timer_label(name))

    async def _confirm_timer_started(self, label: str) -> None:
        """Queue a brief "timer started" toast on the kiosk. Best-effort."""
        clean = str(label or "").strip()
        msg = (f"{clean} timer started"
               if clean and clean.lower() != "timer" else "Timer started")
        await actions.post_deck_confirmation(
            self.client, self.config.base_url, msg
        )

    async def _start_deck_recipe_timer(self, timer: TimerState,
                                       recipe_spec: dict) -> None:
        """Start the shared server timer for a recipe suggestion and bind the
        key's face to it, falling back to a deck-local countdown offline."""
        created = None
        if self.client is not None:
            created = await actions.create_recipe_timer(
                self.client,
                self.config.base_url,
                step_index=recipe_spec.get("step_index"),
                label=recipe_spec.get("label", ""),
                seconds=recipe_spec.get("seconds"),
            )
        if created is not None:
            timer.bind(created)
        else:
            timer.start_local(float(recipe_spec.get("seconds") or 0))
        await self._confirm_timer_started(recipe_spec.get("label", ""))

    async def _cancel_bound_server_timer(self, timer: TimerState) -> None:
        """Best-effort DELETE of a key's bound server timer, keeping the rest
        of the key's local state (used mid-cycle before starting the next
        stage). A local-only run has nothing to cancel remotely."""
        if timer.timer_id is not None and self.client is not None:
            await actions.cancel_server_timer(
                self.client, self.config.base_url, timer.timer_id
            )
        timer.timer_id = None

    async def _cancel_deck_timer(self, timer: TimerState) -> None:
        """Cancel a key's timer everywhere: the server registry entry (so the
        web UI clears on its next poll) and the local face."""
        await self._cancel_bound_server_timer(timer)
        timer.clear()

    def _timer_label(self, name: str) -> str:
        """Best-effort display label for a timer key (per-key override first,
        then the static ACTIONS registry), used to name the shared server timer.
        Falls back to a generic 'Timer' so the server entry is never blank."""
        for spec in self.key_overrides.values():
            if spec.name == name and spec.kind == "timer":
                return actions.clean_timer_label(spec.label) or "Timer"
        spec = actions.ACTIONS.get(name)
        if spec is not None and spec.kind == "timer":
            return actions.clean_timer_label(spec.label) or "Timer"
        return "Timer"

    def _timer_key_names(self) -> list[str]:
        """Timer action names in page order, deduplicated, first occurrence wins.

        Drawn from the live page layout so timer-override slots are included
        alongside the stock timer_1/2/3 keys. The order is what the recipe
        suggestions are assigned against (first timer key gets the first
        suggestion), so it mirrors what the user sees left-to-right, top-to-bottom.
        """
        names: list[str] = []
        for page in self.pages:
            for spec in page:
                if spec is not None and spec.kind == "timer" and spec.name not in names:
                    names.append(spec.name)
        return names

    async def _refresh_recipe_timers(self) -> None:
        """Fetch the active recipe's timer suggestions and map them onto the
        timer keys. Best-effort: failures clear nothing destructively, they just
        leave the keys on their last state. An empty result (no active recipe)
        clears the mapping so the keys return to their stock manual labels."""
        if self.client is None:
            return
        names = self._timer_key_names()
        if not names:
            return
        suggestions = await actions.fetch_timer_suggestions(
            self.client, self.config.base_url
        )
        defaults = [self._timer_default_label(n) for n in names]
        specs = actions.recipe_timer_key_specs(suggestions, len(names), defaults)
        new_map: dict[str, dict] = {}
        for name, spec in zip(names, specs):
            # Only record slots that carry a real suggestion; a None duration is a
            # fallback slot that should keep behaving like a manual timer key.
            if spec.get("seconds") is not None:
                new_map[name] = spec
        if new_map != self.recipe_timer_specs:
            self.recipe_timer_specs = new_map
            self._draw_page()

    def _timer_key_labels(self) -> dict[str, str]:
        """Label each timer key answers to when adopting a shared server timer.

        The active recipe's suggestion label wins (so a step timer started on
        the recipe page lands on the key showing that step), falling back to
        the key's own display label (so a "Pasta" timer started from the web
        lands on the deck's Pasta key)."""
        labels: dict[str, str] = {}
        for name in self._timer_key_names():
            spec = self.recipe_timer_specs.get(name)
            labels[name] = (spec or {}).get("label") or self._timer_label(name)
        return labels

    async def _refresh_server_timers(self) -> None:
        """Reconcile every timer key with the shared server registry, on poll.

        Piggybacks on the existing status poll (one GET /timers, no extra
        loop, kind to a Pi 3). A timer cancelled from the web UI clears its
        deck key on this pass, a running timer started elsewhere lands on the
        matching key by label, and a bound key's deadline is corrected if the
        timer was replaced. Skipped when the deck shows no timer keys and
        holds no bindings, so such a deck never pays for the call."""
        if self.client is None:
            return
        names = self._timer_key_names()
        if not names and not self.timers:
            return
        server_timers = await actions.fetch_timers(self.client, self.config.base_url)
        for name in names:
            if name not in self.timers:
                self.timers[name] = TimerState()
        if actions.sync_timer_bindings(
            self.timers, self._timer_key_labels(), server_timers, time.time()
        ):
            self._draw_page()

    def _has_kind(self, *kinds: str) -> bool:
        """True when any visible page slot carries one of the given kinds."""
        wanted = set(kinds)
        for page in self.pages:
            for spec in page:
                if spec is not None and spec.kind in wanted:
                    return True
        return False

    async def _refresh_meal_today(self) -> None:
        """Fetch today's planned meal for the meal_today info key.

        Best-effort and only when a meal_today key is actually shown, so a deck
        without one never pays for the call. A failure degrades to the neutral
        fallback rather than disturbing the status poll."""
        if self.client is None:
            return
        has_meal_key = any(
            spec is not None and spec.kind == "info"
            and spec.status_field == "meal_today"
            for page in self.pages for spec in page
        )
        if not has_meal_key:
            return
        label = await actions.fetch_meal_today(self.client, self.config.base_url)
        if label != self.meal_today:
            self.meal_today = label
            self._draw_page()

    def _set_scanner_mode_label(self, label: str) -> None:
        """Paint a just-cycled scanner mode on the scan_mode key immediately,
        so the face answers the press before the full status refresh lands."""
        if label and label != self.scanner_mode_label:
            self.scanner_mode_label = label
            self._draw_page()

    async def _refresh_scanner_mode(self) -> None:
        """Refresh the active scanner mode label for any scan_mode key.

        Best-effort and only when a scan_mode key is shown, so a deck without one
        never pays for the call. Redraws when the mode changes (for example after
        it is cycled from another device)."""
        if self.client is None or not self._has_kind("scan_mode"):
            return
        try:
            r = await self.client.get(
                f"{self.config.base_url.rstrip('/')}/pending/scanner-mode", timeout=4.0
            )
            label = (r.json() or {}).get("label", "") if r.status_code == 200 else ""
        except Exception:  # noqa: BLE001 - unreachable -> keep last label
            return
        if label and label != self.scanner_mode_label:
            self.scanner_mode_label = label
            self._draw_page()

    async def _refresh_health(self) -> None:
        """Refresh the host-bridge health summary for any health key.

        Best-effort and only when a health key is actually shown, so a deck
        without one never polls the bridge. An unreachable bridge degrades to
        the neutral grey state rather than disturbing the status poll."""
        if not self._has_kind("health"):
            return
        before = (self.health.label("Health"), self.health.color("#000"))
        await self.health.refresh(getattr(self.config, "host_bridge_url", ""))
        after = (self.health.label("Health"), self.health.color("#000"))
        if after != before:
            self._draw_page()

    # -- camera ------------------------------------------------------------

    def _camera_entry_for(self, name: str = "") -> Optional[dict]:
        """The camera dict called ``name``, or the first usable one when blank.

        ``config.cameras`` is a list of dicts pushed by the app. A non-empty
        ``name`` matches by camera name (case-insensitive); if it does not match,
        or is blank, the first camera with a snapshot URL or an HA entity is used
        so a key bound to a since-removed camera still shows something."""
        cams = getattr(self.config, "cameras", None) or []
        want = name.strip().lower()
        if want:
            for cam in cams:
                if isinstance(cam, dict) and str(cam.get("name", "")).strip().lower() == want:
                    return cam
        for cam in cams:
            if isinstance(cam, dict) and (cam.get("snapshot_url") or cam.get("ha_entity")):
                return cam
        return None

    def _camera_target(self, name: str = "") -> tuple[str, Optional[dict]]:
        """Return (snapshot_url, headers) for the chosen camera, or ("", None).

        Home Assistant cameras resolve to a bearer-authenticated URL (HA rejects
        the token in the query string); other cameras use their stored URL."""
        cam = self._camera_entry_for(name)
        if not cam:
            return "", None
        return actions.camera_snapshot_target(
            cam,
            getattr(self.config, "ha_base_url", "") or "",
            getattr(self.config, "ha_token", "") or "",
        )

    def _camera_url_for(self, name: str = "") -> str:
        """Snapshot URL (the cache key) for the camera called ``name``."""
        return self._camera_target(name)[0]

    def _first_camera_url(self) -> str:
        """Snapshot URL of the first configured camera, or "" when none."""
        return self._camera_url_for("")

    def _cached_snapshot(self, url: str) -> Optional[bytes]:
        """Last good JPEG bytes cached for ``url``, or None if nothing cached."""
        entry = self._camera_cache.get(url)
        return entry[0] if entry else None

    async def _camera_snapshot(self, url: str = "", max_age: float = 0.0,
                               headers: Optional[dict] = None) -> Optional[bytes]:
        """Fetch a camera snapshot JPEG, cached per URL. None on failure.

        ``url`` is the snapshot endpoint (empty resolves to the first camera) and
        ``headers`` carries any auth (HA cameras need a bearer header). Reuses the
        cached frame when it is younger than ``max_age`` seconds (0 forces a fresh
        fetch) so the draw loop does not hammer the camera. Any network or service
        error returns None and leaves the cache untouched, so a transient hiccup
        keeps showing the last good frame rather than blanking.
        """
        if not url:
            url, headers = self._camera_target("")
        if not url or self.client is None:
            return None
        if max_age > 0:
            entry = self._camera_cache.get(url)
            if entry is not None and (time.monotonic() - entry[1]) < max_age:
                return entry[0]
        try:
            r = await self.client.get(url, timeout=4.0, headers=headers or None)
            if r.status_code == 200 and r.content:
                self._camera_cache[url] = (r.content, time.monotonic())
                return r.content
        except Exception:  # noqa: BLE001 - keep the last good frame, never crash
            pass
        return None

    def _shown_camera_targets(self) -> list[tuple[str, Optional[dict]]]:
        """Distinct (url, headers) for the single-key camera faces on this page."""
        targets: list[tuple[str, Optional[dict]]] = []
        seen: set[str] = set()
        for spec in self._current():
            if spec is not None and spec.kind == "camera":
                url, headers = self._camera_target(getattr(spec, "camera_name", "") or "")
                if url and url not in seen:
                    seen.add(url)
                    targets.append((url, headers))
        return targets

    async def _refresh_camera_snapshot(self) -> None:
        """Refresh the cached snapshots for the camera faces on this page, on poll.

        Best-effort and only for the cameras a visible key actually shows, so a
        deck without one never pays for the fetch and two keys on different
        cameras each refresh. Redraws when any new frame arrives so the faces stay
        current between presses."""
        targets = self._shown_camera_targets()
        if not targets:
            return
        changed = False
        for url, headers in targets:
            before = self._cached_snapshot(url)
            snap = await self._camera_snapshot(url, max_age=0.0, headers=headers)
            if snap is not None and snap is not before:
                changed = True
        if changed:
            self._draw_page()

    def _enter_camera_full(self, camera_name: str = "") -> None:
        """Take over the whole deck with the live camera overlay.

        Starts a refresh task that paints every key from one frame of the chosen
        camera (``camera_name`` empty = the first camera). A no camera or no event
        loop case is a safe no-op. Re-entry while already active is ignored so a
        double press does not stack tasks.
        """
        if self._camera_full_active or self.loop is None or not self.loop.is_running():
            return
        self._camera_full_name = camera_name or ""
        self._camera_full_active = True
        # Treat the overlay as activity and ensure the deck is lit, so it does
        # not fight the idle blanker while the user is watching.
        self._last_activity = time.monotonic()
        if self._idle_blanked:
            self._idle_blanked = False
            try:
                self.deck.set_brightness(BRIGHTNESS_STEPS[self._bright_idx])
            except Exception:  # noqa: BLE001 - best-effort wake
                pass
        self._camera_full_task = self.loop.create_task(self._camera_full_loop())

    def _exit_camera_full(self) -> None:
        """Leave the full-deck overlay and redraw the normal current page."""
        if not self._camera_full_active:
            return
        self._camera_full_active = False
        task, self._camera_full_task = self._camera_full_task, None
        if task is not None:
            task.cancel()
        self._last_activity = time.monotonic()
        self._draw_page()

    def _set_full_deck_tiles(self, tiles: list) -> None:
        """Push a row-major list of per-key tiles to the physical deck.

        Honours the configured rotation exactly like the normal draw loop, so a
        turned deck shows the frame the right way up. Defensive: a short or empty
        tile list simply skips the missing keys rather than raising.
        """
        from StreamDeck.ImageHelpers import PILHelper

        # The overlay overwrites whatever faces the page draw left behind, so
        # the next _draw_page must repaint every key from scratch.
        self._face_cache.clear()

        rotation = self.config.rotation
        for index, tile in enumerate(tiles):
            if index >= self.key_count:
                break
            image = tile.rotate(-rotation, expand=True) if rotation else tile
            phys = layout.rotated_index(index, self.key_count, rotation)
            self.deck.set_key_image(phys, PILHelper.to_native_format(self.deck, image))

    async def _camera_full_refresh_once(self) -> bool:
        """Paint one frame across the whole deck. Returns False when unavailable.

        Fetches a fresh snapshot, slices it across every key, and pushes the
        tiles. When there is no camera or the snapshot fails it paints a simple
        "No camera" message across the deck and returns False so the caller can
        decide to stop. Any unexpected error is swallowed so the overlay loop
        never crashes the daemon.
        """
        rows, cols = self.deck.key_layout()
        key_size = self.deck.key_image_format()["size"]
        try:
            _url, _headers = self._camera_target(self._camera_full_name)
            snap = await self._camera_snapshot(_url, max_age=0.0, headers=_headers)
            if snap is None:
                self._set_full_deck_tiles(
                    render.message_across_deck(rows, cols, key_size, "No camera")
                )
                return False
            from PIL import Image
            import io as _io
            with Image.open(_io.BytesIO(snap)) as src:
                src.load()
                frame = src.convert("RGB")
            tiles = render.slice_full_image(frame, rows, cols, key_size)
            self._set_full_deck_tiles(tiles)
            return True
        except Exception:  # noqa: BLE001 - never let the overlay crash the loop
            try:
                self._set_full_deck_tiles(
                    render.message_across_deck(rows, cols, key_size, "No camera")
                )
            except Exception:  # noqa: BLE001
                pass
            return False

    async def _camera_full_loop(self) -> None:
        """Refresh the full-deck overlay until it is exited or cancelled."""
        try:
            while self._camera_full_active:
                ok = await self._camera_full_refresh_once()
                # Each tick counts as activity so the idle blanker stays out of
                # the way while the overlay is up.
                self._last_activity = time.monotonic()
                if not ok and not self._camera_url_for(self._camera_full_name):
                    # No camera is even configured: stop rather than spin showing
                    # the placeholder forever. A configured-but-down camera keeps
                    # retrying so it recovers when the feed comes back.
                    self._camera_full_active = False
                    self._camera_full_task = None
                    self._draw_page()
                    return
                await asyncio.sleep(
                    max(1, int(self.config.camera_full_refresh_seconds))
                )
        except asyncio.CancelledError:  # noqa: PERF203 - normal exit path
            pass

    # -- idle logo face (FoodAssistant-gic5, was -zttc) ----------------------

    def _display_grid(self) -> tuple[int, int]:
        """(rows, cols) of the key grid as the user sees it after rotation.

        A full-deck frame is composed for the displayed grid and pushed
        through ``_set_full_deck_tiles``, which rotates each tile onto its
        physical key, so a turned deck shows the image the right way up."""
        if self.key_count in layout.GRID:
            d_cols, d_rows = layout.display_dims(self.key_count, self.config.rotation)
            return d_rows, d_cols
        rows, cols = self.deck.key_layout()
        return rows, cols

    def _enter_display_off_logo(self) -> None:
        """Light the Pantry Raider mark across every key when the deck idles.

        One static frame, no loop; ``_draw_page`` stays out of the way until the
        face exits. Best-effort: a failed paint leaves the page up rather than
        raising into the idle loop."""
        if self._logo_face_active:
            return
        self._logo_face_active = True
        try:
            rows, cols = self._display_grid()
            key_size = self.deck.key_image_format()["size"]
            tiles = render.splash_tiles(rows, cols, key_size)
            if tiles:
                self._set_full_deck_tiles(tiles)
        except Exception:  # noqa: BLE001 - keep the page rather than crash
            self._logo_face_active = False

    def _exit_display_off_logo(self) -> None:
        """Leave the idle logo face and redraw the normal page."""
        if not self._logo_face_active:
            return
        self._logo_face_active = False
        self._draw_page()

    def _timer_default_label(self, name: str) -> str:
        """The stock label a timer key shows with no recipe suggestion."""
        spec = actions.ACTIONS.get(name)
        if spec is not None:
            return spec.label
        for ov in self.key_overrides.values():
            if ov.name == name:
                return ov.label
        return "Timer"

    def _recipe_timer_label(self, name: str, base_label: str) -> str:
        """Label for a timer key, preferring the active recipe's suggestion."""
        spec = self.recipe_timer_specs.get(name)
        if spec is not None:
            return spec.get("label") or base_label
        return base_label

    def _info_label(self, spec: ActionSpec) -> str:
        """Label for an info key, drawn from its polled field.

        Today only the meal_today field is wired; an unknown field falls back to
        the key's stock label so a new info key degrades gracefully.
        """
        if spec.status_field == "meal_today":
            return self.meal_today or spec.label
        return spec.label

    def _override_timer_minutes(self, name: str) -> int:
        """Preset minutes for a timer key, or 0 for a count-up timer.

        Checks per-key overrides first, then the static ACTIONS registry so the
        stock preset timers (timer_eggs/pasta/rice) load their whole duration on
        a single press just like a configured timer override.
        """
        for spec in self.key_overrides.values():
            if spec.name == name and spec.kind == "timer":
                return spec.timer_minutes
        spec = actions.ACTIONS.get(name)
        if spec is not None and spec.kind == "timer":
            return spec.timer_minutes
        return 0

    def _weather_for(self, name: str) -> WeatherState:
        """The WeatherState a widget key draws from (per-key override or shared)."""
        return self.override_weather.get(name, self.weather)

    def _weather_cycle(self, name: str) -> None:
        """Advance the pressed weather key to its next stat and redraw."""
        self._weather_for(name).cycle_stat()
        self._draw_page()

    def _forecast_cycle(self, name: str) -> None:
        """Advance the pressed forecast key to its next day and redraw.

        A forecast override draws from its own per-location WeatherState; the
        stock forecast key falls back to the shared widget.
        """
        self._weather_for(name).cycle_forecast_day()
        self._draw_page()

    async def _handle(self, spec: ActionSpec, long_press: bool = False) -> None:
        ctx = ActionContext(
            client=self.client,
            base_url=self.config.base_url,
            refresh=self._refresh,
            navigate=self._navigate,
            cycle_brightness=self._cycle_brightness,
            page_next=self._page_next,
            page_prev=self._page_prev,
            timer_press=self._timer_press,
            weather_refresh=self._refresh_weather,
            weather_cycle=self._weather_cycle,
            forecast_cycle=self._forecast_cycle,
            scanner_label_set=self._set_scanner_mode_label,
            ha_base_url=self.config.ha_base_url,
            ha_token=self.config.ha_token,
            host_bridge_url=getattr(self.config, "host_bridge_url", ""),
            bridge_token_path=getattr(self.config, "bridge_token_path", ""),
            ha_entity_refresh=self._refresh_ha_entities,
            keypad_enter=self._enter_keypad,
            keypad_press=self._keypad_press,
            shopping_check_enter=self._enter_shopping_check,
            shopping_check_press=self._shopping_check_press,
        )
        try:
            msg = await actions.run_action(spec, ctx, long_press=long_press)
            if msg:
                log.info("%s -> %s", spec.name, msg)
        except Exception as e:  # noqa: BLE001 - one bad press must not crash
            log.warning("action %s failed: %s", spec.name, e)

    # -- effects exposed to actions ---------------------------------------

    async def _wake_from_idle(self) -> None:
        """Restore the current page after the idle timer blanked or logo-lit the deck.

        Clears the idle logo face first so the guarded _draw_page below actually
        repaints (a live logo face otherwise makes _draw_page a no-op)."""
        self._idle_blanked = False
        self._logo_face_active = False
        self.deck.set_brightness(BRIGHTNESS_STEPS[self._bright_idx])
        self._draw_page()

    def _reset_weather_cycles_if_idle(self) -> None:
        """Return any cycled weather/forecast key to its default after a quiet
        spell, so a glance-and-leave deck looks stock again.

        Independent of the idle-blank timeout: even with blanking disabled, a
        weather stat or forecast day the user paged to should drift back to the
        default once no one has touched it for ``WEATHER_AUTO_RESET_SECS``. Only
        redraws when something actually reset, so the common idle tick stays
        cheap.
        """
        now = time.monotonic()
        changed = False
        for w in (self.weather, *self.override_weather.values()):
            if actions.should_auto_reset(now, w.last_interaction):
                w.reset_to_default()
                changed = True
        if changed:
            self._draw_page()

    async def _idle_loop_once(self) -> None:
        """Check idle state and blank the deck if the timeout has elapsed.

        This is the per-tick body extracted for testability. The main
        _idle_loop calls this repeatedly on a 10-second interval.
        """
        self._reset_weather_cycles_if_idle()
        timeout_mins = self.config.idle_timeout_minutes
        # The camera overlay treats each refresh tick as activity, but guard here
        # too so the blanker never fights a live overlay. _idle_blanked is set
        # whether the deck went fully dark OR is showing the idle logo, so the
        # timeout never re-fires while already idle.
        if timeout_mins <= 0 or self._idle_blanked or self._camera_full_active:
            return
        idle_secs = time.monotonic() - self._last_activity
        if idle_secs >= timeout_mins * 60:
            # The deck has reached its idle timeout. With the toggle on, light
            # the Pantry Raider mark instead of blanking to black; a key press
            # (or a cross-surface wake) returns to the keys. With it off, go
            # fully dark exactly as before. Either way the deck is now idle.
            self._idle_blanked = True
            if _idle_logo_due(
                True,
                getattr(self.config, "logo_when_display_off", True),
                self._camera_full_active,
            ):
                log.info("Stream Deck idle for %.0fs -- showing logo", idle_secs)
                self._enter_display_off_logo()
            else:
                log.info("Stream Deck idle for %.0fs -- blanking", idle_secs)
                self._logo_face_active = False
                self.deck.set_brightness(0)
                self.deck.reset()
                # reset() wiped the key images, so the wake-up _draw_page must
                # repaint every face, not trust the cache of what was showing.
                self._face_cache.clear()

    async def _report_activity(self) -> None:
        """Tell the host bridge a key was pressed so the kiosk display wakes.

        Best-effort: a missing bridge (dev box, non-Pi) is a no-op."""
        url = getattr(self.config, "host_bridge_url", "")
        if not url:
            return
        token_path = getattr(self.config, "bridge_token_path", "")
        try:
            async with httpx.AsyncClient(timeout=2.0) as c:
                r = await c.post(f"{url}/activity", json={"source": "streamdeck"},
                                 headers=actions.bridge_headers(token_path))
            if r.status_code == 401:
                actions.invalidate_bridge_token(token_path)
        except Exception:
            pass

    async def _poll_shared_activity(self) -> None:
        """Adopt the bridge's shared activity on poll, waking a blanked deck.

        Activity on another surface (a screen touch) resets the local idle timer
        and UNBLANKS an idle-blanked deck, so waking either surface wakes both
        (FoodAssistant-exuv). The idle logo face coincides with _idle_blanked, so
        a cross-surface wake also returns the deck from the raccoon to its page
        via _wake_from_idle. The logo itself is driven by the deck's OWN idle,
        not the kitchen display state (FoodAssistant-gic5)."""
        url = getattr(self.config, "host_bridge_url", "")
        if not url:
            return
        try:
            async with httpx.AsyncClient(timeout=2.0) as c:
                headers = actions.bridge_headers(
                    getattr(self.config, "bridge_token_path", ""))
                data = (await c.get(f"{url}/activity", headers=headers)).json()
        except Exception:
            return
        wake, self._seen_external_activity = _external_wake_due(
            data.get("last_activity"), time.time(), self._seen_external_activity
        )
        if wake:
            self._last_activity = time.monotonic()
        if wake and self._idle_blanked:
            await self._wake_from_idle()

    async def _idle_loop(self) -> None:
        """Blank the deck after idle_timeout_minutes without a key press."""
        while True:
            await asyncio.sleep(10)
            # Adopt activity and display state from the other surface before
            # deciding to blank.
            await self._poll_shared_activity()
            await self._idle_loop_once()

    # -- watchdog / config watch -------------------------------------------

    def _read_config_mtime(self) -> float:
        """Modification time of the loaded config file, or 0 when there is none."""
        if not self.config_path:
            return 0.0
        try:
            return os.path.getmtime(self.config_path)
        except OSError:
            return 0.0

    def _deck_is_healthy(self) -> bool:
        """Cheap liveness probe for the deck.

        Checks both the HID handle and the background read thread. A crashed or
        wedged deck raises on key_count()/key_image_format(). A physically
        unplugged deck does not raise (those are in-memory constants), but its
        read thread dies; checking it catches the disconnect case. While the deck
        is intentionally blanked for idle we skip the probe so the watchdog does
        not fight the idle blanker.
        """
        if self._idle_blanked:
            return True
        try:
            self.deck.key_count()
            self.deck.key_image_format()
            # key_count() and key_image_format() are class constants in the
            # StreamDeck library and succeed even when the USB device is gone.
            # The background read thread dies on physical disconnect, so check it.
            read_thread = getattr(self.deck, '_read_thread', None)
            if read_thread is not None and not read_thread.is_alive():
                return False
            return True
        except Exception:  # noqa: BLE001 - any failure means re-init
            return False

    async def _watchdog_once(self) -> None:
        """One watchdog tick: apply a pending config change, then health-check.

        A config rewrite (the setup page changing rotation or the key layout)
        is handled first as a clean in-process re-init. Independently, if the
        deck has stopped answering, it is re-initialised so it recovers without
        a device reboot.
        """
        mtime = self._read_config_mtime()
        if mtime and mtime != self._config_mtime:
            self._config_mtime = mtime
            log.info("config file changed; re-initialising deck")
            await self.reinit(reload_config=True)
            return
        # Also retry when _deck_live is False: a previous reinit() failed (the
        # deck was unplugged and not yet back). The health probe alone won't
        # catch this because the closed handle's constant properties still pass.
        if self._deck_live and self._deck_is_healthy():
            # Deck is present and answering. If it had been lost, reinit()
            # already logged the reattach, so just clear the flag.
            self._deck_lost_logged = False
            return
        # The deck is gone or wedged. Log the loss once (the backoff is handled
        # by the loop below via _next_watchdog_delay), then try to bring it back.
        # A successful reinit re-opens and fully re-renders the current layout,
        # so a replug restores the same page, brightness, and live faces with no
        # human action.
        if not self._deck_lost_logged:
            log.warning("Stream Deck disconnected; polling for it to return.")
            self._deck_lost_logged = True
        await self.reinit(reload_config=False)

    def _next_watchdog_delay(self) -> float:
        """Seconds to wait before the next watchdog tick, advancing the backoff.

        Steady WATCHDOG_INTERVAL while the deck is live so a config change is
        caught promptly. While the deck is gone the delay grows on the reconnect
        backoff and settles at its cap, so a long absence (or a box with no deck
        attached) idle-waits quietly instead of enumerating every few seconds
        (FoodAssistant-o29k).
        """
        if self._deck_live:
            self._reconnect_delay = 0.0
            return WATCHDOG_INTERVAL
        self._reconnect_delay = _next_backoff(self._reconnect_delay)
        return self._reconnect_delay

    async def _watchdog_loop(self) -> None:
        """Periodically watch for config changes and a wedged deck."""
        while True:
            await asyncio.sleep(self._next_watchdog_delay())
            try:
                await self._watchdog_once()
            except Exception as e:  # noqa: BLE001 - never let the watchdog die
                log.debug("watchdog tick failed: %s", e)

    async def _refresh(self) -> None:
        await self._poll_once()
        self._draw_page()

    async def _refresh_weather(self) -> None:
        has_weather_key = any(
            spec is not None and spec.kind in ("weather", "forecast")
            for page in self.pages for spec in page
        )
        if not has_weather_key and not self.override_weather:
            return
        # Refresh every weather tile concurrently over one shared client: the
        # override keys each carry their own (possibly different) location, and
        # refreshing them one by one meant a single stalled request delayed all
        # the rest (FoodAssistant-17tb). refresh() never raises, so gather is
        # safe, and each request has its own tight timeout.
        states: list[WeatherState] = []
        if has_weather_key:
            states.append(self.weather)
        states.extend(self.override_weather.values())
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
            await asyncio.gather(*(w.refresh(client) for w in states))
        self._draw_page()

    async def _refresh_ha_entities(self) -> None:
        if not self.ha_entities or not self.config.ha_base_url or not self.config.ha_token:
            return
        for state in self.ha_entities.values():
            await state.refresh(self.config.ha_base_url, self.config.ha_token)
        self._draw_page()

    def _cycle_brightness(self) -> int:
        self._bright_idx = (self._bright_idx + 1) % len(BRIGHTNESS_STEPS)
        pct = BRIGHTNESS_STEPS[self._bright_idx]
        self.deck.set_brightness(pct)
        return pct

    def _page_next(self) -> None:
        if self.keypad_mode:
            self.keypad_page_idx = (self.keypad_page_idx + 1) % len(self.keypad_pages)
        elif self.shopping_check_mode:
            # The quick-check page has no further pages; its only paging key is
            # the Back key, so a next is treated as an exit too.
            self._exit_shopping_check()
            return
        else:
            self.page = (self.page + 1) % len(self.pages)
        self._draw_page()

    def _page_prev(self) -> None:
        if self.keypad_mode:
            self.keypad_page_idx = (self.keypad_page_idx - 1) % len(self.keypad_pages)
        elif self.shopping_check_mode:
            # The Back key on the quick-check page returns to the normal layout.
            self._exit_shopping_check()
            return
        else:
            self.page = (self.page - 1) % len(self.pages)
        self._draw_page()

    def _navigate_async(self, path: str) -> None:
        """Schedule a kiosk navigation from a synchronous key handler.

        Best-effort and only when the event loop is live, so a unit test that
        calls a press handler without a running loop simply skips the jump.
        """
        if self.loop is None or not self.loop.is_running():
            return
        self.loop.create_task(self._navigate(path))

    async def _navigate(self, path: str) -> bool:
        url = f"{self.config.base_url}/{path.lstrip('/')}"
        if self.config.kiosk_cdp_url and self.client is not None:
            try:
                cdp = self.config.kiosk_cdp_url.rstrip("/")
                r = await self.client.get(f"{cdp}/json")
                if r.status_code == 200:
                    targets = r.json()
                    page = next(
                        (t for t in targets if t.get("type") == "page"), None
                    )
                    ws_url = page.get("webSocketDebuggerUrl") if page else None
                    if ws_url:
                        import websockets
                        async with websockets.connect(ws_url) as ws:
                            await ws.send(json.dumps({
                                "id": 1,
                                "method": "Page.navigate",
                                "params": {"url": url},
                            }))
                            await asyncio.wait_for(ws.recv(), timeout=3.0)
                        return True
            except Exception:  # noqa: BLE001 - fall through to desktop opener
                pass
        opener = shutil.which("xdg-open")
        if opener:
            try:
                subprocess.Popen(
                    [opener, url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
            except Exception:  # noqa: BLE001
                pass
        return False

    # -- polling -----------------------------------------------------------

    async def _poll_once(self) -> None:
        if self.client is None:
            return
        self.status = await actions.poll_status(
            self.client, self.config.base_url, self.config.soon_days
        )
        # Refresh the active-recipe timer labels on the same cadence as the
        # status counts (sbu3). Defensive inside, so a failure never disturbs the
        # status poll above.
        await self._refresh_recipe_timers()
        # Reconcile the timer keys with the shared server registry (the single
        # source of countdown truth), same cadence: a timer cancelled from the
        # web clears its key, one started elsewhere lands on the matching key.
        await self._refresh_server_timers()
        # Refresh today's planned meal for any meal_today info key, on the same
        # cadence. Skipped when no such key is shown so the poll stays cheap.
        await self._refresh_meal_today()
        # Refresh host-bridge health for any health key, same cadence. Skipped
        # when no such key is shown so the poll stays cheap and off-Pi safe.
        await self._refresh_health()
        # Refresh the cached snapshot behind any single-key camera face, same
        # cadence. Skipped when no camera key is shown or none is configured.
        await self._refresh_camera_snapshot()
        # Refresh the active scanner mode for any scan_mode key, same cadence.
        await self._refresh_scanner_mode()
        # While the shopping quick-check page is up, keep its item keys in step
        # with the live list (checked items drop out, new ones appear), same
        # cadence. Skipped entirely when the page is not showing.
        await self._refresh_shopping_check()

    def _tick_timers(self) -> bool:
        """Advance all active timers. Returns True if any expired this tick.

        The list comprehension (not a bare any() over a generator) makes sure
        every timer ticks even when an earlier one expires the same second.
        """
        return any([t.tick() for t in self.timers.values()])

    async def _poll_forever(self) -> None:
        tick = 0
        last_clock_minute: Optional[str] = None
        while True:
            await asyncio.sleep(1)
            tick += 1
            try:
                # Tick the clock key: redraw when the HH:MM changes so the face
                # stays current without a full status poll. Cheap when no clock
                # key is shown (the redraw is gated on its presence).
                if self._has_kind("clock"):
                    minute = datetime.now().strftime("%H:%M")
                    if minute != last_clock_minute:
                        last_clock_minute = minute
                        self._draw_page()
                expired = self._tick_timers()
                any_running = any(t.is_running() for t in self.timers.values())
                any_alerting = any(t.alert_active() for t in self.timers.values())
                # While any timer alert is undismissed, advance the blink phase
                # so the key flashes bright/dim each tick until it is pressed.
                if any_alerting:
                    self._blink_phase += 1
                else:
                    self._blink_phase = 0
                # Redraw every second while a timer is active, alerting, or just
                # expired; otherwise only redraw after a full poll cycle.
                if any_running or any_alerting or expired:
                    self._draw_page()
                if tick >= self.config.poll_seconds:
                    tick = 0
                    await self._poll_once()
                    self._draw_page()
                weather_secs = self.config.weather_poll_minutes * 60
                weather_due = self.weather.age_seconds() >= weather_secs or any(
                    w.age_seconds() >= weather_secs
                    for w in self.override_weather.values()
                )
                if weather_secs > 0 and weather_due:
                    await self._refresh_weather()
                ha_secs = self.config.ha_poll_seconds
                if (ha_secs > 0 and self.ha_entities
                        and any(e.age_seconds() >= ha_secs
                                for e in self.ha_entities.values())):
                    await self._refresh_ha_entities()
            except Exception as e:  # noqa: BLE001 - keep polling
                log.debug("poll cycle failed: %s", e)


def find_deck():
    """Return the first attached Stream Deck, or None."""
    from StreamDeck.DeviceManager import DeviceManager

    decks = DeviceManager().enumerate()
    return decks[0] if decks else None


async def wait_for_deck(enumerate_fn=find_deck, sleep=asyncio.sleep,
                        should_continue=None):
    """Block until a Stream Deck is attached, then return the handle.

    Enumerates on the reconnect backoff (see _next_backoff) so a deck plugged
    in seconds after boot is picked up almost at once, while a box with no deck
    attached at all idle-waits quietly instead of crash-looping through systemd
    (FoodAssistant-o29k). The wait is logged once when it starts and once when a
    deck appears, so a long wait is not a spin-loop of messages.

    ``enumerate_fn`` and ``sleep`` are injectable for tests. ``should_continue``
    is an optional predicate the loop checks each iteration so a test can bound
    the wait; in production it defaults to forever, and returns None only if a
    bounded caller gives up.
    """
    deck = enumerate_fn()
    if deck is not None:
        return deck
    log.warning("No Stream Deck attached; waiting for one to be plugged in.")
    delay = 0.0
    while should_continue is None or should_continue():
        delay = _next_backoff(delay)
        await sleep(delay)
        deck = enumerate_fn()
        if deck is not None:
            log.info("Stream Deck attached; starting up.")
            return deck
    return None


async def main_async(config: Config, config_path: Optional[str] = None,
                     deck=None) -> int:
    """Run the controller. ``deck`` may be a pre-opened handle from the early
    boot splash (__main__), adopted as-is so the splash survives until the
    first page draw; when None the first attached deck is opened here.

    A missing deck at startup is not fatal: rather than exit and let systemd
    thrash-restart, the process idle-waits in-process for a deck to be plugged
    in, so the in-process recovery is the primary mechanism and systemd stays a
    backstop for a hard crash only (FoodAssistant-o29k)."""
    if deck is None:
        deck = await wait_for_deck()
    if deck is None:
        log.error("No Stream Deck found. Check the USB connection and udev rule.")
        return 1
    controller = Controller(deck, config, config_path=config_path)
    try:
        await controller.run()
    finally:
        controller.close()
    return 0
