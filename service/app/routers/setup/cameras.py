"""Camera discovery and setup: LAN scan and probe, Home Assistant cameras,
Frigate, and Reolink."""

from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...config import settings
from ._common import router


class HaCameraDiscoverPayload(BaseModel):
    # Optional overrides so a freshly typed (not yet saved) base/token can be
    # tested before the user commits them. Blank falls back to saved settings.
    base_url: str = ""
    token: str = ""


class CameraScanPayload(BaseModel):
    cidr: str = ""   # blank = this server's own /24


@router.post("/cameras/scan")
async def scan_ip_cameras(payload: CameraScanPayload = CameraScanPayload()):
    """Scan the LAN for IP cameras (FoodAssistant-d9rx).

    Probes each host for common camera ports and, for HTTP hosts, well-known
    snapshot paths, returning candidates the user can preview and add. Runs the
    blocking sweep in a thread so the event loop stays free."""
    import anyio
    from ...services import camera_scan, lan_scan
    # The scan only ever sweeps a private LAN range, so it can never be pointed at
    # a public target (FoodAssistant-tfrm). Reject an explicit non-LAN CIDR with a
    # clear message rather than silently falling back to auto-detection.
    explicit = (payload.cidr or "").strip()
    if explicit and not lan_scan.is_private_cidr(explicit):
        return {"ok": False, "error": "Only a private home-network range can be "
                "scanned (10.x, 172.16-31.x, or 192.168.x). Enter a CIDR like "
                "192.168.1.0/24."}
    # Same resolution as the device scan: explicit, then a remembered range, this
    # host's LAN interface, and a Grocy/Mealie URL host, skipping Docker subnets.
    cidr = lan_scan.resolve_lan_cidr(explicit, candidates=[camera_scan.best_lan_cidr()])
    if not cidr:
        return {"ok": False, "error": "Could not determine the local network; enter a CIDR like 192.168.1.0/24."}
    # Remember a good (non-Docker) range so the next blank scan (camera or device)
    # reuses it without the user retyping it.
    if not lan_scan.looks_dockerish(cidr) and cidr != settings.lan_scan_cidr:
        try:
            settings.save({"lan_scan_cidr": cidr})
        except Exception:
            pass
    result = await anyio.to_thread.run_sync(lambda: camera_scan.scan_for_cameras(cidr))
    if result.get("error"):
        return {"ok": False, "error": result["error"]}
    cameras = result.get("cameras", [])
    responded = result.get("responded", 0)
    scanned = result.get("scanned", 0)
    # When we had to guess and the guess is a Docker bridge subnet, tell the user
    # to enter their real LAN (the app runs in a container, FoodAssistant-d9rx).
    hint = ""
    if not (payload.cidr or "").strip() and camera_scan.looks_dockerish(cidr):
        hint = (f"{cidr} looks like a Docker network, not your LAN. Enter your "
                f"home network instead, e.g. 192.168.1.0/24.")
    # Diagnostic note so 'no cameras' is actionable: nothing answering at all on a
    # subnet the user is sure has cameras usually means this container cannot
    # route to the LAN (run with host networking, or add by IP).
    note = ""
    if not cameras:
        if responded == 0:
            note = (f"Scanned {scanned} hosts on {cidr} and nothing answered on camera "
                    "ports. If cameras are definitely on this network, this app is running "
                    "in a Docker container and probably cannot reach your LAN. Run it with "
                    "host networking (network_mode: host), or add the camera by IP above.")
        else:
            note = (f"Found {responded} host(s) with camera-like ports open, but none "
                    "exposed a recognized snapshot path. Add one by IP above using the "
                    "closest brand template.")
    return {"ok": True, "cidr": cidr, "cameras": cameras, "responded": responded,
            "scanned": scanned, "hint": hint, "note": note}


class CameraProbePayload(BaseModel):
    ip: str = ""
    username: str = ""
    password: str = ""


@router.post("/cameras/probe")
async def probe_ip_camera(payload: CameraProbePayload):
    """Re-probe one scanned camera with login credentials (FoodAssistant-ij6w).

    For a password-protected camera the scan can only report that it needs a
    login; with the user's credentials this finds a working snapshot path, reads
    the brand and resolution, and returns a snapshot URL with the credentials
    embedded so it previews and saves without a separate login step."""
    import anyio
    from ...services import camera_scan, lan_scan
    ip = (payload.ip or "").strip()
    if not ip:
        return {"ok": False, "error": "No camera IP given."}
    # A probe HTTP-connects to the given address, so it must be a private LAN
    # host: never let it reach the server itself or a public target
    # (FoodAssistant-tfrm). ``is_private_cidr`` accepts a bare IP as a /32.
    if not lan_scan.is_private_cidr(ip):
        return {"ok": False, "error": "Only a camera on your local network can be "
                "probed (a 10.x, 172.16-31.x, or 192.168.x address)."}
    result = await anyio.to_thread.run_sync(
        lambda: camera_scan.probe_with_auth(ip, payload.username, payload.password))
    return result


