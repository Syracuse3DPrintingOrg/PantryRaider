"""The setup code: a way in for an owner who can only reach this kitchen from outside.

An install with no password only answers devices on the home network, and an
unconfigured one does not run its setup wizard for visitors from outside. The
setup code lets the owner continue from anywhere: it is printed to the app's
log (docker logs) and nowhere else, works once, and lasts 24 hours.

The code is written to data_dir/setup-code (0600, never backed up) and printed
with print(), never through logging, so log files and support bundles never
hold it.

Wrong tries are limited per client address (the first X-Forwarded-For entry
behind a proxy): 5 a minute. There is deliberately no global ceiling: anyone
who can reach the page could otherwise rotate made-up addresses and keep the
owner locked out, and 192 random bits valid for a day need no throttle
against guessing. The limiter tracks at most 1024 addresses; one that does not
fit is simply not tracked.

The password that goes with the code in the rescue is different: a code that
leaked (a shared log) must not let someone guess the password with made-up
addresses. Each code allows WRONG_PASSWORDS_PER_CODE wrong passwords in all,
counted in the code's own file so every worker sees them; then the code is
cleared and the page prints a new one when it is opened again.
"""
from __future__ import annotations

import hmac
import json
import logging
import secrets
import threading
import time
from pathlib import Path

log = logging.getLogger("foodassistant.setup_code")

FILENAME = "setup-code"
LIFETIME = 24 * 3600
WRONG_PER_MINUTE = 5
WINDOW = 60.0
MAX_KEYS = 1024
WRONG_PASSWORDS_PER_CODE = 10
LINE = ("Pantry Raider setup code: {code}. Enter it on the page this kitchen shows "
        "when you open it from outside your home network. It works once, within 24 "
        "hours.")

_lock = threading.Lock()
_state: dict = {"memory": None, "memory_only": False, "last_wrong_log": None,
                "wrong_since_log": 0}


class _Limiter:
    """Wrong tries per key in a sliding minute, with a bounded table."""

    def __init__(self, limit: int = WRONG_PER_MINUTE, window: float = WINDOW,
                 max_keys: int = MAX_KEYS):
        self.limit = limit
        self.window = window
        self.max_keys = max_keys
        self._events: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        for key in list(self._events):
            times = [t for t in self._events[key] if t >= cutoff]
            if times:
                self._events[key] = times
            else:
                del self._events[key]

    def blocked(self, key: str, now: float) -> bool:
        with self._lock:
            times = [t for t in self._events.get(key, ()) if t >= now - self.window]
            return len(times) >= self.limit

    def record(self, key: str, now: float) -> None:
        with self._lock:
            if key not in self._events and len(self._events) >= self.max_keys:
                self._prune(now)
                if len(self._events) >= self.max_keys:
                    return   # not tracked, so never blocked
            times = [t for t in self._events.get(key, ()) if t >= now - self.window]
            times.append(now)
            self._events[key] = times[-self.limit:]

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._events)


limiter = _Limiter()


def _path() -> Path:
    from ..config import settings
    return Path(settings.data_dir) / FILENAME


def _read():
    if _state["memory_only"]:
        return _state["memory"]
    try:
        data = json.loads(_path().read_text())
        if isinstance(data, dict) and isinstance(data.get("code"), str):
            return data
    except (OSError, ValueError):
        pass
    return _state["memory"]


def _write(data) -> None:
    _state["memory"] = data
    if _state["memory_only"]:
        return
    path = _path()
    try:
        if data is None:
            path.unlink(missing_ok=True)
            return
        from .. import durable
        durable.write_json(path, data, keep_last_good=False)
    except OSError:
        pass


def current(now=None):
    """The valid code, or None."""
    t = time.time() if now is None else float(now)
    data = _read()
    if not data:
        return None
    try:
        created = float(data.get("created"))
    except (TypeError, ValueError):
        return None
    if t - created >= LIFETIME or t < created - 300:
        return None
    return data.get("code")


def _announce(code: str) -> None:
    print(LINE.format(code=code), flush=True)


def ensure(reason: str = "", now=None) -> str:
    """Reuse a code under 24 hours old, else create and print a new one."""
    t = time.time() if now is None else float(now)
    with _lock:
        code = current(t)
        if code:
            return code
        code = secrets.token_urlsafe(24)
        _write({"code": code, "created": t})
    _announce(code)
    return code


def log_if_present(now=None) -> None:
    """Print the code again at start while it is still valid."""
    code = current(now)
    if code:
        _announce(code)


def clear() -> None:
    with _lock:
        _write(None)


def delete_expired(now=None) -> None:
    data = _read()
    if data and current(now) is None:
        clear()


def limiter_key(request) -> str:
    """The forwarded client address for a proxied request, else the peer."""
    try:
        from .access import classify_origin, first_forwarded_ip, unwrap_peer
        if classify_origin(request) == "proxied":
            first = first_forwarded_ip(request)
            if first:
                return first
        peer = unwrap_peer(request.client.host if request.client else None)
        return peer or "unknown"
    except Exception:
        return "unknown"


def _log_wrong(now: float) -> None:
    _state["wrong_since_log"] += 1
    last = _state["last_wrong_log"]
    if last is None or now - last >= 60:
        log.warning("Wrong setup code entered %d time(s) in the last minute.",
                    _state["wrong_since_log"])
        _state["last_wrong_log"] = now
        _state["wrong_since_log"] = 0


def record_wrong(key: str, now=None) -> None:
    t = time.time() if now is None else float(now)
    limiter.record(key or "unknown", t)
    _log_wrong(t)


def record_wrong_password(now=None) -> bool:
    """Count a wrong password given with the valid code (the rescue).

    Returns True when this used the code up: it is cleared, and the next
    visit to the page prints a new one."""
    t = time.time() if now is None else float(now)
    with _lock:
        data = _read()
        if not data or current(t) is None:
            return False
        data = dict(data)
        try:
            count = int(data.get("wrong_passwords") or 0) + 1
        except (TypeError, ValueError):
            count = 1
        if count >= WRONG_PASSWORDS_PER_CODE:
            _write(None)
            used_up = True
        else:
            data["wrong_passwords"] = count
            _write(data)
            used_up = False
    if used_up:
        log.warning("A setup code was used up by wrong passwords; opening the page prints "
                    "a new one.")
    return used_up


def verify(code: str, key: str, now=None) -> str:
    """'ok', 'wrong' or 'slow_down'."""
    t = time.time() if now is None else float(now)
    key = key or "unknown"
    if limiter.blocked(key, t):
        return "slow_down"
    expected = current(t)
    given = str(code or "").strip()
    if expected and given and hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8")):
        return "ok"
    record_wrong(key, t)
    return "wrong"


def reset(persist: bool = True) -> None:
    """Forget the code and the limiter (tests). persist=False never touches disk."""
    with _lock:
        _state["memory"] = None
        _state["memory_only"] = not persist
        _state["last_wrong_log"] = None
        _state["wrong_since_log"] = 0
        limiter.clear()
