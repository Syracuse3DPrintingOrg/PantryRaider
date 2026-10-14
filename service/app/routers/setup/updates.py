"""Software updates (host bridge OTA, Watchtower), the Grocy config repair,
and full-stack restore."""

import os
import httpx
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...config import settings
from ...hardware import is_raspberry_pi
from ...services.bridge import bridge_client
from ._common import router, _HOST_BRIDGE, _push_update_channel


@router.post("/kiosk/install")
async def kiosk_install():
    """Provision the kiosk service for a display attached after first install."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    try:
        async with bridge_client(timeout=15.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/kiosk/install")
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.post("/update")
async def update_software():
    """Pull the latest version and restart the service, via the host bridge.

    Available on any Pi appliance (both Pi Remote and Pi Hosted) since both run
    the host bridge. The bridge shells out to foodassistant-update, which adapts
    to the device: a Pi Remote runs from a Python venv (git pull, venv pip when
    requirements changed, service restart), while a Pi Hosted box runs from a
    Docker image (docker compose pull + recreate the service container, with a
    build-from-source fallback). Either way the bridge runs the work
    synchronously and returns {ok, before, after, restarted, log}; a failure
    leaves the running version untouched and reports the error. The work can
    take a couple of minutes on a Pi, so the proxy timeout is generous.

    A server set up with install.sh updates the same way once its host bridge
    has proven it is ours (services/update_path.py); every other server is
    pointed at Watchtower or at the tool that runs its container.
    """
    if settings.is_pi_appliance():
        return JSONResponse(await run_host_bridge_update())
    from ...services import update_path
    r = await update_path.resolve(force=True)
    if r["kind"] == update_path.KIND_SERVER_BRIDGE:
        if update_path.usable(r):
            return JSONResponse(await run_host_bridge_update())
        if r["bridge"] == update_path.BRIDGE_MISMATCH:
            return JSONResponse({"ok": False, "error": update_path.BRIDGE_MISMATCH_MSG})
        return JSONResponse({"ok": False, "error": update_path.BRIDGE_DOWN_MSG})
    if r["kind"] == update_path.KIND_WATCHTOWER:
        return await update_server()
    return JSONResponse({"ok": False, "no_updater": True, "error": (
        update_path.CONTAINER_HINT_MSG if r.get("bridge_hint") else _NO_UPDATER_MSG)})


async def run_host_bridge_update(scheduled: bool = False) -> dict:
    """POST the host bridge OTA and return its JSON result. Shared by the manual
    Update button and the automatic-update scheduler (FoodAssistant-k2kk).

    The current update channel is re-pushed first so the helper always follows
    the saved setting, even when the settings-save push was missed (bridge down
    or predating the /update/channel route).

    The scheduler passes scheduled=True, which lets the updater hold on a
    version that already failed to start on this device instead of trying it
    again every few hours (FoodAssistant-g3n7i). A manual press sends no body,
    so it always tries."""
    await _push_update_channel(settings.update_channel)
    try:
        async with bridge_client(timeout=620.0) as c:
            if scheduled:
                r = await c.post(f"{_HOST_BRIDGE}/update", json={"scheduled": True})
            else:
                r = await c.post(f"{_HOST_BRIDGE}/update")
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


# How long to wait between the re-probes after a Grocy config repair. PHP
# re-reads a changed config.php within a couple of seconds (opcache revalidates
# on a timer), so the first probe can still see the old file. Module-level so
# tests can zero it.
_GROCY_REPAIR_PROBE_DELAY = 1.5
_GROCY_REPAIR_PROBES = 3


@router.post("/grocy/repair")
async def grocy_repair_config():
    """Fix Grocy's config.php after a Grocy image update, on a Pi Hosted
    appliance: the host bridge runs the foodassistant-grocy-repair helper
    (which rewrites the pre-4.7 AUTH_CLASS namespace, with a backup, and only
    when the new class exists in the container), then Grocy is re-probed so
    the banner's button can report whether the inventory is back.

    Any other install answers with the hand edit instead: a server has no
    bridge and a satellite's Grocy lives on its main server.
    """
    import asyncio
    from ...services.grocy import GrocyClient, AUTH_CLASS_ANY_HINT, repair_available

    async def _hand_edit() -> str:
        # Name the login handler this Grocy's config.php actually uses, so an
        # LDAP or reverse-proxy install is never told to switch to the default
        # login. Grocy's own error page says which one; without it, give the
        # fix that names no class rather than guessing the default login.
        probe = GrocyClient()
        try:
            await probe.health_check()
        except Exception:
            pass
        return getattr(probe, "last_hint", "") or AUTH_CLASS_ANY_HINT

    if not repair_available():
        return JSONResponse({"ok": False, "action": "refused",
                             "reason": "This install cannot edit Grocy's files "
                                       "itself. " + await _hand_edit()})
    try:
        async with bridge_client(timeout=180.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/grocy/repair-config")
        result = r.json() if r.content else {}
    except Exception as e:
        return JSONResponse({"ok": False, "action": "failed",
                             "reason": "Could not reach the device helper "
                                       f"({e.__class__.__name__}). Try again "
                                       "in a moment, or apply the edit by hand. "
                                       + await _hand_edit()})
    if not isinstance(result, dict):
        result = {"ok": False, "action": "failed",
                  "reason": "The device helper gave an unexpected reply."}
    grocy = GrocyClient()
    grocy_ok = False
    for attempt in range(_GROCY_REPAIR_PROBES):
        grocy_ok = await grocy.health_check()
        if grocy_ok or not result.get("ok"):
            break
        if attempt + 1 < _GROCY_REPAIR_PROBES:
            await asyncio.sleep(_GROCY_REPAIR_PROBE_DELAY)
    result["grocy_ok"] = grocy_ok
    if not grocy_ok and getattr(grocy, "last_error", ""):
        result["grocy_detail"] = grocy.last_error
    return JSONResponse(result)


def _watchtower_url() -> str:
    return os.environ.get("WATCHTOWER_URL", "http://watchtower:8080").rstrip("/")


def _watchtower_token() -> str:
    from ...services.update_path import watchtower_configured
    if not watchtower_configured():
        return ""
    return os.environ.get("WATCHTOWER_HTTP_API_TOKEN", "")


# A server install without the Watchtower token (the Unraid template, or a
# compose file of the user's own) is updated by whatever runs its container.
_NO_UPDATER_MSG = ("This install is updated by the tool that runs its container. "
                   "On Unraid, use Apply update on the Docker tab.")

# The updater is configured but nothing answers. Older compose files run the
# archived containrrr/watchtower, which cannot talk to Docker 29 and
# crash-loops, and the app cannot replace the compose file itself (it only
# mounts ./data), so the fix has to be spelled out for the user.
UPDATER_DOWN_MSG = (
    "The automatic updater is not answering. Compose files from before this "
    "release use an updater that stops working on Docker 29 and newer. In your "
    "Pantry Raider folder, back up the data, grocy and mealie folders first (the "
    "new file also upgrades the bundled Mealie, which cannot be undone), then "
    "download the current docker-compose.prod.yml as docker-compose.yml and run "
    "docker compose up -d with the profiles you use. The README's Updating "
    "section has the exact commands. Or update by hand with the commands below.")

_UPDATER_BUSY_MSG = ("An update is already running. Give it a minute, then "
                     "check the version again.")


@router.get("/updater-status")
async def updater_status():
    """Whether this server's automatic updater is set up and answering, so the
    Updates pane can warn about a dead one before the user presses Update now.

    Server mode only. The probe is an unauthenticated GET on /v1/update: any
    HTTP answer at all means a process is listening (the maintained fork
    answers 405 to GET, the old containrrr image 401). The token is never sent,
    because the old image runs an update for an authenticated request of any
    method. Only a connection failure or a timeout counts as not reachable.

    kind and bridge say how this install updates (services/update_path.py). A
    server whose host bridge installs updates answers configured True,
    reachable when the bridge can be used, and the message to show when not.
    """
    from ...services import update_path
    token = _watchtower_token()
    if settings.deployment_mode != "server" or not token:
        if settings.deployment_mode == "server" and not token:
            r = await update_path.resolve()
            if r["kind"] == update_path.KIND_SERVER_BRIDGE:
                usable = update_path.usable(r)
                message = ""
                if r["bridge"] == update_path.BRIDGE_UNREACHABLE:
                    message = update_path.BRIDGE_DOWN_MSG
                elif r["bridge"] == update_path.BRIDGE_MISMATCH:
                    message = update_path.BRIDGE_MISMATCH_MSG
                return {"configured": True, "reachable": usable, "kind": r["kind"],
                        "bridge": r["bridge"], "message": message}
            return {"configured": False, "reachable": False, "kind": r["kind"],
                    "bridge": r["bridge"]}
        r = await update_path.resolve()
        return {"configured": False, "reachable": False, "kind": r["kind"],
                "bridge": r["bridge"]}
    kind = {"kind": update_path.KIND_WATCHTOWER, "bridge": update_path.BRIDGE_NOT_CHECKED}
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            await c.get(f"{_watchtower_url()}/v1/update")
    except (httpx.HTTPError, httpx.InvalidURL):
        return {"configured": True, "reachable": False, **kind}
    return {"configured": True, "reachable": True, **kind}


@router.get("/update/state")
async def update_state():
    """What the host updater last installed (version, channel, release key),
    relayed from the bridge on installs whose bridge installs updates; {}
    everywhere else and on any failure."""
    from ...services import update_path
    try:
        if not settings.is_pi_appliance():
            r = await update_path.resolve()
            if not (r["kind"] == update_path.KIND_SERVER_BRIDGE and update_path.usable(r)):
                return {}
        async with bridge_client(timeout=3.0) as c:
            resp = await c.get(f"{_HOST_BRIDGE}/update/state")
        if resp.status_code != 200:
            return {}
        body = resp.json()
        return body if isinstance(body, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


@router.post("/update-server")
async def update_server():
    """Apply an update now on a non-Pi server by triggering Watchtower's HTTP
    API, so the user does not wait for the daily poll. The watchtower service in
    docker-compose.prod.yml exposes the API with a shared token; the app reaches
    it on the compose network. Degrades gracefully when watchtower is not
    running or not configured.
    """
    if settings.is_pi_appliance():
        return JSONResponse(
            {"ok": False, "error": "On a Pi appliance use the Update now button above."})
    url = _watchtower_url()
    token = _watchtower_token()
    if not token:
        # A server set up with install.sh has its own host updater; an old
        # open Settings tab that still calls this route keeps working.
        from ...services import update_path
        r = await update_path.resolve(force=True)
        if r["kind"] == update_path.KIND_SERVER_BRIDGE and update_path.usable(r):
            return JSONResponse(await run_host_bridge_update())
        if r["kind"] == update_path.KIND_CONTAINER and r.get("bridge_hint"):
            return JSONResponse({"ok": False, "no_updater": True,
                                 "error": update_path.CONTAINER_HINT_MSG})
        return JSONResponse({"ok": False, "no_updater": True, "error": _NO_UPDATER_MSG})
    started = {"ok": True, "message": (
        "Update started. If a newer version was published, the app will "
        "download it and restart in a moment.")}
    try:
        # Fail fast on connect so a missing or stopped watchtower returns a
        # clear message in a couple of seconds instead of hanging until a
        # reverse proxy cuts the request. async=true makes the maintained fork
        # answer 202 at once; the old image ignores it and holds the request
        # until the pull finishes, which the read timeout below turns into
        # "started".
        timeout = httpx.Timeout(connect=4.0, read=30.0, write=10.0, pool=4.0)
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(f"{url}/v1/update?async=true",
                             headers={"Authorization": f"Bearer {token}"})
    except httpx.ReadTimeout:
        return {"ok": True, "message": (
            "The update has started and is still downloading. The app will "
            "restart shortly.")}
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return JSONResponse({"ok": False, "error": UPDATER_DOWN_MSG})
    except Exception as e:
        return JSONResponse({"ok": False, "error": (
            "The automatic updater did not answer properly. Try again in a "
            "minute, or update by hand with the commands below. "
            f"({e.__class__.__name__})")})
    if r.status_code in (200, 202):
        return started
    if r.status_code == 429:
        return JSONResponse({"ok": False, "error": _UPDATER_BUSY_MSG})
    return JSONResponse(
        {"ok": False, "error": f"Updater returned HTTP {r.status_code}."})


class _RestoreReq(BaseModel):
    source: str = ""


@router.post("/restore")
async def restore_full_stack(req: _RestoreReq):
    """Full Grocy + Mealie + app snapshot restore, via the host bridge.

    Only meaningful on a Pi appliance: the restore stops and restarts the whole
    docker stack and swaps the bind-mounted data dirs, which only the host
    bridge (running as root) can do. We do NOT accept a browser upload of the
    (large) snapshot; instead the body's {source} is either an absolute path to
    a .tar.gz already on the device, or "rclone:<remote-path>" to pull from the
    configured rclone remote first. The bridge runs foodassistant-restore
    synchronously and returns {ok, error, source, restored_dirs, snapshot,
    restarted, log}; the helper sets the current data aside (.pre-restore-<stamp>,
    never deleted) and tries to restart on any mid-restore failure so the device
    is never left down. Stopping, pulling, unpacking and restarting can take a
    while on a Pi, so the proxy timeout is generous.
    """
    if not is_raspberry_pi():
        return JSONResponse(
            {"ok": False, "error": "Full-stack restore is only available on this appliance."})
    try:
        async with bridge_client(timeout=920.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/restore", json={"source": req.source})
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})
