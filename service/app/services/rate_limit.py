"""A small in-memory attempt limiter for the app's own auth paths.

The local password, the local TOTP, the kiosk PIN, and the pairing request are
all secrets (or credential-minting actions) an internet-exposed install could be
hammered against. Nothing in the app throttled them before (FoodAssistant-7svb),
so a bare password behind a reverse proxy was brute-forceable. This adds a
sliding-window lockout keyed by the caller's address.

Scope and honesty: the state is per worker process, held in memory. With more
than one uvicorn worker the effective attempt budget multiplies by the worker
count, and a restart clears the counters. This is a real, documented limit: it
raises the cost of a brute-force by a large constant and stops casual scripts,
but it is not a substitute for a fronting proxy's own rate limiting or fail2ban.
Because it keys on the connecting address, a request arriving through a reverse
proxy is keyed by the proxy's address, so remote clients behind one proxy share
a budget. The internet second factor (see request_origin) is the primary control
for outside logins; this throttle is the backstop.

Pure and clock-injectable so it unit-tests without sleeping.
"""
from __future__ import annotations

import threading
import time


class RateLimiter:
    """Sliding-window failure lockout.

    A key is blocked once it accumulates ``max_attempts`` recorded events within
    ``window`` seconds; the block lifts automatically as those events age out of
    the window. ``record`` logs one event, ``reset`` clears a key (call it on a
    genuine success), ``retry_after`` returns the seconds until the key is
    allowed again (0 when it is allowed now). Thread-safe.
    """

    def __init__(self, max_attempts: int = 10, window: float = 300.0):
        self.max_attempts = max(1, int(max_attempts))
        self.window = float(window)
        self._events: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune_locked(self, key: str, now: float) -> list[float]:
        cutoff = now - self.window
        times = [t for t in self._events.get(key, ()) if t >= cutoff]
        if times:
            self._events[key] = times
        else:
            self._events.pop(key, None)
        return times

    def retry_after(self, key: str, now: float | None = None) -> int:
        """Seconds until ``key`` may try again, or 0 when it is allowed now."""
        now = time.time() if now is None else now
        with self._lock:
            times = self._prune_locked(key, now)
            if len(times) < self.max_attempts:
                return 0
            # The block clears when the (max_attempts)-th most recent event ages
            # out of the window, dropping the count below the threshold.
            unblock = times[-self.max_attempts] + self.window
            return max(1, int(unblock - now) + 1)

    def blocked(self, key: str, now: float | None = None) -> bool:
        return self.retry_after(key, now) > 0

    def record(self, key: str, now: float | None = None) -> None:
        """Log one attempt against ``key`` (call on a failure)."""
        now = time.time() if now is None else now
        if not key:
            return
        with self._lock:
            self._events.setdefault(key, []).append(now)
            self._prune_locked(key, now)

    def reset(self, key: str) -> None:
        """Forget a key's attempts (call on a genuine success)."""
        if not key:
            return
        with self._lock:
            self._events.pop(key, None)


# Shared limiters, one per surface so a run of bad PINs never locks out password
# logins and vice versa. Tuned loose enough that a fat-fingered owner is not
# locked out in normal use, tight enough to gut an online brute force.
#
# login_guard: wrong local password / wrong local TOTP.
# pin_guard:   wrong kiosk PIN.
# pairing_guard: pairing requests (spam/DoS of the admin's approval screen);
#   counts every request, not just failures, since the code is confirmed by the
#   server operator rather than guessed by the caller.
login_guard = RateLimiter(max_attempts=10, window=300.0)
pin_guard = RateLimiter(max_attempts=10, window=300.0)
pairing_guard = RateLimiter(max_attempts=15, window=300.0)


def client_key(request) -> str:
    """A rate-limit key for a request: the connecting peer address, or ''."""
    try:
        return request.client.host if request.client else ""
    except Exception:
        return ""
