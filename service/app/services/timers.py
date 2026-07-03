"""Server-side timer registry shared by every surface.

Timers live on the MAIN server so the web UI, Stream Deck, and satellites all
see the SAME running countdowns: any surface can poll and they agree. Each timer
carries a label, a total duration (seconds), and a server-computed remaining
time plus running/expired state.

Why two clocks: we count down with time.monotonic() (immune to wall-clock jumps
and NTP steps) for a correct local remaining value, AND we publish an absolute
time.time() epoch deadline. The epoch deadline is the shareable part: a satellite
or Stream Deck on a different machine cannot read our monotonic clock, but it can
take deadline_epoch and subtract its own time.time() to agree on remaining,
assuming roughly synced wall clocks (NTP). The state computation is a pure helper
(remaining_from_deadline) so it is unit-testable without sleeping.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _Timer:
    id: int
    label: str
    total_seconds: float
    # monotonic deadline drives our own countdown; epoch deadline is shared out.
    deadline_monotonic: float
    deadline_epoch: float
    created_epoch: float


_lock = threading.Lock()
_timers: dict[int, _Timer] = {}
_next_id = 0


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


def _serialize(t: _Timer, now_monotonic: float | None = None) -> dict:
    """Render a timer to a JSON-friendly dict with a fresh server-computed
    remaining/running/expired. running is simply "not expired"."""
    if now_monotonic is None:
        now_monotonic = time.monotonic()
    remaining, expired = remaining_from_deadline(t.deadline_monotonic, now_monotonic)
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
    now_mono = time.monotonic()
    now_epoch = time.time()
    with _lock:
        _next_id += 1
        timer = _Timer(
            id=_next_id,
            label=str(label or "").strip() or f"Timer {_next_id}",
            total_seconds=total,
            deadline_monotonic=now_mono + total,
            deadline_epoch=now_epoch + total,
            created_epoch=now_epoch,
        )
        _timers[timer.id] = timer
        return _serialize(timer, now_mono)


def list_timers() -> list[dict]:
    """Return every timer (running and expired) with fresh remaining values,
    oldest first."""
    now_mono = time.monotonic()
    with _lock:
        return [_serialize(t, now_mono) for t in sorted(_timers.values(), key=lambda t: t.id)]


def get_timer(timer_id: int) -> dict | None:
    """Return one timer's current state, or None if it does not exist."""
    with _lock:
        t = _timers.get(timer_id)
        if t is None:
            return None
        return _serialize(t)


def extend_timer(timer_id: int, seconds: float) -> dict | None:
    """Add `seconds` to a RUNNING timer and return its fresh state.

    Both deadlines move together (the monotonic one that drives our own
    countdown and the shared epoch one other surfaces subtract their clock
    from), and total_seconds grows to match so progress displays stay honest.
    Returns None for a missing or already-expired timer: a finished countdown
    is an alert waiting to be dismissed, not something to quietly restart.
    Raises ValueError on a non-positive or unparseable amount."""
    try:
        extra = float(seconds)
    except (TypeError, ValueError):
        raise ValueError("seconds must be a number")
    if extra <= 0:
        raise ValueError("seconds must be greater than 0")

    now_mono = time.monotonic()
    with _lock:
        t = _timers.get(timer_id)
        if t is None:
            return None
        _, expired = remaining_from_deadline(t.deadline_monotonic, now_mono)
        if expired:
            return None
        t.deadline_monotonic += extra
        t.deadline_epoch += extra
        t.total_seconds += extra
        return _serialize(t, now_mono)


def cancel_timer(timer_id: int) -> bool:
    """Remove a timer. Returns True if it existed, False otherwise."""
    with _lock:
        return _timers.pop(timer_id, None) is not None


def clear_all() -> int:
    """Drop every timer at once (the Timers page Clear all button, and test
    cleanup). Returns how many timers were removed."""
    with _lock:
        count = len(_timers)
        _timers.clear()
        return count
