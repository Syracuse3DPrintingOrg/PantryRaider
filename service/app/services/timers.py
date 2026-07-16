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
data_dir, the same pattern as scanner_mode.py. A server running multiple
uvicorn workers must see the same registry from every worker, or a timer
started through one worker is invisible to the poll another worker answers.
Reads check the file's mtime and only re-parse when it changed, so a poll
costs one stat call. A side effect worth keeping: timers now survive an app
restart (their epoch deadlines stay valid). If data_dir is not writable
(tests, a read-only mount) the module quietly degrades to the old
process-local in-memory behavior.

Concurrency (FoodAssistant-k7cw): writes are atomic (temp file + os.replace),
and every mutation's read-modify-write additionally holds the shared
cross-process file lock (services/state_lock.py), so two workers creating or
cancelling timers at the same time can no longer lose one of the updates.
Reads stay lock-free: the mtime cache keeps a poll at one stat call.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .state_lock import state_write_lock


@dataclass
class _Timer:
    id: int
    label: str
    total_seconds: float
    # Absolute wall-clock deadline: the shared countdown clock (see docstring).
    deadline_epoch: float
    created_epoch: float


_lock = threading.Lock()
_timers: dict[int, _Timer] = {}
_next_id = 0
# mtime of the state file our in-memory view corresponds to (None = never seen).
_mtime: int | None = None


def _state_file() -> Path:
    # Resolved per call (not at import) so tests that repoint data_dir work.
    from ..config import settings
    return Path(settings.data_dir) / "timers.json"


def _load_locked() -> None:
    """Refresh the in-process registry from the state file if it changed on
    disk. Caller holds the lock."""
    global _timers, _next_id, _mtime
    try:
        sf = _state_file()
        mtime = sf.stat().st_mtime_ns
    except OSError:
        return  # no file yet (fresh install, or unwritable data_dir)
    if mtime == _mtime:
        return
    try:
        data = json.loads(sf.read_text())
        rows = {}
        for raw in data.get("timers", []):
            t = _Timer(
                id=int(raw["id"]), label=str(raw.get("label", "")),
                total_seconds=float(raw.get("total_seconds", 0)),
                deadline_epoch=float(raw.get("deadline_epoch", 0)),
                created_epoch=float(raw.get("created_epoch", 0)),
            )
            rows[t.id] = t
        nxt = int(data.get("next", 0))
    except (OSError, ValueError, KeyError, TypeError):
        return  # a torn or corrupt file never breaks a poll; keep what we have
    _mtime = mtime
    _timers = rows
    _next_id = max(nxt, _next_id, *(rows or [0]))


def _save_locked() -> None:
    """Write the registry to the state file (atomic replace, best effort).
    Caller holds the lock."""
    global _mtime
    sf = _state_file()
    try:
        blob = {
            "next": _next_id,
            "timers": [
                {"id": t.id, "label": t.label, "total_seconds": t.total_seconds,
                 "deadline_epoch": t.deadline_epoch, "created_epoch": t.created_epoch}
                for t in sorted(_timers.values(), key=lambda t: t.id)
            ],
        }
        tmp = sf.with_name(sf.name + ".tmp")
        tmp.write_text(json.dumps(blob))
        os.replace(tmp, sf)
        _mtime = sf.stat().st_mtime_ns
    except OSError:
        pass  # data_dir not writable: fall back to process-local behavior


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
        # Absolute deadline (wall clock) so other machines can agree on remaining.
        "deadline_epoch": t.deadline_epoch,
        "created_epoch": t.created_epoch,
    }


def create_timer(label: str, seconds: float) -> dict:
    """Create and start a timer counting down `seconds` from now. Raises
    ValueError on a non-positive or unparseable duration."""
    try:
        total = float(seconds)
    except (TypeError, ValueError):
        raise ValueError("seconds must be a number")
    if total <= 0:
        raise ValueError("seconds must be greater than 0")

    global _next_id
    now_epoch = time.time()
    with _lock, state_write_lock(_state_file()):
        _load_locked()
        _next_id += 1
        timer = _Timer(
            id=_next_id,
            label=str(label or "").strip() or f"Timer {_next_id}",
            total_seconds=total,
            deadline_epoch=now_epoch + total,
            created_epoch=now_epoch,
        )
        _timers[timer.id] = timer
        _save_locked()
        return _serialize(timer, now_epoch)


def list_timers() -> list[dict]:
    """Return every timer (running and expired) with fresh remaining values,
    oldest first."""
    now_epoch = time.time()
    with _lock:
        _load_locked()
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
    try:
        extra = float(seconds)
    except (TypeError, ValueError):
        raise ValueError("seconds must be a number")
    if extra <= 0:
        raise ValueError("seconds must be greater than 0")

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
        return _serialize(t, now_epoch)


def cancel_timer(timer_id: int) -> bool:
    """Remove a timer. Returns True if it existed, False otherwise."""
    with _lock, state_write_lock(_state_file()):
        _load_locked()
        if _timers.pop(timer_id, None) is None:
            return False
        _save_locked()
        return True


def clear_all() -> int:
    """Drop every timer at once (the Timers page Clear all button, and test
    cleanup). Returns how many timers were removed."""
    with _lock, state_write_lock(_state_file()):
        _load_locked()
        count = len(_timers)
        _timers.clear()
        _save_locked()
        return count
