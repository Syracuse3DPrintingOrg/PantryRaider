"""Cloudflare Turnstile signup CAPTCHA.

Dark until CLOUD_TURNSTILE_SITE_KEY and CLOUD_TURNSTILE_SECRET are both set,
the same gating pattern as Google sign-in. The signup form renders the widget,
which yields a token; the server verifies it with Cloudflare's siteverify
endpoint before creating the account.

Failure policy is per call site, chosen by the caller through fail_open. An
explicit "not a human" from Cloudflare always blocks. What differs is a
transport error (Cloudflare unreachable or a bad body):

- The signup form calls with fail_open=False, so a challenge that cannot be
  verified blocks the signup. A gate whose answer is unknown should not wave
  someone through; the account can be created a moment later once Cloudflare is
  reachable again.
- Other call sites (the recipe share paths) call with the default fail_open=True
  and lean on their other layers (honeypot, per-account and per-IP rate limit,
  and, for a linked app, an already-proven install) when Cloudflare is down, so
  one Cloudflare outage does not stop every real share.
"""
from __future__ import annotations

import httpx

from .config import settings

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

# Tests inject an httpx.MockTransport here so no network call is made.
transport = None


def enabled() -> bool:
    return bool(settings.turnstile_site_key and settings.turnstile_secret)


def verify(token: str, remoteip: str = "", fail_open: bool = True) -> bool:
    """True when Turnstile is off, or the token is a valid human response.

    fail_open decides what happens when Cloudflare cannot be reached or sends
    back something unreadable: True lets the request through, False blocks it.
    Signup passes fail_open=False so an unverifiable challenge stops the signup;
    see the module docstring for why each caller chooses as it does. A missing
    token, or an explicit "not a human" from Cloudflare, always blocks."""
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
        # Cloudflare unreachable or a bad body: fall back to the caller's policy
        # (see module docstring). Signup fails closed; the share paths fail open.
        return fail_open
