import asyncio
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from contextlib import asynccontextmanager

from .config import settings, APP_NAME, APP_VERSION
from .database import engine, get_db, Base
from .ingress import ingress_redirect
from .models import db_models  # noqa: F401 - registers models with Base
from .services.defaults import seed_defaults
from .routers import analyze, defaults, inventory, expiring, ui, setup, pending, mealie, admin, qr, tunnel, grocy, satellite, proxy, devices, current_recipe, events, action_items, nutrition, audit, affiliate


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
    # Belt-and-braces multi-process detection (FoodAssistant-0fho): warn loudly
    # if another live app process is already serving this data dir (uvicorn
    # started with several workers), then keep our own heartbeat fresh.
    # Best-effort: an unwritable data dir just leaves the guard silent.
    try:
        from .services import instance_guard
        instance_guard.check_on_startup()
        app.state.instance_guard_task = asyncio.create_task(
            instance_guard.heartbeat_task())
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
    # Surface a live Pi power/thermal condition as an on-screen kiosk toast
    # (FoodAssistant-h28s). Pi hardware only: off a Pi there is no host bridge
    # to probe, so the task is never started.
    try:
        from .hardware import is_raspberry_pi
        if is_raspberry_pi():
            app.state.pi_health_task = asyncio.create_task(_periodic_pi_health())
    except Exception:
        pass
    # Scheduled USB flash-drive backups (FoodAssistant-ch6d). Runs on every
    # mode; the loop is a no-op until usb_backup_interval_hours is set.
    app.state.usb_backup_task = asyncio.create_task(_periodic_usb_backup())
    yield
    for attr in ("sync_task", "auto_update_task", "usb_backup_task",
                 "instance_guard_task", "pi_health_task"):
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
                if au.should_run(settings.is_satellite(), APP_VERSION,
                                 last_server_version(), settings.update_channel):
                    result = await run_host_bridge_update()
                    if result.get("ok"):
                        import logging
                        logging.getLogger("foodassistant.autoupdate").info(
                            "Auto-update ran: before=%s after=%s restarted=%s",
                            result.get("before"), result.get("after"), result.get("restarted"))
        except Exception:
            pass
        await asyncio.sleep(6 * 3600)


async def _periodic_pi_health():
    """Toast a live Pi power/thermal condition on the kiosk (FoodAssistant-h28s).

    Polls the host bridge warnings feed (through the same setup/system/health
    relay the nav icon and status page use, so the action-items mirror runs
    too) about once a minute and hands it to the pure edge-trigger in
    pi_health, which queues an on-screen toast only when a condition first goes
    live. Best-effort throughout: an unreachable bridge or a partial read just
    means no toast this pass. The nav-bar triangle and the status page are
    unchanged; this only adds the always-shown toast the kiosk was missing."""
    from .services import pi_health
    from .routers.setup import system_health

    async def _fetch():
        data = await system_health()
        return data.get("warnings") if isinstance(data, dict) else None

    # Settle after boot so a brief power dip while everything spins up does not
    # greet the user with a toast before the device is even idle.
    await asyncio.sleep(60)
    while True:
        try:
            await pi_health.poll_and_toast(_fetch)
        except Exception:
            pass
        await asyncio.sleep(60)


async def _periodic_usb_backup():
    """Back up to an attached USB drive on the configured interval.

    Checks every 15 minutes whether a run is due (interval on, enough time
    since the last successful run). The last-run time is persisted, so a
    restart never resets the clock, and a missing drive just means the pass is
    skipped and retried on the next check once a drive is plugged in.
    """
    import time as _time
    from .services import usb_backup
    # Let the app settle after boot before touching external storage.
    await asyncio.sleep(120)
    while True:
        try:
            if usb_backup.is_due(settings.usb_backup_interval_hours,
                                 settings.usb_backup_last, _time.time()):
                result = await usb_backup.run_backup()
                if result.get("ok"):
                    import logging
                    logging.getLogger("foodassistant.usbbackup").info(
                        "Scheduled USB backup written: %s", result.get("file"))
        except Exception:
            pass
        await asyncio.sleep(900)


app = FastAPI(
    title=APP_NAME,
    description="Food spoilage tracker with LLM-powered photo import and Grocy integration",
    version=APP_VERSION,
    lifespan=lifespan,
)

# No CORS middleware on purpose (security review, Jul 2026). Every browser
# client is same-origin: the web UI and kiosk pages fetch relative URLs, the
# phone QR flow opens the app's own address, and the setup wizard posts to its
# own origin. The headless clients (Home Assistant REST sensors, the satellite
# sync, the Stream Deck controller, the host bridge) are server-side HTTP with
# no CORS preflight at all. The old allow_origins=["*"] therefore served no
# client and only widened the browser attack surface (any web page could probe
# the LAN address and read whatever answers without a login). Same-origin is
# the browser default and strictly safer. If a cross-origin browser client
# ever appears, add CORSMiddleware back gated on an env-only allowlist
# setting, never "*".

