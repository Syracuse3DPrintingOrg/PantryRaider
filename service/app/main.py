import asyncio
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from contextlib import asynccontextmanager

from .config import settings, APP_NAME, APP_VERSION
from .database import engine, ensure_schema, get_db, Base
from .ingress import ingress_redirect
from .models import db_models  # noqa: F401 - registers models with Base
from .services.defaults import seed_defaults
from .services.diagnostics import configure_console_logging
from .services import pairing as pairing_svc
from .routers import analyze, defaults, inventory, expiring, ui, setup, pending, mealie, admin, qr, tunnel, grocy, satellite, proxy, devices, current_recipe, events, action_items, nutrition, audit, affiliate, printing, cook_wizard, recipes, gadgets, pairing, ha, cub

# uvicorn wires handlers only for its own loggers, so the app's INFO lines
# (first-boot provisioning, the readiness gate, background tasks) used to be
# invisible in `docker logs`: a first boot going wrong looked like a silent
# container. Attach the guarded stdout handler at import time so everything
# from the first lifespan task onward shows on the console. Idempotent, so
# --reload restarts and repeat imports never print a line twice.
configure_console_logging()


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
    # create_all never adds a new column to an existing table; backfill any
    # post-release column additions so an upgraded install keeps working.
    ensure_schema(engine)
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
    # Community shelf life (FoodAssistant-ezkh): refresh the overrides feed
    # daily and upload any queued anonymous corrections. Idle unless the
    # matching settings are on; every pass is best-effort.
    app.state.community_expiry_task = asyncio.create_task(
        _periodic_community_expiry())
    # Home Assistant thermometer source (FoodAssistant-mnks): read configured
    # HA temperature entities into the gadgets state so probes work without a
    # host Bluetooth reader. Runs on every mode; the loop sits idle until the
    # source is enabled with entities and an HA connection.
    try:
        from .services.gadgets_ha import poll_loop as _gadgets_ha_loop
        app.state.gadgets_ha_task = asyncio.create_task(_gadgets_ha_loop())
    except Exception:
        pass
    # ESPHome WiFi thermometer source (FoodAssistant-0oq3): poll each configured
    # ESP device's web_server sensor into the gadgets state so a DIY WiFi probe
    # works without a host Bluetooth reader. Idle until the source is enabled
    # with at least one device.
    try:
        from .services.gadgets_esp import poll_loop as _gadgets_esp_loop
        app.state.gadgets_esp_task = asyncio.create_task(_gadgets_esp_loop())
    except Exception:
        pass
    # Fridge/freezer and door-open protection alarms (FoodAssistant-5c61):
    # ingest re-evaluates on every reading push, and this sweep catches what
    # a push cannot: a sensor gone silent past its staleness window, and a
    # door whose open timer runs out with no new advertisement. No-op unless
    # hygrometers or contact sensors are configured.
    try:
        app.state.gadget_alarms_task = asyncio.create_task(
            _periodic_gadget_alarms())
    except Exception:
        pass
    # Reolink self-detection pop-ups (FoodAssistant-umnj / akd0): poll each
    # Reolink camera that opted into pop-ups for a live AI detection and pop it
    # up on the kiosk. The loop no-ops until such a camera is configured.
    try:
        app.state.reolink_poll_task = asyncio.create_task(_periodic_reolink_poll())
    except Exception:
        pass
    # Zero-touch first run (FoodAssistant-syxf): when a co-hosted Grocy or
    # Mealie comes up still on its factory sign-in and nothing is configured
    # yet, connect it automatically. Background task so startup never waits;
    # it exits immediately on a configured install and never touches one.
    if not settings.is_satellite() and (
            not settings.grocy_api_key
            or not (settings.mealie_api_key and settings.mealie_base_url)):
        try:
            from .services.first_run import startup_first_run
            app.state.first_run_task = asyncio.create_task(startup_first_run())
        except Exception:
            pass
    # Advertise this install over mDNS so the Home Assistant integration can
    # find it on the LAN (FoodAssistant-ju93 follow-up). Best-effort: the
    # helper itself already swallows every failure, this try/except is just
    # belt-and-braces against something unexpected in the hostname/version
    # lookups themselves.
    try:
        from .services import discovery
        from .config import device_hostname
        await discovery.start(device_hostname(), settings.deployment_mode or "server",
                               APP_VERSION, settings.device_id)
    except Exception:
        pass
    yield
    for attr in ("sync_task", "auto_update_task", "usb_backup_task",
                 "instance_guard_task", "pi_health_task", "first_run_task",
                 "gadgets_ha_task", "gadgets_esp_task", "reolink_poll_task",
                 "gadget_alarms_task", "community_expiry_task"):
        task = getattr(app.state, attr, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    try:
        from .services import discovery
        await discovery.stop()
    except Exception:
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


async def _periodic_gadget_alarms():
    """Sweep the fridge/freezer and door-open protection alarms
    (FoodAssistant-5c61) about once a minute.

    Ingest already re-evaluates on every readings push; this pass exists for
    the silences: a hygrometer that stopped broadcasting past its staleness
    window, and a door whose left-open threshold elapses between
    advertisements. Runs in a thread so the state-file locking never blocks
    the event loop, and best-effort so a bad pass just means the next one
    catches up."""
    from fastapi.concurrency import run_in_threadpool
    from .services import gadgets
    while True:
        try:
            if settings.hygrometers_enabled or settings.contacts_enabled:
                await run_in_threadpool(gadgets.run_protection_sweep)
        except Exception:
            pass
        await asyncio.sleep(60)


async def _periodic_reolink_poll():
    """Pop up a Reolink camera on its own AI detection (FoodAssistant-umnj).

    A Reolink camera with a "pop up on" type set can trigger a kiosk camera
    pop-up without a Home Assistant automation. This polls each such camera's
    AI-detection state every few seconds and queues a pop-up when a watched
    type is alarming. Best-effort: when no Reolink camera has opted into
    pop-ups (the common case) the pass does nothing, and an unreachable camera
    is skipped. Only meaningful while on-screen events are enabled, since that
    is the channel the pop-up rides."""
    from .services import camera_popup_poll, cameras as _cams, ha_events
    await asyncio.sleep(45)  # settle after boot
    while True:
        try:
            cams = settings.streamdeck_cameras or []
            if settings.ha_events_enabled and any(
                    isinstance(c, dict) and c.get("popup_types") and _cams.is_reolink(c)
                    for c in cams):
                await camera_popup_poll.poll_reolink_cameras_once(
                    cams, _cams.fetch_reolink_ai_state,
                    int(settings.ha_camera_popup_seconds or 20),
                    ha_events.add_camera)
        except Exception:
            pass
        await asyncio.sleep(8)


async def _periodic_community_expiry():
    """Community shelf life housekeeping (FoodAssistant-ezkh), hourly.

    Two independent, settings-gated chores: pull the aggregated overrides feed
    from Forager when the day-old cache is stale (use_community_expiry), and
    upload any queued anonymous expiry corrections (share_expiry_learning; the
    flush also discards the queue if sharing has been turned off, so nothing
    consented-then-withdrawn is ever sent). Both are calm about failure: an
    unreachable Forager just means the next pass tries again."""
    from .services import community_expiry, expiry_learning
    await asyncio.sleep(90)  # settle after boot
    while True:
        try:
            if settings.use_community_expiry and community_expiry.is_stale():
                await community_expiry.refresh()
        except Exception:
            pass
        try:
            await expiry_learning.flush()
        except Exception:
            pass
        await asyncio.sleep(3600)


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
    # First-run backend provisioning runs from the wizard BEFORE setup is
    # complete (auto-configuring the co-hosted Grocy, connecting a running
    # Mealie, revealing the generated sign-in). Without a bypass the
    # setup-redirect middleware answers these POSTs with the setup HTML page,
    # which the wizard tries to parse as JSON and fails ("Unexpected token '<'").
    # They only touch the local backends; once configured they are auth-gated
    # like the rest of /setup (FoodAssistant).
    "/setup/first-run/grocy", "/setup/first-run/mealie",
    "/setup/first-run/reveal",
    # The wizard's live inventory-setup indicator polls the local Grocy status
    # and the install log while the stack is still coming up on first boot, so
    # these read-only status endpoints must answer JSON before setup completes
    # (same reason as the first-run POSTs above); otherwise the progress spinner
    # gets the setup HTML page and silently stalls (FoodAssistant-f8kp).
    "/setup/grocy/local-status", "/setup/logs/grocy", "/setup/logs/mealie",
    # The kiosk splash and the wizard show a QR code for finishing setup from a
    # phone; the <img> fetches /ui/qr BEFORE setup completes, so without a
    # bypass it gets the setup-redirect HTML and renders as a broken image.
    "/ui/qr",
    # The kiosk-latched setup page polls this to learn that setup just finished
    # from another device (FoodAssistant-6v9q), so it must answer JSON before
    # setup completes, not the setup-redirect HTML page. It exposes a single
    # one-shot "setup finished, navigate now" boolean and nothing else, and the
    # flag only ever exists once setup HAS completed, at which point this path
    # is auth-gated like the rest of /setup; the appliance's own display then
    # reaches it through the loopback trust below.
    "/setup/kiosk/navigate/pending",
    # The first-boot readiness page and its poll (FoodAssistant-0m61) exist
    # only BEFORE setup completes, so they must serve rather than redirect.
    "/ui/getting-ready", "/ui/getting-ready/status",
    # The wizard's remote-access step turns Forager on before setup completes;
    # these must answer JSON, not the setup-redirect page ("Unexpected token <").
    "/setup/tunnel/enable", "/setup/tunnel/disable", "/setup/tunnel/status",
    # Satellites pull config here; the handler enforces its own X-API-Key, so
    # it is safe to skip the setup-redirect/auth wrappers.
    "/api/config/satellite",
    # A satellite's wizard requests access from its main server BEFORE this
    # device is configured: the relay endpoints must answer JSON, not the
    # setup-redirect HTML page ("Unexpected token <").
    "/setup/pairing/request", "/setup/pairing/status",
    # And the server side of the same handshake: an unconfigured (or freshly
    # set up) server must answer a pairing request with JSON too. The status
    # poll is prefix-matched below (its request token rides in the path).
    "/api/pairing/request",
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
    # A satellite asking to pair has no API key yet, by definition. The handler
    # enforces its own gates (LAN-only client address, the
    # local_device_pairing_enabled toggle); approving stays auth-required.
    "/api/pairing/request",
}
# Always-public path PREFIXES: the satellite's pairing status poll carries its
# per-request token in the path, so it cannot be an exact-match entry. Same
# handler-enforced gates as /api/pairing/request above.
_ALWAYS_PUBLIC_PREFIXES = ("/api/pairing/status/",)


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
# kiosk viewer (FoodAssistant-e9al). /ha/connect saves this install's Home
# Assistant base URL and long-lived token and probes the URL server-side, so it
# is an admin action too (FoodAssistant-0h8i); a headless X-API-Key client (the
# HA integration handing the connection back) keeps full access.
_ADMIN_ONLY_PATHS = ("/ui/camera/preview", "/ha/connect")


