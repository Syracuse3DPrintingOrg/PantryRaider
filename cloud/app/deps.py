"""FastAPI dependencies: database session and token authentication."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .config import settings
from .database import SessionLocal
from .models import Account, AuthSession, Instance
from .security import token_hash

# The one message every seam gives a disabled account: login, provisioning,
# and the AI proxy all refuse with it, so the owner knows what happened
# instead of guessing at a generic auth failure.
ACCOUNT_DISABLED_MESSAGE = ("This account has been disabled. "
                            "Contact support to restore access.")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def client_ip(request: Request) -> str:
    """The real client IP for rate limiting, behind exactly one proxy (Caddy).

    The app is only reachable through Caddy on the internal Docker network,
    so Caddy is the direct peer and appends the true client to
    X-Forwarded-For. The trustworthy entry is therefore the LAST one Caddy
    wrote, not the leftmost (which the client can spoof to dodge the per-IP
    limiter). Falls back to the direct peer when no header is present, so it
    still works when the app is hit directly in tests or local runs.
    """
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else "unknown"


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# The portal login cookie. Holds the same session token the JSON login
# endpoint returns as a bearer; only the transport differs.
SESSION_COOKIE = "forager_session"


def _bearer(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, detail="Missing bearer token")
    return auth[len("Bearer "):].strip()


def account_for_session(db: Session, token: str) -> Account | None:
    """The account behind a session token, or None if unknown or expired."""
    if not token:
        return None
    sess = db.query(AuthSession).filter_by(token_hash=token_hash(token)).first()
    if not sess or sess.expires_at < utc_now_iso():
        return None
    account = db.get(Account, sess.account_id)
    # Disabling an account kills its existing sessions too, not just new
    # logins: the next page load or API call behaves like a logout.
    if account and account.disabled:
        return None
    return account


def is_admin(account: Account | None) -> bool:
    """Whether this account's email is on the CLOUD_ADMIN_EMAILS allowlist.

    Parsed per call so tests (and a restarted env) take effect immediately;
    an empty setting means nobody is an admin."""
    if not account:
        return False
    allowed = {e.strip().lower() for e in settings.admin_emails.split(",")
               if e.strip()}
    return account.email in allowed


def current_account(request: Request, db: Session = Depends(get_db)) -> Account:
    """Resolve a portal session token to its account, enforcing expiry."""
    account = account_for_session(db, _bearer(request))
    if not account:
        raise HTTPException(401, detail="Invalid or expired session")
    return account


def cookie_account(request: Request,
                   db: Session = Depends(get_db)) -> Account | None:
    """The web portal's session: same tokens as the bearer flow, carried in
    an HttpOnly cookie so a browser can hold one. Returns None rather than
    raising, so page routes can redirect to the login page instead of
    showing a bare 401."""
    return account_for_session(db, request.cookies.get(SESSION_COOKIE, ""))


def current_instance(request: Request, db: Session = Depends(get_db)) -> Instance:
    """Resolve an instance token to its paired install and touch last-seen.

    The last-seen update rides the authenticated request itself, the same
    heartbeat-on-pull pattern the app's satellite registry uses."""
    token = _bearer(request)
    inst = db.query(Instance).filter_by(token_hash=token_hash(token)).first()
    if not inst:
        raise HTTPException(401, detail="Invalid instance token")
    inst.last_seen_at = utc_now_iso()
    if ver := request.headers.get("X-Device-Version", ""):
        inst.app_version = ver[:40]
    if mode := request.headers.get("X-Device-Mode", ""):
        inst.deployment_mode = mode[:40]
    db.commit()
    return inst
