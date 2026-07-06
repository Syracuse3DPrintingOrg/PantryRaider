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
_windows: dict[str, tuple[int, int]] = {}  # key -> (window_start_minute, count)


def allow(key: str, limit: int, now: float | None = None) -> bool:
    """True if ``key`` may make another request this minute. limit<=0 disables."""
    if limit <= 0:
        return True
    minute = int((time.time() if now is None else now) // 60)
    with _LOCK:
        start, count = _windows.get(key, (minute, 0))
        if start != minute:
            start, count = minute, 0
        if count >= limit:
            _windows[key] = (start, count)
            return False
        _windows[key] = (start, count + 1)
        return True


def reset() -> None:
    with _LOCK:
        _windows.clear()
