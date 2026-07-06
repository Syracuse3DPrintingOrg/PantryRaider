"""Cloudflare Turnstile signup CAPTCHA.

Dark until CLOUD_TURNSTILE_SITE_KEY and CLOUD_TURNSTILE_SECRET are both set,
the same gating pattern as Google sign-in. The signup form renders the widget,
which yields a token; the server verifies it with Cloudflare's siteverify
endpoint before creating the account.

Failure policy: an explicit "not a human" from Cloudflare blocks the signup,
but a transport error (Cloudflare unreachable) does not, since the honeypot,
per-IP rate limit, disposable-domain block, and password policy still apply and
a Cloudflare outage should not stop every real signup.
"""
from __future__ import annotations

import httpx

from .config import settings

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

# Tests inject an httpx.MockTransport here so no network call is made.
transport = None


def enabled() -> bool:
    return bool(settings.turnstile_site_key and settings.turnstile_secret)


def verify(token: str, remoteip: str = "") -> bool:
    """True when Turnstile is off, or the token is a valid human response."""
    if not enabled():
        return True
    if not token:
        return False
    data = {"secret": settings.turnstile_secret, "response": token}
    if remoteip:
        data["remoteip"] = remoteip
    try:
        client = (httpx.Client(transport=transport, timeout=10)
                  if transport is not None else httpx.Client(timeout=10))
        with client as c:
            r = c.post(SITEVERIFY_URL, data=data)
        return bool(r.json().get("success"))
    except (httpx.HTTPError, ValueError):
        # Cloudflare unreachable or a bad body: fail open (see module docstring).
        return True
