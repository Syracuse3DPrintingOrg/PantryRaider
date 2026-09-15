"""A tiny single-value TTL cache (pure logic, no I/O).

Built for the satellite /timers hot path: a kiosk page, the Start Page, the
screensaver pills, and a Stream Deck can all poll GET /timers within the same
second, and on a Pi Remote every one of those polls used to become its own
round trip to the main server. Holding the last upstream response for a
second or two collapses that burst into one request without the countdowns
ever looking stale (every surface ticks locally from deadline_epoch between
polls anyway).

Deliberately minimal: one value, wall-clock via an injectable `now` so tests
never sleep, and an explicit invalidate() for mutations. Not thread-safe by
design; the app uses it from a single asyncio event loop.
"""
from __future__ import annotations

import time
from typing import Any, Optional


class TTLCache:
    """Hold one value for at most `ttl` seconds."""

    def __init__(self, ttl: float) -> None:
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        self.ttl = float(ttl)
        self._value: Any = None
        self._stored_at: Optional[float] = None

    def get(self, now: Optional[float] = None) -> Any:
        """The cached value, or None when empty or expired."""
        if self._stored_at is None:
            return None
        if (time.monotonic() if now is None else now) - self._stored_at >= self.ttl:
            self.invalidate()
            return None
        return self._value

    def set(self, value: Any, now: Optional[float] = None) -> None:
        """Store `value`, restarting the TTL clock."""
        self._value = value
        self._stored_at = time.monotonic() if now is None else now

    def invalidate(self) -> None:
        """Drop the cached value immediately (e.g. after a mutation)."""
        self._value = None
        self._stored_at = None