def _is_admin_only(path: str) -> bool:
    if path in _ADMIN_ONLY_PATHS:
        return True
    return any(path == p or path.startswith(p + "/") for p in _ADMIN_ONLY_PREFIXES)


def _is_static(path: str) -> bool:
    return path.startswith("/static/")


# Satellite forwarding for the native recipe library (FoodAssistant-g0fd).
# Recipes and the meal plan live in the MAIN SERVER's database when the fleet
# runs Pantry Raider's own recipe store, the same way pending scans do. A
# satellite therefore forwards every /mealie and /recipes call upstream instead
# of answering from its own (empty) tables. Only active in native mode: an
# install whose library still runs in Mealie keeps today's path, where the
# local MealieClient reaches the server's Mealie through the /api/proxy hop.
# Registered before (inside) the auth middleware, so local auth still applies
# to the caller; the upstream hop authenticates with the satellite's API key.
_SATELLITE_RECIPE_PREFIXES = ("/mealie", "/recipes")
# Thermometer data lives on the server: a satellite forwards its BLE reader's
# pushes and the kiosk's polls upstream so the server is the fleet hub and its
# Timers page (and the satellite's kiosk, which shows the server's pages) both
# see the probes (FoodAssistant-v7om). The server owns the device config, so a
# thermometer the satellite discovers is added once, on the server.
_SATELLITE_GADGET_PREFIXES = ("/gadgets",)
# Printers are system-level: one label and one document printer live on the
# main server, so a satellite relays its PRINT actions upstream and the server
# prints to the shared printer (FoodAssistant-eml9). Only the actual print
# jobs forward; the Bluetooth bridge setup and printer discovery stay local,
# because the satellite is the device physically hosting a printer to share up.
_SATELLITE_PRINT_PATHS = frozenset({
    "/printing/label", "/printing/label/batch",
    "/printing/decorative", "/printing/document",
})

