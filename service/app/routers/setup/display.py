"""Kiosk activity, screensaver, display power and rotation, touch driver
provisioning, and the Pi's network settings.

These call the host bridge, a small helper service on 127.0.0.1:9299 on the
Pi host. docker-compose.appliance.yml uses network_mode: host, so localhost in
the container is localhost on the host. On non-Pi or non-appliance installs
the endpoints return a clear error.
"""

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...config import settings
from ...hardware import is_raspberry_pi
from ...services.bridge import bridge_client
from ...services.host_panel import classify_active_connection
from ._common import router, _DISPLAY_TOUCH_DRIVER, _HOST_BRIDGE


@router.post("/kiosk/activity")
async def kiosk_activity():
    """Report kiosk user activity to the host bridge so it wakes the display
    (and the Stream Deck, which polls the bridge). No-op off a Pi."""
    if not is_raspberry_pi():
        return {"ok": True, "woke": False}
    try:
        async with bridge_client(timeout=4.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/activity", json={"source": "kiosk"})
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/kiosk/activity")
async def kiosk_activity_state():
    """Current shared activity/display state from the host bridge."""
    if not is_raspberry_pi():
        return {"ok": True, "last_activity": 0, "display_blanked": False}
    try:
        async with bridge_client(timeout=4.0) as c:
            r = await c.get(f"{_HOST_BRIDGE}/activity")
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/kiosk/screensaver")
async def kiosk_screensaver(request: Request):
    """Relay the kiosk screensaver overlay's show/hide state to the host bridge
    so the Stream Deck can raise its display-off logo during a soft sleep, where
    the panel stays lit but the saver covers it (FoodAssistant-qh8p).

    The screensaver reports this edge-triggered on show and hide only, and the
    bridge stores it without touching the shared activity epoch, so it is never
    a wake source (FoodAssistant-ofip). No-op off a Pi."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    active = bool(body.get("active")) if isinstance(body, dict) else False
    if not is_raspberry_pi():
        return {"ok": True, "screensaver_active": active}
    try:
        async with bridge_client(timeout=4.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/screensaver", json={"active": active})
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/display/blank")
async def display_blank():
    """Manually blank the kiosk display (Pi only)."""
    if not is_raspberry_pi():
        return {"ok": False, "error": "Not available on this platform."}
    try:
        async with bridge_client(timeout=6.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/display/blank")
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/display/wake")
async def display_wake():
    """Manually wake the kiosk display (Pi only)."""
    if not is_raspberry_pi():
        return {"ok": False, "error": "Not available on this platform."}
    try:
        async with bridge_client(timeout=6.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/display/wake")
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/network/status")
async def network_status():
    """Current Wi-Fi SSID and hostname, via the Pi host bridge."""
    if not is_raspberry_pi():
        return {"ok": False, "error": "Not available on this platform."}
    try:
        async with bridge_client(timeout=4.0) as c:
            wifi = (await c.get(f"{_HOST_BRIDGE}/wifi/status")).json()
            hn = (await c.get(f"{_HOST_BRIDGE}/hostname")).json()
        # Which link actually carries the network (FoodAssistant-1idf). The
        # bridge reports the active connection from the default route; classify
        # it here (a pure, tested helper) rather than in the bridge so a Pi on
        # Ethernet shows a calm "not in use" Wi-Fi line instead of a scary
        # "unavailable". Fall back to the bridge's own value if present.
        active = classify_active_connection(wifi.get("default_route", ""))
        if active == "none":
            active = wifi.get("active_connection", "")
        return {
            "ok": True,
            "ssid": wifi.get("ssid", ""),
            "wifi_state": wifi.get("state", ""),
            "wifi_detail": wifi.get("detail", ""),
            "ethernet": wifi.get("ethernet", {}),
            "active_connection": active,
            "hostname": hn.get("hostname", ""),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/network/scan")
async def network_scan():
    """List visible Wi-Fi networks, via the Pi host bridge."""
    if not is_raspberry_pi():
        return {"ok": False, "error": "Not available on this platform."}
    try:
        async with bridge_client(timeout=25.0) as c:
            r = (await c.get(f"{_HOST_BRIDGE}/wifi/scan")).json()
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)}


class WifiPayload(BaseModel):
    ssid: str = ""
    password: str = ""


@router.post("/network/wifi")
async def network_wifi(payload: WifiPayload):
    """Connect to a Wi-Fi network (Pi appliance only)."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    if not payload.ssid.strip():
        return JSONResponse({"ok": False, "error": "SSID is required."})
    try:
        async with bridge_client(timeout=35.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/wifi/connect",
                             json={"ssid": payload.ssid.strip(), "password": payload.password})
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


class HostnamePayload(BaseModel):
    hostname: str = ""


@router.post("/network/hostname")
async def network_hostname(payload: HostnamePayload):
    """Change the device hostname (Pi appliance only)."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    name = payload.hostname.strip().lower()
    if not name:
        return JSONResponse({"ok": False, "error": "Hostname is required."})
    try:
        async with bridge_client(timeout=10.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/hostname", json={"hostname": name})
        # The device hostname memo (config.py) now holds the old name; drop it
        # so links built from device_hostname() follow the rename immediately.
        from ...config import invalidate_hostname_cache
        invalidate_hostname_cache()
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.get("/display/rotation")
async def display_rotation_status():
    """Current KMS framebuffer rotation (Pi appliance only)."""
    if not is_raspberry_pi():
        return {"ok": False, "error": "Not available on this platform."}
    try:
        async with bridge_client(timeout=4.0) as c:
            r = (await c.get(f"{_HOST_BRIDGE}/display/rotation")).json()
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)}


class KmsRotationPayload(BaseModel):
    degrees: int = 0
    reboot: bool = False


@router.post("/display/rotation")
async def set_display_rotation(payload: KmsRotationPayload):
    """Set the KMS framebuffer rotation (Pi appliance only). Takes effect after reboot."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    if payload.degrees not in (0, 90, 180, 270):
        return JSONResponse({"ok": False, "error": "degrees must be 0, 90, 180, or 270."})
    try:
        async with bridge_client(timeout=20.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/display/rotation",
                             json={"degrees": payload.degrees, "reboot": payload.reboot})
        settings.save({"display_rotation": payload.degrees})
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.post("/touch/provision")
async def touch_provision(request: Request):
    """Apply the boot config the attached touch panel needs (Pi appliance only).

    Fills the gap where a display type chosen in the wizard after first boot was
    never provisioned: an ADS7846 SPI panel needs SPI enabled and the ads7846
    overlay in config.txt before its touch registers. Uses the saved display
    type unless one is passed in the body. A reboot is required to load a new
    overlay."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    try:
        body = await request.json()
    except Exception:
        body = {}
    dtype = (body.get("display_type") or settings.display_type or "generic")
    reboot = bool(body.get("reboot", False))
    driver = _DISPLAY_TOUCH_DRIVER.get(dtype, "generic")
    try:
        async with bridge_client(timeout=20.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/touch/provision",
                             json={"driver": driver, "reboot": reboot})
        # An old host bridge (updated app, stale bridge) has no such route and
        # answers 404 {"error": "not found"}. Say so plainly instead of leaking
        # a bare "not found" (FoodAssistant-vbfp): the bridge is redeployed by
        # the updater now, so the fix is to run the update once more or reboot.
        if r.status_code == 404:
            return JSONResponse({"ok": False, "error":
                "The host bridge on this device is out of date and cannot set up "
                "touch yet. Run Backup & Updates, Update once more (it now "
                "refreshes the bridge), or reboot, then try again."})
        out = r.json()
        out["driver"] = driver
        return out
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})
