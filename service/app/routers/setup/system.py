"""Maintenance actions, service status, install logs, hardware and system
health, resource use, and the status summary."""

import asyncio
import json
from fastapi.responses import JSONResponse
from fastapi.concurrency import run_in_threadpool

from ...config import settings
from ...database import SessionLocal
from ...dependencies import reset_providers
from ...hardware import is_raspberry_pi
from ...services.bridge import bridge_client
from ...services import status_summary
from ._common import router, TestGrocyPayload, TestMealiePayload, _HOST_BRIDGE
from .page import _detect_local_grocy
from .cloud import cloud_status
from .remote_access import tunnel_status
from .connections import test_grocy, test_mealie
from .display import network_status


@router.post("/maintenance/reboot")
async def maintenance_reboot():
    """Reboot the appliance now via the host bridge (Pi appliance only)."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    try:
        async with bridge_client(timeout=15.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/reboot")
        if r.status_code != 200:
            # An out-of-date bridge answers 404 {"error": "not found"} for a
            # route it predates; surface something actionable instead of the
            # bare bridge body (FoodAssistant-pnz4).
            return JSONResponse({"ok": False, "error":
                "The device helper does not support this yet; run Update, then try again."})
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.post("/maintenance/reload")
async def maintenance_reload():
    """Re-read settings.json and reset provider/recipe caches without a restart.

    Works on any platform: it picks up an out-of-band settings change (a restore
    or hand edit) and rebuilds the cached AI provider and Mealie clients so the
    new values take effect immediately (FoodAssistant-wvwm)."""
    applied = settings.reload()
    reset_providers()
    from ...services.recipe_source import reset_mealie_cache
    from ...services.recipe_match import reset_staple_cache
    reset_mealie_cache()
    reset_staple_cache()
    return {"ok": True, "reloaded": len(applied)}


@router.post("/streamdeck/restart")
async def streamdeck_restart():
    """Restart the Stream Deck systemd service (Pi appliance only)."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    try:
        async with bridge_client(timeout=15.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/streamdeck/restart")
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.post("/kiosk/restart")
async def kiosk_restart():
    """Restart the kiosk browser so display scale/rotation changes apply."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    try:
        async with bridge_client(timeout=35.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/kiosk/restart")
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.post("/mealie/start")
async def mealie_start():
    """Kick off the Mealie container start on a Pi appliance via the host bridge.

    The bridge runs the image pull/up in the background and returns at once, so
    a short timeout is enough; the web UI polls /mealie/status for progress.
    """
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    try:
        async with bridge_client(timeout=10.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/mealie/start")
        result = r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})
    # Zero-touch first run (FoodAssistant-syxf): once the just-started Mealie
    # serves HTTP, connect it automatically (API token, secured sign-in,
    # default shopping list). Background task with patient polling, since the
    # first start downloads the image; it stops on its own if Mealie turns out
    # to be configured already. On the appliance the app reaches Mealie on
    # loopback at the published port.
    if not (settings.mealie_api_key and settings.mealie_base_url):
        from ...services.first_run import provision_mealie_when_up
        asyncio.create_task(provision_mealie_when_up("http://localhost:9285"))
    return result


@router.get("/mealie/status")
async def mealie_status():
    """Mealie start progress (not-installed / starting / running), via the bridge."""
    if not is_raspberry_pi():
        return {"ok": False, "error": "Not available on this platform."}
    try:
        async with bridge_client(timeout=12.0) as c:
            r = (await c.get(f"{_HOST_BRIDGE}/mealie/status")).json()
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)}


_LOG_NAMES = {"mealie", "kiosk", "streamdeck", "grocy"}


@router.get("/grocy/local-status")
async def grocy_local_status():
    """Whether a Grocy on this machine answers HTTP yet. Returns {ok, serving, url}.

    Polled by the setup wizard on every install shape, not just a Pi: on an
    appliance it gates the live first-boot install log (FoodAssistant-n5ky), and
    on a plain server it is how the Inventory step notices the bundled Grocy
    coming up and connects itself instead of leaving the user at a dead end
    (FoodAssistant-nkxd). This only probes loopback candidates, so it is a
    local-availability answer, never a scan of anything the caller names.
    """
    url = await _detect_local_grocy()
    return {"ok": True, "serving": bool(url), "url": url}


@router.get("/logs/{name}")
async def install_logs(name: str):
    """Tail of an install/start log (mealie / kiosk / streamdeck / grocy), via
    the bridge.

    Mirrors the /mealie/status proxy: the setup UI polls this while a start or
    install is in flight to show live output. Returns {ok, name, running,
    lines}. Unknown names and bridge errors are reported, never raised.
    """
    if not is_raspberry_pi():
        return {"ok": False, "error": "Not available on this platform."}
    if name not in _LOG_NAMES:
        return {"ok": False, "error": "unknown log name"}
    try:
        async with bridge_client(timeout=6.0) as c:
            r = (await c.get(f"{_HOST_BRIDGE}/logs/{name}")).json()
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/hardware/status")
async def hardware_status():
    """Display / Stream Deck presence and service state, via the Pi host bridge.

    Off a Pi there is no host bridge to probe, so return a clean "nothing
    attached" shape (rather than an error) so the setup UI's attached-hardware
    panel degrades gracefully instead of showing a failure.
    """
    if not is_raspberry_pi():
        return {
            "ok": True,
            "display": {"present": False, "connectors": []},
            "streamdeck": {"present": False, "model": ""},
        }
    try:
        async with bridge_client(timeout=6.0) as c:
            r = (await c.get(f"{_HOST_BRIDGE}/hardware/status")).json()
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)}


# Sync the system-warning action items at most this often. The navbar polls
# this route from every open page (5-minute interval plus one hit per page
# load), so the throttle keeps the inbox sync to once a minute, matching the
# bridge's own monitoring loop.
_WARN_SYNC_THROTTLE_SECS = 60.0
_warn_last_sync = 0.0


def _sync_warning_items(warnings: list) -> None:
    """Raise/retire system-warning action items from the bridge feed, throttled.

    Best-effort by design: the health poll must keep answering even when the
    database is momentarily busy, so any failure here is swallowed.
    """
    global _warn_last_sync
    # A satellite's inbox lives on the main server (the action-items routes
    # forward upstream), so locally raised items would never be seen there;
    # the navbar icon and the settings banner still show its warnings.
    if settings.is_satellite():
        return
    import time as _time
    now = _time.monotonic()
    if now - _warn_last_sync < _WARN_SYNC_THROTTLE_SECS:
        return
    _warn_last_sync = now
    from ...services import action_items
    db = SessionLocal()
    try:
        action_items.sync_system_warnings(db, warnings)
    except Exception:
        pass
    finally:
        db.close()


@router.get("/system/health")
async def system_health():
    """Pi power/thermal/disk warnings, via the host bridge.

    Served from the bridge's continuously monitored /system/warnings snapshot
    (60-second loop), falling back to the on-demand /system/health probe on an
    older bridge. Off a Pi there is no bridge to probe, so return a clean "no
    warnings" shape (rather than an error) so the navbar indicator simply shows
    nothing instead of a failure on a server or phone. As a side effect the
    active warnings are mirrored into the action-items inbox (throttled), so a
    condition like undervoltage shows up there once and archives itself when
    it clears.
    """
    if not is_raspberry_pi():
        return {"ok": True, "warnings": []}
    try:
        async with bridge_client(timeout=6.0) as c:
            resp = await c.get(f"{_HOST_BRIDGE}/system/warnings")
            if resp.status_code == 404:  # older bridge: on-demand probe only
                resp = await c.get(f"{_HOST_BRIDGE}/system/health")
            r = resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e), "warnings": []}
    if isinstance(r, dict):
        _sync_warning_items(r.get("warnings") or [])
    return r


@router.get("/resources")
async def system_resources():
    """Live hardware readings for the Settings Resources pane.

    CPU use (two /proc/stat samples a moment apart), load, memory, disks,
    uptime, and temperature come from the pure collector in
    services/resources.py, which omits anything this environment cannot
    provide. On a Pi the host bridge's warnings snapshot adds the firmware
    power/throttle state (and a temperature fallback when the container
    cannot see a thermal zone); an unreachable bridge just leaves those out.
    """
    from ...services import resources as res
    from ...services import beszel
    prev = await run_in_threadpool(res.sample_cpu)
    await asyncio.sleep(0.3)
    cur = await run_in_threadpool(res.sample_cpu)
    data = await run_in_threadpool(res.collect, settings.data_dir, prev, cur)
    link = beszel.dashboard_link(settings.beszel_enabled, settings.beszel_url)
    if link:
        data["beszel_url"] = link
    if is_raspberry_pi():
        try:
            async with bridge_client(timeout=4.0) as c:
                r = (await c.get(f"{_HOST_BRIDGE}/system/warnings")).json()
            if isinstance(r, dict):
                power = res.decode_throttle_word(r.get("throttled"))
                if power:
                    data["power"] = power
                if "temperature" not in data and isinstance(r.get("temp_c"), (int, float)):
                    data["temperature"] = {"celsius": round(float(r["temp_c"]), 1)}
        except Exception:
            pass
    data["ok"] = True
    return data


async def _safe(coro, default):
    """Await a check coroutine, degrading any failure to a default (never raises).

    The Status dashboard aggregates several subsystem checks; a single slow or
    broken one must not fail the whole page, so every probe is wrapped here and
    a failure just shows that row as unknown/unreachable.
    """
    try:
        return await coro
    except Exception:
        return default


async def _ha_probe() -> dict:
    """Lightweight Home Assistant reachability check for the Status dashboard.

    Returns ``{configured, ok}``. With a direct connection, ok is whether
    Home Assistant answered (credentials stay on the server; only the boolean
    leaves). A satellite that reaches Home Assistant through its main server
    reports what the server said at the last sync. Fails soft.
    """
    from ...services import ha_client
    try:
        m = ha_client.mode()
    except Exception:  # noqa: BLE001
        m = "none"
    if m == "direct":
        try:
            return {"configured": True, "ok": bool(await ha_client.ping())}
        except Exception:  # noqa: BLE001
            return {"configured": True, "ok": False}
    if m == "upstream":
        return {"configured": True,
                "ok": bool((settings.fleet_relay or {}).get("ha_available"))}
    return {"configured": False, "ok": False}


@router.get("/status/summary")
async def status_summary_route():
    """Aggregate every subsystem's health for the Settings Status dashboard.

    Reuses the same checks that back the individual panes (network, Forager,
    the cached update result, Grocy/Mealie/Home Assistant connections), gathers
    only the rows this deployment mode shows, and runs them concurrently with
    short timeouts. Each probe degrades to unknown/unreachable on any error, so
    the dashboard always renders. The raw results are handed to the pure
    ``status_summary.build_summary`` mapper, which decides each pill's state and
    detail; this route only does the IO.
    """
    is_pi = is_raspberry_pi()
    sat = settings.is_satellite()

    # Which rows this mode shows, and the coroutine (or value) that feeds each.
    # Kept in step with the Jinja gating in _pane_status.html.
    probes: dict = {}

    if is_pi:
        probes["connection"] = _safe(network_status(), {"ok": False})
    if is_pi and not sat:  # pi_hosted: a physical Pi we own end to end
        probes["pi_health"] = _safe(system_health(), {"ok": False, "warnings": []})

    # Software update: read the cached update-check bookkeeping, never a live
    # GitHub call, so the dashboard is instant.
    probes["update"] = {
        "checked_at": settings.update_last_checked,
        "available": settings.update_last_available,
        "latest": settings.update_last_latest,
    }

    if not sat:
        probes["forager"] = _safe(cloud_status(), None)
        probes["remote_access"] = _safe(tunnel_status(), None)
        grocy_configured = bool(
            settings.grocy_api_key and settings.grocy_address_chosen())
        if grocy_configured:
            probes["grocy"] = _safe(_grocy_status_probe(), {"configured": True, "ok": False})
        else:
            probes["grocy"] = {"configured": False, "ok": False}
        if settings.mealie_api_key and settings.mealie_base_url:
            probes["mealie"] = _safe(_mealie_status_probe(), {"configured": True, "ok": False})
    else:
        probes["main_server"] = settings.satellite_last_sync if isinstance(
            settings.satellite_last_sync, dict) else {}

    if settings.ha_connected():
        probes["home_assistant"] = _safe(_ha_probe(), {"configured": True, "ok": False})

    # Resolve the coroutines concurrently, then map to pill states (pure).
    keys = list(probes.keys())
    resolved = await asyncio.gather(*[
        v if asyncio.iscoroutine(v) else _as_value(v) for v in probes.values()
    ])
    raw = dict(zip(keys, resolved))
    return status_summary.build_summary(raw)


async def _as_value(v):
    """Wrap a plain value so it can sit beside coroutines in asyncio.gather."""
    return v


async def _grocy_status_probe() -> dict:
    """Grocy reachability for the dashboard, reusing the pane's test path."""
    res = await test_grocy(TestGrocyPayload())
    if isinstance(res, JSONResponse):
        res = json.loads(res.body)
    out = {"configured": True, "ok": bool(res.get("ok"))}
    if not out["ok"] and res.get("fallback_url"):
        out["detail"] = (
            f"Not reachable at the saved address. Grocy answers at "
            f"{res['fallback_url']}: test the inventory connection to switch to it.")
    return out


async def _mealie_status_probe() -> dict:
    """Mealie reachability for the dashboard, reusing the pane's test path."""
    res = await test_mealie(TestMealiePayload())
    if isinstance(res, JSONResponse):
        res = json.loads(res.body)
    return {"configured": True, "ok": bool(res.get("ok"))}
