"""Server-side registry of satellite (pi_remote) devices.

A satellite dials out to this server on every config pull (boot, the periodic
re-sync, or a manual sync), so we record it there: the heartbeat rides the
existing request, no separate channel. That makes discovery work on any network
topology, since the device always initiates the connection and the server never
needs to reach it first.

The registry also holds a one-slot command queue per device. An admin can queue
a command (for example "resync") from the server UI; the device drains it on its
next heartbeat and acts on it. Pull-based, so it too is topology independent.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..database import SessionLocal
from ..models.db_models import SatelliteDevice

# Commands the server may queue for a device. The satellite ignores any it does
# not recognise, so this list can grow without breaking older satellites.
KNOWN_COMMANDS = {"resync"}

# A device is considered online if it has been seen within this many seconds.
# Generous so a device on a long sync interval still reads as online between
# heartbeats; the satellite default interval is 15 minutes.
ONLINE_WINDOW_SECONDS = 45 * 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_heartbeat(
    device_id: str,
    *,
    hostname: str | None = None,
    ip: str | None = None,
    deployment_mode: str | None = None,
    version: str | None = None,
) -> str | None:
    """Upsert a device from a heartbeat and return any queued command.

    The command is consumed (cleared) as it is handed back, so each queued
    command runs once. Returns None when nothing is queued or device_id is
    empty.
    """
    if not device_id:
        return None
    db = SessionLocal()
    try:
        dev = db.query(SatelliteDevice).filter_by(device_id=device_id).first()
        if dev is None:
            dev = SatelliteDevice(device_id=device_id, first_seen=_now(), source="heartbeat")
            db.add(dev)
        if hostname is not None:
            dev.hostname = hostname
        if ip:
            dev.ip = ip
        if deployment_mode is not None:
            dev.deployment_mode = deployment_mode
        if version is not None:
            dev.version = version
        dev.last_seen = _now()
        command = dev.pending_command
        dev.pending_command = None  # drained: a queued command fires once
        db.commit()
        return command
    finally:
        db.close()


def _is_online(last_seen: str | None) -> bool:
    if not last_seen:
        return False
    try:
        seen = datetime.fromisoformat(last_seen)
    except ValueError:
        return False
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - seen).total_seconds()
    return age <= ONLINE_WINDOW_SECONDS


def _serialize(dev: SatelliteDevice) -> dict:
    return {
        "device_id": dev.device_id,
        "hostname": dev.hostname,
        "ip": dev.ip,
        "deployment_mode": dev.deployment_mode,
        "version": dev.version,
        "label": dev.label,
        "source": dev.source,
        "pending_command": dev.pending_command,
        "first_seen": dev.first_seen,
        "last_seen": dev.last_seen,
        "online": _is_online(dev.last_seen),
    }


def list_devices() -> list[dict]:
    """All known devices, most-recently-seen first, with an online flag."""
    db = SessionLocal()
    try:
        rows = db.query(SatelliteDevice).all()
    finally:
        db.close()
    out = [_serialize(r) for r in rows]
    out.sort(key=lambda d: d.get("last_seen") or "", reverse=True)
    return out


def queue_command(device_id: str, command: str) -> bool:
    """Queue a command for a device to pick up on its next heartbeat.

    Returns False for an unknown command or unknown device.
    """
    if command not in KNOWN_COMMANDS:
        return False
    db = SessionLocal()
    try:
        dev = db.query(SatelliteDevice).filter_by(device_id=device_id).first()
        if dev is None:
            return False
        dev.pending_command = command
        db.commit()
        return True
    finally:
        db.close()


def set_label(device_id: str, label: str) -> bool:
    db = SessionLocal()
    try:
        dev = db.query(SatelliteDevice).filter_by(device_id=device_id).first()
        if dev is None:
            return False
        dev.label = label or None
        db.commit()
        return True
    finally:
        db.close()


def forget_device(device_id: str) -> bool:
    """Drop a device from the registry. It reappears on its next heartbeat."""
    db = SessionLocal()
    try:
        dev = db.query(SatelliteDevice).filter_by(device_id=device_id).first()
        if dev is None:
            return False
        db.delete(dev)
        db.commit()
        return True
    finally:
        db.close()


def record_scan_result(ip: str, *, hostname: str | None = None,
                       version: str | None = None, deployment_mode: str | None = None) -> None:
    """Record a device found by a LAN scan that has not registered itself.

    Keyed by IP under a synthetic device_id so a manual scan can surface
    instances that have not yet sent a heartbeat (or never will, for a server).
    A real heartbeat later, carrying the device's true device_id, takes over as
    the authoritative row.
    """
    if not ip:
        return
    synthetic = f"scan:{ip}"
    db = SessionLocal()
    try:
        # If a heartbeat row already advertises this IP, do not shadow it.
        existing = db.query(SatelliteDevice).filter_by(ip=ip).first()
        if existing is not None and existing.source == "heartbeat":
            return
        dev = db.query(SatelliteDevice).filter_by(device_id=synthetic).first()
        if dev is None:
            dev = SatelliteDevice(device_id=synthetic, first_seen=_now(), source="scan")
            db.add(dev)
        dev.ip = ip
        if hostname is not None:
            dev.hostname = hostname
        if version is not None:
            dev.version = version
        if deployment_mode is not None:
            dev.deployment_mode = deployment_mode
        dev.last_seen = _now()
        db.commit()
    finally:
        db.close()