@router.get("/cameras/scan-default")
async def scan_default_cidr():
    """The CIDR the camera scan would default to, so the UI can pre-fill and
    show it before scanning (FoodAssistant-d9rx)."""
    from ...services import camera_scan, lan_scan
    # Resolve like the actual scan does (remembered range, LAN interface, then a
    # Grocy/Mealie URL host) so the pre-filled default is the real LAN, not Docker.
    cidr = lan_scan.resolve_lan_cidr("", candidates=[camera_scan.best_lan_cidr()]) or ""
    return {"cidr": cidr, "dockerish": camera_scan.looks_dockerish(cidr) if cidr else False}


_HA_ON_SERVER_MSG = "Home Assistant is set up on the main server."
_HA_NO_ANSWER_MSG = ("Home Assistant did not answer with its entities. Check the address "
                     "and the token.")


def _ha_relayed_here() -> bool:
    """A satellite that reaches Home Assistant through its main server."""
    try:
        return bool(settings.is_satellite() and settings.fleet_relay_active("ha"))
    except Exception:  # noqa: BLE001
        return False


@router.post("/ha/cameras")
async def ha_discover_cameras(payload: HaCameraDiscoverPayload):
    """Discover Home Assistant camera entities.

    Lists HA's ``camera.*`` entities with a friendly name and a preview
    address on this app (an admin-only route that fetches the picture
    server-side), so no token is ever part of an address the page sees.
    Credentials come from app settings, but the request body may override
    them so the Cameras page can test a token before it is saved.
    """
    if _ha_relayed_here():
        return JSONResponse({"ok": False, "error": _HA_ON_SERVER_MSG}, status_code=409)
    from urllib.parse import quote
    from ...services import ha_client
    base = (payload.base_url or settings.streamdeck_ha_base_url or "").strip().rstrip("/")
    token = (payload.token or settings.streamdeck_ha_token or "").strip()
    if not base or not token:
        return {"ok": False, "error": "Set the Home Assistant URL and token first."}
    try:
        states = await ha_client.list_states(base=base, token=token)
    except Exception:  # noqa: BLE001
        states = []
    if not states:
        # A working Home Assistant always lists some entities, so nothing at
        # all means it did not answer or did not accept the token.
        return {"ok": False, "error": _HA_NO_ANSWER_MSG}
    cameras = []
    for st in states if isinstance(states, list) else []:
        entity = st.get("entity_id", "") if isinstance(st, dict) else ""
        if not isinstance(entity, str) or not entity.startswith("camera."):
            continue
        attrs = st.get("attributes") or {}
        name = (attrs.get("friendly_name") if isinstance(attrs, dict) else "") \
            or entity.split(".", 1)[1].replace("_", " ").title()
        cameras.append({
            "entity_id": entity,
            "name": str(name),
            "snapshot_url": "ui/camera/preview?entity=" + quote(entity, safe=""),
            "stream_url": "",
        })
    cameras.sort(key=lambda c: c["name"].lower())
    return {"ok": True, "cameras": cameras}


@router.get("/ha/entities")
async def ha_actionable_entities():
    """Home Assistant entities for the custom-key builder (FoodAssistant-yzjr).

    Returns the entities a key press can act on (lights, switches, scenes,
    scripts, ...), grouped by domain with each entity's friendly name, current
    state, and a sensible default service. Degrades to an empty list with a
    hint when this install has no Home Assistant connection of its own, so the
    builder falls back to free-text fields."""
    if _ha_relayed_here():
        return JSONResponse({"ok": False, "error": _HA_ON_SERVER_MSG}, status_code=409)
    from ...services import ha_actions, ha_client
    if ha_client.mode() != "direct":
        return {"ok": True, "configured": False, "groups": [],
                "hint": "Connect Home Assistant first (Settings, Home Assistant) to pick your devices here."}
    entities = await ha_actions.list_actionable_entities()
    return {"ok": True, "configured": True,
            "groups": ha_actions.group_by_domain(entities)}


class FrigateDiscoverPayload(BaseModel):
    base_url: str = ""


@router.post("/cameras/frigate")
async def frigate_discover_cameras(payload: FrigateDiscoverPayload):
    """Discover cameras from a Frigate NVR (FoodAssistant-7ror).

    Fetches Frigate's ``/api/config`` and returns each camera with a still and a
    stream URL, mirroring the Home Assistant camera discovery. Frigate carries no
    login, so the URLs hold no secret and the browser can preview them; adding one
    stores a plain camera entry the existing resolve/proxy path serves. Fails
    soft with a clear message when Frigate is unreachable or lists no cameras."""
    import anyio
    from ...services import frigate
    result = await anyio.to_thread.run_sync(lambda: frigate.discover(payload.base_url))
    return result


