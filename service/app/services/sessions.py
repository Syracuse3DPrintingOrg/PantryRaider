"""Signed-in browsers and trusted devices: data_dir/sessions.json
(FoodAssistant-6lbsa, FoodAssistant-qtm5f).

The sign-in cookie is signed, not stored, so on its own a copy of it stays
good until it runs out. This file gives each sign-in an id (``sid`` in the
cookie) and keeps:

- sessions: what Settings, Security lists under Signed-in browsers (when it
  signed in, when it was last used, a short description of the browser).
- revoked: the ids that were signed out. Signing out, or Sign out on one
  browser in Settings, puts the id here, so a copied cookie stops working
  too. An entry is dropped once the cookie it names would have run out.
- trusted: browsers that entered an authenticator code and asked not to be
  asked again for 30 days. The browser holds a random token in its own
  cookie; only its hash is stored. A trust is tied to the session generation
  it was made under, so changing the password or two-factor, or Sign out
  everywhere, ends every trust at once.

The file follows the state-file pattern: memory is the truth, writes go
through a StateWriter, last-used times are written at most every 10 minutes.
A cookie whose id is not on file (an older sign-in, or a lost file) is added,
never refused, so nobody is signed out by this file going missing.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from pathlib import Path

from .state_lock import state_write_lock
from .state_writer import StateWriter

FILENAME = "sessions.json"
SID_KEY = "sid"
TOUCH_SECONDS = 600.0
MAX_SESSIONS = 200
MAX_TRUSTED = 50
TRUST_DAYS = 30
TRUST_COOKIE = "pr_trusted"
DAY = 86400

_lock = threading.RLock()
_writer = StateWriter("sessions")
_state: dict = {"doc": None, "mtime": None, "dirty_at": None}


def _path() -> Path:
    from ..config import settings
    return Path(settings.data_dir) / FILENAME


def _empty() -> dict:
    return {"sessions": {}, "revoked": {}, "trusted": {}}


def _doc() -> dict:
    """The in-memory document, read from disk when it changed there."""
    if _writer.busy() and _state["doc"] is not None:
        return _state["doc"]
    path = _path()
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = None
    if _state["doc"] is None or (mtime is not None and mtime != _state["mtime"]):
        doc = _empty()
        if mtime is not None:
            try:
                raw = json.loads(path.read_text())
                if isinstance(raw, dict):
                    for k in doc:
                        if isinstance(raw.get(k), dict):
                            doc[k] = raw[k]
            except (OSError, ValueError):
                pass
        _state["doc"], _state["mtime"] = doc, mtime
    return _state["doc"]


def _save() -> None:
    doc = _state["doc"]
    _state["dirty_at"] = None
    try:
        _writer.write(_path(), json.dumps(doc).encode("utf-8"), _written)
    except OSError:
        pass


def _written(mtime) -> None:
    if mtime is not None:
        _state["mtime"] = mtime


def _prune(doc: dict, now: float) -> None:
    from ..session_cookie import IDLE_REMEMBERED
    for sid in [s for s, r in doc["revoked"].items() if not isinstance(r, (int, float)) or r < now]:
        doc["revoked"].pop(sid, None)
    for sid in [s for s, r in doc["sessions"].items()
                if now - float(r.get("last_seen", 0) or 0) > IDLE_REMEMBERED]:
        doc["sessions"].pop(sid, None)
    if len(doc["sessions"]) > MAX_SESSIONS:
        oldest = sorted(doc["sessions"], key=lambda s: doc["sessions"][s].get("last_seen", 0))
        for sid in oldest[:len(doc["sessions"]) - MAX_SESSIONS]:
            doc["sessions"].pop(sid, None)
    for tid in [t for t, r in doc["trusted"].items() if float(r.get("expires", 0) or 0) < now]:
        doc["trusted"].pop(tid, None)


def describe(ua: str) -> str:
    """A short name for a browser from its User-Agent: "Chrome on Android"."""
    ua = ua or ""
    browser = next((name for key, name in (
        ("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
        ("SamsungBrowser", "Samsung Internet"), ("Chrome/", "Chrome"),
        ("Safari/", "Safari")) if key in ua), "A browser")
    system = next((name for key, name in (
        ("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
        ("Windows", "Windows"), ("Mac OS X", "Mac"), ("CrOS", "ChromeOS"),
        ("Linux", "Linux")) if key in ua), "")
    return f"{browser} on {system}" if system else browser


# --- Signed-in browsers ------------------------------------------------------


def check(session, ua: str = "", now: float | None = None) -> bool:
    """Called on each signed-in request. Gives an older sign-in an id, notes
    when it was last used, and returns False when it was signed out."""
    now = time.time() if now is None else now
    sid = session.get(SID_KEY)
    with _lock:
        doc = _doc()
        if isinstance(sid, str) and sid in doc["revoked"]:
            return False
        if not isinstance(sid, str) or not sid:
            sid = secrets.token_urlsafe(16)
            session[SID_KEY] = sid
        rec = doc["sessions"].get(sid)
        if rec is None:
            with state_write_lock(_path()):
                doc["sessions"][sid] = {"created": int(now), "last_seen": int(now),
                                        "browser": describe(ua),
                                        "role": str(session.get("role", ""))[:16]}
                _prune(doc, now)
                _save()
        elif now - float(rec.get("last_seen", 0) or 0) > TOUCH_SECONDS:
            rec["last_seen"] = int(now)
            with state_write_lock(_path()):
                _save()
    return True


def new_id(session) -> None:
    """A fresh sign-in id. Its row is added on the browser's next request,
    which knows what browser it is."""
    session[SID_KEY] = secrets.token_urlsafe(16)


def revoke(sid: str, now: float | None = None) -> bool:
    from ..session_cookie import IDLE_REMEMBERED
    now = time.time() if now is None else now
    if not isinstance(sid, str) or not sid:
        return False
    with _lock, state_write_lock(_path()):
        doc = _doc()
        known = doc["sessions"].pop(sid, None) is not None
        doc["revoked"][sid] = int(now + IDLE_REMEMBERED)
        _prune(doc, now)
        _save()
    return known


def revoke_all_but(sid: str | None, now: float | None = None) -> int:
    """Sign out every listed browser except ``sid``; how many were."""
    now = time.time() if now is None else now
    with _lock:
        others = [s for s in _doc()["sessions"] if s != sid]
    for s in others:
        revoke(s, now)
    return len(others)


def listing(current_sid: str | None) -> list[dict]:
    with _lock:
        rows = [dict(r, id=s, current=(s == current_sid))
                for s, r in _doc()["sessions"].items()]
    rows.sort(key=lambda r: (not r["current"], -float(r.get("last_seen", 0) or 0)))
    return rows


# --- Trusted devices -----------------------------------------------------------


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def trust(ua: str = "", now: float | None = None) -> str:
    """Remember this browser for TRUST_DAYS; returns the token for its cookie."""
    from . import auth_state
    now = time.time() if now is None else now
    token = secrets.token_urlsafe(32)
    with _lock, state_write_lock(_path()):
        doc = _doc()
        doc["trusted"][_hash(token)] = {"created": int(now),
                                        "expires": int(now + TRUST_DAYS * DAY),
                                        "gen": auth_state.session_gen(),
                                        "browser": describe(ua)}
        _prune(doc, now)
        if len(doc["trusted"]) > MAX_TRUSTED:
            oldest = sorted(doc["trusted"], key=lambda t: doc["trusted"][t]["created"])
            for t in oldest[:len(doc["trusted"]) - MAX_TRUSTED]:
                doc["trusted"].pop(t, None)
        _save()
    return token


def is_trusted(token: str, now: float | None = None) -> bool:
    from . import auth_state
    now = time.time() if now is None else now
    if not isinstance(token, str) or not token:
        return False
    with _lock:
        rec = _doc()["trusted"].get(_hash(token))
    return bool(rec) and float(rec.get("expires", 0)) > now and \
        rec.get("gen") == auth_state.session_gen()


def forget_trusted() -> None:
    with _lock, state_write_lock(_path()):
        _doc()["trusted"].clear()
        _save()


def trusted_count(now: float | None = None) -> int:
    from . import auth_state
    now = time.time() if now is None else now
    with _lock:
        return sum(1 for r in _doc()["trusted"].values()
                   if float(r.get("expires", 0)) > now and r.get("gen") == auth_state.session_gen())


def reset() -> None:
    """Forget everything (tests)."""
    _writer.wait_idle()
    with _lock:
        _state.update({"doc": None, "mtime": None, "dirty_at": None})
    try:
        _path().unlink(missing_ok=True)
    except OSError:
        pass


def wait_idle() -> None:
    _writer.wait_idle()
