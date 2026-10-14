"""Passkey (WebAuthn / FIDO2) sign-in for the Forager account.

Two ceremonies, each a begin/finish pair:

- Registration (a signed-in account adds a passkey from the Security pane):
  begin hands the browser creation options with a fresh challenge stashed
  server-side; finish verifies the attestation and stores the new credential.
- Authentication (sign in with a passkey from the login page): begin hands the
  browser request options; finish verifies the assertion, checks the signature
  counter advanced (a clone or replay is refused), and opens the normal portal
  session, exactly like a password login.

A passkey is always an addition. Registering one never touches the password or
two-factor sign-in, and both keep working. Challenges live server-side in
webauthn_challenges, named by a short-lived HttpOnly cookie, so a finish cannot
be replayed or guessed. Only public keys are stored, never a secret.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from .. import ratelimit, webauthn_service as wa
from ..config import settings
from ..deps import (ACCOUNT_DISABLED_MESSAGE, client_ip, cookie_account,
                    get_db, require_same_origin, utc_now_iso)
from ..models import Account, WebAuthnChallenge, WebAuthnCredential
from ..security import new_token, token_hash
from .accounts import _issue_session
from .portal import SESSION_COOKIE

router = APIRouter(include_in_schema=False)

# The cookies that name a pending challenge. Separate names for the two
# ceremonies so a registration challenge can never stand in for a sign-in.
REG_COOKIE = "forager_passkey_reg"
AUTH_COOKIE = "forager_passkey_auth"
_CHALLENGE_TTL_MINUTES = 5

# What the browser is told when something goes wrong. Deliberately plain and
# free of jargon: the person just needs to know to try again with their device.
_ADD_FAILED = ("That did not work. Try adding your passkey again, and make sure "
               "you finish the prompt from your device.")
_SIGNIN_FAILED = ("That did not work. Try again, and make sure you finish the "
                  "prompt from your device.")


def _client(request: Request) -> str:
    return client_ip(request)


def _too_many(request: Request, account_id: int = 0) -> bool:
    """Whether this caller has spent its passkey budget for the minute. Applied
    per IP always, and per account when one is known."""
    ok = ratelimit.allow(f"passkey-ip:{_client(request)}",
                         settings.passkey_rate_per_minute)
    if ok and account_id:
        ok = ratelimit.allow(f"passkey-acct:{account_id}",
                             settings.passkey_rate_per_minute)
    return not ok


def _stash_challenge(db: Session, purpose: str, challenge_b64: str,
                     account_id: int = 0) -> str:
    """Persist a challenge and return the plaintext cookie token that names it.
    Stored hashed, like every other credential; expires fast."""
    token = new_token("pwc")
    expires = datetime.now(timezone.utc) + timedelta(
        minutes=_CHALLENGE_TTL_MINUTES)
    db.add(WebAuthnChallenge(
        token_hash=token_hash(token), purpose=purpose, account_id=account_id,
        challenge=challenge_b64,
        expires_at=expires.isoformat(timespec="seconds"),
        created_at=utc_now_iso()))
    db.commit()
    return token


def _take_challenge(db: Session, request: Request, cookie: str, purpose: str
                    ) -> WebAuthnChallenge | None:
    """The live challenge named by the cookie, consumed (deleted) so it works
    exactly once. None when missing, expired, or the wrong purpose."""
    token = request.cookies.get(cookie, "")
    if not token:
        return None
    row = (db.query(WebAuthnChallenge)
           .filter_by(token_hash=token_hash(token), purpose=purpose).first())
    if not row:
        return None
    if row.expires_at < utc_now_iso():
        db.delete(row)
        db.commit()
        return None
    # Detach the values before deleting so the caller can still read them.
    data = WebAuthnChallenge(challenge=row.challenge, account_id=row.account_id)
    db.delete(row)
    db.commit()
    return data


def _fail(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


# --- Registration: a signed-in account adds a passkey ---------------------

@router.post("/account/passkeys/register/begin")
def register_begin(request: Request,
                   account: Account | None = Depends(cookie_account),
                   db: Session = Depends(get_db)):
    if not account:
        return _fail("Please sign in first.", 401)
    if _too_many(request, account.id):
        return _fail("Too many attempts. Wait a minute and try again.", 429)
    rp_id, _origin = wa.rp_id_and_origin(request)
    existing = (db.query(WebAuthnCredential)
                .filter_by(account_id=account.id).all())
    exclude = [wa.descriptor(c.credential_id, c.transports) for c in existing]
    options_json, challenge_b64 = wa.registration_options_json(
        rp_id, account.id, account.email, exclude)
    token = _stash_challenge(db, "register", challenge_b64, account.id)
    resp = JSONResponse(content={"options": options_json})
    resp.set_cookie(REG_COOKIE, token, max_age=_CHALLENGE_TTL_MINUTES * 60,
                    httponly=True, samesite="lax",
                    secure=settings.cookie_secure)
    return resp


@router.post("/account/passkeys/register/finish")
async def register_finish(request: Request,
                          account: Account | None = Depends(cookie_account),
                          db: Session = Depends(get_db)):
    if not account:
        return _fail("Please sign in first.", 401)
    if _too_many(request, account.id):
        return _fail("Too many attempts. Wait a minute and try again.", 429)
    try:
        body = await request.json()
    except (ValueError, TypeError):
        body = {}
    credential = body.get("credential")
    nickname = (body.get("nickname") or "").strip()[:120] or "Passkey"
    stash = _take_challenge(db, request, REG_COOKIE, "register")
    resp_fail = _fail(_ADD_FAILED)
    resp_fail.delete_cookie(REG_COOKIE)
    if not credential or not stash or stash.account_id != account.id:
        return resp_fail
    rp_id, origin = wa.rp_id_and_origin(request)
    try:
        verified = wa.verify_registration(
            credential=credential,
            expected_challenge=wa.base64url_to_bytes(stash.challenge),
            rp_id=rp_id, origin=origin)
    except Exception:
        # py_webauthn raises a family of validation errors (bad challenge,
        # wrong origin, tampered attestation); all map to the same plain
        # message, never leaking which check failed.
        return resp_fail
    cred_id_b64 = wa.bytes_to_base64url(verified.credential_id)
    # A passkey signs in exactly one account: if this credential id is already
    # on file, do not silently move it. Treat a re-add of the caller's own key
    # as success (idempotent), and refuse one that belongs elsewhere.
    owner = (db.query(WebAuthnCredential)
             .filter_by(credential_id=cred_id_b64).first())
    if owner and owner.account_id != account.id:
        out = _fail("That passkey is already registered to another account.")
        out.delete_cookie(REG_COOKIE)
        return out
    if owner:
        out = JSONResponse(content={"ok": True, "nickname": owner.nickname})
        out.delete_cookie(REG_COOKIE)
        return out
    transports = ""
    raw_transports = (credential.get("response", {}) or {}).get("transports")
    if isinstance(raw_transports, list):
        transports = ",".join(str(t) for t in raw_transports if t)[:120]
    db.add(WebAuthnCredential(
        account_id=account.id, credential_id=cred_id_b64,
        public_key=wa.bytes_to_base64url(verified.credential_public_key),
        sign_count=verified.sign_count or 0, transports=transports,
        nickname=nickname, created_at=utc_now_iso()))
    db.commit()
    out = JSONResponse(content={"ok": True, "nickname": nickname})
    out.delete_cookie(REG_COOKIE)
    return out


@router.post("/account/passkeys/{cred_id}/remove")
def passkey_remove(cred_id: int, request: Request,
                   account: Account | None = Depends(cookie_account),
                   db: Session = Depends(get_db),
                   _csrf: None = Depends(require_same_origin)):
    """Remove one of the caller's own passkeys. Scoped to the signed-in
    account, so an id belonging to someone else is a no-op, not a delete."""
    from fastapi.responses import RedirectResponse
    if not account:
        return RedirectResponse("/login", status_code=303)
    cred = (db.query(WebAuthnCredential)
            .filter_by(id=cred_id, account_id=account.id).first())
    if cred:
        db.delete(cred)
        db.commit()
    return RedirectResponse("/account?m=passkey-removed#security",
                            status_code=303)


# --- Authentication: sign in with a passkey -------------------------------

@router.post("/login/passkey/begin")
async def login_begin(request: Request, db: Session = Depends(get_db)):
    if _too_many(request):
        return _fail("Too many attempts. Wait a minute and try again.", 429)
    try:
        body = await request.json()
    except (ValueError, TypeError):
        body = {}
    email = (body.get("email") or "").strip().lower()
    rp_id, _origin = wa.rp_id_and_origin(request)
    allow = []
    if email:
        # When an email is offered, steer the browser to that account's
        # passkeys. An unknown email simply yields an empty (usernameless)
        # list, so the page never reveals which addresses have an account.
        account = db.query(Account).filter_by(email=email).first()
        if account:
            creds = (db.query(WebAuthnCredential)
                     .filter_by(account_id=account.id).all())
            allow = [wa.descriptor(c.credential_id, c.transports)
                     for c in creds]
    options_json, challenge_b64 = wa.authentication_options_json(rp_id, allow)
    token = _stash_challenge(db, "auth", challenge_b64)
    resp = JSONResponse(content={"options": options_json})
    resp.set_cookie(AUTH_COOKIE, token, max_age=_CHALLENGE_TTL_MINUTES * 60,
                    httponly=True, samesite="lax",
                    secure=settings.cookie_secure)
    return resp


@router.post("/login/passkey/finish")
async def login_finish(request: Request, db: Session = Depends(get_db)):
    if _too_many(request):
        return _fail("Too many attempts. Wait a minute and try again.", 429)
    try:
        body = await request.json()
    except (ValueError, TypeError):
        body = {}
    credential = body.get("credential")
    stash = _take_challenge(db, request, AUTH_COOKIE, "auth")
    resp_fail = _fail(_SIGNIN_FAILED, 401)
    resp_fail.delete_cookie(AUTH_COOKIE)
    if not credential or not stash:
        return resp_fail
    # The assertion names its credential id; that resolves the account, which
    # is what makes usernameless (discoverable) sign-in work.
    cred_id = credential.get("id") or credential.get("rawId") or ""
    cred = (db.query(WebAuthnCredential)
            .filter_by(credential_id=cred_id).first())
    if not cred:
        return resp_fail
    account = db.get(Account, cred.account_id)
    if not account or account.disabled:
        out = _fail(ACCOUNT_DISABLED_MESSAGE if account else _SIGNIN_FAILED,
                    403 if account else 401)
        out.delete_cookie(AUTH_COOKIE)
        return out
    rp_id, origin = wa.rp_id_and_origin(request)
    try:
        verified = wa.verify_authentication(
            credential=credential,
            expected_challenge=wa.base64url_to_bytes(stash.challenge),
            rp_id=rp_id, origin=origin,
            public_key=wa.base64url_to_bytes(cred.public_key),
            sign_count=cred.sign_count)
    except Exception:
        return resp_fail
    # Replay / clone defense: the signature counter must advance. A stored key
    # that never advances (an authenticator that keeps no counter) is the one
    # allowed exception.
    if not wa.sign_count_ok(cred.sign_count, verified.new_sign_count):
        return resp_fail
    cred.sign_count = verified.new_sign_count
    cred.last_used_at = utc_now_iso()
    db.commit()
    # Open the normal portal session, the same one a password login gets.
    token = _issue_session(db, account.id)
    out = JSONResponse(content={"ok": True, "next": "/account"})
    out.set_cookie(SESSION_COOKIE, token,
                   max_age=settings.session_ttl_hours * 3600,
                   httponly=True, samesite="lax", secure=settings.cookie_secure)
    out.delete_cookie(AUTH_COOKIE)
    return out