class ReolinkAddPayload(BaseModel):
    name: str = ""
    host: str = ""
    port: str = ""
    channel: int = 0
    username: str = ""
    password: str = ""
    stream_quality: str = "main"   # "main" (full) or "sub" (lighter)
    test: bool = True              # verify the snapshot before saving


def _reolink_entry_from_payload(payload: ReolinkAddPayload) -> dict:
    """Compose a Reolink camera entry from the add/preview form fields.

    The login rides the entry only server-side; callers strip the password
    before handing anything back to the page."""
    host = (payload.host or "").strip().rstrip("/")
    return {
        "name": (payload.name or host).strip(),
        "source": "reolink",
        "host": host,
        "port": (payload.port or "").strip(),
        "channel": payload.channel,
        "username": (payload.username or "").strip(),
        "password": payload.password or "",
        "stream_quality": "sub" if str(payload.stream_quality).lower() == "sub" else "main",
    }


async def _reolink_snapshot_or_error(entry: dict):
    """Fetch a Reolink still with the real token sign-in the camera proxy uses.

    Returns ``(content, content_type, None)`` on a good image, or
    ``(None, None, message)`` with a friendly rejected/unreachable/no-snapshot
    message on failure. The token and login stay on the server. Shared by the
    preview and add flows so both behave identically."""
    from ...services import cameras as cam_svc
    from ...services import egress
    try:
        status, content, ctype = await cam_svc.fetch_reolink_snapshot(entry)
    except cam_svc.ReolinkAuthError:
        return None, None, "The camera rejected that username or password."
    except egress.BlockedHostError:
        return None, None, cam_svc.BLOCKED_HOST_MESSAGE
    except Exception as e:
        return None, None, f"Could not reach the camera: {e}"
    if not cam_svc._looks_like_jpeg(status, content, ctype):
        return None, None, f"The camera did not return a snapshot (HTTP {status})."
    return content, ctype, None


@router.post("/cameras/reolink/preview")
async def preview_reolink_camera(payload: ReolinkAddPayload):
    """Preview a Reolink camera before adding it (FoodAssistant-26mf).

    Signs in with the entered host, channel, and login and streams back the
    still image bytes, so the setup page can confirm the feed and the login are
    right before saving, the same way the Frigate and IP-scan flows offer a
    Preview. Nothing is saved. The login is posted in the request body and the
    token stays on the server, so no credential ever appears in a browser URL or
    the page source. Failures come back as the same rejected, unreachable, or
    no-snapshot messages the Add verify returns."""
    from fastapi.responses import Response
    entry = _reolink_entry_from_payload(payload)
    if not entry["host"]:
        return JSONResponse({"ok": False, "error": "Enter the camera's address."},
                            status_code=400)
    content, ctype, error = await _reolink_snapshot_or_error(entry)
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=502)
    return Response(content=content, media_type=ctype or "image/jpeg")


@router.post("/cameras/reolink")
async def add_reolink_camera(payload: ReolinkAddPayload):
    """Add a Reolink camera by host, channel, and login (FoodAssistant-qft4).

    The credentials are kept on the server: the snapshot and RTSP URLs are
    composed here (never sent to the browser) and the saved entry is fetched
    server-side by the camera proxy, so no login ever reaches the page.
    Best-effort probes the camera's device info to recognise a doorbell model
    and flag two-way-talk capability (``device_type``/``two_way_talk`` on the
    stored entry); a failed probe just leaves those unset, it never blocks
    adding the camera. Two-way audio itself is not implemented (it needs a
    WebRTC session); this only surfaces the capability. Optionally verifies
    the still image before saving so a wrong login is caught up front. Returns
    the stored entry without the password."""
    entry = _reolink_entry_from_payload(payload)
    if not entry["host"]:
        return {"ok": False, "error": "Enter the camera's address."}
    if payload.test:
        # Verify with the real token sign-in the camera proxy uses, so a wrong
        # login is caught up front. The credentials never leave the server.
        _content, _ctype, error = await _reolink_snapshot_or_error(entry)
        if error:
            return {"ok": False, "error": error}
    try:
        from ...services import cameras as cam_svc, camera_detect
        dev_info = await cam_svc.fetch_reolink_dev_info(entry)
        caps = camera_detect.reolink_capabilities(dev_info)
        entry["device_type"] = "doorbell" if caps["is_doorbell"] else "camera"
        entry["two_way_talk"] = caps["two_way_talk"]
    except Exception:
        pass  # capability detection is a nicety, never blocks adding the camera
    cams = [c for c in (settings.streamdeck_cameras or []) if isinstance(c, dict)]
    cams.append(entry)
    settings.save({"streamdeck_cameras": cams})
    # Hand back the entry without the secret so the page can show a row.
    safe = {k: v for k, v in entry.items() if k != "password"}
    return {"ok": True, "camera": safe}