_satellite_fwd_client = None


def _satellite_recipe_upstream(path: str) -> str | None:
    """The server base URL when this request is data a satellite must forward
    (native recipes, thermometer gadgets, or a print job), else None."""
    if not (settings.is_satellite() and settings.remote_server_url
            and settings.upstream_api_key):
        return None
    base = settings.remote_server_url.rstrip("/")
    # Print jobs normally go to the server's one system printer. Exception
    # (FoodAssistant-eml9 follow-up): when THIS satellite physically hosts the
    # label printer (a Supvan the bridge registered as a local CUPS queue), its
    # label jobs print locally instead of round-tripping through the server,
    # which is more robust (no dependency on the server being up to print a
    # label on a printer that is right here). Document jobs still forward, since
    # the document printer is server-side.
    if path in _SATELLITE_PRINT_PATHS:
        _label_paths = ("/printing/label", "/printing/label/batch",
                        "/printing/decorative")
        if path in _label_paths:
            try:
                from .services import printing as _printing
                if _printing.local_label_queue(settings.label_printer_queue):
                    return None
            except Exception:
                pass
        return base
    # Thermometers: always forwarded on a satellite (the server holds the
    # config and the readings), no backend condition. The one exception is
    # /gadgets/install, which sets up the BLE reader on THIS device's host (the
    # satellite is the one with the radio), so it must run locally.
    if any(path == p or path.startswith(p + "/")
           for p in _SATELLITE_GADGET_PREFIXES):
        if path == "/gadgets/install":
            return None
        # With the gadget relay on (FoodAssistant-me3t) the local reader's two
        # endpoints are answered by THIS device: the satellite ingests every
        # push itself and forwards a tagged copy upstream through a retry
        # queue (services/gadgets_relay.py, so a server blip cannot lose a
        # button press), and /gadgets/config hands the reader the server's
        # device lists merged with any local ones (so a reboot with the
        # server down still yields a working config). Everything else under
        # /gadgets, the state polls and the device management calls, still
        # forwards: the server stays the fleet hub.
        if path in ("/gadgets/readings", "/gadgets/config"):
            from .services import gadgets_relay
            if gadgets_relay.relay_active():
                return None
        return base
    if not any(path == p or path.startswith(p + "/")
               for p in _SATELLITE_RECIPE_PREFIXES):
        return None
    # The Mealie migration is a one-time server-side action; the endpoint's own
    # answer tells the user to run it there, which beats silently kicking off a
    # migration from a kiosk.
    if path == "/recipes/migrate-from-mealie":
        return None
    from .services import recipe_source
    if recipe_source.active_backend() != recipe_source.BACKEND_NATIVE:
        return None
    return base


