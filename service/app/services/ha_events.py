"""On-screen Home Assistant event channel.

Home Assistant pushes events to Pantry Raider (a rest_command in an automation),
and the kiosk / web UI polls for them and shows them on the display: notification
toasts and camera pop-ups (for example, pop up the doorbell camera when a person
is detected). Events live in a small ring capped by count and age, so a kiosk
that was off does not get flooded with a backlog when it polls.

Each event has a monotonically increasing ``id`` so a client can poll for "what
is new since the last id I saw" without missing or replaying events.

Sharing (FoodAssistant-0fho): the ring persists to a small state file under
data_dir, the same pattern as scanner_mode.py. A server running multiple
uvicorn workers must share the ring, or an event HA posts through one worker
never reaches the kiosk polling another worker. Reads check the file's mtime
and only re-parse when it changed, so a poll costs one stat call; polling
never writes the file, only adding an event does. If data_dir is not writable
(tests, a read-only mount) the module quietly degrades to the old
process-local in-memory behavior.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

# Keep only the most recent events, and drop anything older than the TTL, so a
# kiosk that was off does not get flooded with a backlog when it polls.
_MAX_EVENTS = 50
_TTL_SECONDS = 120

_lock = threading.Lock()
_events: list[dict] = []
_next_id = 1
# mtime of the state file our in-memory view corresponds to (None = never seen).
_mtime: int | None = None


def _state_file() -> Path:
    # Resolved per call (not at import) so tests that repoint data_dir work.
    from ..config import settings
    return Path(settings.data_dir) / "ha_events.json"


def _load_locked() -> None:
    """Refresh the in-process ring from the state file if it changed on disk.
    Caller holds the lock."""
    global _events, _next_id, _mtime
    try:
        sf = _state_file()
        mtime = sf.stat().st_mtime_ns
    except OSError:
        return  # no file yet (fresh install, or unwritable data_dir)
    if mtime == _mtime:
        return
    try:
        data = json.loads(sf.read_text())
        events = [e for e in data.get("events", []) if isinstance(e, dict) and "id" in e]
        nxt = int(data.get("next", 1))
    except (OSError, ValueError, TypeError):
        return  # a torn or corrupt file never breaks a poll; keep what we have
    _mtime = mtime
    _events = events
    _next_id = max(nxt, _next_id)


def _save_locked() -> None:
    """Write the ring to the state file (atomic replace, best effort). Caller
    holds the lock."""
    global _mtime
    sf = _state_file()
    try:
        tmp = sf.with_name(sf.name + ".tmp")
        tmp.write_text(json.dumps({"next": _next_id, "events": _events}))
        os.replace(tmp, sf)
        _mtime = sf.stat().st_mtime_ns
    except OSError:
        pass  # data_dir not writable: fall back to process-local behavior


def _prune_locked(now: float) -> None:
    global _events
    cutoff = now - _TTL_SECONDS
    _events = [e for e in _events if e["ts"] >= cutoff][-_MAX_EVENTS:]


def _add(event: dict) -> int:
    global _next_id
    now = time.time()
    with _lock:
        _load_locked()
        event["id"] = _next_id
        event["ts"] = now
        _next_id += 1
        _events.append(event)
        _prune_locked(now)
        _save_locked()
        return event["id"]


def add_notification(message: str, title: str = "", level: str = "info",
                     timeout: int = 0) -> int:
    """Queue a notification toast. ``level`` is info/success/warning/error."""
    lvl = level if level in ("info", "success", "warning", "error") else "info"
    return _add({
        "type": "notification",
        "message": str(message),
        "title": str(title),
        "level": lvl,
        "timeout": max(0, int(timeout or 0)),
    })


def add_confirmation(message: str, title: str = "", timeout: int = 5) -> int:
    """Queue a deck-action confirmation toast (FoodAssistant-rdlo).

    Distinct from an HA notification on purpose: the kiosk shows these even
    when on-screen Home Assistant events are turned off, because they are local
    feedback that a Stream Deck press worked, not Home Assistant traffic. Rides
    the same ring and since-id contract as everything else, so it degrades and
    shares across workers exactly like the rest."""
    return _add({
        "type": "confirm",
        "message": str(message),
        "title": str(title),
        "level": "success",
        "timeout": max(0, int(timeout or 0)),
    })


def add_warning(message: str, title: str = "", key: str = "",
                level: str = "warning", timeout: int = 0, pane: str = "") -> int:
    """Queue a device-health warning toast (FoodAssistant-h28s).

    Used for a live Raspberry Pi power/thermal condition (under-voltage,
    over-temperature, throttling). Distinct from a Home Assistant notification
    on purpose: like a deck confirmation it shows even when on-screen HA events
    are turned off, because it is a local device-health alert, not Home
    Assistant traffic. ``level`` is warning or error (anything else clamps to
    warning) so the toast reads as a real alert, not an "it worked". ``key`` is
    the underlying warning key (undervoltage, throttled, ...) so a client can
    tell one condition from another. ``pane`` is an optional settings pane id
    (e.g. "pane-network") the toast should deep-link to on click, instead of
    the generic /setup landing page (FoodAssistant-44f6); any warning can carry
    one, not just device health. Rides the same ring and since-id contract as
    everything else, so it degrades and shares across workers like the rest."""
    lvl = level if level in ("warning", "error") else "warning"
    return _add({
        "type": "warning",
        "message": str(message),
        "title": str(title),
        "key": str(key),
        "level": lvl,
        "timeout": max(0, int(timeout or 0)),
        "pane": str(pane or ""),
    })


def add_camera(name: str = "", src: str = "", seconds: int = 0) -> int:
    """Queue a camera pop-up. ``src`` is the proxy snapshot path the kiosk shows."""
    return _add({
        "type": "camera",
        "name": str(name),
        "src": str(src),
        "seconds": max(0, int(seconds or 0)),
    })


def add_navigate(path: str) -> int:
    """Queue a kiosk page-change event. ``path`` is an app-relative path (e.g.
    "ui/cook"), so a Home Assistant automation can drive which page the display
    shows (FoodAssistant-i4rs). The kiosk navigates same-origin only."""
    return _add({"type": "navigate", "path": str(path or "").strip()})


def poll(after_id: int = 0) -> dict:
    """Events newer than ``after_id``, plus the current last id.

    A fresh client should first read ``last_id`` (with after_id huge, or via the
    returned value) so it only sees events that arrive after it connects, rather
    than replaying the recent ring on load. Polling never writes the state file
    (the TTL/size prune is applied in memory and re-applied on the next add).
    """
    now = time.time()
    with _lock:
        _load_locked()
        _prune_locked(now)
        last = _next_id - 1
        try:
            after = int(after_id)
        except (TypeError, ValueError):
            after = 0
        fresh = [dict(e) for e in _events if e["id"] > after]
    return {"events": fresh, "last_id": last}


def last_id() -> int:
    with _lock:
        _load_locked()
        return _next_id - 1


def reset() -> None:
    """Clear all events and drop the state file (used by tests)."""
    global _events, _next_id, _mtime
    with _lock:
        _events = []
        _next_id = 1
        _mtime = None
        try:
            _state_file().unlink(missing_ok=True)
        except OSError:
            pass