# Paths that bypass both setup-redirect and auth checks
_SETUP_BYPASS = {
    "/setup", "/setup/save", "/setup/theme", "/setup/scale", "/setup/mode",
    # The wizard's Hardware step applies a preset before setup is finished, so
    # this must answer with JSON rather than serve the setup-redirect page
    # (FoodAssistant-kl5n). Like /setup/save it saves settings; once setup
    # completes it is auth-protected like the rest of /setup.
    "/setup/preset/apply",
    "/setup/custom-theme", "/setup/custom-theme/delete",
    "/setup/background", "/setup/background/image", "/setup/background/clear",
    "/setup/storage-categories",
    "/setup/test/grocy", "/setup/test/vision", "/setup/test/remote",
    "/setup/test/provider", "/setup/test/mealie", "/setup/test/recipes",
    "/setup/totp/generate", "/setup/totp/verify", "/setup/totp/disable",
    "/setup/satellite/sync", "/setup/ha/cameras",
    # The wizard's AI step signs in to Forager before setup is finished, so
    # these must answer rather than serve the setup-redirect page: signin and
    # the Google sign-in landing do the actual link, meta gates the Google
    # button. Like the /setup/test/* probes they only dial out (to the
    # configured cloud), and once setup completes they are auth-protected
    # like the rest of /setup.
    "/setup/cloud/signin", "/setup/cloud/meta", "/setup/cloud/oauth-return",
    # The last wizard step can start Mealie and poll its status before setup is
    # finished, so these must return JSON, not the setup-redirect HTML page.
    "/setup/mealie/start", "/setup/mealie/status",
    # Satellites pull config here; the handler enforces its own X-API-Key, so
    # it is safe to skip the setup-redirect/auth wrappers.
    "/api/config/satellite",
    "/health", "/docs", "/openapi.json", "/redoc",
    # PWA install assets: an install can be started from the login or setup
    # screen, so the OS must fetch these before any credentials exist.
    "/manifest.webmanifest", "/sw.js",
}
# Paths that are public REGARDLESS of configuration state: the login page,
# the root redirect ("/" only redirects to /ui/, whose target enforces auth),
# health/docs, and the satellite config pull (its handler enforces its own
# X-API-Key). Everything else in _SETUP_BYPASS is only auth-exempt while the
# instance is UNCONFIGURED: the wizard must work before credentials exist, but
# once setup completes the settings surface is as protected as any other page.
# Before this split, /setup/save stayed permanently unauthenticated, letting
# any LAN client overwrite settings including auth_password (rules audit, Jul
# 2026).
_ALWAYS_PUBLIC = {
    "/api/config/satellite", "/health", "/docs", "/openapi.json", "/redoc",
    "/ui/login", "/",
    # The web manifest and service worker must load on the login screen too, so
    # the browser can offer "Install" before the user signs in (PWA install
    # needs both reachable pre-auth). Both expose only static, public assets.
    "/manifest.webmanifest", "/sw.js",
}


# Admin-only surface for the optional viewer role (RBAC-lite, security review
# Jul 2026). A session opened with the viewer password can use every kitchen
# page (inventory, timers, scanning, cooking) but is kept out of anything that
# changes how the install works: the whole settings surface (/setup covers the
# wizard, saves, deployment-mode switches, network, and maintenance actions)
# and the admin surface (/admin covers backup downloads, restore, updates, and
# diagnostics). Kept in one place next to the auth middleware so the protected
# list is easy to audit and extend. The kiosk PIN gate and the loopback trust
# below are separate mechanisms and unchanged.
_ADMIN_ONLY_PREFIXES = ("/setup", "/admin")

# Individual admin-only routes that live outside the /setup and /admin trees.
# The arbitrary-URL camera preview fetches a URL the admin is about to add on
# the Cameras setup page, so it is an admin setup action even though the rest of
# /ui stays open to a viewer for the kiosk. The configured-camera snapshot and
# stream (/ui/camera/{idx}/...) are not listed: they serve saved cameras to the
# kiosk viewer (FoodAssistant-e9al).
_ADMIN_ONLY_PATHS = ("/ui/camera/preview",)


def _is_admin_only(path: str) -> bool:
    if path in _ADMIN_ONLY_PATHS:
        return True
    return any(path == p or path.startswith(p + "/") for p in _ADMIN_ONLY_PREFIXES)


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
    if request.url.path in _ALWAYS_PUBLIC or _is_static(request.url.path):
        return await call_next(request)
    if request.url.path in _SETUP_BYPASS and not settings.is_configured():
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
    if key_ok:
        # API keys stay full-access: they drive Home Assistant automations and
        # satellite syncs, which need endpoints a viewer session does not get.
        return await call_next(request)
    if session_ok:
        # A missing role means an admin session (every session predating the
        # viewer feature, and every admin login). Viewers are blocked from the
        # settings and admin surfaces: a browser page request is bounced back
        # to the app, anything else gets an explicit 403.
        if request.session.get("role") == "viewer" and _is_admin_only(request.url.path):
            if request.method == "GET" and request.url.path == "/setup":
                return ingress_redirect(request, "/ui/")
            return JSONResponse({"detail": "This needs the admin password."},
                                status_code=403)
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


