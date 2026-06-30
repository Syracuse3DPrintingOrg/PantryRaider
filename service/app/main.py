import asyncio
import secrets

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from contextlib import asynccontextmanager

from .config import settings, APP_VERSION
from .database import engine, get_db, Base
from .ingress import ingress_redirect
from .models import db_models  # noqa: F401 - registers models with Base
from .services.defaults import seed_defaults
from .routers import analyze, defaults, inventory, expiring, ui, setup, pending, mealie, admin, qr, tunnel, grocy, satellite, proxy, devices, current_recipe, events, action_items, nutrition, audit


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Bring up file logging early so startup itself is captured when debug
    # logging is enabled (FoodAssistant-asra). Best-effort: a read-only data dir
    # just leaves console logging in place.
    try:
        from .services.diagnostics import configure_file_logging
        configure_file_logging(settings.data_dir, settings.debug_logging)
    except Exception:
        pass
    Base.metadata.create_all(bind=engine)
    db = next(get_db())
    try:
        seed_defaults(db)
    finally:
        db.close()
    # Sync the kiosk display idle timeout to the host bridge on boot so a bridge
    # restart picks up the current value (FoodAssistant-otiy). Best-effort.
    try:
        from .routers.setup import _push_display_idle
        await _push_display_idle()
    except Exception:
        pass
    # A satellite mirrors its main server: pull backend config + defaults on
    # boot. Best-effort so an unreachable server never blocks startup.
    if settings.is_satellite():
        try:
            from .services.satellite import sync_from_upstream
            sync_from_upstream()
        except Exception:
            pass
        # Keep mirroring while the server-side config drifts.
        app.state.sync_task = asyncio.create_task(_periodic_satellite_sync())
    # Pi appliances keep themselves current when the global auto_update flag is
    # on (FoodAssistant-k2kk). A non-Pi server uses Watchtower instead.
    if settings.is_pi_appliance():
        app.state.auto_update_task = asyncio.create_task(_periodic_auto_update())
    yield
    for attr in ("sync_task", "auto_update_task"):
        task = getattr(app.state, attr, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


async def _periodic_satellite_sync():
    """Re-pull backend config from the main server every
    settings.satellite_sync_minutes (0 disables). Best-effort and cancellable."""
    from fastapi.concurrency import run_in_threadpool
    from .services.satellite import sync_from_upstream
    while True:
        minutes = settings.satellite_sync_minutes
        if not minutes or minutes <= 0:
            await asyncio.sleep(300)   # re-check the toggle later
            continue
        await asyncio.sleep(minutes * 60)
        try:
            await run_in_threadpool(sync_from_upstream)
        except Exception:
            pass


async def _periodic_auto_update():
    """Apply updates on a Pi appliance while the global auto_update flag is on.

    Re-checks every few hours. A satellite only updates when its server is on a
    different version (so the fleet converges on the server's version); a Pi
    Hosted box attempts on each pass (the OTA no-ops when already current). The
    bridge restarts the app as part of an update, which cancels this task with
    the old process; the new process schedules it again, so a single extra pass
    after a restart is harmless because the OTA is idempotent.
    """
    from .services import auto_update as au
    from .services.satellite import last_server_version
    from .routers.setup import run_host_bridge_update
    # Settle after boot so a freshly provisioned device is not updated mid-setup.
    await asyncio.sleep(600)
    while True:
        try:
            if settings.auto_update and settings.is_pi_appliance():
                if au.should_run(settings.is_satellite(), APP_VERSION, last_server_version()):
                    result = await run_host_bridge_update()
                    if result.get("ok"):
                        import logging
                        logging.getLogger("foodassistant.autoupdate").info(
                            "Auto-update ran: before=%s after=%s restarted=%s",
                            result.get("before"), result.get("after"), result.get("restarted"))
        except Exception:
            pass
        await asyncio.sleep(6 * 3600)


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
    "/setup", "/setup/save", "/setup/theme", "/setup/scale", "/setup/mode",
    "/setup/storage-categories",
    "/setup/test/grocy", "/setup/test/vision", "/setup/test/remote",
    "/setup/test/provider", "/setup/test/mealie", "/setup/test/recipes",
    "/setup/totp/generate", "/setup/totp/verify", "/setup/totp/disable",
    "/setup/satellite/sync", "/setup/ha/cameras",
    # Satellites pull config here; the handler enforces its own X-API-Key, so
    # it is safe to skip the setup-redirect/auth wrappers.
    "/api/config/satellite",
    "/health", "/docs", "/openapi.json", "/redoc",
}
# "/" only redirects to /ui/, so it can safely skip auth (the target enforces it)
_PUBLIC_PATHS = _SETUP_BYPASS | {"/ui/login", "/"}


def _is_static(path: str) -> bool:
    return path.startswith("/static/")


