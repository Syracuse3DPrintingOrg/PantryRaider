import asyncio
import logging
import re
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.datastructures import MutableHeaders
from starlette.middleware.sessions import SessionMiddleware
from contextlib import asynccontextmanager

from .config import settings, APP_NAME, APP_VERSION, SettingsLockedError
from .database import engine, ensure_schema, get_db, Base
from .ingress import ingress_redirect
from .models import db_models  # noqa: F401 - registers models with Base
from .services.defaults import seed_defaults
from .services.diagnostics import configure_console_logging
from .services import pairing as pairing_svc
from .services import access, auth_state, credentials, lan, local_agents, upstream
from .services import setup_code as setup_code_svc
from .routers import analyze, defaults, inventory, expiring, ui, setup, pending, mealie, admin, qr, tunnel, grocy, satellite, proxy, devices, current_recipe, events, action_items, nutrition, audit, affiliate, printing, cook_wizard, recipes, gadgets, pairing, ha, cub, kiosk_status, receipt
from .routers import credentials as credentials_router
from .routers import kiosk_launch
from .routers import security as security_router
from .routers import setup_code as setup_code_router

log = logging.getLogger("foodassistant.access")

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
    # Multi-process detection (FoodAssistant-0fho): the app supports one
    # process per data dir (FoodAssistant-n6mta), so warn loudly if another
    # live app process is already serving it (a second uvicorn worker or a
    # second container), then keep our own heartbeat fresh.
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
        # Notices about a damaged settings file or database found while the
        # app was starting (durable.py, database.py) go to the inbox now.
        from .services import action_items as _action_items
        _action_items.flush_storage_notices(db)
    finally:
        db.close()
    # Rolling database snapshots for the start-up check to put back
    # (database.py). The first one waits a minute so it never slows startup.
    app.state.db_snapshot_task = asyncio.create_task(_periodic_db_snapshot())
    await _access_startup()
    # Custom kiosk tabs saved before links were checked may point somewhere a
    # tab should never go (javascript:, data:). Report them once at startup so
    # they show up in the action inbox. Best-effort: a failure here must never
    # stop the app from starting.
    try:
        from .services import url_safety
        db = next(get_db())
        try:
            url_safety.report_blocked_tabs(db)
        finally:
            db.close()
    except Exception:
        pass
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
    # Keep this device current while the global auto_update flag is on
    # (FoodAssistant-k2kk). Every mode runs the loop; each pass decides whether
    # this install can update itself (a Pi appliance, or a server whose host
    # helper proved itself), and a server on Watchtower is left to it.
    app.state.auto_update_task = asyncio.create_task(_periodic_auto_update())
    # Access housekeeping: retire the old trust for programs on this device
    # once they all sign in, retire unused older keys, keep the deck config
    # current. First pass after five minutes, then hourly.
    app.state.access_housekeeping_task = asyncio.create_task(
        _periodic_access_housekeeping())
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
                 "gadget_alarms_task", "community_expiry_task",
                 "access_housekeeping_task", "print_service_task",
                 "db_snapshot_task"):
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
    for flush in (auth_state.flush, credentials.flush):
        try:
            flush(force=True)
        except Exception:
            pass


async def _swallow(coro_factory):
    """Run a background coroutine whose failure must never surface."""
    try:
        await coro_factory()
    except Exception:
        pass


async def _access_startup():
    """Access state at start, each step best effort."""
    try:
        auth_state.ensure_initialized()
    except Exception:
        log.warning("Access state could not be prepared", exc_info=True)
    # A full-stack restore on the device (foodassistant-restore) brought a
    # different settings.json while the app was stopped: record it the way
    # the in-app restore does.
    try:
        auth_state.consume_restore_marker()
    except Exception:
        log.warning("Could not record the device restore in the access state",
                    exc_info=True)
    # The setup code: printed while an install is unconfigured, forgotten once
    # a password exists, and shown again at start while one is still valid.
    try:
        if not settings.is_configured():
            # A code from before a restart is printed again: a recreated
            # container starts with an empty log.
            if setup_code_svc.current():
                setup_code_svc.log_if_present()
            else:
                setup_code_svc.ensure("unconfigured")
        elif settings.auth_password:
            setup_code_svc.clear()
        else:
            setup_code_svc.log_if_present()
    except Exception:
        pass
    try:
        from .services import ha_policy_migration
        db = next(get_db())
        try:
            ha_policy_migration.migrate_once(db)
        finally:
            db.close()
    except Exception:
        pass
    # Which of the device's own programs are installed decides whether the
    # old trust for them applies on a server (access.legacy_trust_applies),
    # so ask the bridge now rather than at the first hourly pass.
    try:
        if local_agents.bridge_present() and not settings.is_pi_appliance():
            app.state.hardware_status_task = asyncio.create_task(
                _swallow(local_agents.bridge_hardware_status))
    except Exception:
        pass
    if not settings.is_satellite():
        try:
            from .services import printing as _printing
            refresh = _printing.refresh_print_service_and_notice
            app.state.print_service_task = asyncio.create_task(_swallow(refresh))
        except Exception:
            pass
    try:
        access.note_password_state_and_notify()
    except Exception:
        pass
    try:
        from .services import deck_config
        deck_config.scrub_ha_camera_token_urls()
    except Exception:
        pass


async def _access_housekeeping_pass(now: float | None = None) -> None:
    """One hourly pass; every step on its own so one failure skips only itself."""
    t = time.time() if now is None else now
    try:
        auth_state.flush(background=True)
    except Exception:
        pass
    hw = None
    try:
        if local_agents.bridge_present():
            hw = await local_agents.bridge_hardware_status()
    except Exception:
        hw = None
    try:
        local_agents.evaluate_retirement(t, hw)
    except Exception:
        log.debug("trust retirement pass failed", exc_info=True)
    try:
        local_agents.check_kiosk_url(hw)
    except Exception:
        pass
    try:
        credentials.retirement_pass(t)
    except Exception:
        log.debug("key retirement pass failed", exc_info=True)
    try:
        from .services import action_items as action_items_svc
        action_items_svc.refresh_unused_paired_keys(now=t)
    except Exception:
        log.debug("unused paired key check failed", exc_info=True)
    try:
        await local_agents.refresh_deck_config_if_key_changed()
    except Exception:
        pass
    try:
        credentials.flush(background=True)
    except Exception:
        pass
    try:
        setup_code_svc.delete_expired()
    except Exception:
        pass


