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
                        password_problem, token_hash, totp_matched_step,
                        totp_verify, verify_password)

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
    # The app's opaque per-install id (device_id), sent on signup so the free
    # trial can be limited to one per install. Not personal data: it names a
    # copy of the software, not a person. Read by signup only; login ignores it.
    install_key: str = ""


def _valid_email(email: str) -> bool:
    local, _, domain = email.partition("@")
    return bool(local) and "." in domain and " " not in email


def authenticate(db: Session, email: str, password: str,
                 now: str | None = None) -> tuple[Account | None, str | None]:
    """Resolve credentials, returning (account, error).

    Shared by portal login, JSON login, and one-step provisioning so all three
    throttle password guessing the same way. On correct credentials returns
    (account, None) and clears the failure counter. On wrong credentials for an
    existing account it records the failure and, once the configured threshold
    is reached, sets locked_until; while locked, a further wrong guess is
    refused with (None, ACCOUNT_LOCKED_MESSAGE).

    The lock throttles WRONG guesses only: the correct password still succeeds
    while locked and clears the lock (FoodAssistant-gszf). These endpoints are
    unauthenticated and take an email, so a hard lock that also refused the
    right password would let anyone who knows a member's address deny them
    access by feeding wrong passwords, an easy pre-auth denial of service. A
    real online brute force is still capped: every wrong guess inside the
    window is rejected, and the second-factor lockout (totp_gate) is a separate
    counter a correct password cannot clear, so 2FA guessing is not weakened.

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

    if verify_password(password, account.password_hash):
        # The real owner: let them in even while a wrong-guess flood has the
        # account in a locked window, and clear the counter behind them.
        if account.failed_logins or account.locked_until:
            account.failed_logins = 0
            account.locked_until = ""
            db.commit()
        return account, None

    # A wrong password. If a lock is already running, refuse without counting
    # further: the window is throttling guesses, and re-arming it on every
    # attempt would only help an attacker keep it hot.
    if account.locked_until and account.locked_until > now:
        return None, ACCOUNT_LOCKED_MESSAGE

    # Count this wrong guess, and lock once the run crosses the threshold.
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

    An authenticator code is single-use here too: totp_verify accepts a +/- one
    step window, so three codes are live at once and a captured one would
    otherwise replay for the full ~90 seconds. We record the newest time-step
    accepted (totp_last_step) and refuse any code whose step is not newer, so a
    replayed code fails even while it is still inside the window. A recovery
    code is single-use as well, burned here on a match. Returns True on success
    (a session may be issued), False on any wrong or replayed code."""
    code = (code or "").strip()
    if not code:
        return False
    if account.totp_secret:
        step = totp_matched_step(account.totp_secret, code)
        if step is not None:
            if account.totp_last_step and step <= account.totp_last_step:
                # A replay: this code's step is not newer than the last one the
                # account already used, so it has been spent.
                return False
            account.totp_last_step = step
            db.commit()
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


def totp_locked(account: Account, now: str | None = None) -> bool:
    """Whether the account's second factor is in a lockout window from too many
    wrong codes. Separate from the password lock so it can never be reached by
    a pre-auth attacker (a correct password is required to even try a code)."""
    now = now or utc_now_iso()
    return bool(account.totp_locked_until) and account.totp_locked_until > now


def register_totp_failure(db: Session, account: Account,
                          now: str | None = None) -> None:
    """Count a wrong second-factor code toward the per-account TOTP lockout,
    arming the window once the run crosses the threshold. Shared by every
    sign-in path (JSON login, provisioning, verify-login, the portal 2FA page)
    so second-factor guessing is capped everywhere, not just per IP."""
    now = now or utc_now_iso()
    account.totp_failures = (account.totp_failures or 0) + 1
    if account.totp_failures >= settings.account_lockout_threshold:
        until = (datetime.fromisoformat(now)
                 + timedelta(minutes=settings.account_lockout_minutes))
        account.totp_locked_until = until.isoformat(timespec="seconds")
    db.commit()


def clear_totp_failures(db: Session, account: Account) -> None:
    """A correct second-factor code clears the run of failures and any lock."""
    if account.totp_failures or account.totp_locked_until:
        account.totp_failures = 0
        account.totp_locked_until = ""
        db.commit()


def totp_gate(db: Session, account: Account, submitted: str | None,
              now: str | None = None) -> str | None:
    """The machine-readable reason a login is blocked on its second factor,
    or None when it may proceed.

    "totp_required" when 2FA is on and no code was supplied, "totp_invalid"
    when the supplied code is wrong, spent, or the account's second factor is
    in a lockout window (too many wrong codes). When 2FA is off the field is
    ignored. A wrong code counts toward that per-account lockout, and a correct
    one clears it, so brute-forcing the six-digit code hits a hard cap on every
    path that calls this, not merely a per-IP rate limit."""
    if not account.totp_enabled:
        return None
    now = now or utc_now_iso()
    if totp_locked(account, now):
        # Refuse without checking the code, so the lock cannot be used as an
        # oracle to keep testing codes.
        return "totp_invalid"
    if not (submitted or "").strip():
        return "totp_required"
    if consume_totp(db, account, submitted):
        clear_totp_failures(db, account)
        return None
    register_totp_failure(db, account, now)
    return "totp_invalid"


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
    # written now, so it lapses on its own with no cron job. When the app sends
    # its install key, the trial is limited to one per install, so a second
    # account from the same install starts without one.
    trial = usage.grant_trial(db, account.id, account.created_at,
                              install_key=payload.install_key)
    # Send a verification email when outgoing mail is configured. Local import
    # keeps the portal-to-accounts dependency one-directional.
    from .portal import send_verification
    send_verification(db, account)
    return {"session_token": _issue_session(db, account.id),
            "trial_granted": trial["granted"],
            # Empty unless the trial was refused; then a customer-ready reason.
            "trial_message": trial["reason"]}


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