@app.middleware("http")
async def redirect_if_unconfigured(request: Request, call_next):
    """Send new installs to /setup until Grocy + vision provider are configured."""
    # The satellite proxy enforces its own X-API-Key, so it must not be caught
    # by the setup-redirect (it has no browser session to redirect anyway).
    if (not settings.is_configured() and request.url.path not in _SETUP_BYPASS
            and not _is_static(request.url.path)
            and not request.url.path.startswith("/api/proxy/")
            # The satellite setup wizard scans the LAN for its main server before
            # this device is configured. That call must return JSON, not an HTML
            # setup redirect, or the wizard's JSON.parse fails.
            and request.url.path != "/api/devices/scan"):
        # Preserve the kiosk latch across the redirect (FoodAssistant-joj1). The
        # appliance display loads /ui/?kiosk=1; without carrying kiosk=1 onto
        # /setup, kiosk-display.js never latches kiosk mode there, so the setup
        # page does not poll kiosk/navigate/pending and the display stays stuck
        # on the wizard after setup completes from another browser.
        target = "/setup?kiosk=1" if request.query_params.get("kiosk") == "1" else "/setup"
        return ingress_redirect(request, target)
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

    # totp_pending means password was accepted but TOTP not yet verified: not authed
    session_ok = request.session.get("authed", False) and not request.session.get("totp_pending")
    sent = request.headers.get("X-API-Key", "")
    valid = settings.valid_api_keys()
    key_ok = bool(sent) and bool(valid) and any(
        secrets.compare_digest(sent, k) for k in valid
    )
    if session_ok or key_ok:
        return await call_next(request)

    if request.url.path.startswith("/ui"):
        return ingress_redirect(request, "/ui/login")
    return JSONResponse({"detail": "Unauthorized"}, status_code=401)


@app.middleware("http")
async def require_pin(request: Request, call_next):
    """Optional numeric PIN gate for the kiosk UI on a satellite. It only guards
    the browser UI (/ui and the root redirect), leaving /setup reachable so the
    PIN can be changed or cleared without SSH. The unlock screen lives at
    /ui/pin and stores a session flag once the code matches.

    When kiosk_readonly_when_locked is True, unauthenticated GET requests are
    allowed through (read-only browsing), while write methods (POST/PUT/PATCH/
    DELETE) from unauthenticated users are rejected with 403."""
    if not settings.pin_lock_active():
        return await call_next(request)
    path = request.url.path
    if not (path == "/" or path.startswith("/ui")):
        return await call_next(request)
    if path in ("/ui/pin", "/ui/pin/verify", "/ui/login") or _is_static(path):
        return await call_next(request)
    if request.session.get("pin_ok"):
        return await call_next(request)
    if settings.kiosk_readonly_when_locked:
        if request.method == "GET":
            request.state.pin_readonly = True
            return await call_next(request)
        return JSONResponse({"detail": "Locked"}, status_code=403)
    return ingress_redirect(request, "/ui/pin")


# SessionMiddleware runs after middlewares above so request.session is available
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, max_age=60 * 60 * 24 * 30)

from pathlib import Path
from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

app.include_router(setup.router)
app.include_router(admin.router)
app.include_router(pending.router)
app.include_router(grocy.router)
app.include_router(mealie.router)
app.include_router(analyze.router)
app.include_router(defaults.router)
app.include_router(inventory.router)
app.include_router(expiring.router)
app.include_router(tunnel.router)
app.include_router(ui.router)
app.include_router(qr.router)
app.include_router(satellite.router)
app.include_router(proxy.router)
app.include_router(devices.router)
app.include_router(current_recipe.recipe_router)
app.include_router(current_recipe.timers_router)
app.include_router(events.router)
app.include_router(action_items.router)
app.include_router(nutrition.router)
app.include_router(audit.router)


@app.get("/")
async def root():
    return RedirectResponse("/ui/", status_code=303)


# Stable fingerprint so a LAN scan can tell a FoodAssistant instance apart from
# any other service answering on the same port. Public (no auth) on purpose: it
# reveals only the app name, version and deployment mode, never config or keys.
_FINGERPRINT = {"app": "foodassistant", "version": APP_VERSION}


@app.get("/health")
async def health():
    if not settings.is_configured():
        return {**_FINGERPRINT, "status": "unconfigured", "setup": "/setup",
                "mode": settings.deployment_mode}
    from .dependencies import get_vision_provider
    from .services.grocy import GrocyClient
    provider = get_vision_provider()
    grocy = GrocyClient()
    if settings.ai_configured():
        vision_status = "ok" if await provider.health_check() else "error"
    else:
        vision_status = "not configured"
    return {
        **_FINGERPRINT,
        "status": "ok",
        "mode": settings.deployment_mode,
        "vision_provider": vision_status,
        "grocy": "ok" if await grocy.health_check() else "error",
    }