async def _periodic_access_housekeeping():
    await asyncio.sleep(300)
    while True:
        await _access_housekeeping_pass()
        await asyncio.sleep(3600)


async def _periodic_db_snapshot():
    """Keep up to three daily database snapshots. take_snapshot decides
    whether one is due (a day old and the database changed), so checking
    hourly costs a stat or two. Cancellable; never raises."""
    from fastapi.concurrency import run_in_threadpool
    from .database import take_snapshot
    await asyncio.sleep(60)
    while True:
        try:
            await run_in_threadpool(take_snapshot)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(3600)


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


async def _auto_update_pass():
    """One auto-update decision; returns the update result or None.

    Off when settings.auto_update is off. A Pi appliance updates through its
    host bridge; a server only when its host helper proved itself
    (update_path.auto_update_eligible), so a server on Watchtower is left to
    Watchtower. A satellite only updates when its server is on a different
    version (so the fleet converges on the server's version).
    """
    from .services import auto_update as au
    from .services.satellite import last_server_version
    from .routers.setup import run_host_bridge_update
    if not settings.auto_update:
        return None
    eligible = settings.is_pi_appliance()
    if not eligible:
        try:
            from .services import update_path
            eligible = bool(await update_path.auto_update_eligible())
        except Exception:
            eligible = False
    if not eligible:
        return None
    if not au.should_run(settings.is_satellite(), APP_VERSION,
                         last_server_version(), settings.update_channel):
        return None
    result = await run_host_bridge_update(scheduled=True)
    if isinstance(result, dict) and result.get("ok"):
        logging.getLogger("foodassistant.autoupdate").info(
            "Auto-update ran: before=%s after=%s restarted=%s",
            result.get("before"), result.get("after"), result.get("restarted"))
    return result


async def _periodic_auto_update():
    """Apply updates while the global auto_update flag is on.

    Re-checks every few hours. The bridge restarts the app as part of an
    update, which cancels this task with the old process; the new process
    schedules it again, so a single extra pass after a restart is harmless
    because the update is idempotent.
    """
    # Settle after boot so a freshly provisioned device is not updated mid-setup.
    await asyncio.sleep(600)
    while True:
        try:
            await _auto_update_pass()
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



