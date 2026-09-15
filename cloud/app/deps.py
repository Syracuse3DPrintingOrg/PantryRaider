"""FastAPI dependencies: database session and token authentication."""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit

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


def _request_host(request: Request) -> str:
    """The host:port this request was served on, from the forwarded host Caddy
    sets, falling back to the Host header. This is the origin a same-site form
    post must match."""
    return (request.headers.get("x-forwarded-host")
            or request.headers.get("host") or "")


def require_same_origin(request: Request) -> None:
    """CSRF guard for cookie-authenticated, state-changing portal and admin
    POSTs. The portal session cookie is SameSite=Lax, which blocks cross-SITE
    posts, but every kitchen subdomain shares the pantryraider.app eTLD+1 and is
    therefore SAME-SITE: a member (or an admin) who opens an attacker's kitchen
    page could have it auto-post to the portal with the Lax cookie attached, to
    cancel their plan, remove a kitchen or passkey, or (for an admin) disable
    accounts and delete community recipes (FoodAssistant-cuvh).

    A browser sends an Origin (and usually a Referer) on any cross-origin form
    POST, and a kitchen subdomain is a different ORIGIN from the portal even
    though it is the same site. So when either header names a host other than
    the one this request was served on, the request is refused. A request with
    neither header is a same-origin form post or a non-browser client (the app,
    the tests), and is allowed: browsers do not omit Origin on a cross-origin
    POST, so the attack always carries the signal this rejects on.

    A header that IS present must name a host we can compare. `Origin: null`
    is the case that matters: a sandboxed iframe (sandbox="allow-forms
    allow-scripts") sends it instead of its real origin, and it parses to no
    host at all. Treating "present but unparseable" as a pass would let an
    attacker's kitchen page opt out of this check by framing its own form,
    with the Lax cookie still attached because the kitchen is same-site. So
    only an ABSENT header is allowed through; a present one has to match.
    """
    served_host = _request_host(request)
    blocked = HTTPException(403, detail="Request blocked for your security. "
                                        "Reload the page and try again.")
    for header in ("origin", "referer"):
        value = request.headers.get(header)
        if not value:
            continue
        host = urlsplit(value).netloc.lower()
        if not host:
            # Present but opaque ("null", or anything without a host).
            raise blocked
        if served_host and host != served_host.lower():
            raise blocked
        # An Origin present and matching is enough; no need to also weigh the
        # Referer once the Origin has cleared.
        if header == "origin":
            return


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


def instance_even_if_disabled(request: Request,
                              db: Session = Depends(get_db)) -> Instance:
    """Resolve an instance token to its paired install and touch last-seen.

    The last-seen update rides the authenticated request itself, the same
    heartbeat-on-pull pattern the app's satellite registry uses.

    This variant does NOT check whether the owning account is disabled, so it
    is only for the two routes that must still answer a disabled account: the
    status read its settings page shows, and self-unlink. Everything else
    wants ``current_instance``.
    """
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


def current_instance(inst: Instance = Depends(instance_even_if_disabled),
                     db: Session = Depends(get_db)) -> Instance:
    """A paired install whose owning account is in good standing.

    Disabling an account already kills its portal sessions (see
    ``account_for_session``) and tears down its tunnels, but instance tokens
    are a separate credential and each router used to have to remember this
    check for itself. The AI proxy and provisioning did; cloud backup and the
    tunnel routes did not, so a disabled account's kitchen could still list,
    download and upload its stored backups. The gate lives here now so a new
    instance-authenticated route inherits it instead of having to remember,
    and the two deliberate exemptions name ``instance_even_if_disabled``
    where anyone reading them can see the choice.
    """
    owner = db.get(Account, inst.account_id)
    if owner and owner.disabled:
        # The structured shape the AI proxy has always returned, kept because
        # the app parses detail["error"] to tell "disabled" apart from a plan
        # or quota refusal and show the right thing.
        raise HTTPException(403, detail={
            "error": "account_disabled",
            "message": ACCOUNT_DISABLED_MESSAGE,
        })
    return inst
