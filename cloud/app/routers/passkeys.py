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
two-factor sign-in, and both keep working. Adding one needs proof beyond the
session cookie (the password, a current authenticator code, or for an account
with no password a sign-in in the last few minutes), because a passkey outlives
a password change: a stolen cookie must not be able to enrol its own. The owner
is emailed whenever one is added.

A sign-in whose assertion carries user verification (the device checked a
fingerprint, face or PIN) counts as both factors and opens a session straight
away. One without it is only something the person has, so an account with
two-factor on goes on to the code step, exactly as after a password. Challenges live server-side in
webauthn_challenges, named by a short-lived HttpOnly cookie, so a finish cannot
be replayed or guessed. Only public keys are stored, never a secret.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from .. import ratelimit, webauthn_service as wa
from ..config import settings
from ..deps import (ACCOUNT_DISABLED_MESSAGE, client_ip, cookie_account,
                    delete_flow_cookie, flow_cookie, get_db, rate_ip,
                    require_same_origin, session_cookie_token,
                    set_session_cookie, utc_now_iso)
from ..email import base_url, send_email
from ..models import Account, AuthSession, WebAuthnChallenge, WebAuthnCredential
from ..security import new_token, token_hash
from .accounts import (_issue_session, authenticate, clear_totp_failures,
                       consume_totp, register_totp_failure, totp_locked)

log = logging.getLogger(__name__)

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

# How recently an account with no password must have signed in for that
# sign-in to stand in for re-entering a password when it adds a passkey.
RECENT_SIGNIN_MINUTES = 10

_STEP_UP_PASSWORD = "To add a passkey, first enter your account password."
_STEP_UP_PASSWORD_OR_CODE = ("To add a passkey, first enter your account "
                             "password or a code from your authenticator app.")
_STEP_UP_CODE = ("To add a passkey, first enter a code from your "
                 "authenticator app.")
_STEP_UP_REAUTH = ("To add a passkey, sign out and sign back in, then add it "
                   f"within {RECENT_SIGNIN_MINUTES} minutes.")


def _client(request: Request) -> str:
    return client_ip(request)


