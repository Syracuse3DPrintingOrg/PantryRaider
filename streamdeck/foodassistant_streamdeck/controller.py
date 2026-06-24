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
import shutil
import subprocess
import time
from typing import Optional

import httpx

from . import actions, layout, render
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


class Controller:
    def __init__(self, deck, config: Config) -> None:
        self.deck = deck
        self.config = config
        self.client: Optional[httpx.AsyncClient] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None

        self.key_count: int = deck.key_count()
        self.pages: list[list[Optional[ActionSpec]]] = layout.build_pages(
            config.keys, self.key_count
        )
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
        self.status: dict[str, int] = {"expiring": 0, "pending": 0}
        self.timers: dict[str, TimerState] = {}  # action name -> timer state
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

    # -- lifecycle ---------------------------------------------------------

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        headers = {"X-API-Key": self.config.api_key} if self.config.api_key else {}
        async with httpx.AsyncClient(timeout=8.0, headers=headers) as client:
            self.client = client
            self.deck.open()
            self.deck.reset()
            self.deck.set_brightness(BRIGHTNESS_STEPS[self._bright_idx])
            self.deck.set_key_callback(self._on_key)
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
            await asyncio.gather(self._poll_forever(), self._idle_loop())

    def close(self) -> None:
        try:
            self.deck.reset()
            self.deck.close()
        except Exception:  # noqa: BLE001 - best effort on shutdown
            pass

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
                if spec.kind == "keypad":
                    label, color = self._keypad_face(spec)
                    alert = False
                    count = None
                elif spec.kind == "timer":
                    t = self.timers.get(spec.name)
                    label = t.label(spec.label) if t else spec.label
                    color = (
                        t.color(spec.color, self._blink_phase) if t else spec.color
                    )
                    alert = t.alert_active() if t else False
                    count = None
                elif spec.kind == "weather":
                    label = self.weather.label(spec.label)
                    color = self.weather.color(spec.color)
                    alert = False
                    count = None
                elif spec.kind == "forecast":
                    label = self.weather.forecast_label(spec.label)
                    color = self.weather.forecast_color(spec.color)
                    alert = False
                    count = None
                elif spec.kind == "ha_entity":
                    ha = self.ha_entities.get(spec.name)
                    label = ha.label(spec.label) if ha else spec.label
                    color = ha.color(spec.color) if ha else spec.color
                    alert = False
                    count = None
                else:
                    count = (
                        self.status.get(spec.status_field)
                        if spec.kind == "status"
                        else None
                    )
                    label = spec.label
                    color = spec.color
                    alert = bool(count)
                image = render.render_key(
                    *self._key_size(),
                    label=label,
                    color=color,
                    count=count,
                    alert=alert,
                    icon=spec.icon,
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
        if long_press:
            self.timers[name].long_press()
        else:
            self.timers[name].short_press()
        # Reset the blink phase so a fresh alert starts on its bright frame.
        self._blink_phase = 0
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

    async def _idle_loop(self) -> None:
        """Blank the deck after streamdeck_idle_timeout minutes without a key press."""
        while True:
            await asyncio.sleep(10)
            timeout_mins = self.config.idle_timeout_minutes
            if timeout_mins <= 0 or self._idle_blanked:
                continue
            idle_secs = time.monotonic() - self._last_activity
            if idle_secs >= timeout_mins * 60:
                log.info("Stream Deck idle for %.0fs -- blanking", idle_secs)
                self._idle_blanked = True
                self.deck.set_brightness(0)
                self.deck.reset()

    async def _refresh(self) -> None:
        await self._poll_once()
        self._draw_page()

    async def _refresh_weather(self) -> None:
        has_weather_key = any(
            spec is not None and spec.kind in ("weather", "forecast")
            for page in self.pages for spec in page
        )
        if not has_weather_key:
            return
        await self.weather.refresh()
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

    def _tick_timers(self) -> bool:
        """Advance all active timers. Returns True if any expired this tick."""
        expired = any(t.tick() for t in self.timers.values())
        return expired

    async def _poll_forever(self) -> None:
        tick = 0
        while True:
            await asyncio.sleep(1)
            tick += 1
            try:
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
                if (weather_secs > 0
                        and self.weather.age_seconds() >= weather_secs):
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


async def main_async(config: Config) -> int:
    deck = find_deck()
    if deck is None:
        log.error("No Stream Deck found. Check the USB connection and udev rule.")
        return 1
    controller = Controller(deck, config)
    try:
        await controller.run()
    finally:
        controller.close()
    return 0
