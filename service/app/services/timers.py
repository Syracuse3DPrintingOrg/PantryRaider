"""Server-side timer registry shared by every surface.

Timers live on the MAIN server so the web UI, Stream Deck, and satellites all
see the SAME running countdowns: any surface can poll and they agree. Each timer
carries a label, a total duration (seconds), and a server-computed remaining
time plus running/expired state.

One clock, shared everywhere: a timer stores an absolute time.time() epoch
deadline, and remaining is derived by subtracting the reader's own time.time()
(the pure helper remaining_from_deadline). That epoch formula was always the
shareable half of the design: a satellite or Stream Deck on a different machine
cannot read this process's monotonic clock, so they already trusted
deadline_epoch. The registry used to ALSO keep a per-process time.monotonic()
deadline for its own countdown, but a monotonic value is meaningless outside
the process that took it, and the registry is now shared across worker
processes through a state file, so the epoch deadline is the single source of
countdown truth for every reader, local or remote (roughly synced wall clocks,
NTP, assumed as before).

Sharing (FoodAssistant-0fho): timers persist to a small state file under
data_dir, the same pattern as scanner_mode.py, so every surface (web UI,
kiosk, Stream Deck) reads one registry and a restore or hand edit is picked
up. The app runs as one process (FoodAssistant-n6mta). Reads check the
file's mtime and only re-parse when it changed, so a poll costs one stat
call. A side effect worth keeping: timers now survive an app
restart (their epoch deadlines stay valid). If data_dir is not writable
(tests, a read-only mount) the module quietly degrades to the old
process-local in-memory behavior.

Concurrency (FoodAssistant-k7cw): writes are atomic (temp file + os.replace),
and every mutation's read-modify-write additionally holds the shared write
lock (services/state_lock.py), so two requests creating or cancelling timers
at the same time can no longer lose one of the updates.
Reads stay lock-free: the mtime cache keeps a poll at one stat call.
"""
from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .state_lock import state_write_lock
from .state_writer import StateWriter


@dataclass
class _Timer:
    id: int
    label: str
    total_seconds: float
    # Absolute wall-clock deadline: the shared countdown clock (see docstring).
    deadline_epoch: float
    created_epoch: float
    # Who set it: a screen id, a Home Assistant satellite, or nothing. Whoever
    # rings for a finished timer reads this to ring where it was asked for.
    origin: str = ""
    # True once the "finished" event has gone out, so it goes out once.
    rang: bool = False
    # What to do once it rings, for a timer a recipe step started ("Next:
    # whisk the filling"), shown on the "Time is up" screen. Optional.
    note: str = ""


_lock = threading.Lock()
_timers: dict[int, _Timer] = {}
_next_id = 0
# What happened to timers lately (started, extended, finished, cancelled,
# dismissed), for callers that ring or draw for them: Home Assistant, a
# screen's own timer bar. Kept in memory only. After a restart the ids start
# over, which a reader sees as a last_id below the one it holds and answers by
# reading the list again.
_MAX_EVENTS = 100
_events: list[dict] = []
_next_event = 1
# Called with each event after it is recorded, outside the lock.
_listeners: list = []
ORIGIN_MAX = 64
NOTE_MAX = 160
# mtime of the state file our in-memory view corresponds to (None = never seen).
_mtime: int | None = None
# A timer change made inside a request is written from a thread, so a slow
# disk never holds up the other requests (services/state_writer.py).
_writer = StateWriter("timers")


def _state_file() -> Path:
    # Resolved per call (not at import) so tests that repoint data_dir work.
    from ..config import settings
    return Path(settings.data_dir) / "timers.json"


def _load_locked() -> None:
    """Refresh the in-process registry from the state file if it changed on
    disk. Caller holds the lock."""
    global _timers, _next_id, _mtime
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
        rows = {}
        for raw in data.get("timers", []):
            t = _Timer(
                id=int(raw["id"]), label=str(raw.get("label", "")),
                total_seconds=float(raw.get("total_seconds", 0)),
                deadline_epoch=float(raw.get("deadline_epoch", 0)),
                created_epoch=float(raw.get("created_epoch", 0)),
                origin=str(raw.get("origin", "") or "")[:ORIGIN_MAX],
                rang=bool(raw.get("rang", False)),
                note=str(raw.get("note", "") or "")[:NOTE_MAX],
            )
            if not all(math.isfinite(v) for v in
                       (t.total_seconds, t.deadline_epoch, t.created_epoch)):
                continue  # a non-finite timer saved by an older build
            rows[t.id] = t
        nxt = int(data.get("next", 0))
    except (OSError, ValueError, KeyError, TypeError):
        return  # a torn or corrupt file never breaks a poll; keep what we have
    _mtime = mtime
    _timers = rows
    _next_id = max(nxt, _next_id, *(rows or [0]))


