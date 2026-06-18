import secrets

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from contextlib import asynccontextmanager

from .config import settings, APP_VERSION
from .database import engine, get_db, Base
from .ingress import ingress_redirect
from .models import db_models  # noqa: F401 — registers models with Base
from .services.defaults import seed_defaults
from .routers import analyze, defaults, inventory, expiring, ui, setup, pending, mealie, admin, qr, tunnel


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    db = next(get_db())
    try:
        seed_defaults(db)
    finally:
        db.close()
    yield


app = FastAPI(
    title="FoodAssistant",
    description="Food spoilage tracker with LLM-powered photo import and Grocy integration",
    version=APP_VERSION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Paths that bypass both setup-redirect and auth checks
_SETUP_BYPASS = {
    "/setup", "/setup/save", "/setup/theme", "/setup/scale", "/setup/storage-categories",
    "/setup/test/grocy", "/setup/test/vision",
    "/setup/test/provider", "/setup/test/mealie", "/setup/test/recipes",
    "/setup/totp/generate", "/setup/totp/verify", "/setup/totp/disable",
    "/health", "/docs", "/openapi.json", "/redoc",
}
# "/" only redirects to /ui/, so it can safely skip auth (the target enforces it)
_PUBLIC_PATHS = _SETUP_BYPASS | {"/ui/login", "/"}


def _is_static(path: str) -> bool:
    return path.startswith("/static/")


@app.middleware("http")
async def redirect_if_unconfigured(request: Request, call_next):
    """Send new installs to /setup until Grocy + vision provider are configured."""
    if (not settings.is_configured() and request.url.path not in _SETUP_BYPASS
            and not _is_static(request.url.path)):
        return ingress_redirect(request, "/setup")
    return await call_next(request)


@app.middleware("http")
async def require_auth(request: Request, call_next):
    """Auth is enabled when AUTH_PASSWORD is set. Browsers authenticate via the
    /ui/login session cookie; headless clients (HA, ESPHome) via X-API-Key."""
    if not settings.auth_password:
        return await call_next(request)
    # Static assets (PWA manifest, icons) are public: the OS fetches install
    # icons without session cookies.
    if request.url.path in _PUBLIC_PATHS or _is_static(request.url.path):
        return await call_next(request)

    # Requests from the loopback address are always trusted (local kiosk, cron jobs).
    if request.client and request.client.host in ("127.0.0.1", "::1"):
        return await call_next(request)

    # totp_pending means password was accepted but TOTP not yet verified — not authed
    session_ok = request.session.get("authed", False) and not request.session.get("totp_pending")
    key_ok = bool(settings.api_key) and secrets.compare_digest(
        request.headers.get("X-API-Key", ""), settings.api_key)
    if session_ok or key_ok:
        return await call_next(request)

    if request.url.path.startswith("/ui"):
        return ingress_redirect(request, "/ui/login")
    return JSONResponse({"detail": "Unauthorized"}, status_code=401)


# SessionMiddleware runs after middlewares above so request.session is available
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, max_age=60 * 60 * 24 * 30)

from pathlib import Path
from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

app.include_router(setup.router)
app.include_router(admin.router)
app.include_router(pending.router)
app.include_router(mealie.router)
app.include_router(analyze.router)
app.include_router(defaults.router)
app.include_router(inventory.router)
app.include_router(expiring.router)
app.include_router(tunnel.router)
app.include_router(ui.router)
app.include_router(qr.router)


@app.get("/")
async def root():
    return RedirectResponse("/ui/", status_code=303)


@app.get("/health")
async def health():
    if not settings.is_configured():
        return {"status": "unconfigured", "setup": "/setup"}
    from .dependencies import get_vision_provider
    from .services.grocy import GrocyClient
    provider = get_vision_provider()
    grocy = GrocyClient()
    if settings.ai_configured():
        vision_status = "ok" if await provider.health_check() else "error"
    else:
        vision_status = "not configured"
    return {
        "status": "ok",
        "vision_provider": vision_status,
        "grocy": "ok" if await grocy.health_check() else "error",
    }
