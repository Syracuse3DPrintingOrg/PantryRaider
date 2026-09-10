"""Scanner context mode (FoodAssistant-8jbk).

A single physical barcode scanner can mean different things depending on what
the user is doing: stocking groceries, using items up, or building a shopping
list. This holds the current mode so the scan endpoint can route a barcode to
the right action, and a Stream Deck key (or the kiosk) can show and change it.

The mode is persisted to a tiny state file under data_dir (FoodAssistant-3jxk):
a server running multiple uvicorn workers must see the same mode from every
worker, or a mode set through one worker misroutes the scan handled by another.
Reads check the file's mtime and only re-parse when it changed, so the per-scan
cost is one stat call. A side effect worth keeping: the mode now survives an
app restart, which is friendlier than snapping back to "inventory". If data_dir
is not writable (tests, a read-only mount) the module quietly degrades to the
old process-local in-memory behavior.

An optional storage location scopes the "audit" mode (FoodAssistant-ugku) to
one place for a stock count; that location lives with the audit session itself
(services/audit.py), not here.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .state_lock import state_write_lock

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

# The in-process view of the shared state file: the last mode read from (or
# written to) disk, and the file mtime it corresponds to so a read can skip
# re-parsing an unchanged file. "mtime" is None until the file has been seen.
_state: dict = {"mode": _DEFAULT_MODE, "mtime": None}


def _state_file() -> Path:
    # Resolved per call (not at import) so tests that repoint data_dir work.
    from ..config import settings
    return Path(settings.data_dir) / "scanner_mode.json"


def _load() -> None:
    """Refresh the in-process mode from the state file if it changed on disk."""
    try:
        sf = _state_file()
        mtime = sf.stat().st_mtime_ns
    except OSError:
        return  # no file yet (fresh install, or unwritable data_dir)
    if mtime == _state["mtime"]:
        return
    try:
        mode = json.loads(sf.read_text()).get("mode")
    except (OSError, ValueError):
        return  # a torn or corrupt file never breaks a scan; keep what we have
    _state["mtime"] = mtime
    if mode in SCANNER_MODES:
        _state["mode"] = mode


def _save() -> None:
    """Write the current mode to the state file (atomic replace, best effort)."""
    sf = _state_file()
    try:
        tmp = sf.with_name(sf.name + ".tmp")
        tmp.write_text(json.dumps({"mode": _state["mode"]}))
        os.replace(tmp, sf)
        _state["mtime"] = sf.stat().st_mtime_ns
    except OSError:
        pass  # data_dir not writable: fall back to process-local behavior


def get_mode() -> str:
    _load()
    return _state["mode"]


def get_state() -> dict:
    """The full scanner state: {mode, label}."""
    mode = get_mode()
    return {"mode": mode, "label": MODE_LABELS.get(mode, mode.title())}


def _set_mode_locked(mode: str) -> dict:
    """Write the mode. Caller holds the cross-process write lock."""
    _state["mode"] = mode if mode in SCANNER_MODES else _DEFAULT_MODE
    _save()
    return {"mode": _state["mode"], "label": MODE_LABELS.get(_state["mode"], _state["mode"].title())}


def set_mode(mode: str) -> dict:
    """Set the mode (unknown values fall back to the default). Returns get_state()."""
    with state_write_lock(_state_file()):
        return _set_mode_locked(mode)


def cycle_mode() -> dict:
    """Advance to the next mode in SCANNER_MODES, wrapping around.

    The read-modify-write (current mode -> next mode) holds the shared file
    lock (FoodAssistant-k7cw) so two workers cycling at once advance twice
    instead of landing on the same mode."""
    with state_write_lock(_state_file()):
        _load()
        try:
            idx = SCANNER_MODES.index(_state["mode"])
        except ValueError:
            idx = -1
        return _set_mode_locked(SCANNER_MODES[(idx + 1) % len(SCANNER_MODES)])


def reset() -> None:
    """Return to the default mode and drop the state file (used by tests)."""
    _state["mode"] = _DEFAULT_MODE
    _state["mtime"] = None
    try:
        _state_file().unlink(missing_ok=True)
    except OSError:
        pass