def _save_locked() -> None:
    """Write the registry to the state file (atomic replace, best effort).
    Caller holds the lock. On the event loop the write itself runs in a
    thread; memory already holds the change."""
    sf = _state_file()
    try:
        blob = {
            "next": _next_id,
            "timers": [
                {"id": t.id, "label": t.label, "total_seconds": t.total_seconds,
                 "deadline_epoch": t.deadline_epoch, "created_epoch": t.created_epoch,
                 "origin": t.origin, "rang": t.rang, "note": t.note}
                for t in sorted(_timers.values(), key=lambda t: t.id)
            ],
        }
        _writer.write(sf, json.dumps(blob).encode("utf-8"), _written)
    except OSError:
        pass  # data_dir not writable: fall back to process-local behavior


def _written(mtime: int | None) -> None:
    # No lock here: an inline write can wait for a background one while the
    # caller holds _lock. A failed write (unwritable data_dir) leaves memory
    # as the only copy, the old process-local behavior.
    global _mtime
    if mtime is not None:
        _mtime = mtime


def remaining_from_deadline(deadline: float, now: float) -> tuple[float, bool]:
    """Pure helper: given a deadline and a current time on the SAME clock, return
    (remaining_seconds, expired). remaining never goes negative; expired is True
    once now has reached or passed the deadline.

    This is the single source of countdown truth and the satellite-shareable
    formula (pass deadline_epoch + the surface's own time.time())."""
    remaining = deadline - now
    if remaining <= 0:
        return 0.0, True
    return remaining, False


# A finished timer stays on screen until someone dismisses it, but not
# forever: one that went off this long ago is dropped. Nothing else bounds the
# list, and a timer started from Home Assistant or a Bandit Cub on a kitchen
# with no screen is never dismissed, so the list (written on every change and
# sent with every poll) would otherwise only grow.
STALE_AFTER_SECONDS = 24 * 3600.0


def _prune_locked(now_epoch: float) -> bool:
    """Drop timers that went off more than STALE_AFTER_SECONDS ago. Caller
    holds the lock. True when something was dropped."""
    stale = [tid for tid, t in _timers.items()
             if now_epoch - t.deadline_epoch > STALE_AFTER_SECONDS]
    for tid in stale:
        del _timers[tid]
    return bool(stale)


def _serialize(t: _Timer, now_epoch: float | None = None) -> dict:
    """Render a timer to a JSON-friendly dict with a fresh server-computed
    remaining/running/expired. running is simply "not expired"."""
    if now_epoch is None:
        now_epoch = time.time()
    remaining, expired = remaining_from_deadline(t.deadline_epoch, now_epoch)
    return {
        "id": t.id,
        "label": t.label,
        "total_seconds": t.total_seconds,
        "remaining_seconds": round(remaining, 3),
        "running": not expired,
        "expired": expired,
        # A finished timer rings until someone dismisses it.
        "state": "ringing" if expired else "running",
        "origin": t.origin,
        "note": t.note,
        # Absolute deadline (wall clock) so other machines can agree on remaining.
        "deadline_epoch": t.deadline_epoch,
        "created_epoch": t.created_epoch,
    }


def _duration(seconds) -> float:
    """A positive, finite number of seconds, or ValueError.

    Infinity or NaN would be saved to the state file and then break every
    later read of the timer list, since JSON has no way to send them.
    """
    try:
        value = float(seconds)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("seconds must be a number")
    if not math.isfinite(value):
        raise ValueError("seconds must be a finite number")
    if value <= 0:
        raise ValueError("seconds must be greater than 0")
    return value


def add_listener(fn) -> None:
    """Call ``fn(event)`` after every timer event (started, extended, finished,
    cancelled, dismissed). Runs outside the lock; an error in one is ignored."""
    if fn not in _listeners:
        _listeners.append(fn)


