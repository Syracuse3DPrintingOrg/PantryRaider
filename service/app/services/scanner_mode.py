"""Scanner context mode (FoodAssistant-8jbk).

A single physical barcode scanner can mean different things depending on what
the user is doing: stocking groceries, using items up, or building a shopping
list. This holds the current mode so the scan endpoint can route a barcode to
the right action, and a Stream Deck key (or the kiosk) can show and change it.

The mode is process-local and in-memory, like the active recipe and timers: a
restart returns to the default "inventory" mode. An optional storage location
scopes the "audit" mode (FoodAssistant-ugku) to one place for a stock count.
"""
from __future__ import annotations

# Ordered so a deck key can cycle through them predictably. "audit" is a
# location-scoped, read-only stock count (FoodAssistant-ugku): in this mode a
# scan is recorded against the active audit session and never queued or
# consumed. It is locked to a location that the audit session itself holds, so
# the cycle just selects the mode; the location is chosen on the /ui/audit page.
SCANNER_MODES: tuple[str, ...] = ("inventory", "consume", "shopping", "audit")
_DEFAULT_MODE = "inventory"

# Short, glanceable labels for the deck/kiosk face.
MODE_LABELS = {
    "inventory": "Stock",
    "consume": "Use",
    "shopping": "Shop",
    "audit": "Audit",
}

_state: dict = {"mode": _DEFAULT_MODE}


def get_mode() -> str:
    return _state["mode"]


def get_state() -> dict:
    """The full scanner state: {mode, label}."""
    mode = _state["mode"]
    return {"mode": mode, "label": MODE_LABELS.get(mode, mode.title())}


def set_mode(mode: str) -> dict:
    """Set the mode (unknown values fall back to the default). Returns get_state()."""
    _state["mode"] = mode if mode in SCANNER_MODES else _DEFAULT_MODE
    return get_state()


def cycle_mode() -> dict:
    """Advance to the next mode in SCANNER_MODES, wrapping around."""
    try:
        idx = SCANNER_MODES.index(_state["mode"])
    except ValueError:
        idx = -1
    return set_mode(SCANNER_MODES[(idx + 1) % len(SCANNER_MODES)])


def reset() -> None:
    """Return to the default mode (used by tests)."""
    _state["mode"] = _DEFAULT_MODE
