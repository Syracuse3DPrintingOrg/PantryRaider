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

Finished timer pills ride the same channel (FoodAssistant-07ee): the kiosk
includes a small "pills" array (id, box in the same panel-normalized space,
done flag, food icon character) so a Done pill drifting into the deck band
shows up on the keys too. The kiosk sends only finished pills, so the payload
stays a handful of numbers.
"""
from __future__ import annotations

import threading
import time

# A kiosk posts every few hundred milliseconds while the saver is up, so a
# state this old means the saver ended (or the kiosk went away).
STALE_AFTER_SECONDS = 10.0

# Hard cap on stored pills: the kiosk sends only finished timers (and it
# simulates at most six pills anyway), so anything past this is junk input.
MAX_PILLS = 6

# An icon is one emoji, possibly with a variation selector; anything longer
# is not an icon.
_MAX_ICON_CHARS = 4

_lock = threading.Lock()
_state: dict = {
    "active": False,
    # Logo bounding box in panel-normalized units (panel width and height are
    # each 1.0; the deck band extends past 0..1 on the layout's side).
    "x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0,
    # Deck band size in panel-normalized units and which side it sits on, as
    # computed by the kiosk, so the deck's slice math needs no panel geometry.
    "band": 0.0, "layout": "off",
    # Finished timer pills, sanitized dicts in the same normalized space.
    "pills": [],
    "updated": 0.0,
    # Set by a deck key press; returned (once) to the kiosk so it dismisses.
    "dismissed": 0.0,
}


def sanitize_pills(raw) -> list[dict]:
    """Pure helper: reduce a posted "pills" value to a safe, small list.

    Each entry keeps only the fields the deck needs (id, x, y, w, h, done,
    icon), with types coerced and the icon truncated, capped at MAX_PILLS
    entries. Anything malformed is dropped rather than raised, so a garbage
    post can never break the channel.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if len(out) >= MAX_PILLS:
            break
        if not isinstance(item, dict):
            continue
        def _num(key: str) -> float:
            v = item.get(key, 0.0)
            return float(v) if isinstance(v, (int, float)) else 0.0
        out.append({
            "id": str(item.get("id") or "")[:64],
            "x": _num("x"), "y": _num("y"),
            "w": _num("w"), "h": _num("h"),
            "done": bool(item.get("done")),
            "icon": str(item.get("icon") or "")[:_MAX_ICON_CHARS],
        })
    return out


def is_fresh(updated: float, now: float, stale_after: float = STALE_AFTER_SECONDS) -> bool:
    """Pure helper: True when a state stamped at ``updated`` still counts as
    live at ``now``. A zero/negative timestamp (never posted) is never fresh."""
    if updated <= 0:
        return False
    return 0 <= (now - updated) <= stale_after


def update(active: bool, x: float = 0.0, y: float = 0.0, w: float = 0.0,
           h: float = 0.0, band: float = 0.0, layout: str = "off",
           pills: list | None = None) -> dict:
    """Record the kiosk's saver state and return any pending dismiss.

    The returned dict carries {"dismiss": bool}: True when a deck key press
    asked for the saver to end since the kiosk's previous post. The mark is
    consumed by this read, so it fires exactly once.
    """
    now = time.time()
    clean_pills = sanitize_pills(pills)
    with _lock:
        dismissed = _state["dismissed"]
        _state.update({
            "active": bool(active),
            "x": float(x), "y": float(y), "w": float(w), "h": float(h),
            "band": float(band), "layout": str(layout),
            "pills": clean_pills,
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
            "pills": list(_state["pills"]),
        }


def reset() -> None:
    """Test helper: return to the never-posted state."""
    with _lock:
        _state.update({"active": False, "x": 0.0, "y": 0.0, "w": 0.0,
                       "h": 0.0, "band": 0.0, "layout": "off",
                       "pills": [], "updated": 0.0, "dismissed": 0.0})
