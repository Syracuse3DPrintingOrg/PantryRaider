"""Forager remote access: the WireGuard hub tunnel switch and its status."""

import httpx
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from ...config import settings, APP_NAME, device_hostname
from ...services.bridge import bridge_client
from ...services import tunnel_local
from ._common import router, _HOST_BRIDGE, _safe_error
from .cloud import _cloud_client, _cloud_disabled_message, _cloud_headers


# ---- Forager remote access (WireGuard hub tunnel, FoodAssistant-uczr) ----
# Remote access gives the kitchen a web address reachable from anywhere through
# Forager. The WireGuard endpoint lives on the Pi via the host bridge (only
# root can create the interface); this app orchestrates it: keygen on the
# bridge, enable on the cloud, then bring the interface up with what the cloud
# returns. Every failure rolls back and reports honestly, and no key material
# ever passes through the app (the private key stays on the device).

_TUNNEL_UPGRADE_MSG = (
    "Remote access is part of a Forager plan. Add it to your account, then turn "
    "on remote access here.")

# The honest error when a server cannot host a tunnel: no host bridge (not a Pi
# appliance) and no in-container WireGuard support.
_TUNNEL_UNSUPPORTED_MSG = (
    "Remote access needs WireGuard support on this server. See the docs. The "
    "container needs the NET_ADMIN capability and the /dev/net/tun device; the "
    "shipped Docker Compose grants both.")

# The port the app itself listens on inside the container (uvicorn binds
# 0.0.0.0:8000; see the Dockerfile CMD). On a server the tunnel runs in this
# container, so the cloud's Caddy route must target this internal port, not the
# host-published 9284 a Pi appliance uses.
SERVER_APP_PORT = 8000


def _tunnel_backend() -> str:
    """Which WireGuard backend brings the tunnel up on this device.

    "bridge": a Pi appliance, where the host bridge (root) owns the interface.
    "local":  a server with in-container WireGuard support (wg tools present and
              /dev/net/tun available), where the app runs wg-quick itself.
    "":       neither, so remote access cannot be hosted here.
    """
    if settings.is_pi_appliance():
        return "bridge"
    if tunnel_local.wg_available():
        return "local"
    return ""


def _tunnel_app_port(backend: str) -> int:
    """The port the cloud's Caddy route should target for this backend: the
    host-published 9284 on a Pi appliance, the app's own internal port on a
    server running WireGuard in-container."""
    return 9284 if backend == "bridge" else SERVER_APP_PORT


async def _tunnel_rollback(base: str, backend: str) -> None:
    """Undo a half-finished enable: take the interface down and tell the cloud
    to disable, both best effort so a rollback never raises. The interface is
    torn down through whichever backend owns it."""
    try:
        if backend == "local":
            await run_in_threadpool(tunnel_local.down)
        else:
            async with bridge_client(timeout=30.0) as c:
                await c.post(f"{_HOST_BRIDGE}/tunnel/down")
    except (httpx.HTTPError, OSError):
        pass
    try:
        async with _cloud_client() as c:
            await c.post(f"{base}/v1/tunnel/disable", headers=_cloud_headers())
    except httpx.HTTPError:
        pass


# The honest refusal when remote access is asked for without any second factor
# in place. Enabling a public web address without one would leave the kitchen
# reachable from the internet behind a single password.
_TUNNEL_NEEDS_2FA_MSG = (
    "Turn on two-factor authentication first (Settings, Security), or make sure "
    "your Forager account has it, so your kitchen is not exposed to the internet "
    "without a second factor.")


async def _account_has_2fa() -> bool:
    """Whether the linked Forager account has two-factor sign-in on.

    Reads the account_2fa flag from GET /v1/instance/me. Best effort: an
    unreachable or unlinked cloud returns False, so the caller falls back to
    requiring device 2FA rather than assuming a factor that may not exist."""
    if not settings.cloud_linked():
        return False
    base = settings.cloud_base_url.rstrip("/")
    try:
        async with _cloud_client() as client:
            r = await client.get(f"{base}/v1/instance/me", headers=_cloud_headers())
    except httpx.HTTPError:
        return False
    if r.status_code != 200:
        return False
    return bool((r.json() or {}).get("account_2fa"))


class TunnelEnablePayload(BaseModel):
    # An optional web address the user chose. Blank keeps the long-standing
    # behavior (the address is derived from the device hostname).
    subdomain: str = ""


@router.get("/tunnel/subdomain-available")
async def tunnel_subdomain_available(name: str = ""):
    """Is a chosen web address free? Proxies the cloud's availability check so
    the browser never talks to Forager directly. Degrades to a plain error the
    Web address field can show, never breaking the settings page.
    """
    if not settings.cloud_linked():
        return {"ok": False, "error":
                "Connect this device to Forager first to choose a web address."}
    base = settings.cloud_base_url.rstrip("/")
    try:
        async with _cloud_client() as c:
            r = await c.get(f"{base}/v1/tunnel/subdomain-available",
                            headers=_cloud_headers(), params={"name": name})
    except httpx.HTTPError as e:
        return {"ok": False, "error": _safe_error(
            f"Forager could not be reached ({e.__class__.__name__}).",
            settings.cloud_instance_token)}
    disabled = _cloud_disabled_message(r)
    if disabled:
        return {"ok": False, "disabled": True, "error": disabled}
    if r.status_code != 200:
        return {"ok": False, "error": f"Forager answered with status {r.status_code}."}
    body = r.json() or {}
    return {"ok": True, "available": bool(body.get("available")),
            "sanitized": body.get("sanitized", ""),
            "suggestion": body.get("suggestion", ""),
            "apex": body.get("apex", "")}


