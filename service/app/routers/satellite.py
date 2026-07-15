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
"""
import secrets

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy.orm import Session

import json

from ..config import settings, SATELLITE_PULL_FIELDS, device_hostname, APP_VERSION
from ..database import SessionLocal
from ..models.db_models import ExpiryDefault, StreamDeckProfile
from ..services import devices

router = APIRouter(prefix="/api/config", tags=["satellite"])


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


@router.get("/satellite")
def satellite_config(
    request: Request,
    x_api_key: str = Header(default=""),
    x_device_id: str = Header(default=""),
    x_device_hostname: str = Header(default=""),
    x_device_mode: str = Header(default=""),
    x_device_version: str = Header(default=""),
    x_device_ip: str = Header(default=""),
):
    """Hand a satellite the backend config it should mirror.

    Auth: X-API-Key must match this server's api_key. We refuse outright if the
    server has no api_key set, so config is never served unauthenticated.

    The satellite's identity headers ride along on this pull, so the request
    doubles as a heartbeat: we record the device and hand back any command queued
    for it (drained as it is returned).
    """
    valid = settings.valid_api_keys()
    if not valid:
        raise HTTPException(
            status_code=503,
            detail="This server has no API key set; set one to enable satellites.",
        )
    if not x_api_key or not any(secrets.compare_digest(x_api_key, k) for k in valid):
        raise HTTPException(status_code=401, detail="Invalid API key.")

    ip = x_device_ip or (request.client.host if request.client else None)
    command = devices.record_heartbeat(
        x_device_id,
        hostname=x_device_hostname or None,
        ip=ip,
        deployment_mode=x_device_mode or None,
        version=x_device_version or None,
    )

    config = {f: getattr(settings, f) for f in SATELLITE_PULL_FIELDS}
    return {
        "ok": True,
        "config": config,
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
        "gadget_config": {
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
        },
        "command": command,
    }
