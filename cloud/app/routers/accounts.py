"""Account signup, login, and the portal's own-account view."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import ratelimit, usage
from ..config import settings
from ..deps import (ACCOUNT_DISABLED_MESSAGE, client_ip, current_account,
                    get_db, utc_now_iso)
from ..models import Account, AuthSession, Instance, RecoveryCode
from ..security import (email_is_disposable, generate_recovery_codes,
                        hash_password, new_token, normalize_recovery_code,
                        password_problem, token_hash, totp_verify,
                        verify_password)

router = APIRouter(prefix="/v1/accounts", tags=["accounts"])

# What every seam tells an account that has locked itself with too many wrong
# passwords. Deliberately vague about how long, so it gives an attacker no
# timer to script against.
ACCOUNT_LOCKED_MESSAGE = "Too many failed attempts. Try again in a few minutes."
DISPOSABLE_EMAIL_MESSAGE = "Please use a non-temporary email address."


class Credentials(BaseModel):
    # A minimal shape check, not full RFC validation (pydantic's EmailStr
    # would pull in email-validator; not worth a dependency to police typos).
    email: str
    password: str
    # A code from the authenticator app (or a recovery code). Optional and
    # ignored unless the account has two-factor sign-in turned on.
    totp: str = ""


def _valid_email(email: str) -> bool:
    local, _, domain = email.partition("@")
    return bool(local) and "." in domain and " " not in email


def authenticate(db: Session, email: str, password: str,
                 now: str | None = None) -> tuple[Account | None, str | None]:
    """Resolve credentials, returning (account, error).

    Shared by portal login, JSON login, and one-step provisioning so all
    three honour the same per-account lockout. On correct credentials returns
    (account, None) and clears the failure counter. On wrong credentials
    returns (None, None); for an existing account it records the failure and,
    once the configured threshold is reached, sets locked_until. While locked,
    returns (None, ACCOUNT_LOCKED_MESSAGE) even for the right password.

    A disabled account is handed back to the caller (whose disabled branch
    runs) before any lockout applies, and lockout never touches it. ``now`` is
    an injectable ISO timestamp so the time-based unlock is unit-testable.
    """
    now = now or utc_now_iso()
    account = db.query(Account).filter_by(email=email.strip().lower()).first()
    if not account:
        # Unknown email: never tracked, so a probe cannot lock out or
        # enumerate accounts that do not exist.
        return None, None

    # Disabled short-circuits: the caller's disabled branch owns the message,
    # and a dead account has nothing to lock.
    if account.disabled:
        if verify_password(password, account.password_hash):
            return account, None
        return None, None

    if account.locked_until and account.locked_until > now:
        return None, ACCOUNT_LOCKED_MESSAGE

    if verify_password(password, account.password_hash):
        if account.failed_logins or account.locked_until:
            account.failed_logins = 0
            account.locked_until = ""
            db.commit()
        return account, None

    # A wrong password for a real account: count it, and lock once the run of
    # failures crosses the threshold.
    account.failed_logins = (account.failed_logins or 0) + 1
    if account.failed_logins >= settings.account_lockout_threshold:
        until = (datetime.fromisoformat(now)
                 + timedelta(minutes=settings.account_lockout_minutes))
        account.locked_until = until.isoformat(timespec="seconds")
    db.commit()
    return None, None


# --- Two-factor sign-in (TOTP), shared by portal, JSON login, and provision ---

def consume_totp(db: Session, account: Account, code: str) -> bool:
    """Whether ``code`` clears the account's second factor: a live TOTP code
    from the authenticator app, or one of the account's recovery codes.

    A recovery code is single-use, so it is burned here on a match. Returns
    True on success (session may be issued), False on any wrong code."""
    code = (code or "").strip()
    if not code:
        return False
    if account.totp_secret and totp_verify(account.totp_secret, code):
        return True
    # Fall back to a recovery code: normalise so it matches with or without
    # the dash, then burn it so it never works twice.
    row = (db.query(RecoveryCode)
           .filter_by(account_id=account.id, used=0,
                      code_hash=token_hash(normalize_recovery_code(code)))
           .first())
    if not row:
        return False
    row.used = 1
    db.commit()
    return True


def totp_gate(db: Session, account: Account, submitted: str | None) -> str | None:
    """The machine-readable reason a login is blocked on its second factor,
    or None when it may proceed.

    "totp_required" when 2FA is on and no code was supplied, "totp_invalid"
    when the supplied code is wrong. When 2FA is off the field is ignored."""
    if not account.totp_enabled:
        return None
    if not (submitted or "").strip():
        return "totp_required"
    return None if consume_totp(db, account, submitted) else "totp_invalid"


def replace_recovery_codes(db: Session, account_id: int) -> list[str]:
    """Delete any existing recovery codes for the account and mint a fresh
    set, storing only the hashes. Returns the plaintext to show once."""
    db.query(RecoveryCode).filter_by(account_id=account_id).delete()
    codes = generate_recovery_codes()
    now = utc_now_iso()
    for code in codes:
        db.add(RecoveryCode(account_id=account_id,
                            code_hash=token_hash(normalize_recovery_code(code)),
                            created_at=now))
    db.commit()
    return codes


def _issue_session(db: Session, account_id: int) -> str:
    token = new_token("prs")
    expires = datetime.now(timezone.utc) + timedelta(hours=settings.session_ttl_hours)
    db.add(AuthSession(token_hash=token_hash(token), account_id=account_id,
                       expires_at=expires.isoformat(timespec="seconds"),
                       created_at=utc_now_iso()))
    db.commit()
    return token


@router.post("/signup")
def signup(payload: Credentials, request: Request, db: Session = Depends(get_db)):
    client = client_ip(request)
    if not ratelimit.allow(f"signup:{client}", settings.signup_rate_per_minute):
        raise HTTPException(429, detail="Too many signup attempts, try again in a minute")
    email = payload.email.strip().lower()
    problem = password_problem(payload.password, email)
    if problem:
        raise HTTPException(400, detail=problem)
    if not _valid_email(email):
        raise HTTPException(400, detail="Enter a valid email address")
    if email_is_disposable(email):
        raise HTTPException(400, detail=DISPOSABLE_EMAIL_MESSAGE)
    if db.query(Account).filter_by(email=email).first():
        raise HTTPException(409, detail="An account with that email already exists")
    account = Account(email=email, password_hash=hash_password(payload.password),
                      created_at=utc_now_iso())
    db.add(account)
    db.commit()
    # Every new account starts its 30-day trial immediately; the expiry is
    # written now, so it lapses on its own with no cron job.
    usage.grant_trial(db, account.id, account.created_at)
    # Send a verification email when outgoing mail is configured. Local import
    # keeps the portal-to-accounts dependency one-directional.
    from .portal import send_verification
    send_verification(db, account)
    return {"session_token": _issue_session(db, account.id)}


@router.post("/login")
def login(payload: Credentials, request: Request, db: Session = Depends(get_db)):
    client = client_ip(request)
    if not ratelimit.allow(f"login:{client}", settings.login_rate_per_minute):
        raise HTTPException(429, detail="Too many login attempts, try again in a minute")
    account, locked = authenticate(db, payload.email, payload.password)
    if locked:
        raise HTTPException(429, detail=locked)
    if not account:
        # One message for both cases, so login does not confirm which emails exist.
        raise HTTPException(401, detail="Invalid email or password")
    if account.disabled:
        raise HTTPException(403, detail=ACCOUNT_DISABLED_MESSAGE)
    gate = totp_gate(db, account, payload.totp)
    if gate:
        # The password was right; the second factor is missing or wrong. A
        # distinct machine-readable body so the app knows to prompt for a code.
        return JSONResponse(status_code=401, content={"error": gate})
    return {"session_token": _issue_session(db, account.id)}


@router.get("/me")
def me(account: Account = Depends(current_account), db: Session = Depends(get_db)):
    state = usage.quota_state(db, account.id, usage.month_key())
    instances = db.query(Instance).filter_by(account_id=account.id).all()
    return {
        "email": account.email,
        "entitlement": state,
        "instances": [
            {"id": i.id, "name": i.name, "app_version": i.app_version,
             "deployment_mode": i.deployment_mode, "last_seen_at": i.last_seen_at}
            for i in instances
        ],
    }
