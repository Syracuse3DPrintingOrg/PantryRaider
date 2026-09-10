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
_windows: dict[str, tuple[int, int]] = {}  # key -> (window_start, count)


def allow(key: str, limit: int, now: float | None = None,
          window_seconds: int = 60) -> bool:
    """True if ``key`` may make another request this window. limit<=0 disables.

    The window is per-minute by default; a caller with a slower budget (share
    emails are capped per hour, not per minute) passes its own window_seconds.
    Keys are independent, so mixing window sizes across keys is fine.
    """
    if limit <= 0:
        return True
    window = int((time.time() if now is None else now) // window_seconds)
    with _LOCK:
        start, count = _windows.get(key, (window, 0))
        if start != window:
            start, count = window, 0
        if count >= limit:
            _windows[key] = (start, count)
            return False
        _windows[key] = (start, count + 1)
        return True


def reset() -> None:
    with _LOCK:
        _windows.clear()
