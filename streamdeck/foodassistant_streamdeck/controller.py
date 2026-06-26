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
    PinBuffer,
    TimerState,
    WeatherState,
)
from .config import BRIGHTNESS_STEPS, Config

log = logging.getLogger("foodassistant.streamdeck")

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
        self.pages: list[list[Optional[ActionSpec]]] = layout.build_pages(
            config.keys, self.key_count
        )
        # Advanced per-key overrides from the web setup page. Parsed into
        # ActionSpec entries and stamped onto the default layout, replacing the
        # stock action at each configured slot.
        self.key_overrides: dict[int, ActionSpec] = actions.overrides_to_specs(
            getattr(config, "key_overrides", []) or [], self.key_count
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
        self.status: dict[str, int] = {
            "expiring": 0, "pending": 0, "shopping": 0, "ready": 0,
        }
        # Today's planned meal name shown on a meal_today info key. Refreshed on
        # the normal status poll; defaults to a neutral face until first fetched.
        self.meal_today: str = "No meal"
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
            location=config.weather_location, units=config.weather_units
        )
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
                    units=config.weather_units,
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

    # -- lifecycle ---------------------------------------------------------

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        headers = {"X-API-Key": self.config.api_key} if self.config.api_key else {}
        async with httpx.AsyncClient(timeout=8.0, headers=headers) as client:
            self.client = client
            self._open_deck()
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
        """
        self.deck.open()
        self.deck.reset()
        self.deck.set_brightness(BRIGHTNESS_STEPS[self._bright_idx])
        self.deck.set_key_callback(self._on_key)
        self._idle_blanked = False

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
                log.error("Stream Deck re-init failed: %s", e)
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
        self.pages = layout.build_pages(cfg.keys, self.key_count)
        self.key_overrides = actions.overrides_to_specs(
            getattr(cfg, "key_overrides", []) or [], self.key_count
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
        return self.pages[self.page % len(self.pages)]

    def _draw_page(self) -> None:
        from StreamDeck.ImageHelpers import PILHelper

        rotation = self.config.rotation
        for index, spec in enumerate(self._current()):
            if spec is None:
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
                    label = actions._clock_label(datetime.now())
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
                elif spec.kind in ("shopping_add", "macro"):
                    # Quick-add and macro override keys are stateless faces: they
                    # show their configured label and colour and do their work on
                    # press (dispatched through actions.run_action), so there is
                    # no live count or alert to compute here.
                    label = spec.label
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
                    color = base_color
                    alert = bool(count)
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
                )
            if rotation:
                # PIL rotates counter-clockwise, so negate to turn the face
                # clockwise (matching how a user physically turns the deck).
                # The HDMI/kiosk display rotation is handled separately at the
                # OS level (xrandr / KMS) and is out of scope here.
                image = image.rotate(-rotation, expand=True)
            # The page slot `index` is a visual position; send it to the
            # physical key it now occupies after the deck is turned.
            phys = layout.rotated_index(index, self.key_count, rotation)
            self.deck.set_key_image(phys, PILHelper.to_native_format(self.deck, image))

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

    def _timer_press(self, name: str, long_press: bool = False) -> None:
        if name not in self.timers:
            self.timers[name] = TimerState()
        timer = self.timers[name]
        # When a recipe is active and this key carries a suggestion, a fresh
        # short press starts the suggested duration locally AND fires a shared
        # server timer so other surfaces (web UI, satellites) see the same
        # countdown. Otherwise fall back to a timer-override preset, then to the
        # stock count-up behaviour.
        recipe_spec = self.recipe_timer_specs.get(name)
        recipe_seconds = recipe_spec.get("seconds") if recipe_spec else None
        preset = self._override_timer_minutes(name)
        starting_fresh = not timer.is_running() and not timer.alert_active()
        if long_press:
            timer.long_press()
        elif recipe_seconds and starting_fresh:
            # Drive the local countdown from the suggestion (the existing
            # rendering keeps working) and start the shared server timer too.
            timer.set_minutes(round(float(recipe_seconds) / 60))
            self._start_recipe_server_timer(recipe_spec)
        elif preset > 0 and starting_fresh:
            timer.set_minutes(preset)
        else:
            timer.short_press()
        # Reset the blink phase so a fresh alert starts on its bright frame.
        self._blink_phase = 0
        self._draw_page()

    def _start_recipe_server_timer(self, recipe_spec: dict) -> None:
        """Fire-and-forget POST that starts the shared server timer for a recipe
        suggestion, so surfaces beyond this deck reflect it too. Best-effort and
        only when the event loop is live (skipped in unit tests with no loop)."""
        if self.client is None or self.loop is None or not self.loop.is_running():
            return
        asyncio.run_coroutine_threadsafe(
            actions.start_recipe_timer(
                self.client,
                self.config.base_url,
                step_index=recipe_spec.get("step_index"),
                label=recipe_spec.get("label", ""),
                seconds=recipe_spec.get("seconds"),
            ),
            self.loop,
        )

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
            ha_base_url=self.config.ha_base_url,
            ha_token=self.config.ha_token,
            ha_entity_refresh=self._refresh_ha_entities,
            keypad_enter=self._enter_keypad,
            keypad_press=self._keypad_press,
        )
        try:
            msg = await actions.run_action(spec, ctx, long_press=long_press)
            if msg:
                log.info("%s -> %s", spec.name, msg)
        except Exception as e:  # noqa: BLE001 - one bad press must not crash
            log.warning("action %s failed: %s", spec.name, e)

    # -- effects exposed to actions ---------------------------------------

    async def _wake_from_idle(self) -> None:
        """Restore the current page after the deck was blanked by the idle timer."""
        self._idle_blanked = False
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
        if timeout_mins <= 0 or self._idle_blanked:
            return
        idle_secs = time.monotonic() - self._last_activity
        if idle_secs >= timeout_mins * 60:
            log.info("Stream Deck idle for %.0fs -- blanking", idle_secs)
            self._idle_blanked = True
            self.deck.set_brightness(0)
            self.deck.reset()

    async def _report_activity(self) -> None:
        """Tell the host bridge a key was pressed so the kiosk display wakes.

        Best-effort: a missing bridge (dev box, non-Pi) is a no-op."""
        url = getattr(self.config, "host_bridge_url", "")
        if not url:
            return
        try:
            async with httpx.AsyncClient(timeout=2.0) as c:
                await c.post(f"{url}/activity", json={"source": "streamdeck"})
        except Exception:
            pass

    async def _poll_shared_activity(self) -> None:
        """Wake the deck if another surface (the kiosk screen) saw activity.

        Polls the host bridge's shared last-activity. When it is fresh, reset
        the local idle timer and wake if blanked, so a screen touch wakes the
        deck even though the two timeouts are independent."""
        url = getattr(self.config, "host_bridge_url", "")
        if not url:
            return
        try:
            async with httpx.AsyncClient(timeout=2.0) as c:
                data = (await c.get(f"{url}/activity")).json()
        except Exception:
            return
        if _external_activity_is_fresh(data.get("last_activity"), time.time()):
            self._last_activity = time.monotonic()
            if self._idle_blanked:
                await self._wake_from_idle()

    async def _idle_loop(self) -> None:
        """Blank the deck after idle_timeout_minutes without a key press."""
        while True:
            await asyncio.sleep(10)
            # Adopt activity from the other surface before deciding to blank.
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
        if not self._deck_live or not self._deck_is_healthy():
            log.warning("Stream Deck not responding; re-initialising")
            await self.reinit(reload_config=False)

    async def _watchdog_loop(self) -> None:
        """Periodically watch for config changes and a wedged deck."""
        while True:
            await asyncio.sleep(5)
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
        if has_weather_key:
            await self.weather.refresh()
        # Override weather keys each fetch their own (possibly different)
        # location, so refresh them alongside the shared widget.
        for w in self.override_weather.values():
            await w.refresh()
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
        else:
            self.page = (self.page + 1) % len(self.pages)
        self._draw_page()

    def _page_prev(self) -> None:
        if self.keypad_mode:
            self.keypad_page_idx = (self.keypad_page_idx - 1) % len(self.keypad_pages)
        else:
            self.page = (self.page - 1) % len(self.pages)
        self._draw_page()

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
        # Refresh today's planned meal for any meal_today info key, on the same
        # cadence. Skipped when no such key is shown so the poll stays cheap.
        await self._refresh_meal_today()

    def _tick_timers(self) -> bool:
        """Advance all active timers. Returns True if any expired this tick."""
        expired = any(t.tick() for t in self.timers.values())
        return expired

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


async def main_async(config: Config, config_path: Optional[str] = None) -> int:
    deck = find_deck()
    if deck is None:
        log.error("No Stream Deck found. Check the USB connection and udev rule.")
        return 1
    controller = Controller(deck, config, config_path=config_path)
    try:
        await controller.run()
    finally:
        controller.close()
    return 0
