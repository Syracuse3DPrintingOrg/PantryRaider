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
from ..version_compare import readable_version

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
    caps=None,
    client: str | None = None,
    peer: str | None = None,
    reported_ip: str | None = None,
    trust: str | None = None,
    ip_unverified: bool | None = None,
) -> str | None:
    """Upsert a device from a heartbeat and return any queued command.

    The command is consumed (cleared) as it is handed back, so each queued
    command runs once. Returns None when nothing is queued or device_id is
    empty. The 0.21 fields (caps, client, peer, reported_ip, trust,
    ip_unverified) are stored only when given, so older callers leave them
    as they were.
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
            dev.version = readable_version(version) or None
        if caps is not None:
            dev.caps = ",".join(sorted({str(c) for c in caps if c}))[:200]
        if client is not None:
            dev.client = str(client)[:80]
        if peer is not None:
            dev.peer = str(peer)[:64]
        if reported_ip is not None:
            dev.reported_ip = str(reported_ip)[:64]
        if trust is not None:
            dev.trust = str(trust)[:120]
        if ip_unverified is not None:
            dev.ip_unverified = 1 if ip_unverified else 0
        dev.last_seen = _now()
        command = dev.pending_command
        dev.pending_command = None  # drained: a queued command fires once
        db.commit()
        return command
    finally:
        db.close()


# The Docker bridge range (lan_scan.looks_dockerish uses the same one): a pull
# that arrives from here came through Docker's port mapping, not from the
# device itself.
_DOCKER_NET = "172.16.0.0/12"


def _parse_ip(value):
    import ipaddress
    text = str(value or "").strip()
    if not text:
        return None
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return None
    mapped = getattr(addr, "ipv4_mapped", None)
    return mapped if mapped is not None else addr


def _is_private_report(addr) -> bool:
    """RFC 1918, ULA or link-local: an address a device on a home LAN has."""
    import ipaddress
    if addr is None or addr.is_loopback or addr.is_unspecified or addr.is_multicast:
        return False
    if addr.version == 4:
        nets = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")
    else:
        nets = ("fc00::/7", "fe80::/10")
    return any(addr in ipaddress.ip_network(n) for n in nets)


def observed_ip(peer, reported, forwarded: bool = False) -> tuple[str, bool]:
    """(address to record, unverified) for one satellite pull. Pure.

    The address a pull arrived from is what the registry records, so a key
    holder cannot point the Home Assistant integration (which polls Bandits
    at this address) somewhere else. The device's own report is used only
    when the observed peer cannot be the device: loopback, the Docker bridge,
    or a pull that came through a reverse proxy (``forwarded``), whose
    address belongs to the proxy. The report is taken only when it is a
    private LAN address, and is then marked unverified.
    """
    import ipaddress
    p = _parse_ip(peer)
    r = _parse_ip(reported)
    if not forwarded and p is not None and p.version == 6 and not p.is_loopback \
            and r is not None and r.version == 4 and _is_private_report(r):
        # A pull over IPv6 from a device that names its IPv4 LAN address:
        # the Home Assistant integration and the Devices list open
        # http://<ip>/, which an IPv6 address (a link-local one above all)
        # does not fit, so the LAN address is recorded, marked unverified.
        return str(r), True
    if not forwarded and p is not None and not p.is_loopback and not (
            p.version == 4 and p in ipaddress.ip_network(_DOCKER_NET)):
        return str(p), False
    if _is_private_report(r):
        return str(r), True
    if p is not None:
        return str(p), True
    return (str(peer or "").strip() or str(reported or "").strip())[:64], True


def _split_caps(value) -> list[str]:
    return [c for c in str(value or "").split(",") if c]


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
    from ..config import APP_VERSION
    from ..version_compare import compare_to, diff_level
    caps = _split_caps(getattr(dev, "caps", None))
    return {
        "device_id": dev.device_id,
        "hostname": dev.hostname,
        "ip": dev.ip,
        "deployment_mode": dev.deployment_mode,
        "version": readable_version(dev.version) or None,
        # Standardised update status against the server's own version, so the
        # Devices page can flag which satellites are behind (FoodAssistant-ny8r).
        "update_status": compare_to(readable_version(dev.version), APP_VERSION),
        # Traffic-light severity: same (green) / patch (yellow) / major_minor
        # (red) versus the server version (FoodAssistant-469o).
        "version_diff": diff_level(readable_version(dev.version), APP_VERSION),
        "label": dev.label,
        "source": dev.source,
        "pending_command": dev.pending_command,
        "first_seen": dev.first_seen,
        "last_seen": dev.last_seen,
        "online": _is_online(dev.last_seen),
        # 0.21: what the device reported on its last pull. Older rows have
        # none of it, which reads as an older satellite.
        "caps": caps,
        "relay": "relay1" in caps,
        "deck_proxy": "deckproxy1" in caps,
        "client": getattr(dev, "client", None) or "",
        "trust_fp": getattr(dev, "trust", None) or "",
        "ip_unverified": bool(getattr(dev, "ip_unverified", 0) or 0),
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


def lan_cidr_from_known_devices() -> str | None:
    """A /24 taken from a checked-in device's real LAN IP, preferring heartbeat
    rows. Lets a bridge-only server (which only sees its Docker subnet) scan the
    LAN where its satellites actually live, so a blank scan still finds the fleet.
    Skips loopback and Docker (172.16/12) addresses."""
    import ipaddress
    db = SessionLocal()
    try:
        rows = db.query(SatelliteDevice).all()
    finally:
        db.close()
    # Heartbeat rows first (a real device that reached the server), then any.
    rows.sort(key=lambda r: 0 if getattr(r, "source", "") == "heartbeat" else 1)
    for dev in rows:
        ip = (dev.ip or "").strip()
        if not ip or ip.startswith("127."):
            continue
        try:
            net = ipaddress.ip_network(f"{ip}/24", strict=False)
        except ValueError:
            continue
        if net.version != 4:
            continue  # a device recorded at an IPv6 address: no /24 to scan
        if net.subnet_of(ipaddress.ip_network("172.16.0.0/12")):
            continue  # Docker network, not a real LAN
        return str(net)
    return None


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
            dev.version = readable_version(version) or None
        if deployment_mode is not None:
            dev.deployment_mode = deployment_mode
        dev.last_seen = _now()
        db.commit()
    finally:
        db.close()
