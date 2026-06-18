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
from .actions import ActionContext, ActionSpec
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

        for index, spec in enumerate(self._current()):
            if spec is None:
                image = render.blank_key(*self._key_size())
            else:
                count = (
                    self.status.get(spec.status_field)
                    if spec.kind == "status"
                    else None
                )
                image = render.render_key(
                    *self._key_size(),
                    label=spec.label,
                    color=spec.color,
                    count=count,
                    alert=bool(count),
                )
            self.deck.set_key_image(index, PILHelper.to_native_format(self.deck, image))

    def _key_size(self) -> tuple[int, int]:
        w, h = self.deck.key_image_format()["size"]
        return w, h

    # -- input -------------------------------------------------------------

    def _on_key(self, deck, key: int, pressed: bool) -> None:
        if not pressed or self.loop is None:
            return
        page = self._current()
        if key >= len(page) or page[key] is None:
            return
        spec = page[key]
        asyncio.run_coroutine_threadsafe(self._handle(spec), self.loop)

    async def _handle(self, spec: ActionSpec) -> None:
        ctx = ActionContext(
            client=self.client,
            base_url=self.config.base_url,
            refresh=self._refresh,
            navigate=self._navigate,
            cycle_brightness=self._cycle_brightness,
            page_next=self._page_next,
            page_prev=self._page_prev,
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

    async def _poll_forever(self) -> None:
        while True:
            await asyncio.sleep(self.config.poll_seconds)
            try:
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
