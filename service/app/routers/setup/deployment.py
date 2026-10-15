"""Satellite connection test, settings sync, LAN pairing relay, and the
one-image switch between pi_hosted and pi_remote."""

import asyncio
import httpx
from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from ...config import settings, SATELLITE_PULL_FIELDS
from ...dependencies import reset_providers
from ...services.bridge import bridge_client
from ...services import egress
from ._common import router, _CLEAR, _HOST_BRIDGE, _refuse_probe_target, _safe_error


class TestRemotePayload(BaseModel):
    remote_server_url: str = ""


@router.post("/test/remote")
async def test_remote(payload: TestRemotePayload):
    """Check that a Pi Remote can reach the Pantry Raider server it controls."""
    url = (payload.remote_server_url or settings.remote_server_url).rstrip("/")
    if not url:
        return {"ok": False, "error": "Server URL is required."}
    # Like the other connection tests, this fetches an address the caller
    # supplies, so it must never be aimed at this device or the cloud
    # metadata address. The guarded client checks every hop, redirects too.
    refusal = _refuse_probe_target(url)
    if refusal:
        return {"ok": False, "error": refusal}
    try:
        async with egress.guarded_async_client(allow_private=True, timeout=6.0,
                                               follow_redirects=True) as client:
            r = await client.get(f"{url}/health")
        if r.status_code == 200:
            return {"ok": True, "message": f"Connected: Pantry Raider reachable at {url}"}
        return {"ok": False, "error": f"HTTP {r.status_code} from {url}/health"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


class SatelliteSyncPayload(BaseModel):
    remote_server_url: str = ""
    upstream_api_key: str = ""


@router.post("/satellite/sync")
async def satellite_sync(payload: SatelliteSyncPayload):
    """Save the upstream link, then pull backend config + defaults from it.

    Used by the satellite setup flow: enter the main server URL + API key, then
    sync. On success the satellite has Grocy/Mealie/AI config and is usable.
    """
    data = {"deployment_mode": "pi_remote"}
    if payload.remote_server_url:
        data["remote_server_url"] = payload.remote_server_url.rstrip("/")
    if payload.upstream_api_key and payload.upstream_api_key != _CLEAR:
        data["upstream_api_key"] = payload.upstream_api_key
    settings.save(data)

    from ...services.satellite import sync_from_upstream
    result = await run_in_threadpool(sync_from_upstream)
    # The recorded last-sync summary (timestamp, ok flag, applied fields) lets
    # the page redraw its Sync Status panel without a full reload.
    last = settings.satellite_last_sync if isinstance(settings.satellite_last_sync, dict) else {}
    if result.get("ok"):
        return {
            "ok": True,
            "message": f"Synced {len(result['applied'])} settings and "
                       f"{result['defaults']} expiry defaults from the server.",
            "last_sync": last,
        }
    return {
        "ok": False,
        "error": result.get("error", "Sync failed."),
        "last_sync": last,
    }


# --- Satellite side of LAN device pairing (FoodAssistant-4box) --------------
# The wizard's Main Server step can ask the server for this device's API key
# instead of the user copying one by hand. The browser cannot call the server
# directly (cross-origin), so these two endpoints relay: open the request,
# then poll its status until a logged-in user on the server approves. Both are
# in main.py's _SETUP_BYPASS because they run before this device is configured.

class PairingRelayPayload(BaseModel):
    server_url: str = ""
    request_id: str = ""


def _pairing_server_base(url: str) -> str:
    return (url or settings.remote_server_url or "").rstrip("/")


@router.post("/pairing/request")
async def pairing_request_relay(payload: PairingRelayPayload):
    """Ask the main server to pair this device; returns its code to display."""
    base = _pairing_server_base(payload.server_url)
    if not base:
        return {"ok": False, "error": "Enter the main server URL first."}
    import socket
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            # kind tells a 0.21 server this is a screen; older servers ignore it.
            r = await client.post(f"{base}/api/pairing/request",
                                  json={"hostname": socket.gethostname(), "kind": "satellite"})
        data = r.json()
    except Exception as e:
        return {"ok": False, "error": _safe_error(e)}
    if not data.get("ok"):
        return {"ok": False, "error": data.get("error", f"HTTP {r.status_code} from the server.")}
    return {"ok": True, "request_id": data.get("request_id", ""),
            "code": data.get("code", ""), "expires_in": data.get("expires_in", 0)}


@router.post("/pairing/status")
async def pairing_status_relay(payload: PairingRelayPayload):
    """Poll the main server for the pairing decision. An approval carries the
    minted API key; the wizard fills upstream_api_key with it client-side and
    the normal save/sync path persists it."""
    base = _pairing_server_base(payload.server_url)
    if not base or not payload.request_id:
        return {"ok": False, "error": "Missing the server URL or request id."}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(f"{base}/api/pairing/status/{payload.request_id}")
        data = r.json()
    except Exception as e:
        return {"ok": False, "error": _safe_error(e)}
    if not data.get("ok"):
        return {"ok": False, "error": data.get("error", f"HTTP {r.status_code} from the server.")}
    out = {"ok": True, "status": data.get("status", "expired")}
    if data.get("status") == "approved":
        out["api_key"] = data.get("api_key", "")
    return out


# --- One-image mode switch (FoodAssistant-dzx9) ----------------------------
# A pi_hosted appliance can stand down its local Grocy/Mealie stack and run as
# a satellite of another server, and later switch back, without reflashing.
# The pure decision/settings logic lives in services/deployment_switch.py; the
# container stop/start is done by the host bridge (root). Nothing is deleted
# either way: the local inventory data stays on the device.

_SATELLITE_KEY_REFUSED_MSG = (
    "The main server refused this key for a screen. Use Request access to pair "
    "this device, or a key made for satellites.")


class SwitchToSatellitePayload(BaseModel):
    remote_server_url: str = ""
    upstream_api_key: str = ""


def _ensure_satellite_sync_task(request: Request) -> None:
    """Start the periodic satellite sync task after a runtime mode flip.

    The lifespan hook only starts it when the app BOOTS as a satellite, so a
    device switched at runtime needs it started here. Idempotent: an already
    running task is left alone.
    """
    from ...main import _periodic_satellite_sync
    task = getattr(request.app.state, "sync_task", None)
    if task is None or task.done():
        request.app.state.sync_task = asyncio.create_task(_periodic_satellite_sync())


@router.post("/deployment/to-satellite")
async def switch_to_satellite(payload: SwitchToSatellitePayload, request: Request):
    """Switch this pi_hosted appliance to satellite duty.

    Order matters for safety: validate everything and prove the main server
    accepts the key FIRST (nothing changed on failure), then park the local
    stack via the bridge, then flip the mode and pull the server's config. The
    pre-switch backend config is snapshotted so Switch Back restores it.
    """
    from ...services import deployment_switch as ds

    ok, err = ds.can_switch_to_satellite(settings.deployment_mode)
    if not ok:
        return {"ok": False, "error": err}
    ok, url_or_err = ds.validate_server_url(payload.remote_server_url)
    if not ok:
        return {"ok": False, "error": url_or_err}
    url = url_or_err
    key = payload.upstream_api_key or ""
    if not key or key == _CLEAR:
        key = settings.upstream_api_key or ""
    if not key:
        return {"ok": False, "error": "The main server's API key is required."}

    # Prove the link works before touching anything: the same endpoint the
    # satellite sync uses, with the same key.
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(f"{url}/api/config/satellite",
                                 headers={"X-API-Key": key})
        if r.status_code == 401:
            return {"ok": False, "error":
                    "The server rejected the API key. Copy it from the main "
                    "server's Settings under Security."}
        if r.status_code == 403:
            return {"ok": False, "error": _SATELLITE_KEY_REFUSED_MSG}
        if r.status_code != 200:
            return {"ok": False, "error": f"The server answered HTTP {r.status_code}. "
                                          "Check the URL points at Pantry Raider."}
    except Exception as e:
        return {"ok": False, "error": f"Could not reach the server: {_safe_error(e, key)}"}

    # Snapshot the local backend config before the satellite sync overwrites it.
    snapshot = ds.hosted_snapshot(
        {f: getattr(settings, f, None) for f in SATELLITE_PULL_FIELDS})

    # Park the local stack (bridge, root). On failure nothing has changed yet.
    try:
        async with bridge_client(timeout=310.0) as client:
            r = await client.post(f"{_HOST_BRIDGE}/stack/standdown")
        body = r.json()
        if r.status_code != 200 or not body.get("ok"):
            return {"ok": False, "error": body.get(
                "error", "The local stack could not be stopped.")}
    except Exception as e:
        return {"ok": False, "error": f"Could not reach the device helper "
                                      f"to stop the local stack: {_safe_error(e, key)}"}

    settings.save(ds.satellite_switch_settings(url, key, snapshot))

    # First pull from the new main server, then keep syncing periodically.
    from ...services.satellite import sync_from_upstream
    result = await run_in_threadpool(sync_from_upstream)
    _ensure_satellite_sync_task(request)
    reset_providers()

    if result.get("ok"):
        return {"ok": True, "message":
                "This device is now a satellite. It pulled "
                f"{len(result.get('applied', []))} settings from the main "
                "server, and the local inventory stack is stopped with its "
                "data kept on this device."}
    return {"ok": True, "message":
            "This device is now a satellite and the local stack is stopped, "
            "but the first sync from the server failed: "
            f"{result.get('error', 'unknown error')}. It will retry "
            "automatically; you can also use Sync Now in Main Server settings."}


@router.post("/deployment/to-hosted")
async def switch_to_hosted(request: Request):
    """Switch a parked appliance back to running its own full stack.

    Only available on a device that was switched with the control above (a
    device flashed as a plain Pi Remote has no local stack and is refused).
    Starts the parked containers, then restores the snapshotted backend config.
    """
    from ...services import deployment_switch as ds

    ok, err = ds.can_switch_back(settings.deployment_mode,
                                 bool(settings.hosted_stack_parked))
    if not ok:
        return {"ok": False, "error": err}

    try:
        async with bridge_client(timeout=610.0) as client:
            r = await client.post(f"{_HOST_BRIDGE}/stack/standup")
        body = r.json()
        if r.status_code != 200 or not body.get("ok"):
            return {"ok": False, "error": body.get(
                "error", "The local stack could not be started.")}
    except Exception as e:
        return {"ok": False, "error": f"Could not reach the device helper "
                                      f"to start the local stack: {_safe_error(e)}"}

    snapshot = settings.hosted_config_snapshot \
        if isinstance(settings.hosted_config_snapshot, dict) else {}
    settings.save(ds.hosted_restore_settings(snapshot))
    # The backend panes are editable again: nothing is server-sourced now.
    object.__setattr__(settings, "server_sourced_fields", set())
    reset_providers()

    # Stop mirroring the old main server.
    task = getattr(request.app.state, "sync_task", None)
    if task is not None:
        task.cancel()
        request.app.state.sync_task = None

    return {"ok": True, "message":
            "This device is running its own inventory stack again. Grocy "
            "(and Mealie, if it was enabled) may take a minute to finish "
            "starting."}