def _record_locked(kind: str, timer: dict, now_epoch: float) -> dict:
    global _next_event
    event = {"id": _next_event, "ts": now_epoch, "type": kind, "timer": timer}
    _next_event += 1
    _events.append(event)
    del _events[:-_MAX_EVENTS]
    return event


def _tell(events) -> None:
    for event in events:
        for fn in list(_listeners):
            try:
                fn(event)
            except Exception:
                pass


def _finish_due_locked(now_epoch: float) -> list[dict]:
    """Record "finished" for each timer whose time is up and has not been
    announced yet. Caller holds both locks and saves when this returns any."""
    out = []
    for t in sorted(_timers.values(), key=lambda t: t.deadline_epoch):
        if not t.rang and t.deadline_epoch <= now_epoch:
            t.rang = True
            out.append(_record_locked("finished", _serialize(t, now_epoch), now_epoch))
    return out


def finish_due() -> int:
    """Announce every timer that has just run out. Cheap when none has: one
    pass over the list in memory, and the file is only written on a change."""
    now_epoch = time.time()
    with _lock:
        _load_locked()
        if not any(not t.rang and t.deadline_epoch <= now_epoch for t in _timers.values()):
            return 0
    with _lock, state_write_lock(_state_file()):
        _load_locked()
        done = _finish_due_locked(now_epoch)
        if done:
            _save_locked()
    _tell(done)
    return len(done)


def next_deadline() -> float | None:
    """When the next running timer ends (epoch seconds), or None."""
    now_epoch = time.time()
    with _lock:
        _load_locked()
        ends = [t.deadline_epoch for t in _timers.values()
                if not t.rang and t.deadline_epoch > now_epoch]
    return min(ends) if ends else None


def create_timer(label: str, seconds: float, origin: str = "", note: str = "") -> dict:
    """Create and start a timer counting down `seconds` from now. Raises
    ValueError on a non-positive or unparseable duration. ``origin`` names
    who set it (a screen id, a Home Assistant satellite)."""
    total = _duration(seconds)

    global _next_id
    now_epoch = time.time()
    with _lock, state_write_lock(_state_file()):
        _load_locked()
        _prune_locked(now_epoch)
        _next_id += 1
        timer = _Timer(
            id=_next_id,
            label=str(label or "").strip() or f"Timer {_next_id}",
            total_seconds=total,
            deadline_epoch=now_epoch + total,
            created_epoch=now_epoch,
            origin=str(origin or "").strip()[:ORIGIN_MAX],
            note=" ".join(str(note or "").split())[:NOTE_MAX],
        )
        _timers[timer.id] = timer
        _save_locked()
        out = _serialize(timer, now_epoch)
        event = _record_locked("started", out, now_epoch)
    _tell([event])
    return out


def list_timers() -> list[dict]:
    """Return every timer (running and expired) with fresh remaining values,
    oldest first."""
    now_epoch = time.time()
    with _lock:
        _load_locked()
        # In memory only: the file catches up at the next change.
        _prune_locked(now_epoch)
        return [_serialize(t, now_epoch) for t in sorted(_timers.values(), key=lambda t: t.id)]


def get_timer(timer_id: int) -> dict | None:
    """Return one timer's current state, or None if it does not exist."""
    with _lock:
        _load_locked()
        t = _timers.get(timer_id)
        if t is None:
            return None
        return _serialize(t)


def extend_timer(timer_id: int, seconds: float) -> dict | None:
    """Add `seconds` to a RUNNING timer and return its fresh state.

    The shared epoch deadline moves (every surface subtracts its own clock
    from it), and total_seconds grows to match so progress displays stay
    honest. Returns None for a missing or already-expired timer: a finished
    countdown is an alert waiting to be dismissed, not something to quietly
    restart. Raises ValueError on a non-positive or unparseable amount."""
    extra = _duration(seconds)

    now_epoch = time.time()
    with _lock, state_write_lock(_state_file()):
        _load_locked()
        t = _timers.get(timer_id)
        if t is None:
            return None
        _, expired = remaining_from_deadline(t.deadline_epoch, now_epoch)
        if expired:
            return None
        t.deadline_epoch += extra
        t.total_seconds += extra
        _save_locked()
        out = _serialize(t, now_epoch)
        event = _record_locked("extended", out, now_epoch)
    _tell([event])
    return out


