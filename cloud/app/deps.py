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
#
# Over TLS it carries the __Host- prefix. Kitchen subdomains share the Forager
# domain, so a page on one could set a cookie for the parent domain under the
# plain name and have the browser send it to the portal ahead of, or instead
# of, the real one (cookie tossing). Browsers only accept a __Host- cookie
# that is Secure, host-only and Path=/, so no subdomain can plant or shadow
# it. Plain HTTP (tests, local dev) cannot use the prefix and keeps the plain
# name.
SESSION_COOKIE = "forager_session"
SECURE_SESSION_COOKIE = "__Host-forager_session"
# Sessions minted before the cookie moved to the __Host- name still arrive
# under the plain name. One of those is honored (and reissued under the new
# name) only if the session was created before this moment: every session
# made since is issued under __Host-, so a plain-name cookie carrying a newer
# session can only have been planted. Old sessions age out within
# session_ttl_hours, after which the plain name is never honored at all.
LEGACY_SESSION_COOKIE_CUTOFF = "2026-10-14T00:00:00+00:00"


def session_cookie_name() -> str:
    """The name the portal session cookie is written under."""
    return SECURE_SESSION_COOKIE if settings.cookie_secure else SESSION_COOKIE


def set_session_cookie(resp, token: str) -> None:
    """Carry a portal session in the browser, dropping any plain-name copy."""
    resp.set_cookie(session_cookie_name(), token,
                    max_age=settings.session_ttl_hours * 3600,
                    httponly=True, samesite="lax",
                    secure=settings.cookie_secure, path="/")
    if settings.cookie_secure:
        resp.delete_cookie(SESSION_COOKIE, path="/")


def clear_session_cookie(resp) -> None:
    """Sign the browser out: expire the session cookie under both names."""
    resp.delete_cookie(SESSION_COOKIE, path="/")
    if settings.cookie_secure:
        resp.delete_cookie(SECURE_SESSION_COOKIE, path="/", secure=True,
                           httponly=True, samesite="lax")


def _legacy_session_ok(db: Session, token: str) -> bool:
    sess = db.query(AuthSession).filter_by(token_hash=token_hash(token)).first()
    return bool(sess and sess.created_at
                and sess.created_at < LEGACY_SESSION_COOKIE_CUTOFF)


def session_cookie_token(request: Request, db: Session | None = None) -> str:
    """The portal session token this browser presented, or "".

    Over TLS only the __Host- cookie counts, apart from a migrating session
    under the plain name (see LEGACY_SESSION_COOKIE_CUTOFF), which is marked
    on request.state so the response reissues it under the new name."""
    if not settings.cookie_secure:
        return request.cookies.get(SESSION_COOKIE, "")
    token = request.cookies.get(SECURE_SESSION_COOKIE, "")
    if token:
        return token
    legacy = request.cookies.get(SESSION_COOKIE, "")
    if not legacy or db is None:
        return ""
    if not _legacy_session_ok(db, legacy):
        return ""
    request.state.reissue_session_cookie = legacy
    return legacy


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
    """Whether this account may use the admin panel.

    Three things must hold (FoodAssistant-fa6): the email is on the
    CLOUD_ADMIN_EMAILS allowlist, the address is confirmed, and two-factor
    sign-in is on. The allowlist alone named an address, not a person: anyone
    could sign up with an admin's address before the admin did. With two-factor
    required, every way into the account (password, Google) also needs the
    authenticator code; a passkey alone is not enough, because the password
    would still open the account without it.

    Parsed per call so tests (and a restarted env) take effect immediately;
    an empty setting means nobody is an admin."""
    if not account:
        return False
    allowed = {e.strip().lower() for e in settings.admin_emails.split(",")
               if e.strip()}
    if account.email not in allowed:
        return False
    return bool(account.email_verified) and bool(account.totp_enabled)


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
    return account_for_session(db, session_cookie_token(request, db))


def instance_even_if_disabled(request: Request,
                              db: Session = Depends(get_db)) -> Instance:
    """Resolve an instance token to its paired install and touch last-seen.

    The last-seen update rides the authenticated request itself, the same
    heartbeat-on-pull pattern the app's satellite registry uses.

    This variant does NOT check whether the owning account is disabled, so it
    is only for the three routes that must still answer a disabled account:
    the status read its settings page shows (GET /v1/instance/me), self-unlink
    (DELETE /v1/instance), and the Google unlock check
    (POST /v1/instance/verify-unlock), which burns the code first and then
    answers a disabled account exactly like an unknown code, so a prober
    learns nothing. Everything else wants ``current_instance``;
    tests/test_instance_disabled_gate.py pins the list.
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
    and the three deliberate exemptions name ``instance_even_if_disabled``
    where anyone reading them can see the choice.
    """
    owner = db.get(Account, inst.account_id)
    if owner and owner.disabled:
        # The structured shape the AI proxy has always returned. Keep it: app
        # releases that know about disabled accounts read detail["error"] to
        # tell "disabled" apart from a plan or quota refusal and show
        # detail["message"]. Older releases never look for it: a refused scan
        # shows the whole body as a generic Forager error, and cloud backup
        # takes any 403 for a plan without the feature.
        raise HTTPException(403, detail={
            "error": "account_disabled",
            "message": ACCOUNT_DISABLED_MESSAGE,
        })
    return inst
