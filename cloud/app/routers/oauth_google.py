"""Google sign-in for the portal, and the app-return variant.

A hand-rolled OpenID Connect authorization-code flow over httpx (scope
"openid email"), no OAuth library: redirect the browser to Google, swap
the returned code for an access token, and read the verified email from
the userinfo endpoint. The email is the identity; a known email signs in
to its account, an unknown one becomes a new account with no password
(they can set one on the account page later).

Two flows share the same Google round-trip, told apart by what was
stashed in the state cookie:

- flow=portal: the "Continue with Google" buttons. Ends in a logged-in
  browser session on /account.
- flow=app: the Pantry Raider app opens this in a browser so a user who
  signs in with Google can link their kitchen without ever typing a
  password into the app. Ends in a redirect to the app's return_url with
  a short-lived single-use provision code, which the app redeems at the
  existing POST /v1/pairing/redeem for its instance token. Handing a
  pairing code to whoever completes a Google login is exactly as trusting
  as handing one to whoever is logged in to the portal (same TTL, same
  single-use redemption), so the app path adds no new attack surface.

Everything is gated on CLOUD_GOOGLE_CLIENT_ID / CLOUD_GOOGLE_CLIENT_SECRET:
unset means the buttons never render and these routes answer 404.
"""
from __future__ import annotations

import base64
import json
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import usage
from ..config import settings
from ..deps import ACCOUNT_DISABLED_MESSAGE, get_db, utc_now_iso
from ..models import Account, PairingCode
from ..security import new_pairing_code, token_hash

router = APIRouter(include_in_schema=False)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

# The in-flight flow rides a short-lived cookie: the CSRF state plus where
# to land afterwards. Tampering only affects the tamperer's own login, and
# return_url is re-validated at the callback.
STATE_COOKIE = "forager_oauth"
_STATE_TTL_SECONDS = 600

# Tests inject an httpx.MockTransport here; production uses the default.
transport: httpx.BaseTransport | None = None


def enabled() -> bool:
    return bool(settings.google_client_id and settings.google_client_secret)


def _require_enabled() -> None:
    if not enabled():
        raise HTTPException(404, detail="Not found")


def _redirect_uri() -> str:
    return settings.public_base_url.rstrip("/") + "/auth/google/callback"