@router.post("/tunnel/enable")
async def tunnel_enable(payload: TunnelEnablePayload = TunnelEnablePayload()):
    """Turn on Forager remote access for this device.

    Safety gates come first: the device must be connected to Forager and must
    have a login password set. Then the WireGuard endpoint is set up through
    whichever backend fits this device: the host bridge on a Pi appliance, or
    in-container wg-quick on a server that has WireGuard support. A device with
    neither gets an honest error. Every path keygens, enables on the cloud (with
    the port the cloud's Caddy route should target, and the chosen web address
    when the user picked one), and brings the interface up; any failure rolls
    back and returns a plain-language error.
    """
    if not settings.cloud_linked():
        return {"ok": False, "error":
                "Connect this device to Forager first, then turn on remote access."}
    if not settings.auth_password:
        return {"ok": False, "error":
                "Set a password under Settings, Security first, so your kitchen "
                "is not open to the internet when you turn on remote access."}
    backend = _tunnel_backend()
    if not backend:
        return {"ok": False, "error": _TUNNEL_UNSUPPORTED_MSG}
    # A second factor must be AVAILABLE for outside logins before the kitchen is
    # put on the internet: either the device's own login 2FA, or a linked
    # Forager account that has 2FA (the cloud enforces it on the Forager path).
    if not settings.local_2fa_active() and not await _account_has_2fa():
        return {"ok": False, "needs_2fa": True, "error": _TUNNEL_NEEDS_2FA_MSG}

    base = settings.cloud_base_url.rstrip("/")
    hostname_hint = device_hostname() or APP_NAME

    # 1. Generate the device keypair. The private key stays on the device: on
    #    the bridge (root) for a Pi, in the container's data_dir for a server.
    if backend == "bridge":
        try:
            async with bridge_client(timeout=15.0) as c:
                kr = await c.post(f"{_HOST_BRIDGE}/tunnel/keygen")
        except httpx.HTTPError:
            return {"ok": False, "error":
                    "The device helper could not be reached to set up remote access."}
        if kr.status_code != 200:
            return {"ok": False, "error":
                    "The device could not set up remote access. Try again in a moment."}
        public_key = (kr.json() or {}).get("public_key", "")
    else:
        try:
            public_key = await run_in_threadpool(tunnel_local.keygen)
        except Exception:
            return {"ok": False, "error":
                    "The server could not set up remote access. Try again in a moment."}
    if not public_key:
        return {"ok": False, "error":
                "The device could not set up remote access. Try again in a moment."}

    # 2. Ask the cloud to admit this device (needs a plan that covers it). The
    #    app_port tells the cloud which port its Caddy route should reach: the
    #    host-published 9284 on a Pi, this container's internal port on a server.
    chosen_subdomain = (payload.subdomain or "").strip()
    try:
        async with _cloud_client() as c:
            cr = await c.post(
                f"{base}/v1/tunnel/enable", headers=_cloud_headers(),
                json={"public_key": public_key, "hostname_hint": hostname_hint,
                      "subdomain": chosen_subdomain,
                      "app_port": _tunnel_app_port(backend)})
    except httpx.HTTPError as e:
        return {"ok": False, "error": _safe_error(
            f"Forager could not be reached ({e.__class__.__name__}). "
            "Check the internet connection and try again.",
            settings.cloud_instance_token)}
    # A disabled account is refused before anything is set up on the cloud,
    # like the 402 below, so there is nothing to roll back.
    disabled = _cloud_disabled_message(cr)
    if disabled:
        return {"ok": False, "disabled": True, "error": disabled}
    if cr.status_code == 402:
        return {"ok": False, "needs_plan": True, "error": _TUNNEL_UPGRADE_MSG}
    if cr.status_code == 409:
        # The chosen web address is taken. Nothing was set up on the cloud, so
        # no rollback is needed; hand the free suggestion back to the field.
        detail = {}
        try:
            detail = (cr.json() or {}).get("detail", {})
        except ValueError:
            detail = {}
        detail = detail if isinstance(detail, dict) else {}
        return {"ok": False, "subdomain_taken": True,
                "suggestion": detail.get("suggestion", ""),
                "error": detail.get("message",
                                    "That web address is already taken. Try another one.")}
    if cr.status_code == 503:
        return {"ok": False, "error":
                "Forager remote access is temporarily unavailable. Try again "
                "in a little while."}
    if cr.status_code != 200:
        return {"ok": False, "error":
                f"Forager answered with status {cr.status_code}."}
    data = cr.json() or {}
    public_url = (data.get("public_url") or "").strip().rstrip("/")

    # 3. Bring the interface up with the parameters the cloud handed back.
    if backend == "bridge":
        up_body = {
            "address": data.get("tunnel_ip", ""),
            "server_public_key": data.get("server_public_key", ""),
            "endpoint": data.get("server_endpoint", ""),
            "allowed_ips": data.get("allowed_ips", ""),
            "dns": data.get("dns", ""),
            "keepalive": data.get("keepalive", 25),
        }
        try:
            async with bridge_client(timeout=45.0) as c:
                ur = await c.post(f"{_HOST_BRIDGE}/tunnel/up", json=up_body)
            up_ok = ur.status_code == 200
        except httpx.HTTPError:
            up_ok = False
    else:
        try:
            await run_in_threadpool(
                tunnel_local.up,
                data.get("tunnel_ip", ""), data.get("server_public_key", ""),
                data.get("server_endpoint", ""), data.get("allowed_ips", ""),
                data.get("keepalive", 25))
            up_ok = True
        except Exception:
            up_ok = False
    if not up_ok:
        await _tunnel_rollback(base, backend)
        return {"ok": False, "error":
                "The device could not finish turning on remote access. "
                "It has been switched back off."}

    # 4. Success: adopt the public address so phone links and QR match, and
    #    record that remote access is on.
    to_save = {"tunnel_enabled": True}
    if public_url:
        to_save["qr_public_url"] = public_url
    settings.save(to_save)
    return {"ok": True, "public_url": public_url}


