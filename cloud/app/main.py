"""Forager: Pantry Raider's hosted subscription service.

A separate FastAPI app from the self-hosted Pantry Raider in service/; the
two share nothing at import time. Design: docs/design/cloud-platform.md.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import retention
from .config import CLOUD_VERSION, settings
from .database import SessionLocal, init_db
from .deps import set_session_cookie
from .routers import (accounts, admin, ai, backup, billing, instances, learn,
                      oauth_google, passkeys, portal, recipe_upload, recipes,
                      shares, stripe_webhook, tunnel)

logger = logging.getLogger("forager")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Brings the schema up safely: an empty database is created and stamped, a
    # database already under Alembic is upgraded, and an existing pre-Alembic
    # database gets only the additive create_all it always did (see
    # database.init_db and migrations/README.md).
    init_db()
    # Fail closed on two-factor encryption: if accounts are already enrolled but
    # CLOUD_TOTP_SECRET_KEY is unset (or malformed), refuse to start rather than
    # run with authenticator seeds we cannot read or, worse, write in the clear.
    from .totp_crypto import ensure_totp_key_available
    ensure_totp_key_available()
    # The daily retention sweep (expired tokens, sessions, old share
    # reports); cancelled cleanly at shutdown.
    sweep_task = retention.start()
    # The tunnel sweep: takes down tunnels of accounts that lost remote access
    # and retries VPS removals the agent missed.
    tunnel_task = retention.start_tunnel_sweep()
    yield
    for task in (sweep_task, tunnel_task):
        if task:
            task.cancel()


# No interactive API docs or schema on the public service: the only API
# clients are Pantry Raider installs, which do not need them, and a published
# map of every route only helps someone probing it.
app = FastAPI(title="Forager", version=CLOUD_VERSION, lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

# The one Content-Security-Policy every response carries. Written against
# what the templates actually do: every stylesheet and script is inline
# (base.html inlines the CSS, the account page inlines its JS, and several
# templates use on* handlers), so both need 'unsafe-inline'; the Turnstile
# human-check loads its script from challenges.cloudflare.com and renders in
# an iframe from the same host; the share page shows a recipe photo from
# wherever the sharer hosts it, so images allow any http(s) origin. Passkeys
# only fetch same-origin endpoints, covered by connect-src 'self'.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://challenges.cloudflare.com; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https: http:; "
    "frame-src https://challenges.cloudflare.com; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


# Room above the largest real upload for multipart framing (boundaries, part
# headers, and the other form fields that ride along with the file).
_BODY_HEADROOM_BYTES = 2_000_000

# The message an over-sized request gets. Written for the person who sent it,
# since the portal upload form reaches this path too.
TOO_LARGE_MESSAGE = "That upload is too large."


def max_request_bytes() -> int:
    """The ceiling on any single request body.

    Taken from the largest upload the service actually accepts (a kitchen's
    backup zip, then a shared recipe or photo) so every legitimate request
    still fits, and read from settings each time so raising a cap raises this
    with it."""
    largest = max(settings.backup_max_bytes,
                  settings.recipe_upload_max_bytes,
                  settings.recipe_image_max_bytes)
    return largest + _BODY_HEADROOM_BYTES


# Registered before security_headers so that one stays outermost and stamps its
# headers on the refusal too.
@app.middleware("http")
async def limit_request_body(request: Request, call_next):
    """Refuse an over-sized request from its declared length, before a byte of
    the body is read.

    The reverse proxy in front turns away huge bodies too (see cloud/Caddyfile),
    but a limit that lives only in the proxy disappears the moment the proxy is
    misconfigured, replaced, or bypassed, and the per-endpoint caps cannot cover
    this: multipart form fields are parsed while an endpoint's dependencies
    resolve, which is before any of its own checks run."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_request_bytes():
        return JSONResponse({"detail": TOO_LARGE_MESSAGE}, status_code=413)
    return await call_next(request)


