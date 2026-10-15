"""The Pi's setup Wi-Fi access point: status, config, and turning it off."""

from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...hardware import is_raspberry_pi
from ...services.bridge import bridge_client
from ._common import router, _HOST_BRIDGE


@router.get("/ap/status")
async def ap_status():
    """Whether the fallback Wi-Fi AP is active (Pi appliance only)."""
    if not is_raspberry_pi():
        return {"ok": True, "active": False}
    try:
        async with bridge_client(timeout=4.0) as c:
            r = (await c.get(f"{_HOST_BRIDGE}/ap/status")).json()
        return r
    except Exception:
        return {"ok": True, "active": False}


@router.post("/ap/disable")
async def ap_disable():
    """Stop the fallback hotspot after the user has configured Wi-Fi."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    try:
        async with bridge_client(timeout=15.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/ap/disable")
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


# Recovery hotspot identity (settable name and password). The two validators
# are pure and module-level so their truth tables are testable; the settings
# card mirrors them client-side and the host bridge re-validates on its side
# of the socket. The DEFAULT SSID stays the internal identifier FoodAssistant;
# only the user-chosen broadcast name varies.
AP_DEFAULT_SSID = "FoodAssistant"


def validate_ap_ssid(ssid: str) -> str:
    """'' when the hotspot name is usable, else a user-facing error. The
    802.11 limit is 1..32 BYTES (not characters); control characters are
    refused so a name can never smuggle lines into the hostapd config."""
    if not ssid or not ssid.strip():
        return "Enter a hotspot name."
    if ssid != ssid.strip():
        return "The hotspot name cannot start or end with spaces."
    if len(ssid.encode("utf-8")) > 32:
        return "The hotspot name must be 32 bytes or fewer."
    if any(ord(c) < 32 or ord(c) == 127 for c in ssid):
        return "The hotspot name cannot contain control characters."
    return ""


def validate_ap_passphrase(passphrase: str) -> str:
    """'' when the password is a valid WPA2 passphrase: 8..63 printable ASCII
    characters (the WPA2 spec's own bounds)."""
    if not passphrase or not (8 <= len(passphrase) <= 63):
        return "The hotspot password must be 8 to 63 characters."
    if any(ord(c) < 32 or ord(c) > 126 for c in passphrase):
        return "The hotspot password can only use letters, digits, spaces, and punctuation."
    return ""


@router.get("/ap/config")
async def ap_config_get():
    """The recovery hotspot's name, password, and state (Pi appliance only).
    The bridge includes the password because this app holds the bridge token;
    a caller without it would get the identity fields only."""
    if not is_raspberry_pi():
        return {"ok": False, "error": "Not available on this platform."}
    try:
        async with bridge_client(timeout=6.0) as c:
            r = (await c.get(f"{_HOST_BRIDGE}/ap/config")).json()
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)}


class ApConfigPayload(BaseModel):
    ssid: str | None = None
    passphrase: str | None = None
    regenerate: bool = False
    # Whether the hotspot name and password are also kept in a text file on
    # the SD card (older bridges do not offer the choice).
    boot_copy: bool | None = None


@router.post("/ap/config")
async def ap_config_set(payload: ApConfigPayload):
    """Change the recovery hotspot's name and/or password via the host bridge.

    An empty ssid resets the name to the default; regenerate mints a fresh
    device-generated password; an omitted field keeps the current value. If
    the hotspot is broadcasting right now, the bridge restarts it, so a user
    connected through it drops and rejoins with the new details.
    """
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    body: dict = {}
    if payload.ssid is not None:
        ssid = payload.ssid.strip()
        if ssid:
            err = validate_ap_ssid(ssid)
            if err:
                return JSONResponse({"ok": False, "error": err}, status_code=400)
        body["ssid"] = ssid  # empty resets to the default name
    if payload.regenerate:
        body["regenerate"] = True
    elif payload.passphrase:
        err = validate_ap_passphrase(payload.passphrase)
        if err:
            return JSONResponse({"ok": False, "error": err}, status_code=400)
        body["passphrase"] = payload.passphrase
    if payload.boot_copy is not None:
        body["boot_copy"] = bool(payload.boot_copy)
    if not body:
        return JSONResponse({"ok": False, "error": "Nothing to change."}, status_code=400)
    try:
        async with bridge_client(timeout=15.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/ap/config", json=body)
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})