@router.post("/tunnel/disable")
async def tunnel_disable():
    """Turn Forager remote access back off.

    Takes the interface down on the device and tells the cloud to disable,
    both best effort. The public web address is cleared from qr_public_url
    only when it is the tunnel's own address, so a separately configured
    reverse-proxy URL is left alone.
    """
    base = settings.cloud_base_url.rstrip("/")

    # Learn the tunnel's public address before disabling so we know whether the
    # stored QR address is the tunnel's or a user's own.
    tunnel_public = ""
    if settings.cloud_linked():
        try:
            async with _cloud_client() as c:
                sr = await c.get(f"{base}/v1/tunnel/status",
                                 headers=_cloud_headers())
            if sr.status_code == 200:
                tunnel_public = (sr.json() or {}).get("public_url", "").strip().rstrip("/")
        except httpx.HTTPError:
            tunnel_public = ""

    # Take the interface down through whichever backend owns it (bridge on a Pi,
    # in-container wg-quick on a server), best effort.
    try:
        if _tunnel_backend() == "local":
            await run_in_threadpool(tunnel_local.down)
        else:
            async with bridge_client(timeout=30.0) as c:
                await c.post(f"{_HOST_BRIDGE}/tunnel/down")
    except (httpx.HTTPError, OSError):
        pass
    if settings.cloud_linked():
        try:
            async with _cloud_client() as c:
                await c.post(f"{base}/v1/tunnel/disable", headers=_cloud_headers())
        except httpx.HTTPError:
            pass

    to_save = {"tunnel_enabled": False}
    stored = (settings.qr_public_url or "").strip().rstrip("/")
    if stored and tunnel_public and stored == tunnel_public:
        to_save["qr_public_url"] = ""
    settings.save(to_save)
    return {"ok": True}


@router.get("/tunnel/status")
async def tunnel_status():
    """Remote-access state for the settings card: the device setting merged
    with the cloud's view and the bridge interface's live handshake."""
    base = settings.cloud_base_url.rstrip("/")
    out = {
        "ok": True,
        "enabled": bool(settings.tunnel_enabled),
        "public_url": (settings.qr_public_url or "").rstrip("/") if settings.tunnel_enabled else "",
        "reachable": False,
        "up": False,
        "last_handshake_seconds": None,
    }
    if settings.cloud_linked():
        try:
            async with _cloud_client() as c:
                sr = await c.get(f"{base}/v1/tunnel/status",
                                 headers=_cloud_headers())
            if sr.status_code == 200:
                body = sr.json() or {}
                out["reachable"] = True
                out["enabled"] = bool(body.get("enabled", out["enabled"]))
                if body.get("public_url"):
                    out["public_url"] = str(body["public_url"]).rstrip("/")
                if body.get("last_handshake") is not None:
                    out["last_handshake_seconds"] = body.get("last_handshake")
        except httpx.HTTPError:
            out["reachable"] = False
    backend = _tunnel_backend()
    if backend == "bridge":
        try:
            async with bridge_client(timeout=10.0) as c:
                br = await c.get(f"{_HOST_BRIDGE}/tunnel/status")
            if br.status_code == 200:
                bbody = br.json() or {}
                out["up"] = bool(bbody.get("up"))
                if bbody.get("last_handshake_seconds") is not None:
                    out["last_handshake_seconds"] = bbody.get("last_handshake_seconds")
        except httpx.HTTPError:
            pass
    elif backend == "local":
        try:
            local = await run_in_threadpool(tunnel_local.status)
            out["up"] = bool(local.get("up"))
            if local.get("last_handshake_seconds") is not None:
                out["last_handshake_seconds"] = local.get("last_handshake_seconds")
        except OSError:
            pass
    return out
