"""Satellite config federation.

A satellite (deployment_mode=pi_remote) runs the full app but owns no backend
config of its own. It pulls everything it needs (Grocy/Mealie/AI keys and the
expiry defaults) from a main server through the single endpoint here, then talks
to those backends directly.

The endpoint is served by the MAIN server. It returns only the shareable
backend config (see SATELLITE_PULL_FIELDS) plus the expiry-defaults table, never
device-local secrets like the session key, UI password, or TOTP seed. Access is
gated by the same X-API-Key the rest of the headless API uses, so a server must
have an API key set before it will hand config to satellites.

From 0.21 the pull is authorized through the request's principal (a
satellite, older full-access or admin key) and what it carries depends on the
satellite: see services/fleet_relay.py. The AI, recipe and camera relays a
0.21 satellite calls instead live in routers/fleet.py, nested in this router.
"""
from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy.orm import Session

import json

from ..config import settings, SATELLITE_PULL_FIELDS, device_hostname, APP_VERSION
from ..database import SessionLocal
from ..models.db_models import ExpiryDefault, StreamDeckProfile
from ..services import devices

# No prefix: the pull declares its full path and the fleet relays are nested
# below, and FastAPI would apply a prefix here to the nested router too.
router = APIRouter(tags=["satellite"])


def _profiles_payload() -> list[dict]:
    """All saved Stream Deck profiles, serialized for a satellite to mirror."""
    db: Session = SessionLocal()
    try:
        rows = db.query(StreamDeckProfile).order_by(StreamDeckProfile.name).all()
        return [
            {
                "name": r.name,
                "deck_size": r.deck_size,
                "key_overrides": json.loads(r.key_overrides or "[]"),
                "updated_at": r.updated_at,
            }
            for r in rows
        ]
    finally:
        db.close()


def _alarms_payload() -> list[dict]:
    """This server's live gadget protection alarms, for a satellite to mirror.
    Best-effort: an alarm read that fails must not cost a satellite its config."""
    try:
        from ..services import gadgets
        return gadgets.active_alarms()
    except Exception:  # noqa: BLE001 - config federation is the job here
        return []


def _defaults_payload() -> list[dict]:
    """The expiry-defaults table, serialized for a satellite to mirror."""
    db: Session = SessionLocal()
    try:
        rows = db.query(ExpiryDefault).all()
        return [
            {
                "category": r.category,
                "name_pattern": r.name_pattern,
                "storage_type": r.storage_type,
                "default_days": r.default_days,
                "notes": r.notes,
                "priority": r.priority,
            }
            for r in rows
        ]
    finally:
        db.close()


_ALLOWED_SCOPES = ("satellite", "legacy", "admin")


def _refuse_puller(p) -> None:
    """The pull's own check, after the access middleware's.

    Only a key may pull (its value signs the reply): a satellite key, an
    older full-access key or an admin key. Old satellites show the 401 and
    503 texts in their sync status, so those stay word for word."""
    from ..services import access, credentials
    if p.kind == "key" and p.scope in _ALLOWED_SCOPES:
        return
    if p.kind == "anonymous" or p.bad_key:
        if not credentials.any_credential_exists():
            raise HTTPException(
                status_code=503,
                detail="This server has no API key set; set one to enable satellites.",
            )
        raise HTTPException(status_code=401, detail="Invalid API key.")
    if p.scope in _ALLOWED_SCOPES:
        # A signed-in browser or this device's own programs, with no key.
        raise HTTPException(status_code=401, detail="Invalid API key.")
    raise HTTPException(status_code=403, detail=access.SCOPE_MSG)


def _gadget_config_payload() -> dict:
    """Gadget device lists for the satellite relay (FoodAssistant-me3t)."""
    return {
        "gadgets_enabled": bool(settings.gadgets_enabled),
        "gadget_devices": settings.gadget_devices or [],
        "hygrometers_enabled": bool(settings.hygrometers_enabled),
        "hygrometer_devices": settings.hygrometer_devices or [],
        "buttons_enabled": bool(settings.buttons_enabled),
        "button_devices": settings.button_devices or [],
        "contacts_enabled": bool(settings.contacts_enabled),
        "contact_devices": settings.contact_devices or [],
        "cub_ble_advertise": bool(settings.cub_ble_advertise),
        "device_id": settings.device_id,
    }