def _pack(payload: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def _unpack(raw: str) -> dict:
    try:
        return json.loads(base64.urlsafe_b64decode(raw.encode()))
    except (ValueError, TypeError):
        return {}


def _safe_return_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def fetch_verified_email(code: str) -> str:
    """Exchange the authorization code and return Google's verified email.

    Raises HTTPException(502) if Google misbehaves and 403 if the email is
    unverified (an unverified address proves nothing about its owner)."""
    with httpx.Client(transport=transport, timeout=15.0) as client:
        token_resp = client.post(GOOGLE_TOKEN_URL, data={
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": _redirect_uri(),
            "grant_type": "authorization_code",
        })
        if token_resp.status_code != 200:
            raise HTTPException(502, detail="Google sign-in failed, try again")
        access_token = token_resp.json().get("access_token", "")
        info_resp = client.get(GOOGLE_USERINFO_URL, headers={
            "Authorization": f"Bearer {access_token}"})
    if info_resp.status_code != 200:
        raise HTTPException(502, detail="Google sign-in failed, try again")
    info = info_resp.json()
    email = (info.get("email") or "").strip().lower()
    if not email or not info.get("email_verified"):
        raise HTTPException(403, detail="Google did not confirm that email address")
    return email


def _account_for_email(db: Session, email: str, create: bool = True):
    """The account behind a Google-verified email. Returns None when no account
    exists and ``create`` is False (a sign-in attempt, which must not silently
    make an account), so the caller can offer to sign up instead."""
    account = db.query(Account).filter_by(email=email).first()
    if account:
        if not account.email_verified:
            # A pre-existing account (for example one made with a password
            # before email was set up) that signs in with Google is now proven
            # to own the address, so mark it verified: no confirmation needed.
            account.email_verified = 1
            db.commit()
        return account
    if not create:
        return None
    if not account:
        # Google already verified the address, so the account starts verified;
        # no confirmation email is needed.
        account = Account(email=email, password_hash="",
                          auth_provider="google", email_verified=1,
                          created_at=utc_now_iso())
        db.add(account)
        db.commit()
        # Google-created accounts start the same 30-day trial as everyone.
        usage.grant_trial(db, account.id, account.created_at)
    return account


def _mint_provision_code(db: Session, account_id: int) -> str:
    """A single-use short-TTL code the app can redeem for its instance
    token, using the same table, TTL, and redeem endpoint as portal-minted
    pairing codes."""
    code = new_pairing_code()
    expires = datetime.now(timezone.utc) + timedelta(
        minutes=settings.pairing_code_ttl_minutes)
    db.add(PairingCode(code_hash=token_hash(code), account_id=account_id,
                       expires_at=expires.isoformat(timespec="seconds"),
                       created_at=utc_now_iso()))
    db.commit()
    return code


@router.get("/auth/google/start")
def google_start(request: Request, flow: str = "portal", intent: str = "signin",
                 device_name: str = "", return_url: str = ""):
    _require_enabled()
    if flow not in ("portal", "app"):
        raise HTTPException(400, detail="Unknown flow")
    if intent not in ("signin", "signup"):
        intent = "signin"
    if flow == "app" and not _safe_return_url(return_url):
        raise HTTPException(400, detail="return_url must be an http(s) URL")
    state = secrets.token_urlsafe(24)
    auth_url = GOOGLE_AUTH_URL + "?" + urlencode({
        "client_id": settings.google_client_id,
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": "openid email",
        "state": state,
    })
    resp = RedirectResponse(auth_url, status_code=303)
    # device_name travels along for symmetry with provisioning, but the
    # app names its kitchen itself when it redeems the code.
    resp.set_cookie(STATE_COOKIE,
                    _pack({"state": state, "flow": flow,
                           "intent": intent,
                           "device_name": device_name,
                           "return_url": return_url}),
                    max_age=_STATE_TTL_SECONDS, httponly=True,
                    samesite="lax", secure=settings.cookie_secure)
    return resp


@router.get("/auth/google/callback")
def google_callback(request: Request, code: str = "", state: str = "",
                    db: Session = Depends(get_db)):
    _require_enabled()
    stashed = _unpack(request.cookies.get(STATE_COOKIE, ""))
    if not code or not state or state != stashed.get("state"):
        raise HTTPException(400, detail="Sign-in session did not match, start again")
    email = fetch_verified_email(code)
    # A sign-in from the login page must not create an account: offer signup
    # instead. The signup button and the app onboarding button create.
    create = stashed.get("intent") == "signup" or stashed.get("flow") == "app"
    account = _account_for_email(db, email, create=create)
    if account is None:
        resp = RedirectResponse("/login?m=google-no-account", status_code=303)
        resp.delete_cookie(STATE_COOKIE)
        return resp
    if account.disabled:
        raise HTTPException(403, detail=ACCOUNT_DISABLED_MESSAGE)

    if stashed.get("flow") == "app":
        return_url = stashed.get("return_url", "")
        if not _safe_return_url(return_url):
            raise HTTPException(400, detail="return_url must be an http(s) URL")
        # A 2FA account: Google proved the email but not the second factor, so
        # do NOT hand out a provision code yet. Raise the same code challenge
        # the portal login uses, carrying the app's return address so a correct
        # code sends the browser back to the app with the code (portal
        # login_2fa_submit does the mint and redirect).
        if account.totp_enabled:
            from .portal import _issue_totp_challenge  # avoids a cycle
            resp = _issue_totp_challenge(
                db, account.id, return_url=return_url,
                device_name=stashed.get("device_name", ""))
            resp.delete_cookie(STATE_COOKIE)
            return resp
        provision_code = _mint_provision_code(db, account.id)
        sep = "&" if "?" in return_url else "?"
        resp = RedirectResponse(f"{return_url}{sep}code={provision_code}",
                                status_code=303)
        resp.delete_cookie(STATE_COOKIE)
        return resp

    # Portal flow: a normal logged-in browser session, unless the account has
    # two-factor sign-in on, in which case Google proved the email but not the
    # second factor, so hand off to the code challenge instead of a session.
    from .portal import _issue_totp_challenge, _start_session  # avoids a cycle
    if account.totp_enabled:
        resp = _issue_totp_challenge(db, account.id)
    else:
        resp = _start_session(db, account.id)
    resp.delete_cookie(STATE_COOKIE)
    return resp
