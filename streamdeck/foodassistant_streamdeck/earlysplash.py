"""Boot splash painted before the heavy service imports (FoodAssistant-krbn).

The controller only paints its splash once ``controller.run()`` is reached,
which sits behind the slowest part of startup on a Pi: importing httpx, the
asyncio machinery, and the rest of the controller module. Until then the deck
shows the Elgato factory logo, so the splash appeared for a blink right before
the first real page replaced it. ``__main__`` calls this module first, before
importing the controller, so the brand mark is on the keys within the time it
takes to import the device library and Pillow, and it stays up until the first
page draw (``_open_deck`` skips its reset when the deck arrives already open,
so nothing blanks the splash in between).

Only two dependencies are imported here, both unavoidable for putting pixels
on the keys at all: the StreamDeck device library (HID access and the native
key image encoding) and, through ``render``, Pillow. Everything else the
service needs loads after the splash is visible.

A note on the bead's other ask, storing the logo in the deck's non-volatile
memory so it shows at power-on before this service starts: the hardware does
not support it. Elgato's published HID protocol for the Classic/MK.2/Module
family has a "Show Logo" feature report (0x03 0x02, which reset() uses) that
re-displays the factory logo from firmware, but no command to replace that
stored image, and the python-elgato-streamdeck library exposes nothing of the
kind either. Painting as early as possible is the closest available behavior.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

log = logging.getLogger("foodassistant.streamdeck")


def paint(deck: Any, rotation: int = 0,
          to_native: Optional[Callable[[Any, Any], Any]] = None) -> bool:
    """Paint the splash tiles across an already-open deck. Returns True when
    a full frame was pushed.

    Mirrors the controller's rotation-aware full-deck geometry so a turned
    deck shows the mark the right way up from the very first frame. Best
    effort by design: any failure (missing asset, dead handle) returns False
    and the caller carries on, because a splash must never block boot.
    ``to_native`` is injectable for tests; it defaults to the StreamDeck
    library's PIL-to-native encoder.
    """
    try:
        if to_native is None:
            from StreamDeck.ImageHelpers import PILHelper
            to_native = PILHelper.to_native_format
        from . import layout, render
        rows, cols = deck.key_layout()
        key_size = deck.key_image_format()["size"]
        tiles = render.splash_tiles(rows, cols, key_size)
        if not tiles:
            return False
        count = deck.key_count()
        for index, tile in enumerate(tiles):
            if index >= count:
                break
            image = tile.rotate(-rotation, expand=True) if rotation else tile
            phys = layout.rotated_index(index, count, rotation)
            deck.set_key_image(phys, to_native(deck, image))
        return True
    except Exception as e:  # noqa: BLE001 - a splash must never block boot
        log.debug("early splash paint failed: %s", e)
        return False


def open_deck_and_paint(rotation: int = 0) -> Optional[Any]:
    """Find and open the first attached deck and paint the splash on it.

    Returns the OPEN deck handle so the controller can adopt it (skipping its
    own open+reset, which would blank the splash), or None when no deck is
    attached or the device library is unavailable; the caller then falls back
    to the controller's normal open path.
    """
    try:
        from StreamDeck.DeviceManager import DeviceManager
        decks = DeviceManager().enumerate()
        if not decks:
            return None
        deck = decks[0]
        deck.open()
        # One reset here clears whatever the factory logo left behind; after
        # this the splash owns the keys until the first real page draw.
        deck.reset()
    except Exception as e:  # noqa: BLE001 - fall back to the normal open path
        log.debug("early splash open failed: %s", e)
        return None
    paint(deck, rotation)
    return deck
