"""Fixed-window in-memory rate limiter.

Deliberately simple for v1: one process, one dict, per-minute windows. It
caps burst abuse on signup and the AI proxy; Caddy adds connection-level
limits in front. If the service ever runs multiple workers this moves to a
shared store, which is why the interface is a plain function.
"""
from __future__ import annotations

import threading
import time

_LOCK = threading.Lock()
# WARNING: this counter lives in one process's memory. It is correct only with a
# single worker. Before this service runs more than one worker (uvicorn/gunicorn
# --workers > 1, or more than one replica), move this to a shared store such as
# Redis; otherwise each worker keeps its own window and the real limit becomes
# limit * worker_count, so every per-account and per-IP cap here is weaker than
# it looks. Keep the plain allow()/reset() interface so that swap stays local.
# key -> (window_start, count, window_seconds). The window size is stored per
# key because callers do not share one: share emails are capped per hour while
# everything else is per minute, and the sweep below has to know which window an
# entry belongs to before it can call that entry stale.
_windows: dict[str, tuple[int, int, int]] = {}

# Several callers key on the raw client address with no sign-in in front, so the
# caller chooses the key space and over IPv6 it is effectively unbounded. A key
# whose own window has already rolled over carries no information (the next call
# starts a fresh count either way), so those are swept out. The sweep walks the
# whole dict, so it runs once every _SWEEP_EVERY calls rather than on every one,
# which keeps the cost per call amortized to roughly constant.
_SWEEP_EVERY = 256
# The hard ceiling that applies after a sweep has run: past this the limiter
# denies a new key rather than growing, so a flood of one-shot keys cannot be
# turned into memory pressure. Anything with real traffic behind it (an account,
# a device, a busy address) is already resident by then, so its own budget still
# decides, and a denial here is the same answer an over-budget caller gets.
_MAX_KEYS = 100_000
_calls_since_sweep = 0


def _sweep_locked(now: float) -> None:
    """Drop every key whose own window has already rolled over. Holds _LOCK."""
    stale = [k for k, (start, _, ws) in _windows.items()
             if start != int(now // ws)]
    for k in stale:
        del _windows[k]


def allow(key: str, limit: int, now: float | None = None,
          window_seconds: int = 60) -> bool:
    """True if ``key`` may make another request this window. limit<=0 disables.

    The window is per-minute by default; a caller with a slower budget (share
    emails are capped per hour, not per minute) passes its own window_seconds.
    Keys are independent, so mixing window sizes across keys is fine.
    """
    global _calls_since_sweep
    if limit <= 0:
        return True
    now = time.time() if now is None else now
    window_seconds = max(1, int(window_seconds))
    window = int(now // window_seconds)
    with _LOCK:
        _calls_since_sweep += 1
        if _calls_since_sweep >= _SWEEP_EVERY or len(_windows) >= _MAX_KEYS:
            _calls_since_sweep = 0
            _sweep_locked(now)
        entry = _windows.get(key)
        if entry is None or entry[0] != window:
            if entry is None and len(_windows) >= _MAX_KEYS:
                return False  # deny rather than grow past the ceiling
            start, count = window, 0
        else:
            start, count = entry[0], entry[1]
        if count >= limit:
            _windows[key] = (start, count, window_seconds)
            return False
        _windows[key] = (start, count + 1, window_seconds)
        return True


def reset() -> None:
    global _calls_since_sweep
    with _LOCK:
        _windows.clear()
        _calls_since_sweep = 0
