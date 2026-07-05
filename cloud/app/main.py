"""Forager: Pantry Raider's hosted subscription service.

A separate FastAPI app from the self-hosted Pantry Raider in service/; the
two share nothing at import time. Design: docs/design/cloud-platform.md.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import CLOUD_VERSION
from .database import init_db
from .routers import (accounts, admin, ai, instances, oauth_google, portal,
                      stripe_webhook)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # create_all for now; becomes `alembic upgrade head` in the entrypoint
    # once migrations exist (see the design doc's migration section).
    init_db()
    yield


app = FastAPI(title="Forager", version=CLOUD_VERSION, lifespan=lifespan)

# The one static asset the portal serves: the Pantry Raider raccoon mark in
# the header. Kept as a file (not inlined) so the 9 KB base64 does not weigh
# down every page; still fully self-contained, no CDN.
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")),
          name="static")

app.include_router(accounts.router)
app.include_router(instances.router)
app.include_router(ai.router)
app.include_router(stripe_webhook.router)
app.include_router(portal.router)
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