def _removed_kind(t: _Timer, now_epoch: float) -> str:
    """Taking away a timer that is still counting is a cancel; taking away one
    that has finished is a dismiss."""
    return "dismissed" if t.deadline_epoch <= now_epoch else "cancelled"


def cancel_timer(timer_id: int) -> bool:
    """Remove a timer, running or finished. Returns True if it existed."""
    now_epoch = time.time()
    with _lock, state_write_lock(_state_file()):
        _load_locked()
        t = _timers.pop(timer_id, None)
        if t is None:
            return False
        _save_locked()
        event = _record_locked(_removed_kind(t, now_epoch), _serialize(t, now_epoch),
                               now_epoch)
    _tell([event])
    return True


def dismiss_timer(timer_id: int) -> str:
    """Dismiss a finished timer. Returns "ok", "running" when it has not
    finished yet (nothing changes), or "missing"."""
    now_epoch = time.time()
    with _lock, state_write_lock(_state_file()):
        _load_locked()
        t = _timers.get(timer_id)
        if t is None:
            return "missing"
        if t.deadline_epoch > now_epoch:
            return "running"
        del _timers[timer_id]
        _save_locked()
        event = _record_locked("dismissed", _serialize(t, now_epoch), now_epoch)
    _tell([event])
    return "ok"


def dismiss_ringing(origin: str | None = None) -> int:
    """Dismiss every finished timer, or only those one origin set. Returns
    how many went."""
    now_epoch = time.time()
    events = []
    with _lock, state_write_lock(_state_file()):
        _load_locked()
        for tid, t in sorted(_timers.items()):
            if t.deadline_epoch > now_epoch:
                continue
            if origin is not None and t.origin != origin:
                continue
            del _timers[tid]
            events.append(_record_locked("dismissed", _serialize(t, now_epoch), now_epoch))
        if events:
            _save_locked()
    _tell(events)
    return len(events)


def clear_all() -> int:
    """Drop every timer at once (the Timers page Clear all button, and test
    cleanup). Returns how many timers were removed."""
    now_epoch = time.time()
    events = []
    with _lock, state_write_lock(_state_file()):
        _load_locked()
        for _tid, t in sorted(_timers.items()):
            events.append(_record_locked(_removed_kind(t, now_epoch),
                                         _serialize(t, now_epoch), now_epoch))
        _timers.clear()
        _save_locked()
    _tell(events)
    return len(events)


def poll_events(after_id: int = 0) -> dict:
    """Timer events newer than ``after_id``, the current last id, and the
    list as it stands. A last_id below the caller's own means the app
    restarted: read the list and carry on from the new id."""
    finish_due()
    try:
        after = int(after_id)
    except (TypeError, ValueError):
        after = 0
    with _lock:
        last = _next_event - 1
        fresh = [dict(e) for e in _events if e["id"] > after]
    return {"events": fresh, "last_id": last, "timers": list_timers()}


def events_behind(result: dict, after_id) -> bool:
    """True when the caller's cursor is ahead of this process (a restart)."""
    try:
        after = int(after_id)
    except (TypeError, ValueError):
        return False
    return after < 999999999 and result["last_id"] < after


async def wait_for_events(after_id: int = 0, timeout: float = 25.0, is_gone=None) -> dict:
    """Long poll for timer events: answers the moment one is recorded, when a
    running timer reaches its end, or after ``timeout`` seconds with none."""
    import asyncio
    deadline = time.monotonic() + max(0.0, float(timeout))
    next_gone = 0.0
    while True:
        result = poll_events(after_id)
        now = time.monotonic()
        if result["events"] or events_behind(result, after_id) or now >= deadline:
            return result
        if is_gone is not None and now >= next_gone:
            next_gone = now + 0.5
            try:
                if await is_gone():
                    return result
            except Exception:
                is_gone = None
        # Sleep to the next thing that can happen: a timer running out, or a
        # short tick to notice one started, cancelled or dismissed elsewhere.
        step = 0.1
        due = next_deadline()
        if due is not None:
            step = min(step, max(0.02, due - time.time()))
        await asyncio.sleep(max(0.02, min(step, deadline - now)))