@app.middleware("http")
async def migrate_session_cookie(request: Request, call_next):
    """Move a portal session still carried under the plain cookie name over to
    the __Host- name. deps.session_cookie_token marks the request when it
    honored a migrating session; the response then reissues it, unless the
    route already wrote or cleared the session cookie itself (a login or a
    sign-out)."""
    response = await call_next(request)
    token = getattr(request.state, "reissue_session_cookie", "")
    if token:
        written = any("forager_session=" in v
                      for v in response.headers.getlist("set-cookie"))
        if not written:
            set_session_cookie(response, token)
    return response


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Baseline browser hardening on every response: no MIME sniffing, no
    framing (clickjacking), a conservative referrer, and the CSP above. Set
    with setdefault so a route that ever needs a different policy can say so
    itself without fighting the middleware."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy",
                                "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Content-Security-Policy", _CSP)
    # Passkeys need the browser's WebAuthn API, which this does not touch;
    # nothing on the portal uses a camera, microphone, location or payment
    # API, so pages (and anything framed into them) are refused all of them.
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
        "interest-cohort=()")
    return response


# The one static asset the portal serves: the Pantry Raider raccoon mark in
# the header. Kept as a file (not inlined) so the 9 KB base64 does not weigh
# down every page; still fully self-contained, no CDN.
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")),
          name="static")

app.include_router(accounts.router)
app.include_router(instances.router)
# shares before recipes: /v1/recipes/shares must win over the community
# router's /v1/recipes/{recipe_id}, whose int path parameter would otherwise
# swallow "shares" and answer 422.
app.include_router(shares.router)
app.include_router(recipes.router)
app.include_router(tunnel.router)
app.include_router(learn.router)
app.include_router(ai.router)
app.include_router(backup.router)
app.include_router(stripe_webhook.router)
app.include_router(portal.router)
app.include_router(billing.router)
app.include_router(passkeys.router)
app.include_router(recipe_upload.router)
app.include_router(oauth_google.router)
app.include_router(admin.router)


# Paths whose 404 stays a JSON body: the API the app talks to, and files.
_JSON_404_PREFIXES = ("/v1/", "/api/", "/static/", "/auth/", "/health")


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    """A page a person asked for that does not exist gets the portal's own
    "Nothing here" page instead of a bare JSON body. The API keeps its JSON
    answers, and every other error is passed through unchanged."""
    if (exc.status_code == 404
            and not request.url.path.startswith(_JSON_404_PREFIXES)
            and "text/html" in request.headers.get("accept", "")):
        from .routers.portal import templates
        return templates.TemplateResponse(request, "not_found.html", {},
                                          status_code=404)
    return await http_exception_handler(request, exc)


_ROBOTS = """User-agent: *
Disallow: /account
Disallow: /admin
Disallow: /r/
Disallow: /reset
Disallow: /verify
Disallow: /login/
Disallow: /auth/
Disallow: /v1/
Disallow: /api/
"""


@app.get("/robots.txt", include_in_schema=False)
def robots():
    """Search engines may list the public pages. Account pages, shared-recipe
    links (unlisted by design) and the API stay out of search results."""
    return PlainTextResponse(_ROBOTS)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return RedirectResponse("/static/mark.svg", status_code=301)


@app.get("/health")
def health():
    """Liveness for the compose healthcheck and outside monitoring. Runs a
    trivial query so a lost database shows up here as a 503, not as a
    healthy app that fails every real request."""
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - any database failure means unhealthy
        logger.exception("Health check could not reach the database")
        return JSONResponse({"status": "error", "app": "pantryraider-cloud",
                             "version": CLOUD_VERSION, "database": "down"},
                            status_code=503)
    finally:
        db.close()
    return {"status": "ok", "app": "pantryraider-cloud", "version": CLOUD_VERSION}


@app.get("/v1/meta")
def meta():
    """Capability discovery for the app: which optional sign-in paths this
    deployment offers, so the app only shows buttons that will work.

    google_unlock is its own flag even though it currently tracks
    oauth_google: the device login page gates its "Sign in with Google"
    button on it, so an app release that ships before this deployment simply
    keeps the button hidden instead of starting a flow that cannot finish."""
    return {"oauth_google": oauth_google.enabled(),
            "google_unlock": oauth_google.enabled()}