def _too_many(request: Request, account_id: int = 0) -> bool:
    """Whether this caller has spent its passkey budget for the minute. Applied
    per IP always, and per account when one is known."""
    ok = ratelimit.allow(f"passkey-ip:{rate_ip(request)}",
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
    token = request.cookies.get(flow_cookie(cookie), "")
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


async def _json_body(request: Request) -> dict:
    """The request's JSON object, or {} when the body is missing or not JSON.

    The finish and begin routes are async only so they can read the body; every
    query and commit after that runs in the threadpool, so a slow database
    never stalls the event loop for every other request."""
    try:
        body = await request.json()
    except (ValueError, TypeError):
        return {}
    return body if isinstance(body, dict) else {}


# --- Step-up before adding a passkey ---------------------------------------

def _step_up_fail(account: Account, message: str = "") -> JSONResponse:
    """Refuse a passkey add that has not proved it is the owner, saying which
    proof this account can give. ``step_up`` tells the page what to ask for:
    "password", "code", or "reauth" (sign in again)."""
    has_password = bool(account.password_hash)
    has_code = bool(account.totp_enabled)
    accepts = []
    if has_password:
        accepts.append("password")
    if has_code:
        accepts.append("code")
    if not accepts:
        accepts = ["reauth"]
    if not message:
        if has_password and has_code:
            message = _STEP_UP_PASSWORD_OR_CODE
        elif has_password:
            message = _STEP_UP_PASSWORD
        elif has_code:
            message = _STEP_UP_CODE
        else:
            message = _STEP_UP_REAUTH
    return JSONResponse(status_code=403,
                        content={"error": message, "step_up": accepts})


def _signed_in_recently(db: Session, request: Request) -> bool:
    """Whether this browser's session was opened in the last few minutes."""
    token = session_cookie_token(request, db)
    if not token:
        return False
    sess = (db.query(AuthSession)
            .filter_by(token_hash=token_hash(token)).first())
    if not sess or not sess.created_at:
        return False
    try:
        opened = datetime.fromisoformat(sess.created_at)
    except ValueError:
        return False
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - opened
    return timedelta(0) <= age <= timedelta(minutes=RECENT_SIGNIN_MINUTES)


def _step_up(db: Session, request: Request, account: Account,
             body: dict) -> JSONResponse | None:
    """None when the caller has proved it is the owner, else the refusal.

    A session cookie alone is not enough: a passkey keeps working after the
    password changes and skips the code step, so a stolen cookie that could
    add one would keep the account for good. Accepted proof, in order: the
    account password (through authenticate, so wrong guesses count toward the
    same lockout as the login page), a current authenticator or recovery code
    (with the same lockout as the code step), or, for an account with no
    password, a session opened in the last few minutes."""
    password = body.get("password") or ""
    code = body.get("code") or ""
    if not isinstance(password, str) or not isinstance(code, str):
        return _step_up_fail(account)
    code = code.strip()
    refusal = None
    if password and account.password_hash:
        ok, locked = authenticate(db, account.email, password)
        if locked:
            return _fail(locked, 429)
        if ok and ok.id == account.id:
            return None
        refusal = _step_up_fail(account, "That password did not match.")
    if code and account.totp_enabled:
        if totp_locked(account):
            return _fail("Too many attempts. Wait a few minutes and try "
                         "again.", 429)
        if consume_totp(db, account, code):
            clear_totp_failures(db, account)
            return None
        register_totp_failure(db, account)
        refusal = _step_up_fail(account, "That code did not match.")
    if refusal is not None:
        return refusal
    if not account.password_hash and _signed_in_recently(db, request):
        return None
    return _step_up_fail(account)


def _send_passkey_added(account: Account, nickname: str) -> None:
    """Tell the owner a passkey was just added, so one they did not add is
    noticed. A no-op when outgoing email is not set up; never raises."""
    text = (
        f"A passkey named \"{nickname}\" was just added to your Forager "
        "account. It can now be used to sign in.\n\n"
        "If you added it, there is nothing else to do.\n\n"
        "If you did not, remove it from the Security section of your account "
        "page right away, then change your password:\n"
        f"{base_url()}/account#security\n"
    )
    try:
        send_email(account.email, "A passkey was added to your Forager account",
                   text)
    except Exception:  # pragma: no cover - send_email already never raises
        log.warning("passkey-added email failed for account %s", account.id)


# --- Registration: a signed-in account adds a passkey ---------------------

@router.post("/account/passkeys/register/begin")
async def register_begin(request: Request,
                         account: Account | None = Depends(cookie_account),
                         db: Session = Depends(get_db),
                         _csrf: None = Depends(require_same_origin)):
    body = await _json_body(request)
    return await run_in_threadpool(_register_begin, request, account, db, body)


def _register_begin(request: Request, account: Account | None, db: Session,
                    body: dict) -> JSONResponse:
    if not account:
        return _fail("Please sign in first.", 401)
    if _too_many(request, account.id):
        return _fail("Too many attempts. Wait a minute and try again.", 429)
    refused = _step_up(db, request, account, body)
    if refused is not None:
        return refused
    rp_id, _origin = wa.rp_id_and_origin(request)
    existing = (db.query(WebAuthnCredential)
                .filter_by(account_id=account.id).all())
    exclude = [wa.descriptor(c.credential_id, c.transports) for c in existing]
    options_json, challenge_b64 = wa.registration_options_json(
        rp_id, account.id, account.email, exclude)
    token = _stash_challenge(db, "register", challenge_b64, account.id)
    resp = JSONResponse(content={"options": options_json})
    resp.set_cookie(flow_cookie(REG_COOKIE), token, max_age=_CHALLENGE_TTL_MINUTES * 60,
                    httponly=True, samesite="lax",
                    secure=settings.cookie_secure)
    return resp


@router.post("/account/passkeys/register/finish")
async def register_finish(request: Request,
                          account: Account | None = Depends(cookie_account),
                          db: Session = Depends(get_db),
                          _csrf: None = Depends(require_same_origin)):
    body = await _json_body(request)
    return await run_in_threadpool(_register_finish, request, account, db, body)


def _register_finish(request: Request, account: Account | None, db: Session,
                     body: dict) -> JSONResponse:
    if not account:
        return _fail("Please sign in first.", 401)
    if _too_many(request, account.id):
        return _fail("Too many attempts. Wait a minute and try again.", 429)
    credential = body.get("credential")
    nickname = (body.get("nickname") or "").strip()[:120] or "Passkey"
    stash = _take_challenge(db, request, REG_COOKIE, "register")
    resp_fail = _fail(_ADD_FAILED)
    delete_flow_cookie(resp_fail, REG_COOKIE)
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
        delete_flow_cookie(out, REG_COOKIE)
        return out
    if owner:
        out = JSONResponse(content={"ok": True, "nickname": owner.nickname})
        delete_flow_cookie(out, REG_COOKIE)
        return out
    transports = ""
    raw_transports = (credential.get("response", {}) or {}).get("transports")
    if isinstance(raw_transports, list):
        transports = ",".join(str(t) for t in raw_transports if t)[:120]
    db.add(WebAuthnCredential(
        account_id=account.id, credential_id=cred_id_b64,
        public_key=wa.bytes_to_base64url(verified.credential_public_key),
        sign_count=verified.sign_count or 0, transports=transports,
        nickname=nickname, created_at=utc_now_iso(),
        user_verified=1 if getattr(verified, "user_verified", False) else 0))
    db.commit()
    _send_passkey_added(account, nickname)
    out = JSONResponse(content={"ok": True, "nickname": nickname})
    delete_flow_cookie(out, REG_COOKIE)
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
    body = await _json_body(request)
    return await run_in_threadpool(_login_begin, request, db, body)


def _login_begin(request: Request, db: Session, body: dict) -> JSONResponse:
    if _too_many(request):
        return _fail("Too many attempts. Wait a minute and try again.", 429)
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
    resp.set_cookie(flow_cookie(AUTH_COOKIE), token, max_age=_CHALLENGE_TTL_MINUTES * 60,
                    httponly=True, samesite="lax",
                    secure=settings.cookie_secure)
    return resp


@router.post("/login/passkey/finish")
async def login_finish(request: Request, db: Session = Depends(get_db)):
    body = await _json_body(request)
    return await run_in_threadpool(_login_finish, request, db, body)


def _login_finish(request: Request, db: Session, body: dict) -> JSONResponse:
    if _too_many(request):
        return _fail("Too many attempts. Wait a minute and try again.", 429)
    credential = body.get("credential")
    stash = _take_challenge(db, request, AUTH_COOKIE, "auth")
    resp_fail = _fail(_SIGNIN_FAILED, 401)
    delete_flow_cookie(resp_fail, AUTH_COOKIE)
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
        delete_flow_cookie(out, AUTH_COOKIE)
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
    if account.totp_enabled and not getattr(verified, "user_verified", False):
        # The device did not check who was holding it, so this proves only
        # that someone has the key. Treat it like a correct password: the
        # account's code step still stands between it and a session.
        from .portal import mint_totp_challenge, set_totp_pending_cookie
        pending = mint_totp_challenge(db, account.id)
        out = JSONResponse(content={"ok": True, "next": "/login/2fa"})
        set_totp_pending_cookie(out, pending)
        delete_flow_cookie(out, AUTH_COOKIE)
        return out
    # Open the normal portal session, the same one a password login gets.
    token = _issue_session(db, account.id)
    out = JSONResponse(content={"ok": True, "next": "/account"})
    set_session_cookie(out, token)
    delete_flow_cookie(out, AUTH_COOKIE)
    return out
