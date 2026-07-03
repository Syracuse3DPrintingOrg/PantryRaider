"""Shared screensaver state for the kiosk panel and the Stream Deck.

When the kiosk screensaver's bouncing logo is on screen and the deck layout
setting says the deck sits next to the panel (FoodAssistant-3fdq), the two act
as one canvas: the kiosk (the animation driver) posts the logo's normalized
position here a few times a second, and the deck controller polls it on its
own slower cadence to render the slice of the logo crossing its keys.

Like the timer registry this state is in-memory and process-local: a restart
simply clears it, and the kiosk repopulates it within a fraction of a second
while the saver is up. Freshness is judged with a pure helper so the logic is
unit-testable without sleeping: a state older than the staleness window counts
as inactive, so a kiosk that died mid-saver never leaves the deck showing a
frozen logo forever.

The deck can also dismiss the saver: a key press posts a dismiss mark, and the
kiosk's next state post returns it, telling the browser to hide the overlay.
"""
from __future__ import annotations

import threading
import time

# A kiosk posts every few hundred milliseconds while the saver is up, so a
# state this old means the saver ended (or the kiosk went away).
STALE_AFTER_SECONDS = 10.0

_lock = threading.Lock()
_state: dict = {
    "active": False,
    # Logo bounding box in panel-normalized units (panel width and height are
    # each 1.0; the deck band extends past 0..1 on the layout's side).
    "x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0,
    # Deck band size in panel-normalized units and which side it sits on, as
    # computed by the kiosk, so the deck's slice math needs no panel geometry.
    "band": 0.0, "layout": "off",
    "updated": 0.0,
    # Set by a deck key press; returned (once) to the kiosk so it dismisses.
    "dismissed": 0.0,
}


def is_fresh(updated: float, now: float, stale_after: float = STALE_AFTER_SECONDS) -> bool:
    """Pure helper: True when a state stamped at ``updated`` still counts as
    live at ``now``. A zero/negative timestamp (never posted) is never fresh."""
    if updated <= 0:
        return False
    return 0 <= (now - updated) <= stale_after


def update(active: bool, x: float = 0.0, y: float = 0.0, w: float = 0.0,
           h: float = 0.0, band: float = 0.0, layout: str = "off") -> dict:
    """Record the kiosk's saver state and return any pending dismiss.

    The returned dict carries {"dismiss": bool}: True when a deck key press
    asked for the saver to end since the kiosk's previous post. The mark is
    consumed by this read, so it fires exactly once.
    """
    now = time.time()
    with _lock:
        dismissed = _state["dismissed"]
        _state.update({
            "active": bool(active),
            "x": float(x), "y": float(y), "w": float(w), "h": float(h),
            "band": float(band), "layout": str(layout),
            "updated": now,
        })
        _state["dismissed"] = 0.0
        if not active:
            return {"dismiss": False}
        return {"dismiss": bool(dismissed)}


def dismiss() -> None:
    """Mark the saver dismissed (a deck key press); the kiosk's next state
    post picks it up and hides the overlay."""
    with _lock:
        _state["dismissed"] = time.time()


def snapshot(now: float | None = None) -> dict:
    """Current state for the deck's poll, staleness already applied."""
    if now is None:
        now = time.time()
    with _lock:
        fresh = is_fresh(_state["updated"], now)
        return {
            "active": bool(_state["active"]) and fresh,
            "x": _state["x"], "y": _state["y"],
            "w": _state["w"], "h": _state["h"],
            "band": _state["band"], "layout": _state["layout"],
        }


def reset() -> None:
    """Test helper: return to the never-posted state."""
    with _lock:
        _state.update({"active": False, "x": 0.0, "y": 0.0, "w": 0.0,
                       "h": 0.0, "band": 0.0, "layout": "off",
                       "updated": 0.0, "dismissed": 0.0})