# Micro-caches for the hottest FORWARDED GET polls (FoodAssistant-7dt9). The
# Timers page polls gadgets/state every 5s and several surfaces can land in the
# same window; without a cache each poll was its own upstream round trip from
# the satellite. Mirrors the /timers TTLCache pattern: short enough that
# readings never look stale, long enough to collapse a poll burst into one
# upstream request. GET-only, exact-path keyed (these paths take no params that
# change the answer per client).
from .services.ttl_cache import TTLCache  # noqa: E402
_FWD_CACHE_PATHS = {"/gadgets/state": TTLCache(2.0)}


@app.middleware("http")
async def forward_native_recipes(request: Request, call_next):
    base = _satellite_recipe_upstream(request.url.path)
    if not base:
        return await call_next(request)
    fwd_cache = _FWD_CACHE_PATHS.get(request.url.path) if request.method == "GET" else None
    if fwd_cache is not None:
        hit = fwd_cache.get()
        if hit is not None:
            content, status, media = hit
            from fastapi.responses import Response
            return Response(content=content, status_code=status, media_type=media)
    global _satellite_fwd_client
    if _satellite_fwd_client is None:
        import httpx
        _satellite_fwd_client = httpx.AsyncClient(timeout=30.0)
    headers = {"X-API-Key": settings.upstream_api_key}
    ct = request.headers.get("content-type")
    if ct:
        headers["Content-Type"] = ct
    body = await request.body()
    try:
        up = await _satellite_fwd_client.request(
            request.method,
            f"{base}{request.url.path}",
            headers=headers,
            params=dict(request.query_params),
            content=body or None,
        )
    except Exception:
        return JSONResponse(
            {"detail": "The main server is not reachable. "
                       "This will return when it is."},
            status_code=502,
        )
    from fastapi.responses import Response
    if fwd_cache is not None and up.status_code == 200:
        fwd_cache.set((up.content, up.status_code,
                       up.headers.get("content-type", "application/json")))
    return Response(content=up.content, status_code=up.status_code,
                    media_type=up.headers.get("content-type", "application/json"))