@app.middleware("http")
async def enforce_demo_read_only(request: Request, call_next):
    """Read-only DEMO MODE gate (FoodAssistant-pxp0).

    A no-op unless settings.demo_mode is on. When on, every state-changing
    request (POST/PUT/PATCH/DELETE) is refused except a tiny session/display-only
    allowlist, so a public demo instance is fully navigable but never mutated.
    Consuming stock, scanning, saving settings, backup/restore, and any
    Grocy/Mealie write are all blocked before they reach a route, so nothing is
    written to the demo's databases.

    A navigation request (one that accepts HTML) is bounced back to where it came
    from with a friendly flash; an API/JSON caller gets a 403 with a stable
    machine-readable body so the front-end can react cleanly.
    """
    if not settings.demo_mode:
        return await call_next(request)
    from .services import demo
    if demo.is_blocked_in_demo(request.method, request.url.path):
        wants_html = "text/html" in request.headers.get("accept", "")
        if wants_html:
            # Stay on the page the visitor was on, with the flash the shared
            # templates already know how to render (?msg / ?msg_type).
            from urllib.parse import urlsplit, urlunsplit, urlencode, parse_qsl
            ref = request.headers.get("referer") or ""
            parts = urlsplit(ref)
            query = dict(parse_qsl(parts.query))
            query["msg"] = demo.DEMO_MESSAGE
            query["msg_type"] = "warning"
            if parts.path:
                # Referer is same-origin and already carries any ingress prefix,
                # so redirect to its path verbatim without re-prefixing.
                target = urlunsplit(("", "", parts.path, urlencode(query), ""))
                return RedirectResponse(target, status_code=303)
            return ingress_redirect(request, "/ui/?" + urlencode(query))
        return JSONResponse(
            {"error": demo.DEMO_ERROR_CODE, "message": demo.DEMO_MESSAGE},
            status_code=403,
        )
    return await call_next(request)


# SessionMiddleware runs after middlewares above so request.session is available
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, max_age=60 * 60 * 24 * 30)

from pathlib import Path
from fastapi.staticfiles import StaticFiles


class CachedStaticFiles(StaticFiles):
    """StaticFiles plus a Cache-Control header on successful responses.

    Starlette sends ETag/Last-Modified but no Cache-Control, so every page
    load revalidates every asset (a conditional request per file). On a Pi
    kiosk that is a burst of round trips into a single uvicorn worker on each
    navigation. Template URLs are version-busted (?v=APP_VERSION), so a day
    of caching is safe: an update changes the URL and skips the cache anyway.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers.setdefault("Cache-Control", "public, max-age=86400")
        return response


app.mount("/static", CachedStaticFiles(directory=Path(__file__).parent / "static"), name="static")

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
app.include_router(affiliate.router)


@app.get("/")
async def root():
    return RedirectResponse("/ui/", status_code=303)


# Progressive Web App (FoodAssistant-fd3z). Both files live under static/pwa but
# are served from the site ROOT so the manifest's scope is "/" and the service
# worker can control every page (a worker's scope can never rise above the path
# it is served from). Public in the auth middleware above, so an install can be
# started from the login/setup screen. The worker itself is conservative: live
# data and auth always win because it is network-first for navigations and API
# calls (see static/pwa/sw.js).
_PWA_DIR = Path(__file__).parent / "static" / "pwa"


@app.get("/manifest.webmanifest")
async def web_manifest():
    from fastapi.responses import FileResponse
    return FileResponse(
        _PWA_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/sw.js")
async def service_worker():
    from fastapi.responses import FileResponse
    # No-cache so an updated worker is picked up promptly; Service-Worker-Allowed
    # keeps the root scope explicit even though the file already lives at "/".
    return FileResponse(
        _PWA_DIR / "sw.js",
        media_type="text/javascript",
        headers={
            "Cache-Control": "no-cache",
            "Service-Worker-Allowed": "/",
        },
    )


# Stable fingerprint so a LAN scan can tell a Pantry Raider instance apart from
# any other service answering on the same port. Public (no auth) on purpose: it
# reveals only the app name, version and deployment mode, never config or keys.
_FINGERPRINT = {"app": "foodassistant", "version": APP_VERSION}


@app.get("/health")
async def health():
    if not settings.is_configured():
        return {**_FINGERPRINT, "status": "unconfigured", "setup": "/setup",
                "mode": settings.deployment_mode, "device_id": settings.device_id}
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
        # Lets a LAN scan recognise and skip this very server when it answers its
        # own probe through a Docker gateway (Pantry Raider).
        "device_id": settings.device_id,
        "vision_provider": vision_status,
        "grocy": "ok" if await grocy.health_check() else "error",
    }
