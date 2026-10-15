"""On-screen Home Assistant event channel.

Home Assistant pushes events to Pantry Raider (a rest_command in an automation),
and the kiosk / web UI polls for them and shows them on the display: notification
toasts and camera pop-ups (for example, pop up the doorbell camera when a person
is detected). Events live in a small ring capped by count and age, so a kiosk
that was off does not get flooded with a backlog when it polls.

Each event has a monotonically increasing ``id`` so a client can poll for "what
is new since the last id I saw" without missing or replaying events.

Sharing (FoodAssistant-0fho): the ring persists to a small state file under
data_dir, the same pattern as scanner_mode.py, so an event HA posts reaches
every kiosk polling for it and survives an app restart. Reads check the file's mtime
and only re-parse when it changed, so a poll costs one stat call; polling
never writes the file, only adding an event does. If data_dir is not writable
(tests, a read-only mount) the module quietly degrades to the old
process-local in-memory behavior.
"""
from __future__ import annotations

import json
import asyncio
import threading
import time
from pathlib import Path

from .state_lock import state_write_lock
from .state_writer import StateWriter

# Keep only the most recent events, and drop anything older than the TTL, so a
# kiosk that was off does not get flooded with a backlog when it polls.
_MAX_EVENTS = 50
_TTL_SECONDS = 120

_lock = threading.Lock()
_events: list[dict] = []
_next_id = 1
# mtime of the state file our in-memory view corresponds to (None = never seen).
_mtime: int | None = None
# Monotonic time of the last flushed write, and the least time between two.
_last_sync: float | None = None
_SYNC_INTERVAL = 5.0
# An event added inside a request (Home Assistant, a deck press) is written
# from a thread, so a slow disk never holds up the other requests or the
# kiosk's long poll (services/state_writer.py). Memory holds the event first.
_writer = StateWriter("ha-events")


def _state_file() -> Path:
    # Resolved per call (not at import) so tests that repoint data_dir work.
    from ..config import settings
    return Path(settings.data_dir) / "ha_events.json"


def _load_locked() -> None:
    """Refresh the in-process ring from the state file if it changed on disk.
    Caller holds the lock."""
    global _events, _next_id, _mtime
    if _writer.busy():
        return  # our own write is on its way; memory is newer than the file
    try:
        sf = _state_file()
        mtime = sf.stat().st_mtime_ns
    except OSError:
        return  # no file yet (fresh install, or unwritable data_dir)
    if mtime == _mtime:
        return
    try:
        data = json.loads(sf.read_text())
        if not isinstance(data, dict):
            raise ValueError("state file is not an object")
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
    global _last_sync
    # This is the busiest store in the app: a chatty Home Assistant
    # automation, a camera poll or a run of Stream Deck presses can add
    # several events a second, and every add rewrites the file. Flushing each
    # of those to the SD card would wear it for state that is gone two
    # minutes later anyway, so the flush is limited to one every
    # _SYNC_INTERVAL seconds. The trade-off: a power cut can lose the last
    # few seconds of toasts, or leave the file damaged, which reads as an
    # empty ring (the same thing a reboot after two minutes shows).
    now = time.monotonic()
    sync = _last_sync is None or now - _last_sync >= _SYNC_INTERVAL
    try:
        sf = _state_file()
        blob = json.dumps({"next": _next_id, "events": _events}).encode("utf-8")
        _writer.write(sf, blob, _written, sync=sync)
        if sync:
            _last_sync = now
    except OSError:
        pass  # data_dir not writable: fall back to process-local behavior


def _written(mtime: int | None) -> None:
    # No lock here: an inline write can wait for a background one while the
    # caller holds _lock.
    global _mtime
    if mtime is not None:
        _mtime = mtime


def _prune_locked(now: float) -> None:
    global _events
    cutoff = now - _TTL_SECONDS
    _events = [e for e in _events if e["ts"] >= cutoff][-_MAX_EVENTS:]


def _add(event: dict) -> int:
    # The only mutation: hold the shared file lock (FoodAssistant-k7cw) across
    # the read-modify-write so two workers adding events at once cannot drop
    # one or reuse an id. Polls stay lock-free (they never write the file).
    global _next_id
    now = time.time()
    with _lock, state_write_lock(_state_file()):
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


def add_navigate(path: str, always: bool = False) -> int:
    """Queue a kiosk page-change event. ``path`` is an app-relative path (e.g.
    "ui/cook"), so a Home Assistant automation can drive which page the display
    shows (FoodAssistant-i4rs). The kiosk navigates same-origin only.

    ``always`` marks a navigation that is local device feedback rather than
    Home Assistant traffic, so the kiosk honours it even when on-screen HA
    events are turned off: a NeoKey press jumping the screen to the scan page
    is the same kind of local feedback as a Stream Deck confirmation toast."""
    return _add({"type": "navigate", "path": str(path or "").strip(),
                 "always": bool(always)})


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


async def wait_for_events(after_id: int = 0, timeout: float = 25.0,
                          interval: float = 0.05, is_gone=None) -> dict:
    """Long-poll: resolve the instant an event newer than ``after_id`` exists,
    or after ``timeout`` seconds with an empty batch.

    This is what makes a physical control feel instant: the kiosk holds one
    request open and the server answers the moment a NeoKey press lands, instead
    of the kiosk polling on a fixed tick and eating up to a tick of lag. Cheap
    and worker-agnostic: each check is the same mtime-gated read of the shared
    ring that ``poll`` does, and ``await`` frees the worker between checks, so a
    held request costs a stat() every ``interval`` and an event written by any
    worker ends the wait."""
    # Monotonic, so the clock jump a Raspberry Pi makes when it reaches a time
    # server after boot neither ends the wait early nor holds it for minutes.
    # is_gone (an async callable, the request's is_disconnected) ends the wait
    # as soon as the browser has given up on it, instead of holding a task for
    # a page that has already moved on.
    deadline = time.monotonic() + max(0.0, float(timeout))
    step = max(0.02, float(interval))
    next_check = 0.0
    while True:
        result = poll(after_id)
        now = time.monotonic()
        if result["events"] or now >= deadline:
            return result
        if is_gone is not None and now >= next_check:
            next_check = now + 0.5
            try:
                if await is_gone():
                    return result
            except Exception:
                is_gone = None
        await asyncio.sleep(step)


def active_count() -> int:
    """Number of live (non-expired) events currently in the ring.

    Pure over the shared ring: loads the state file and prunes by TTL the same
    way poll() does, then counts what remains. Feeds the Start Page Glance
    "Alerts" pill so it can show how many on-screen events are live without
    replaying them. Counts every event type still inside the TTL window."""
    now = time.time()
    with _lock:
        _load_locked()
        _prune_locked(now)
        return len(_events)


def last_id() -> int:
    with _lock:
        _load_locked()
        return _next_id - 1


def reset() -> None:
    """Clear all events and drop the state file (used by tests)."""
    global _events, _next_id, _mtime, _last_sync
    _writer.wait_idle()
    with _lock:
        _events = []
        _next_id = 1
        _mtime = None
        _last_sync = None
        try:
            _state_file().unlink(missing_ok=True)
        except OSError:
            pass
