"""The browser sign-in cookie (FoodAssistant-9g6os).

Starlette's SessionMiddleware, with four changes that keep people signed in
while they use the kitchen and nowhere else:

- Its own name. The cookie used to be called ``session``, which Flask,
  Starlette and plenty of other apps also use. Cookies are shared by every
  port on a host, so signing in to another app on the same server silently
  signed the browser out of this one. It is now ``pr_session``. A browser
  that still holds an old ``session`` cookie this app signed (the signature is
  checked, so another app's cookie is never touched) is moved over on its
  next visit, and nobody is signed out by the change.
- A sliding window. Starlette writes the cookie only when the session
  changes, so a browser used every day was still signed out 30 days after it
  signed in. The cookie is now re-signed once a day while in use (every half
  hour for a browser that was not remembered), so the 30 days count from the
  last visit, with a one-year cap from sign-in.
- Remember this device. A session whose ``remember`` is False gets a cookie
  with no expiry (the browser drops it when it closes) and a 12-hour idle
  limit. Sessions from before this change are remembered, as they were.
- Secure behind a proxy. The cookie is marked Secure when the request came
  over HTTPS, or through a proxy this app trusts (the Forager tunnel, a proxy
  on this machine or the Docker host) that says the browser used HTTPS.

The session itself (the signed JSON) is unchanged, so every key code reads
from request.session means what it did.
"""
from __future__ import annotations

import json
import time
from base64 import b64decode, b64encode

import itsdangerous
from itsdangerous.exc import BadSignature
from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection
from starlette.middleware.sessions import Session

COOKIE = "pr_session"
LEGACY_COOKIE = "session"
DAY = 86400
IDLE_REMEMBERED = 30 * DAY
IDLE_BROWSER = 12 * 3600
REFRESH_REMEMBERED = DAY
REFRESH_BROWSER = 1800
ABSOLUTE = 365 * DAY
CREATED_KEY = "created"
REMEMBER_KEY = "remember"


def remembered(data) -> bool:
    return data.get(REMEMBER_KEY, True) is not False


def _idle(data) -> int:
    return IDLE_REMEMBERED if remembered(data) else IDLE_BROWSER


def _refresh(data) -> int:
    return REFRESH_REMEMBERED if remembered(data) else REFRESH_BROWSER


def secure_request(scope, headers) -> bool:
    """Whether the browser reached this app over HTTPS."""
    if scope.get("scheme") == "https":
        return True
    proto = (headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if proto != "https":
        return False
    client = scope.get("client")
    if not client:
        return False
    try:
        from .services.rate_limit import _trusted_proxy_peer
        return _trusted_proxy_peer(str(client[0]))
    except Exception:
        return False


class PRSessionMiddleware:
    def __init__(self, app, secret_key: str, same_site: str = "lax") -> None:
        self.app = app
        self.signer = itsdangerous.TimestampSigner(str(secret_key))
        self.same_site = same_site

    def _load(self, raw: str, now: float):
        """(data, signed_at) for a cookie value, or None when it is not ours,
        damaged, idle too long or past the one-year cap."""
        try:
            payload, signed_at = self.signer.unsign(raw.encode("utf-8"), return_timestamp=True)
            data = json.loads(b64decode(payload))
        except (BadSignature, ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        ts = signed_at.timestamp()
        if now - ts > _idle(data):
            return None
        created = data.get(CREATED_KEY)
        if isinstance(created, (int, float)) and now - created > ABSOLUTE:
            return None
        return data, ts

    def _cookie(self, name: str, value: str, max_age: int | None, secure: bool) -> str:
        parts = [f"{name}={value}", "path=/"]
        if max_age is not None:
            parts.append(f"Max-Age={max_age}")
        parts += ["httponly", f"samesite={self.same_site}"]
        if secure:
            parts.append("Secure")
        return "; ".join(parts)

    def _expire(self, name: str, secure: bool) -> str:
        return self._cookie(name, "null", None, secure) + "; expires=Thu, 01 Jan 1970 00:00:00 GMT"

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        conn = HTTPConnection(scope)
        now = time.time()
        loaded = None
        migrated = False
        if COOKIE in conn.cookies:
            loaded = self._load(conn.cookies[COOKIE], now)
        if loaded is None and LEGACY_COOKIE in conn.cookies:
            loaded = self._load(conn.cookies[LEGACY_COOKIE], now)
            migrated = loaded is not None
        if loaded is not None:
            data, signed_at = loaded
            scope["session"] = Session(data)
            had = bool(data)
        else:
            scope["session"] = Session()
            signed_at, had = now, False
        secure = secure_request(scope, conn.headers)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                session = scope["session"]
                headers = MutableHeaders(scope=message)
                if session.accessed:
                    headers.add_vary_header("Cookie")
                stale = now - signed_at > _refresh(session)
                if session and (session.modified or migrated or stale):
                    body = dict(session)
                    body.setdefault(CREATED_KEY, int(now))
                    data = self.signer.sign(b64encode(json.dumps(body).encode("utf-8")))
                    max_age = IDLE_REMEMBERED if remembered(body) else None
                    headers.append("Set-Cookie", self._cookie(COOKIE, data.decode("utf-8"),
                                                              max_age, secure))
                elif not session and session.modified and had:
                    headers.append("Set-Cookie", self._expire(COOKIE, secure))
                if migrated:
                    # Only reached for an old cookie this app signed, so no
                    # other app's sign-in is ever cleared here.
                    headers.append("Set-Cookie", self._expire(LEGACY_COOKIE, secure))
            await send(message)

        await self.app(scope, receive, send_wrapper)
