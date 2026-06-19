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
from typing import Optional

import httpx

from . import actions, layout, render
from .actions import ActionContext, ActionSpec, TimerState
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
        self.status: dict[str, int] = {"expiring": 0, "pending": 0}
        self.timers: dict[str, TimerState] = {}  # action name -> timer state

        try:
            self._bright_idx = BRIGHTNESS_STEPS.index(
                min(BRIGHTNESS_STEPS, key=lambda s: abs(s - config.brightness))
            )
        except ValueError:
            self._bright_idx = len(BRIGHTNESS_STEPS) // 2

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
            self._draw_page()
            log.info(
                "Connected to %s (%d keys, %d page(s))",
                self.deck.deck_type(),
                self.key_count,
                len(self.pages),
            )
            await self._poll_forever()

    def close(self) -> None:
        try:
            self.deck.reset()
            self.deck.close()
        except Exception:  # noqa: BLE001 - best effort on shutdown
            pass

    # -- rendering ---------------------------------------------------------

    def _current(self) -> list[Optional[ActionSpec]]:
        return self.pages[self.page % len(self.pages)]

    def _draw_page(self) -> None:
        from StreamDeck.ImageHelpers import PILHelper

        rotation = self.config.rotation
        for index, spec in enumerate(self._current()):
            if spec is None:
                image = render.blank_key(*self._key_size())
            else:
                if spec.kind == "timer":
                    t = self.timers.get(spec.name)
                    label = t.label(spec.label) if t else spec.label
                    color = t.color(spec.color) if t else spec.color
                    alert = t.alert_active() if t else False
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

    def _key_size(self) -> tuple[int, int]:
        w, h = self.deck.key_image_format()["size"]
        return w, h

    def _visual_slot(self, phys: int) -> int:
        """Invert the draw-time index mapping for a pressed physical key.

        ``rotated_index`` maps visual slot -> physical key. We invert it by
        searching the visual slots for the one that lands on ``phys``. For 180
        this is exact; for 90/270 it inherits the same best-effort transpose
        limitation noted in ``layout.rotated_index`` (a wide deck cannot map
        perfectly onto its transpose), so an unmapped press falls back to the
        physical index unchanged.
        """
        rotation = self.config.rotation
        if not rotation:
            return phys
        for slot in range(self.key_count):
            if layout.rotated_index(slot, self.key_count, rotation) == phys:
                return slot
        return phys

    # -- input -------------------------------------------------------------

    def _on_key(self, deck, key: int, pressed: bool) -> None:
        if not pressed or self.loop is None:
            return
        # `key` is the physical index pressed. Invert the draw-time mapping to
        # recover the visual slot, so the action matches what the user sees.
        slot = self._visual_slot(key)
        page = self._current()
        if slot >= len(page) or page[slot] is None:
            return
        spec = page[slot]
        asyncio.run_coroutine_threadsafe(self._handle(spec), self.loop)

    def _timer_press(self, name: str) -> None:
        if name not in self.timers:
            self.timers[name] = TimerState()
        self.timers[name].press()
        self._draw_page()

    async def _handle(self, spec: ActionSpec) -> None:
        ctx = ActionContext(
            client=self.client,
            base_url=self.config.base_url,
            refresh=self._refresh,
            navigate=self._navigate,
            cycle_brightness=self._cycle_brightness,
            page_next=self._page_next,
            page_prev=self._page_prev,
            timer_press=self._timer_press,
        )
        try:
            msg = await actions.run_action(spec, ctx)
            if msg:
                log.info("%s -> %s", spec.name, msg)
        except Exception as e:  # noqa: BLE001 - one bad press must not crash
            log.warning("action %s failed: %s", spec.name, e)

    # -- effects exposed to actions ---------------------------------------

    async def _refresh(self) -> None:
        await self._poll_once()
        self._draw_page()

    def _cycle_brightness(self) -> int:
        self._bright_idx = (self._bright_idx + 1) % len(BRIGHTNESS_STEPS)
        pct = BRIGHTNESS_STEPS[self._bright_idx]
        self.deck.set_brightness(pct)
        return pct

    def _page_next(self) -> None:
        self.page = (self.page + 1) % len(self.pages)
        self._draw_page()

    def _page_prev(self) -> None:
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
                # Redraw every second while a timer is active or just expired;
                # otherwise only redraw after a full poll cycle.
                if any_running or expired:
                    self._draw_page()
                if tick >= self.config.poll_seconds:
                    tick = 0
                    await self._poll_once()
                    self._draw_page()
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
