"""LAN device pairing endpoints (main server side, FoodAssistant-4box).

A new satellite's setup wizard asks this server for its own API key instead of
the user copying one by hand:

  POST /api/pairing/request              open a request -> {request_id, code}
  GET  /api/pairing/status/{request_id}  the satellite's poll
  GET  /api/pairing/pending              undecided requests (Devices pane)
  POST /api/pairing/approve              mint a scoped key for the device
  POST /api/pairing/deny                 refuse the request

The request and status endpoints are deliberately unauthenticated (the
satellite has no key yet; see _ALWAYS_PUBLIC / _PUBLIC_PREFIXES in main.py),
so each one enforces its own gates here: the feature toggle
(``local_device_pairing_enabled``), a private-address check on the caller, and
main-server-only (a satellite owns no keys to hand out). Approve, deny, and
the pending list go through the normal auth middleware like every admin call.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..config import settings
from ..services import ha_events, pairing

router = APIRouter(prefix="/api/pairing", tags=["pairing"])


class PairingRequestBody(BaseModel):
    hostname: str = ""
    # What the device's key should do: satellite, ha, display or ha_rest.
    # Blank lets the server suggest one from the hostname.
    kind: str = ""


class PairingDecisionBody(BaseModel):
    request_id: str = ""
    name: str = ""  # approve only: key label; defaults to the device hostname
    kind: str = ""  # approve only: the key's scope; defaults to the suggestion


def _gate(request: Request) -> JSONResponse | None:
    """The shared refusals for the unauthenticated pairing endpoints."""
    if not settings.local_device_pairing_enabled:
        return JSONResponse({"ok": False, "error": "Device pairing is turned off "
                             "on this server (Settings, Security & Access)."},
                            status_code=403)
    if settings.is_satellite():
        return JSONResponse({"ok": False, "error": "This device is a satellite; "
                             "pair with the main server instead."}, status_code=403)
    # LAN-only, and not merely "the immediate peer looks private": a request that
    # reached us through a reverse proxy or the tunnel is refused, so pairing
    # (which mints an API key) is never exposed to the internet (FoodAssistant-
    # gbs8).
    if not pairing.is_local_network_request(request):
        return JSONResponse({"ok": False, "error": "Pairing only works from "
                             "the local network."}, status_code=403)
    return None


@router.post("/request")
async def request_pairing(body: PairingRequestBody, request: Request):
    """Open a pairing request (unauthenticated, LAN-only). Returns the code the
    satellite must display so the user can match it against this server's."""
    refusal = _gate(request)
    if refusal is not None:
        return refusal
    client_ip = request.client.host if request.client else ""
    # Throttle request spam so a LAN box cannot flood the admin's approval screen
    # or the event ring faster than MAX_PENDING alone bounds (FoodAssistant-7svb).
    from ..services.rate_limit import pairing_guard
    if pairing_guard.blocked(client_ip):
        return JSONResponse({"ok": False, "error": "Too many pairing requests. "
                             "Wait a few minutes and try again."}, status_code=429)
    pairing_guard.record(client_ip)
    created = pairing.create_request(body.hostname, client_ip, (body.kind or "").strip())
    if created is None:
        return JSONResponse({"ok": False, "error": "Too many pairing requests "
                             "are already waiting. Approve or deny them on the "
                             "server, or try again in a few minutes."},
                            status_code=429)
    # Surface the ask on the server's screens: a warning toast shows even when
    # on-screen Home Assistant events are off, and deep-links to the Devices
    # pane where the approve/deny card lives.
    who = body.hostname.strip() or "A kitchen device"
    ha_events.add_warning(
        f"{who} is asking to join with code {created['code']}. "
        "Approve or deny it in Settings, Devices.",
        title="Device pairing request", key="pairing", pane="pane-devices",
    )
    return {"ok": True, "request_id": created["request_id"],
            "code": created["code"], "expires_in": pairing.TTL_SECONDS}


@router.get("/status/{request_id}")
async def pairing_status(request_id: str, request: Request):
    """The satellite's poll (unauthenticated, LAN-only). Approved answers carry
    the minted key; the long random request_id is the read handle."""
    refusal = _gate(request)
    if refusal is not None:
        return refusal
    return {"ok": True, **pairing.get_status(request_id)}


@router.get("/pending")
async def pending():
    """Undecided requests for the Devices pane (auth-required, like approve)."""
    return {"ok": True, "requests": pairing.pending_requests()}


@router.post("/approve")
async def approve(body: PairingDecisionBody):
    """Mint a key of its own for the requesting device (auth-required).

    The key is limited to what that kind of device needs, shows in Settings,
    Security under its name, and can be narrowed or revoked there.
    """
    from ..services import credentials
    # Peek at the pending record for the default name before deciding.
    rec = next((r for r in pairing.pending_requests()
                if r["request_id"] == body.request_id), None)
    if rec is None:
        return JSONResponse({"ok": False, "error": "That pairing request has "
                             "expired or was already decided. Ask the device "
                             "to request access again."}, status_code=404)
    kind = (body.kind or "").strip() or rec.get("suggested_kind") or "satellite"
    if kind not in pairing.KINDS:
        return JSONResponse({"ok": False, "error": "Choose what this device's key "
                             "can do."}, status_code=400)
    name = body.name.strip() or rec["hostname"] or "Paired device"
    # Mint the key BEFORE marking the request approved, so a key the device
    # receives always authenticates; the rare double-click race revokes it.
    cred, key = credentials.mint(kind, name, "paired", created_by="pairing")
    if pairing.approve(body.request_id, key, name, cred["id"]) is None:
        try:
            credentials.revoke(cred["id"])
        except Exception:
            pass
        return JSONResponse({"ok": False, "error": "That pairing request was "
                             "already decided."}, status_code=409)
    return {"ok": True, "name": name, "kind": kind}


@router.post("/deny")
async def deny(body: PairingDecisionBody):
    """Refuse a pairing request (auth-required). No key is created."""
    if not pairing.deny(body.request_id):
        return JSONResponse({"ok": False, "error": "That pairing request has "
                             "expired or was already decided."}, status_code=404)
    return {"ok": True}
