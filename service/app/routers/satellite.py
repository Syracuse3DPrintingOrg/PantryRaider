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

from ..config import settings, SATELLITE_PULL_FIELDS
from ..database import SessionLocal
from ..models.db_models import ExpiryDefault
from ..services import devices

router = APIRouter(prefix="/api/config", tags=["satellite"])


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
    if not settings.api_key:
        raise HTTPException(
            status_code=503,
            detail="This server has no API key set; set one to enable satellites.",
        )
    if not secrets.compare_digest(x_api_key, settings.api_key):
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
        "expiry_defaults": _defaults_payload(),
        "command": command,
    }