@app.middleware("http")
async def redirect_if_unconfigured(request: Request, call_next):
    """Send new installs to /setup until Grocy + vision provider are configured,
    and hold a brand-new Pi appliance on the first-boot progress page until its
    co-hosted inventory is actually connected."""
    # First-boot readiness gate (FoodAssistant-0m61 / -n2b4): on a brand-new Pi
    # appliance the app serves minutes before its co-hosted Grocy does, so
    # instead of landing on a wizard that cannot finish, browsers land on a live
    # progress page until the inventory is CONNECTED (an API key is saved). The
    # steer is keyed on gate_active(), NOT is_configured(): the appliance seeds
    # its own Grocy URL, so a wizard password flips is_configured() true while
    # the inventory is still empty, and that used to release the user into a
    # Grocy-less app (n2b4). gate_possible() is pi_hosted-only, so servers and
    # satellites never enter this block. The gate is sticky-off: readiness
    # remembers the connect, a dismissal, or the backstop, so it never reappears.
    from .services import readiness
    gated = False
    if readiness.gate_possible():
        if (request.url.path == "/setup" and request.method == "GET"
                and request.query_params.get("skip_ready") == "1"):
            # The getting-ready page's escape hatch: remember the choice so the
            # wizard is not bounced straight back.
            readiness.dismiss()
        elif await readiness.gate_active():
            gated = True
            # /setup is in the bypass set below, so steer it to the progress
            # page here; every other navigable path takes the /setup hop and
            # lands on the progress page from there.
            if request.url.path == "/setup" and request.method == "GET":
                target = "/ui/getting-ready"
                if request.query_params.get("kiosk") == "1":
                    target += "?kiosk=1"
                return ingress_redirect(request, target)
    # A request is steered to setup (or, while the gate holds, to the progress
    # page via the /setup hop above) when the install is unconfigured OR the
    # first-boot gate is still holding. The path filter is identical in both
    # cases. The satellite proxy enforces its own X-API-Key, so it must not be
    # caught by the setup-redirect (it has no browser session to redirect anyway).
    if ((not settings.is_configured() or gated) and request.url.path not in _SETUP_BYPASS
            and not _is_static(request.url.path)
            and not request.url.path.startswith("/api/proxy/")
            # The pairing status poll (token in the path) must answer JSON on
            # an unconfigured server, same as /api/pairing/request above.
            and not request.url.path.startswith(_ALWAYS_PUBLIC_PREFIXES)
            # The satellite setup wizard scans the LAN for its main server before
            # this device is configured. That call must return JSON, not an HTML
            # setup redirect, or the wizard's JSON.parse fails.
            and request.url.path != "/api/devices/scan"):
        # Preserve the kiosk latch across the redirect (FoodAssistant-joj1). The
        # appliance display loads /ui/?kiosk=1; without carrying kiosk=1 onto
        # /setup, kiosk-display.js never latches kiosk mode there, so the
        # phone-setup splash never renders and the setup page's kiosk hand-off
        # poller (gated on that latch, it watches kiosk/navigate/pending,
        # FoodAssistant-6v9q) never runs: the display would stay stuck on the
        # wizard after setup completes from another browser.
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
    if request.url.path.startswith(_ALWAYS_PUBLIC_PREFIXES):
        return await call_next(request)
    if request.url.path in _SETUP_BYPASS and not settings.is_configured():
        return await call_next(request)

    # Requests from the loopback address are always trusted (local kiosk, cron jobs).
    if request.client and request.client.host in ("127.0.0.1", "::1"):
        return await call_next(request)

    # A Bandit Cub fetching its own firmware cannot present a key: the stock
    # ESPHome update component sends no headers, so automatic Cub updates
    # would 401 on every install with a password. What these two endpoints
    # hand out is a board name, a version, and the same image the project's
    # public GitHub release serves, so they are open to the local network
    # only, the same shape as the pairing endpoints above. A browser on the
    # public URL still reaches them through its session below.
    if (request.url.path.startswith("/cub/firmware/") and request.method == "GET"
            and request.client
            and pairing_svc.is_local_network_request(request)):
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


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Add conservative security headers to every response (security audit,
    Jul 2026). X-Frame-Options blocks clickjacking of the kiosk, nosniff stops
    MIME sniffing, and a same-origin referrer policy avoids leaking paths. A
    full Content-Security-Policy is deliberately not set here: the UI relies on
    inline event handlers and inline scripts, so a strict CSP needs live kiosk
    testing first (tracked separately)."""
    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    # Content-Security-Policy (FoodAssistant-rnnn). Everything the pages load
    # is served by this app, with two deliberate loosenings: inline script and
    # style are allowed (the templates lean on inline handlers and style
    # blocks throughout), and img-src additionally allows any http(s) host
    # plus data:/blob: because the photo screensaver's "web addresses" source
    # hands raw external image links straight to the browser, the QR/label
    # previews use data: URIs, and the label designer uses blob:. worker-src
    # covers the barcode scanner's blob worker. The practical win over no CSP:
    # external script/style injection, plugins, base hijacks, and form
    # exfiltration are all blocked. Validated by driving every major page in
    # a kiosk-emulating Chromium and asserting zero violations.
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob: https: http:; "
        "media-src 'self' blob:; "
        "connect-src 'self'; "
        "worker-src 'self' blob:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'self'",
    )
    return response


# SessionMiddleware runs after middlewares above so request.session is available
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, max_age=60 * 60 * 24 * 30)


# Response compression (FoodAssistant-7dt9). Rendered pages ship 40-130 KB of
# HTML per kiosk navigation and are deliberately never cached (network-first),
# so gzip cuts every page load and JSON poll by 75-85 percent, which is most of
# what a Pi kiosk actually waits on. Image-ish paths are EXCLUDED: recompressing
# a JPEG camera stream or a recipe photo wastes Pi CPU for zero size win, so a
# thin outer shim strips the accept-encoding header on those before GZip looks.
from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402

_NO_GZIP_PREFIXES = ("/ui/camera", "/recipes/images", "/ui/screensaver")

app.add_middleware(GZipMiddleware, minimum_size=500)


@app.middleware("http")
async def skip_gzip_for_images(request: Request, call_next):
    # Runs OUTSIDE GZipMiddleware (later-added middleware is outermost), so
    # dropping the header here makes GZip pass image responses through as-is.
    if request.url.path.startswith(_NO_GZIP_PREFIXES):
        request.scope["headers"] = [
            (k, v) for k, v in request.scope["headers"] if k != b"accept-encoding"
        ]
    return await call_next(request)


# Session cookie hardening (FoodAssistant-g6ai): when the request itself arrived
# over TLS, mark the session Set-Cookie Secure so the browser never replays it
# over plain HTTP. Registered AFTER SessionMiddleware on purpose: Starlette runs
# later-added middleware outermost, and the cookie is only visible outside the
# session layer. Per-request rather than SessionMiddleware's static https_only
# flag because the same device is legitimately reached both ways: HTTP on the
# trusted LAN (where Secure would break login) and HTTPS via direct TLS. The
# Forager tunnel terminates TLS upstream, so those requests reach the app as
# HTTP and are unchanged here; this hardens direct-TLS deployments without
# touching LAN behaviour.
@app.middleware("http")
async def secure_session_cookie(request: Request, call_next):
    response = await call_next(request)
    if request.url.scheme == "https":
        cookies = response.headers.getlist("set-cookie")
        if any(c.lower().startswith("session=") and "secure" not in c.lower()
               for c in cookies):
            del response.headers["set-cookie"]
            for c in cookies:
                if c.lower().startswith("session=") and "secure" not in c.lower():
                    c = c + "; Secure"
                response.headers.append("set-cookie", c)
    return response

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
            # A year + immutable: every template URL is ?v=APP_VERSION busted,
            # so an update changes the URL; the old one can cache forever and
            # the kiosk never spends a round trip revalidating it (7dt9).
            response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
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
app.include_router(printing.router)
app.include_router(cook_wizard.router)
app.include_router(recipes.router)
app.include_router(gadgets.router)
app.include_router(pairing.router)
app.include_router(ha.router)
app.include_router(cub.router)


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