@app.exception_handler(SettingsLockedError)
async def _settings_locked(_request: Request, exc: SettingsLockedError):
    """A save while settings.json is damaged with no good copy (config.py):
    say why instead of answering with a server error."""
    return JSONResponse({"detail": str(exc)}, status_code=503)


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
    "/setup/cloud/signin", "/setup/cloud/meta", "/setup/cloud/oauth-state",
    "/setup/cloud/oauth-return",
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
    # The wizard's remote-access step reads the tunnel state before setup
    # completes, so these must answer JSON, not the setup-redirect page
    # ("Unexpected token <"). Turning the tunnel ON is deliberately absent:
    # the wizard asks for it in the save that completes setup instead
    # (FoodAssistant-mzsci), so /setup/tunnel/enable sits behind the login
    # like the rest of /setup and never runs on an unconfigured install.
    "/setup/tunnel/disable", "/setup/tunnel/status",
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
# and health. The satellite config pull is not here: it is a fleet route,
# which the access rules serve to keyless callers so the handler can answer
# with its own 401 or 503 (services/access.py). The interactive API pages (/docs, /redoc, /openapi.json) are NOT
# here: on an install with a password they now need one, so the full route map
# is not readable by anything that can reach the port (launch audit, Sep 2026).
# They stay reachable while the install is unconfigured, which is also when no
# password exists to ask for. Everything else in _SETUP_BYPASS is only auth-exempt while the
# instance is UNCONFIGURED: the wizard must work before credentials exist, but
# once setup completes the settings surface is as protected as any other page.
# Before this split, /setup/save stayed permanently unauthenticated, letting
# any LAN client overwrite settings including auth_password (rules audit, Jul
# 2026).
_ALWAYS_PUBLIC = {
    "/health",
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


# A second screen used to be installed with its login switched off, and the auth
# middleware returns immediately when no password is stored, so anything else on
# the network could reboot it, trigger an update or a restore, switch its
# deployment mode, read its recovery hotspot password, turn remote access on, or
# act on the main server through the forwarding routes below. New installs ask
# for a password like every other one, and until a device that predates that has
# been given one, these stay on its own screen (security audit, Sep 2026). The
# local display, deck and sensor reader all talk to the app over the loopback
# address, so none of them are affected.
#
# Scoped to a second screen on purpose. Every other install only ends up with no
# password when its owner turned the login off deliberately (is_configured()
# refuses to call an install ready otherwise), and locking their settings page
# would break a working setup rather than close a hole they did not open.
#
# Actions this app performs on the device itself (through the local root helper)
# or that hand back a device secret:
#
# Deliberately NOT listed: joining a Wi-Fi network (/setup/network/scan and
# /setup/network/wifi). Those two are how someone rescues a device that has
# fallen off the network, over its own recovery hotspot, where the request
# cannot come from the loopback address. Locking them would strand a device
# with no password set and no working network.
_HOST_ACTION_PATHS = frozenset({
    "/setup/deployment/to-satellite", "/setup/deployment/to-hosted",
    "/setup/network/hostname", "/setup/touch/provision",
    "/setup/maintenance/reboot", "/setup/maintenance/reload",
    "/setup/kiosk/install", "/setup/kiosk/restart",
    "/setup/streamdeck/install", "/setup/streamdeck/restart",
    "/setup/update", "/setup/update-server", "/setup/restore",
    "/setup/grocy/repair", "/setup/cloud/backup/restore",
    "/setup/tunnel/enable", "/setup/tunnel/disable",
    "/setup/ap/disable",
    # GET hands back the recovery hotspot's password, POST rewrites it, so this
    # one is closed for reads too.
    "/setup/ap/config",
    "/setup/calibrate/touch/reset", "/setup/calibrate/touch/apply",
})
# Same rule, but only for requests that change something: the matching read is
# what the Display settings card needs to draw itself.
_HOST_ACTION_WRITE_PATHS = frozenset({"/setup/display/rotation"})

# A second screen keeps no data of its own: these routes forward to the main
# server carrying that server's API key, which is full-access, so an
# unauthenticated caller on the network would be acting on the main server
# through this device. Only closed when the device really does forward; a
# standalone install answers these locally and is untouched.
_UPSTREAM_FORWARD_PREFIXES = ("/pending", "/audit", "/current-recipe",
                              "/timers", "/action-items")


_CAMERA_FEED_RE = re.compile(r"^/ui/camera/[0-9]+/(snapshot|stream)$")


def _gate_applies(path: str, method: str, snap) -> bool:
    """Whether this route stays closed to anonymous callers on a satellite with
    no password (a kiosk session, an agent, an allowed key and legacy loopback
    still pass; services/access.py decides)."""
    if not snap.is_satellite:
        return False
    if path in _HOST_ACTION_PATHS:
        return True
    if method not in ("GET", "HEAD") and path in _HOST_ACTION_WRITE_PATHS:
        return True
    if any(path == p or path.startswith(p + "/") for p in _UPSTREAM_FORWARD_PREFIXES):
        return bool(snap.upstream_linked)
    # Recipe, gadget and print forwards act on the main server: closed when
    # the server itself has a password (an open server loses nothing here).
    if snap.server_has_password is True and _satellite_recipe_upstream(path):
        return True
    # Camera feeds the main server relays: closed unless the server is known
    # to have no password.
    if (_CAMERA_FEED_RE.match(path) and snap.camera_relay_active
            and snap.server_has_password is not False):
        return True
    return False


def _is_static(path: str) -> bool:
    return path.startswith("/static/")


# Satellite forwarding for the native recipe library (FoodAssistant-g0fd).
# Recipes and the meal plan live in the MAIN SERVER's database when the fleet
# runs Pantry Raider's own recipe store, the same way pending scans do. A
# satellite therefore forwards every /mealie and /recipes call upstream instead
# of answering from its own (empty) tables. Only active in native mode: an
# install whose library still runs in Mealie keeps today's path, where the
# local MealieClient reaches the server's Mealie through the /api/proxy hop.
# Runs after the auth step of the request gate, so local auth still applies
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
    if not (settings.is_satellite() and settings.upstream_linked()):
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
        # A NeoKey press lands here as its slot's action string. It must be
        # answered by THIS device because locality differs per action (the
        # kiosk jump is local, the mode and the shared timers are the
        # server's), so the handler forwards the fleet-owned pieces itself,
        # the same way POST /pending/scanner-mode does (FoodAssistant-bo9v).
        if path == "/gadgets/key-action":
            return None
        # The plug-in accessory registry (STEMMA QT / Qwiic) is device-local
        # by design (services/stemma.py): a board is plugged into ONE device,
        # so adding, editing, removing, and key-testing one are answered by
        # THIS device and never touch the server's registry
        # (FoodAssistant-t0vp5). The state poll is answered here too: the
        # route fetches the server's fleet snapshot itself (satellite_upstream_get)
        # and lays this device's own accessory section over it, so the pane
        # on the device holding the pad shows that pad.
        if path == "/gadgets/stemma" or path.startswith("/gadgets/stemma/"):
            return None
        if path == "/gadgets/state":
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


async def satellite_upstream_get(path: str) -> dict | None:
    """The main server's JSON answer to GET ``path``, for a route that a
    satellite answers LOCALLY but builds on the fleet picture
    (GET /gadgets/state, FoodAssistant-t0vp5). Same client, key, and
    micro-cache as the forwarding middleware. None when this device is not a
    satellite, the server does not answer, or the answer is not a JSON
    object, so the caller falls back to what it knows locally."""
    if not (settings.is_satellite() and settings.upstream_linked()):
        return None
    fwd_cache = _FWD_CACHE_PATHS.get(path)
    hit = fwd_cache.get() if fwd_cache is not None else None
    if hit is not None:
        content = hit[0]
    else:
        global _satellite_fwd_client
        if _satellite_fwd_client is None:
            import httpx
            _satellite_fwd_client = httpx.AsyncClient(timeout=30.0)
        up = await upstream.send(
            _satellite_fwd_client, "GET",
            f"{settings.remote_server_url.rstrip('/')}{path}",
            params={}, content=None)
        if up.status_code != 200:
            return None
        content = up.body
        if fwd_cache is not None:
            fwd_cache.set((up.body, up.status_code, up.media_type))
    import json as _json
    try:
        data = _json.loads(content)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


# The forwarded-path guard lives with the forwarding helper (services/upstream.py).
_forward_path_ok = upstream.path_ok
_forward_url = upstream.path_url
_FORWARD_BAD_PATH_MSG = upstream.BAD_PATH_MSG


async def forward_native_recipes(request: Request):
    """The main server's answer when a satellite forwards this request, else
    None. The last step of the request gate (_RequestGate below)."""
    base = _satellite_recipe_upstream(request.url.path)
    if not base:
        return None
    if not _forward_path_ok(request.url.path):
        return JSONResponse({"detail": _FORWARD_BAD_PATH_MSG}, status_code=400)
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
    params = dict(request.query_params)
    if request.url.path == "/gadgets/outputs":
        # The whole /gadgets family forwards, so a Bandit's key LEDs are drawn
        # by the server. Everything they show is fleet state (the mode, the
        # timers, the alarms) except one local fact: whether the Manage screen
        # is open on THIS device, which is what brings the mode colors in. The
        # server cannot know that about a satellite, so the answer rides along
        # with the request instead of the keys never leaving pink.
        # The colors go with it for the same reason: the pad is plugged into
        # THIS device, so its look is chosen here, in this device's settings.
        # Without them the server would answer with its own colors and the
        # pickers on a satellite would appear to do nothing.
        from .services import stemma as _stemma
        params["on_manage"] = "1" if _stemma.manage_open() else "0"
        params["rest_color"] = settings.neokey_rest_color
        params["timer_color"] = settings.neokey_timer_color
    resp = await upstream.forward(
        _satellite_fwd_client, request, _forward_url(base, request.url.path),
        params=params, read_get_body=True,
        unreachable_detail=upstream.UNREACHABLE_RETURN)
    if fwd_cache is not None and resp.status_code == 200:
        fwd_cache.set((resp.body, resp.status_code, resp.media_type))
    return resp


# The kitchen screen's sign-in, the setup code and the sign-in page: exempt
# from the setup redirect and the kiosk PIN, because each answers for itself.
# The sign-in page has to answer before setup completes once a password is
# set, since the wizard then needs a sign-in (with no password it sends the
# browser on to the wizard by itself).
_ACCESS_SETUP_EXEMPT = frozenset({"/ui/kiosk/launch", "/ui/kiosk/relaunch", "/ui/setup-code",
                                  "/ui/login"})


async def redirect_if_unconfigured(request: Request):
    """Send new installs to /setup until Grocy + vision provider are configured,
    and hold a brand-new Pi appliance on the first-boot progress page until its
    co-hosted inventory is actually connected. None lets the request on."""
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
            # The kitchen screen's sign-in and the setup code answer before
            # setup completes (the screen then lands on /setup?kiosk=1), and
            # the fleet relays answer with their own JSON.
            and request.url.path not in _ACCESS_SETUP_EXEMPT
            and not request.url.path.startswith("/api/fleet/")
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
    return None


# Fire-and-forget tasks the auth middleware starts (the screen's relaunch),
# held here so the event loop does not drop them before they finish.
_BACKGROUND: set = set()

_READER_PATHS = frozenset({"/gadgets/config", "/gadgets/outputs", "/cub/summary",
                           "/gadgets/readings", "/gadgets/key-action",
                           "/pending/scanner-mode", "/pending/scan"})


def _peer(request: Request) -> str:
    return access.unwrap_peer(request.client.host if request.client else None) or ""


def _legacy_class(request: Request, path: str) -> str:
    """Which kind of local caller relied on the old trust for programs on this device."""
    headers = request.headers
    own = access.own_loopback_names()
    for name in ("origin", "referer"):
        value = headers.get(name)
        if not value:
            continue
        host = access.host_name(value)
        if value.strip().lower() == "null" or (
                host and host not in ("localhost", "127.0.0.1", "::1") and host not in own):
            return "same_host_proxy"
    ua = headers.get("user-agent", "")
    client = access.parse_client(headers)
    if "Mozilla" in ua:
        return "browser"
    if client.startswith("streamdeck/") or (
            ua.startswith("python-httpx/") and not local_agents.deck_proven()):
        return "deck"
    if client.startswith("gadgets/") or (
            path in _READER_PATHS and not local_agents.gadgets_proven()):
        return "reader"
    return "other"


def _record_legacy(request: Request, path: str) -> None:
    try:
        auth_state.record_legacy_use(_legacy_class(request, path),
                                     request.headers.get("user-agent", ""), path)
    except Exception:
        pass


def _resolve_principal(request: Request, origin: str, snap, path: str) -> access.Principal:
    """One principal per request: a key, an agent, a session, the kitchen
    screen, the old local trust, or nobody."""
    headers = request.headers
    client = access.parse_client(headers)
    caps = access.parse_caps(headers)
    key = headers.get("x-api-key", "")
    bearer = False
    if not key and path.startswith("/api/proxy/ha/"):
        auth = headers.get("authorization", "")
        if auth[:7].lower() == "bearer ":
            key = auth[7:].strip()
            bearer = True
    bad = False
    if key:
        if key.startswith("pra1."):
            agent = local_agents.match_agent_key(key) if origin == "plain_loopback" else None
            if agent:
                return access.Principal("agent", "deck" if agent == "streamdeck" else "reader",
                                        "agent:" + agent, client, caps, origin)
        else:
            rec = credentials.lookup(key)
            if rec:
                return access.Principal("key", rec.get("scope"), rec.get("id"), client, caps,
                                        origin)
        bad = True
        if bearer:
            # A Home Assistant token sent to the proxy is not one of our keys.
            bad = False
        elif origin == "plain_loopback" and snap.legacy_trust_active:
            # Before 0.21 loopback was trusted before the key was read, so a
            # stale key from a program on this device is noted, not refused.
            bad = False
            _record_legacy(request, path)
    session = request.session
    if (session.get("authed") and session.get("role") != "kiosk"
            and not auth_state.session_is_current(session)):
        # Opened before a password or two-factor change, or before the owner
        # signed out everywhere: no longer a sign-in.
        for k in ("authed", "role", "session_gen", "totp_pending", "pending_role"):
            session.pop(k, None)
    if session.get("authed") and not session.get("totp_pending"):
        role = session.get("role")
        if role == "kiosk":
            if origin == "plain_loopback" and session.get("kiosk_gen") == auth_state.kiosk_gen():
                return access.Principal("kiosk", "kiosk", None, client, caps, origin)
        elif role == "viewer":
            return access.Principal("session", "viewer", None, client, caps, origin)
        else:
            return access.Principal("session", "admin", None, client, caps, origin)
    if origin == "plain_loopback" and snap.legacy_trust_active:
        return access.Principal("legacy_loopback", "admin", None, client, caps, origin)
    return access.ANONYMOUS(origin, client=client, caps=caps, bad_key=bad)


def _every_request(request: Request) -> None:
    """Cheap checks every request runs: a same-host proxy, and a first password."""
    try:
        signal = access.same_host_proxy_signal(request)
        if signal and auth_state.record_same_host_proxy(signal):
            episode = (auth_state.load().get("same_host_proxy") or {}).get("episode") or ""
            access.raise_item("same_host_proxy", access.SAME_HOST_PROXY_ITEM_TITLE,
                              access.SAME_HOST_PROXY_ITEM_BODY, f"same_host_proxy:{episode}",
                              level="warning")
    except Exception:
        pass
    access.note_password_state_and_notify()


def _local_date(iso_value) -> str:
    ts = auth_state.parse_ts(iso_value)
    if ts is None:
        return ""
    from datetime import datetime
    return datetime.fromtimestamp(ts).date().isoformat()


def _record_keyless(request: Request, family: str) -> None:
    ua = request.headers.get("user-agent", "")
    peer = _peer(request)
    try:
        if family in access.FAMILIES:
            auth_state.record_keyless_family(family, ua, peer)
        if ua.startswith("HomeAssistant/") and not request.headers.get("x-pr-client"):
            auth_state.record_keyless_ha(peer)
        if ua.lower().startswith("esphome") and family == "kitchen":
            auth_state.record_keyless_esp(peer)
    except Exception:
        pass


def _key_label(request: Request, p) -> tuple[str, str]:
    name = credentials.display_name(p.credential_id or "")
    client = p.client or (request.headers.get("user-agent", "").split() or ["an unknown program"])[0]
    return name, client[:64]


def _apply_notes(request: Request, d, p, family: str, path: str, snap) -> None:
    """Side effects of a decision: counters, sightings, inbox items. Never raises."""
    from datetime import date
    for note in d.notes:
        try:
            if note.startswith("setup_code:"):
                setup_code_svc.ensure(note.split(":", 1)[1])
            elif note.startswith("sighting:"):
                kind = note.split(":", 1)[1]
                host = access.sighting_host(request)
                frm = access.first_forwarded_ip(request) or _peer(request)
                if auth_state.record_open_sighting(host, kind, frm):
                    until = snap.grace_until or ""
                    day = _local_date(until)
                    if kind == "name":
                        access.raise_item(
                            "open_mode_name", access.NAME_ITEM_TITLE,
                            access.NAME_ITEM_BODY.format(name=host, date=day),
                            f"open_mode_name:{until}", level="warning")
                    else:
                        access.raise_item(
                            "open_mode_grace", access.GRACE_ITEM_TITLE.format(date=day),
                            access.GRACE_ITEM_BODY.format(host=host, date=day),
                            f"open_mode_grace:{until}", level="warning")
            elif note == "keyless":
                _record_keyless(request, family)
            elif note == "gateway_check":
                peer = _peer(request)
                # In the Home Assistant add-on, Home Assistant itself calls
                # from the add-on network's gateway, so that is expected there.
                if (peer and peer in lan.default_gateways() and lan.in_docker_bridge()
                        and not access.addon_runtime()
                        and access.gateway_peer_is_unclear(request.headers.get("host", ""))):
                    if auth_state.record_gateway_peer():
                        access.raise_item(
                            "open_gateway_peer", "Some requests come through Docker's gateway",
                            access.gateway_hint(),
                            f"open_gateway_peer:{auth_state.gateway_episode() or ''}")
            elif note == "legacy_use":
                _record_legacy(request, path)
            elif note == "key_admin_refused":
                credentials.record_refusal(p.credential_id or "", path)
                name, client = _key_label(request, p)
                access.raise_item_once(
                    "key_admin_refused", access.KEY_ADMIN_ITEM_TITLE,
                    access.KEY_ADMIN_ITEM_BODY.format(name=name, client=client),
                    f"key_admin_refused:{p.credential_id}:{date.today().isoformat()}")
            elif note == "key_admin_loopback":
                name, client = _key_label(request, p)
                access.raise_item_once(
                    "key_admin_loopback", access.KEY_ADMIN_LOOPBACK_ITEM_TITLE,
                    access.KEY_ADMIN_LOOPBACK_ITEM_BODY.format(name=name, client=client),
                    f"key_admin_loopback:{p.credential_id}:{date.today().isoformat()}")
            elif note == "relaunch":
                if (p.origin == "plain_loopback"
                        and "Mozilla" in request.headers.get("user-agent", "")
                        and not snap.legacy_trust_active
                        and local_agents.kiosk_installed() is True):
                    task = asyncio.get_running_loop().create_task(
                        local_agents.request_kiosk_relaunch("refused"))
                    _BACKGROUND.add(task)
                    task.add_done_callback(_BACKGROUND.discard)
        except Exception:
            log.debug("access note %s failed", note, exc_info=True)


def _after_serve(request: Request, p, family: str, path: str, method: str) -> None:
    try:
        headers = request.headers
        if p.kind == "key" and p.credential_id and not getattr(request.state, "pr_skip_use", False):
            cub_id = headers.get("x-cub-id", "")
            credentials.record_use(p.credential_id, {
                "ip": _peer(request), "client": p.client,
                "ua": headers.get("user-agent", ""), "family": family,
                "method": method, "path": path,
                "device_id": headers.get("x-device-id", "") or cub_id,
                "device_mode": headers.get("x-device-mode", ""), "cub": bool(cub_id),
            })
        elif p.kind == "agent" and p.credential_id:
            agent = p.credential_id.split(":", 1)[1]
            version = p.client.split("/", 1)[1] if "/" in p.client else ""
            host = headers.get("host", "")
            origin_url = f"{request.url.scheme}://{host}" if host else ""
            local_agents.record_agent_use(agent, p.caps, version, origin_url)
    except Exception:
        log.debug("recording a served request failed", exc_info=True)


def _wants_html(request: Request) -> bool:
    return request.method in ("GET", "HEAD") and "text/html" in request.headers.get("accept", "")


_PAGE_STEPS = {"setup_code_page": ("setup_code", "setup"),
               "open_refused_page": ("open_refused", "open"),
               "name_refused_page": ("name_refused", "open")}


def _respond(request: Request, d):
    if d.action == "redirect_login":
        return ingress_redirect(request, "/ui/login")
    if d.action == "redirect_home":
        return ingress_redirect(request, d.target or "/ui/")
    if d.action in _PAGE_STEPS and _wants_html(request):
        step, mode = _PAGE_STEPS[d.action]
        try:
            return setup_code_router.render_page(request, step, d.status or 403, mode=mode)
        except Exception:
            log.debug("refusal page failed to render", exc_info=True)
    body = {"detail": d.detail}
    if d.code:
        body["code"] = d.code
    return JSONResponse(body, status_code=d.status or 403)


def require_auth(request: Request):
    """One principal per request, then one decision (services/access.py).

    Browsers sign in at /ui/login; headless clients send X-API-Key (a key
    minted in Settings, Security, or an older full-access key); this device's
    Stream Deck and sensor reader sign in with agent keys and its screen with
    a launch ticket. An install with no password answers its home network
    and, for 14 days after the update, visitors through a proxy too.

    Returns (refusal, None) to stop the request here, or (None, served) to let
    it on, where served() records the use once the response starts."""
    path = request.url.path
    method = request.method.upper()
    origin = access.classify_origin(request)
    if _is_static(path):
        request.state.principal = access.ANONYMOUS(origin)
        return None, None
    snap = access.snapshot(request)
    p = _resolve_principal(request, origin, snap, path)
    request.state.principal = p
    family = access.route_family(method, path)
    # A Bandit Cub checking for an update cannot present a key: the stock
    # ESPHome update component sends no headers, so its manifest request would
    # 401 on every install with a password. The manifest hands out a board
    # name and a version, so it alone is open to the local network, the same
    # shape as the pairing endpoints. The firmware images are not:
    # firmware 0.19 and later never downloads its over-the-air image from this
    # server (it takes the published GitHub release), and an older Cub is
    # refused one by the route itself, so the gated image path now only serves
    # installs with no password and the browser flasher, which has a session.
    if (family != "public" and method in ("GET", "HEAD")
            and request.url.path == "/cub/firmware/manifest.json"
            and pairing_svc.is_local_network_request(request)):
        family = "public"
    _every_request(request)
    host = request.headers.get("host", "")
    host_ok = access.home_host_ok(request) if origin == "lan_direct" else True
    origin_ok = access.origin_header_ok(request.headers, host) if origin == "lan_direct" else True
    gated = _gate_applies(path, method, snap)
    d = access.decide(p, method, path, family, snap, host_ok=host_ok, origin_ok=origin_ok,
                      gated=gated, setup_bypass=path in _SETUP_BYPASS)
    if d.notes:
        _apply_notes(request, d, p, family, path, snap)
    if d.action in ("serve", "serve_grace"):
        if p.kind == "kiosk":
            # The session cookie is only re-sent when the session changes, so
            # a daily stamp keeps the screen's 30-day cookie fresh.
            from datetime import date
            today = date.today().isoformat()
            if request.session.get("kiosk_day") != today:
                request.session["kiosk_day"] = today
        return None, lambda: _after_serve(request, p, family, path, method)
    return _respond(request, d), None


def require_pin(request: Request):
    """Optional numeric PIN gate for the kiosk UI on a satellite. It only guards
    the browser UI (/ui and the root redirect), leaving /setup reachable so the
    PIN can be changed or cleared without SSH. The unlock screen lives at
    /ui/pin and stores a session flag once the code matches.

    When kiosk_readonly_when_locked is True, unauthenticated GET requests are
    allowed through (read-only browsing), while write methods (POST/PUT/PATCH/
    DELETE) from unauthenticated users are rejected with 403. None lets the
    request on."""
    if not settings.pin_lock_active():
        return None
    path = request.url.path
    if not (path == "/" or path.startswith("/ui")):
        return None
    if (path in ("/ui/pin", "/ui/pin/verify", "/ui/login") or _is_static(path)
            or path in _ACCESS_SETUP_EXEMPT):
        return None
    if request.session.get("pin_ok"):
        return None
    if settings.kiosk_readonly_when_locked:
        if request.method == "GET":
            request.state.pin_readonly = True
            return None
        return JSONResponse({"detail": "Locked"}, status_code=403)
    return ingress_redirect(request, "/ui/pin")


def enforce_demo_read_only(request: Request):
    """Read-only DEMO MODE gate (FoodAssistant-pxp0).

    A no-op unless settings.demo_mode is on. When on, every state-changing
    request (POST/PUT/PATCH/DELETE) is refused except a tiny session/display-only
    allowlist, so a public demo instance is fully navigable but never mutated.
    Consuming stock, scanning, saving settings, backup/restore, and any
    Grocy/Mealie write are all blocked before they reach a route, so nothing is
    written to the demo's databases.

    A navigation request (one that accepts HTML) is bounced back to where it came
    from with a friendly flash; an API/JSON caller gets a 403 with a stable
    machine-readable body so the front-end can react cleanly. None lets the
    request on.
    """
    if not settings.demo_mode:
        return None
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
            # Exactly one leading slash: a path like //evil.com/x (or /\evil.com)
            # is read by the browser as another site, which would turn this
            # into an open redirect.
            if (parts.path.startswith("/") and parts.path[1:2] not in ("/", "\\")
                    and "\\" not in parts.path):
                # Referer is same-origin and already carries any ingress prefix,
                # so redirect to its path verbatim without re-prefixing.
                target = urlunsplit(("", "", parts.path, urlencode(query), ""))
                return RedirectResponse(target, status_code=303)
            return ingress_redirect(request, "/ui/?" + urlencode(query))
        # "detail" carries the same sentence because that is the field the
        # page scripts already show in their error toasts; without it a
        # refused action read "Could not mark Milk opened:" with no reason.
        # The header lets demo-banner.js spot a refusal without reading the
        # body, so actions with no error message of their own still answer.
        return JSONResponse(
            {"error": demo.DEMO_ERROR_CODE, "message": demo.DEMO_MESSAGE,
             "detail": demo.DEMO_MESSAGE},
            status_code=403,
            headers={"X-Demo-Read-Only": "1"},
        )
    return None


# Conservative security headers on every response the gate below answers or
# lets through (security audit, Jul 2026). X-Frame-Options blocks clickjacking
# of the kiosk, nosniff stops MIME sniffing, and a same-origin referrer policy
# avoids leaking paths. Each is only added when the route did not set its own.
#
# Content-Security-Policy (FoodAssistant-rnnn). Everything the pages load is
# served by this app. Script is 'self' only: no inline <script> block and no
# on*= handler attribute runs, so injected markup cannot run code. Pages keep
# their code in static/js and pass server data in <script
# type="application/json"> blocks (data, never executed); controls name what
# they run with data-on-* attributes, read by static/js/lib/dom.js
# (FoodAssistant-1099g), and tests/test_csp_inline_script.py keeps it that
# way. Two deliberate loosenings remain: inline style is allowed (the
# templates lean on style blocks and attributes throughout), and img-src
# additionally allows any http(s) host plus data:/blob: because the photo
# screensaver's "web addresses" source hands raw external image links straight
# to the browser, the QR/label previews use data: URIs, and the label designer
# uses blob:. worker-src covers the barcode scanner's blob worker.
# Validated by driving every major page in a kiosk-emulating Chromium and
# asserting zero violations.
_SECURITY_HEADERS = (
    ("X-Frame-Options", "SAMEORIGIN"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "same-origin"),
    ("Content-Security-Policy",
     "default-src 'self'; "
     "script-src 'self'; "
     "style-src 'self' 'unsafe-inline'; "
     "img-src 'self' data: blob: https: http:; "
     "media-src 'self' blob:; "
     "connect-src 'self'; "
     "worker-src 'self' blob:; "
     "object-src 'none'; "
     "base-uri 'self'; "
     "form-action 'self'; "
     "frame-ancestors 'self'"),
)


class _RequestGate:
    """Every request-gating check, in one plain ASGI layer inside the session.

    These used to be six stacked @app.middleware("http") layers, each with its
    own task and body-stream copy per request. The steps run in the same order
    those layers did (outermost first), and the first one that answers ends
    the request:

    1. security headers, added to whatever response leaves this layer
    2. enforce_demo_read_only
    3. require_pin
    4. require_auth (records a served request when its response starts)
    5. redirect_if_unconfigured
    6. forward_native_recipes (a satellite's forwarded families)

    Plain ASGI rather than BaseHTTPMiddleware: the steps only need the
    request's headers, session and state, which a Request over the scope gives
    them, and the response, streamed or not, passes through untouched apart
    from its header block.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        send = _send_with(send, _add_security_headers)
        response = enforce_demo_read_only(request)
        if response is None:
            response = require_pin(request)
        if response is None:
            response, served = require_auth(request)
            if served is not None:
                send = _send_with(send, lambda _headers: served())
        if response is None:
            response = await redirect_if_unconfigured(request)
        if response is None:
            response = await forward_native_recipes(request)
        if response is None:
            await self.app(scope, receive, send)
        else:
            await response(scope, receive, send)


def _send_with(send, on_start):
    """send, with on_start(headers) run on the response's header block first."""
    async def wrapped(message):
        if message["type"] == "http.response.start":
            on_start(MutableHeaders(scope=message))
        await send(message)
    return wrapped


def _add_security_headers(headers: MutableHeaders) -> None:
    for name, value in _SECURITY_HEADERS:
        headers.setdefault(name, value)


# Session cookie hardening (FoodAssistant-g6ai): when the request itself arrived
# over TLS, mark the session Set-Cookie Secure so the browser never replays it
# over plain HTTP. Done in _EdgeMiddleware, outside SessionMiddleware, because
# the cookie is only visible outside the session layer. Per-request rather than
# SessionMiddleware's static https_only flag because the same device is
# legitimately reached both ways: HTTP on the trusted LAN (where Secure would
# break login) and HTTPS via direct TLS. The Forager tunnel terminates TLS
# upstream, so those requests reach the app as HTTP and are unchanged here;
# this hardens direct-TLS deployments without touching LAN behaviour.
def _secure_session_cookie(headers: MutableHeaders) -> None:
    cookies = headers.getlist("set-cookie")
    if any(c.lower().startswith("session=") and "secure" not in c.lower()
           for c in cookies):
        del headers["set-cookie"]
        for c in cookies:
            if c.lower().startswith("session=") and "secure" not in c.lower():
                c = c + "; Secure"
            headers.append("set-cookie", c)


# Requests that change nothing, so the cross-site check below leaves them alone.
_CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Response compression (FoodAssistant-7dt9). Rendered pages ship 40-130 KB of
# HTML per kiosk navigation and are deliberately never cached (network-first),
# so gzip cuts every page load and JSON poll by 75-85 percent, which is most of
# what a Pi kiosk actually waits on. Image-ish paths are EXCLUDED: recompressing
# a JPEG camera stream or a recipe photo wastes Pi CPU for zero size win, so
# _EdgeMiddleware strips the accept-encoding header on those before GZip looks.
from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402

_NO_GZIP_PREFIXES = ("/ui/camera", "/recipes/images", "/ui/screensaver")

_CROSS_SITE_DETAIL = ("That request came from another site, so it was "
                      "refused. Open Pantry Raider directly and try again.")


class _EdgeMiddleware:
    """The outermost layer, outside compression and the session.

    1. Refuse a state-changing request that the browser says began on another
       site (launch audit, Sep 2026). The session cookie is SameSite=Lax, which
       already stops cross-site form posts in current browsers; this is the
       second lock for anything older or misconfigured. Sec-Fetch-Site is
       written by the browser and cannot be set from a page, so a POST that
       declares itself cross-site or same-site is not one of this app's own
       pages talking to itself. The header is absent on every non-browser
       client (Home Assistant, the Stream Deck, satellite sync, the sensor
       reader, a Bandit Cub, curl), so those are untouched. Origin equality is
       deliberately NOT checked: behind Home Assistant Ingress and other
       reverse proxies the browser's Origin is the proxy host, and comparing
       it against the app's own address would reject every proxied install.
    2. Over TLS, mark the session cookie Secure (_secure_session_cookie).
    3. On image-ish paths, drop accept-encoding so GZip passes them through.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if request.method not in _CSRF_SAFE_METHODS:
            if request.headers.get("sec-fetch-site", "") in ("cross-site", "same-site"):
                response = JSONResponse({"detail": _CROSS_SITE_DETAIL}, status_code=403)
                await response(scope, receive, send)
                return
        if request.url.scheme == "https":
            send = _send_with(send, _secure_session_cookie)
        if request.url.path.startswith(_NO_GZIP_PREFIXES):
            scope["headers"] = [
                (k, v) for k, v in scope["headers"] if k != b"accept-encoding"
            ]
        await self.app(scope, receive, send)


# Registration order: Starlette runs the last-added middleware outermost, so a
# request passes _EdgeMiddleware, then GZip, then the session, then the gate.
# The gate must sit inside SessionMiddleware (it reads request.session), and
# the edge must sit outside both (it rewrites the session's Set-Cookie and the
# accept-encoding GZip reads).
app.add_middleware(_RequestGate)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key,
                   max_age=60 * 60 * 24 * 30, same_site="lax")
# compresslevel 6, not the library default of 9: level 9 spends over twice the
# CPU of level 6 for under half a percent of extra shrink (measured on the
# rendered Manage Pantry page: 28,149 vs 28,071 bytes, 2.4ms vs 5.3ms on a dev
# box, several times that on a Pi 4). Compression runs on every HTML and JSON
# response, so this is pure response-time win at identical behavior.
app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=6)
app.add_middleware(_EdgeMiddleware)


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
app.include_router(proxy.admin_router)
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
app.include_router(kiosk_status.router)
app.include_router(receipt.router)
app.include_router(credentials_router.router)
app.include_router(kiosk_launch.router)
app.include_router(setup_code_router.router)
app.include_router(security_router.router)


@app.get("/")
async def root(request: Request):
    return ingress_redirect(request, "/ui/")


# Progressive Web App (FoodAssistant-fd3z). Both files live under static/pwa but
# are served from the site ROOT so the manifest's scope is "/" and the service
# worker can control every page (a worker's scope can never rise above the path
# it is served from). Public in the auth middleware above, so an install can be
# started from the login/setup screen. The worker itself is conservative: live
# data and auth always win because it is network-first for navigations and API
# calls (see static/pwa/sw.js).
_PWA_DIR = Path(__file__).parent / "static" / "pwa"


@app.get("/manifest.webmanifest")
async def web_manifest(request: Request):
    from fastapi.responses import FileResponse
    from .ingress import ingress_path, prefix_manifest
    prefix = ingress_path(request)
    if prefix:
        # Under Home Assistant Ingress the manifest's root paths would point
        # at the Home Assistant host, so they are served with the prefix.
        import json as _json
        raw = _json.loads((_PWA_DIR / "manifest.webmanifest").read_text("utf-8"))
        return JSONResponse(prefix_manifest(raw, prefix),
                            media_type="application/manifest+json",
                            headers={"Cache-Control": "no-cache",
                                     "Vary": "X-Ingress-Path"})
    return FileResponse(
        _PWA_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=86400",
                 "Vary": "X-Ingress-Path"},
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


def _health_detail_allowed(request: Request) -> bool:
    """Whether this caller may see WHY a backend is unhealthy.

    /health itself stays public so a LAN scan, the fleet dashboard and an
    uptime check can all see that an instance is alive. The error detail is
    different: it quotes the backend's own message, which can name config
    files and paths, and /health is reachable through the tunnel. So once a
    password is set, the detail is kept for the people who run the install:
    the device's own screen (loopback), an API-key client, and a signed-in
    session (any signed-in principal). Settings shows the same detail behind
    auth.
    """
    if not settings.auth_password:
        return True
    return access.principal(request).kind != "anonymous"


# The upstream half of /health (the AI provider and Grocy answers) is held for
# this long. Kiosks, the Docker healthcheck, LAN scans and uptime monitors all
# poll /health, and without this every poll was a live call to both backends.
_HEALTH_UPSTREAM_TTL = 30.0
_health_upstream = TTLCache(_HEALTH_UPSTREAM_TTL)


def _health_upstream_key() -> tuple:
    """What the cached answer was measured against. A settings change that
    points at a different backend misses the cache instead of reporting the
    old backend's state for up to the TTL."""
    return (settings.deployment_mode, settings.ai_configured(),
            settings.vision_provider, settings.provider_key(settings.vision_provider),
            settings.grocy_base_url, settings.grocy_api_key,
            settings.remote_server_url)


def _health_clients():
    """Build the provider and Grocy client. Run in a worker thread: the first
    provider build imports an SDK and sets up TLS, which is slow, blocking work
    that must never sit on the event loop."""
    from . import dependencies
    from .services.grocy import GrocyClient
    return dependencies.get_vision_provider(), GrocyClient()


async def _check_upstreams() -> dict:
    """The provider and Grocy answers for /health, at most one live round per
    _HEALTH_UPSTREAM_TTL seconds. The blocking setup runs in a worker thread;
    the checks themselves are async network calls, run side by side."""
    key = _health_upstream_key()
    cached = _health_upstream.get()
    if cached is not None and cached["key"] == key:
        return cached
    provider, grocy = await asyncio.to_thread(_health_clients)

    async def vision() -> str:
        if not settings.ai_configured():
            return "not configured"
        return "ok" if await provider.health_check() else "error"

    vision_status, grocy_ok = await asyncio.gather(vision(), grocy.health_check())
    answer = {
        "key": key,
        "vision": vision_status,
        "grocy_ok": bool(grocy_ok),
        "grocy_detail": (getattr(grocy, "last_error", "")
                         or "Grocy did not answer its status check."),
        "grocy_hint": getattr(grocy, "last_hint", ""),
    }
    _health_upstream.set(answer)
    return answer


@app.get("/health")
async def health(request: Request):
    if not settings.is_configured():
        return {**_FINGERPRINT, "status": "unconfigured", "setup": "/setup",
                "mode": settings.deployment_mode, "device_id": settings.device_id}
    upstreams = await _check_upstreams()
    vision_status = upstreams["vision"]
    grocy_ok = upstreams["grocy_ok"]
    result = {
        **_FINGERPRINT,
        "status": "ok",
        "mode": settings.deployment_mode,
        # Lets a LAN scan recognise and skip this very server when it answers its
        # own probe through a Docker gateway (Pantry Raider).
        "device_id": settings.device_id,
        "vision_provider": vision_status,
        "grocy": "ok" if grocy_ok else "error",
    }
    if not grocy_ok and _health_detail_allowed(request):
        # Additive: why Grocy is in error (a broken config.php after an image
        # update, an unreachable host), so support can read the cause here
        # instead of over SSH. Absent when Grocy is fine.
        result["grocy_detail"] = upstreams["grocy_detail"]
        hint = upstreams["grocy_hint"]
        if hint:
            result["grocy_hint"] = hint
    # A column backfill that did not apply leaves the app running on an older
    # table, so it has to be visible somewhere. A count only: the exception and
    # the data_dir path go to the log, never to this public endpoint.
    from .database import schema_failures
    missing = schema_failures()
    if missing:
        result["schema"] = "error"
        result["schema_columns_pending"] = missing
    return result