@router.get("/api/config/satellite")
def satellite_config(
    request: Request,
    x_api_key: str = Header(default=""),
    x_device_id: str = Header(default=""),
    x_device_hostname: str = Header(default=""),
    x_device_mode: str = Header(default=""),
    x_device_version: str = Header(default=""),
    x_device_ip: str = Header(default=""),
    x_config_nonce: str = Header(default=""),
):
    """Hand a satellite the backend config it should mirror.

    Auth: a satellite, older full-access or admin key (X-API-Key). We refuse
    outright when no key exists here at all, so config is never served
    unauthenticated.

    The satellite's identity headers ride along on this pull, so the request
    doubles as a heartbeat: we record the device and hand back any command queued
    for it (drained as it is returned).

    What the config carries depends on the satellite (services/fleet_relay.py):
    one that relays (X-PR-Caps relay1) gets no AI or recipe keys, an older one
    keeps getting them while the owner's policy allows it.
    """
    from ..services import access, credentials, fleet_relay
    from ..services.satellite import config_signature, extras_digest

    p = access.principal(request)
    _refuse_puller(p)

    caps = set(p.caps or ())
    peer = request.client.host if request.client else ""
    client = p.client or request.headers.get("X-PR-Client", "")
    if p.credential_id:
        # Every pull, with or without relay1: the ratchet waits until every
        # device that pulled with this key in 30 days has sent relay1, so a
        # second Bandit sharing an extra key keeps its payload until it
        # updates too.
        try:
            credentials.mark_caps(p.credential_id, sorted(caps), device_id=x_device_id,
                                  client=client,
                                  ua=request.headers.get("User-Agent", ""), peer=peer)
        except Exception:  # noqa: BLE001 - bookkeeping never costs a pull
            pass
    relay = "relay1" in caps
    deck_proxy = "deckproxy1" in caps
    ratcheted = (not relay) and bool(p.credential_id) and credentials.relay_ratcheted(
        p.credential_id)

    # Heartbeat first, so the auto policy below already sees this pull's caps.
    # The address recorded is the one the pull came from: the Home Assistant
    # integration polls Bandits there, so a key holder cannot point it
    # elsewhere by reporting a different address.
    ip, unverified = devices.observed_ip(peer, x_device_ip,
                                         forwarded=p.origin == "proxied")
    command = devices.record_heartbeat(
        x_device_id,
        hostname=x_device_hostname or None,
        ip=ip or None,
        deployment_mode=x_device_mode or None,
        version=x_device_version or None,
        caps=sorted(caps),
        client=client,
        peer=peer,
        reported_ip=x_device_ip or None,
        trust=request.headers.get("X-Device-Trust") or None,
        ip_unverified=unverified,
    )

    service_legacy = (not ratcheted) and fleet_relay.effective_policy("service") == "allow"
    deck_legacy = (not ratcheted) and fleet_relay.effective_policy("deck") == "allow"
    if not service_legacy:
        try:
            db = SessionLocal()
            try:
                fleet_relay.maybe_raise_rotation_advisory(db)
            finally:
                db.close()
        except Exception:  # noqa: BLE001 - an advisory never costs a pull
            pass

    values = {f: getattr(settings, f) for f in SATELLITE_PULL_FIELDS}
    config = fleet_relay.build_satellite_config(
        values, relay=relay, deck_proxy=deck_proxy, service_legacy=service_legacy,
        deck_legacy=deck_legacy,
        block=fleet_relay.fleet_block(settings) if relay else None)
    # A satellite enforces the Home Assistant opt-in only once this server's
    # own key migration has run.
    try:
        hpv = int(settings.ha_key_policy_version or 0)
    except (TypeError, ValueError):
        hpv = 0
    config["ha_policy_version"] = 1 if hpv >= 1 else 0

    extras = {
        # The server's own hostname, so a satellite configured with a bare IP can
        # learn the mDNS name and fall back to <host>.local when DHCP reassigns
        # the host's IP (FoodAssistant-k9a8).
        "server_hostname": device_hostname(),
        # The server's running version, so a satellite with auto-update on can
        # converge on the same version as its server (FoodAssistant-k2kk).
        "server_version": APP_VERSION,
        "expiry_defaults": _defaults_payload(),
        "streamdeck_profiles": _profiles_payload(),
        # Gadget device lists for the satellite relay (FoodAssistant-me3t): a
        # satellite merges these into its own GET /gadgets/config, so its
        # Bluetooth reader also watches devices configured here on the
        # server. cub_ble_advertise and device_id ride along so a satellite's
        # Cub broadcast carries this server's flag and install tag.
        "gadget_config": _gadget_config_payload(),
        # The live protection alarms (FoodAssistant-me3t): a satellite relaying
        # its readings leaves alarm evaluation to this server, so the server's
        # alarms are mirrored onto the satellite's own screen. This pull is the
        # backstop for a satellite with no sensor reader of its own; the relay's
        # POST /gadgets/readings reply carries them within seconds otherwise.
        "gadget_alarms": _alarms_payload(),
        "command": command,
    }
    extras = json.loads(json.dumps(extras, default=str))
    # The signature covers config, so the digest of everything beside it goes
    # inside: a satellite from 0.21 on applies profiles, alarms and defaults
    # only when they match what the server signed.
    config["extras_sha256"] = extras_digest(extras)

    # Normalize through a JSON round-trip so the structure we sign is byte-for-byte
    # what the satellite decodes from the response (tuples become lists, etc.),
    # otherwise the signatures would not match on some field types.
    config = json.loads(json.dumps(config, default=str))
    # Prove to the satellite that this response came from a holder of its API key,
    # so a LAN impostor cannot poison its backend config (FoodAssistant-619i). The
    # satellite sends a fresh nonce; we sign nonce+config with the key it
    # presented (which matched one of ours) and echo the nonce back.
    nonce = (x_config_nonce or "").strip()
    signature = config_signature(x_api_key, nonce, config) if nonce else ""
    return {
        "ok": True,
        "config": config,
        # The nonce echoed back and the HMAC over nonce+config, so the satellite
        # can verify authenticity before applying credential-bearing fields.
        "config_nonce": nonce,
        "config_signature": signature,
        **extras,
    }


# The relays a 0.21 satellite calls (/api/fleet/*). Nested here, not included
# by main.py, so the app's entry module never imports this release's newer
# modules at import time.
from . import fleet as _fleet  # noqa: E402

router.include_router(_fleet.router)
