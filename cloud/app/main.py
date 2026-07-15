"""Forager: Pantry Raider's hosted subscription service.

A separate FastAPI app from the self-hosted Pantry Raider in service/; the
two share nothing at import time. Design: docs/design/cloud-platform.md.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from .config import CLOUD_VERSION
from .database import init_db
from .routers import (accounts, admin, ai, backup, instances, learn,
                      oauth_google, passkeys, portal, recipe_upload, recipes,
                      shares, stripe_webhook, tunnel)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Brings the schema up safely: an empty database is created and stamped, a
    # database already under Alembic is upgraded, and an existing pre-Alembic
    # database gets only the additive create_all it always did (see
    # database.init_db and migrations/README.md).
    init_db()
    yield


app = FastAPI(title="Forager", version=CLOUD_VERSION, lifespan=lifespan)

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
app.include_router(passkeys.router)
app.include_router(recipe_upload.router)
app.include_router(oauth_google.router)
app.include_router(admin.router)


@app.get("/health")
def health():
    return {"status": "ok", "app": "pantryraider-cloud", "version": CLOUD_VERSION}


@app.get("/v1/meta")
def meta():
    """Capability discovery for the app: which optional sign-in paths this
    deployment offers, so the app only shows buttons that will work."""
    return {"oauth_google": oauth_google.enabled()}
