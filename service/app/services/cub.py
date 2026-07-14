"""Bandit Cub support: the pure summary builder and the Cub device registry.

A Bandit Cub is a small ESP32 companion display that polls GET /cub/summary
(routers/cub.py) for everything it shows. This module keeps the decision logic
pure (what view to show, which blocks join the idle rotation, how per-Cub
overrides merge onto the global settings) so it unit-tests without a server,
per docs/design/bandit-cub.md. All IO (Grocy, timers, gadgets, the DB session)
stays in the router; the registry helpers below are the one exception, on the
exact services/devices.py pattern the satellite registry uses.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from ..database import SessionLocal
from ..models.db_models import CubDevice

# Idle views a Cub can be told to show when nothing takes over.
CUB_VIEWS = ("expiring", "rotation", "clock")

# Blocks the idle rotation may cycle through. Kept to what phase-1 firmware
# renders; unknown names in cub_rotation are filtered out rather than sent.
CUB_ROTATION_BLOCKS = ("expiring", "pending", "clock")

# The content settings a Cub cares about, as they appear in the summary's
# settings block (bare names). Per-Cub overrides may hold any subset of these.
_MERGE_KEYS = {
    "default_view", "timers_take_over", "probes_take_over",
    "alerts_take_over",
    "rotation", "rotate_seconds", "poll_seconds",
}

# A Cub polls every cub_poll_seconds (15 by default), so a generous few-minute
# window still flags a dead device quickly. Same online logic as satellites
# (services/devices.py), just sized to the faster heartbeat.
CUB_ONLINE_WINDOW_SECONDS = 5 * 60


# -- pure: settings merge ------------------------------------------------------


def merge_cub_settings(global_settings: dict, per_cub_overrides) -> dict:
    """The effective content settings for one Cub: global merged with the
    device's overrides. Pure.

    ``global_settings`` holds the bare-name keys (default_view, ...);
    ``per_cub_overrides`` is whatever the registry row stored, so anything
    malformed (not a dict, unknown keys, a wrong type, an unknown view) is
    ignored rather than breaking the device's poll.
    """
    merged = dict(global_settings)
    if not isinstance(per_cub_overrides, dict):
        return merged
    for key, value in per_cub_overrides.items():
        if key not in _MERGE_KEYS:
            continue
        base = global_settings.get(key)
        if key == "default_view":
            if value in CUB_VIEWS:
                merged[key] = value
        elif key == "rotation":
            if isinstance(value, list):
                merged[key] = value
        elif isinstance(base, bool):
            if isinstance(value, bool):
                merged[key] = value
        elif isinstance(base, int):
            if isinstance(value, int) and not isinstance(value, bool):
                merged[key] = value
        elif value is not None:
            merged[key] = value
    return merged


# -- pure: view decision and rotation ------------------------------------------


def decide_view(timers: list[dict], probes: list[dict], merged: dict,
                alerts: list[dict] | None = None) -> str:
    """What the Cub should show right now. Pure.

    Priority (docs/design/bandit-cub.md): "alert" when a protection alarm (a
    fridge out of range, a door left open; FoodAssistant-5c61) is live and
    the alerts takeover is on, since spoiling groceries outrank everything;
    then "timers" when any timer is running or ringing and the takeover
    toggle is on; else "probe" when a probe has a target set and takeover is
    on; else the configured default view. ``timers`` is the summary's timers
    block (running and just-expired, i.e. ringing, entries alike take the
    display).
    """
    if merged.get("alerts_take_over", True) and alerts:
        return "alert"
    if merged.get("timers_take_over") and timers:
        return "timers"
    if merged.get("probes_take_over") and any(
            isinstance(p, dict) and p.get("target_c") is not None for p in probes):
        return "probe"
    view = merged.get("default_view")
    return view if view in CUB_VIEWS else "expiring"


def rotation_blocks(merged: dict) -> list[str]:
    """The idle-rotation block list, already filtered to blocks the firmware
    knows how to draw, in the configured order, deduped. Pure."""
    raw = merged.get("rotation")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for name in raw:
        if name in CUB_ROTATION_BLOCKS and name not in out:
            out.append(name)
    return out


# -- pure: block assembly --------------------------------------------------------


def timers_block(all_timers: list[dict]) -> list[dict]:
    """The summary's timers list from services/timers.list_timers() output:
    id, label, and the epoch deadline the device ticks against locally. Pure."""
    out = []
    for t in all_timers:
        if not isinstance(t, dict):
            continue
        out.append({
            "id": t.get("id"),
            "label": t.get("label", ""),
            "deadline_epoch": t.get("deadline_epoch"),
            "expired": bool(t.get("expired", False)),
        })
    return out


def probes_block(gadget_devices: list[dict]) -> list[dict]:
    """Flatten services/gadgets.get_state() devices into per-probe entries:
    one row per probe with its device id/name, temperature, target, direction,
    and the device's staleness. Pure."""
    out = []
    for dev in gadget_devices:
        if not isinstance(dev, dict):
            continue
        for probe in dev.get("probes") or []:
            if not isinstance(probe, dict):
                continue
            out.append({
                "id": dev.get("id", ""),
                "name": dev.get("name", ""),
                "probe": probe.get("index"),
                "temp_c": probe.get("temp_c"),
                "target_c": probe.get("target_c"),
                "direction": probe.get("direction", "above"),
                "stale": bool(dev.get("stale", True)),
            })
    return out


def hygrometers_block(hygro_devices: list[dict]) -> list[dict]:
    """The summary's hygrometers list from services/gadgets.get_state()
    hygrometers: one row per sensor with its id, name, location label,
    temperature, humidity, and staleness, so a Cub can render fridge status.
    Additive and calm: malformed rows are skipped, never raised on. Pure."""
    out = []
    for dev in hygro_devices:
        if not isinstance(dev, dict):
            continue
        out.append({
            "id": dev.get("id", ""),
            "name": dev.get("name", ""),
            "location": dev.get("location", ""),
            "temp_c": dev.get("temp_c"),
            "humidity": dev.get("humidity"),
            "stale": bool(dev.get("stale", True)),
        })
    return out


def alerts_block(alarms: list[dict]) -> list[dict]:
    """The summary's active protection alarms (FoodAssistant-5c61) from
    services/gadgets.active_alarms(): one row per live alarm with its kind
    ("hygrometer" or "contact"), the device's name and location, the
    user-forward message, and when it started. Additive and calm: malformed
    rows are skipped, never raised on. Pure."""
    out = []
    for alarm in alarms or []:
        if not isinstance(alarm, dict):
            continue
        out.append({
            "kind": alarm.get("kind", ""),
            "id": alarm.get("device_id", ""),
            "name": alarm.get("name", ""),
            "location": alarm.get("location", ""),
            "message": alarm.get("message", ""),
            "started_epoch": int(alarm.get("started_epoch") or 0),
        })
    return out


def expiring_block(items: list[dict] | None, window_days: int, *,
                   ok: bool = True, top_n: int = 3) -> dict:
    """Bucket counts plus the few soonest names, from the same expiring items
    list the /expiring/count cache holds (raw Grocy stock rows enriched with
    days_remaining, sorted soonest first). ok=False (or items=None) degrades
    to calm zeros, never a raise. Pure."""
    if not ok or items is None:
        return {"ok": False, "expired": 0, "today": 0, "soon": 0,
                "window_days": window_days, "top": []}
    expired = today = soon = 0
    top: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        days = item.get("days_remaining")
        if not isinstance(days, (int, float)):
            continue
        if days < 0:
            expired += 1
        elif days == 0:
            today += 1
        elif days <= window_days:
            soon += 1
        else:
            continue
        if len(top) < top_n:
            name = (item.get("product") or {}).get("name") or item.get("name") or "Unknown"
            top.append({"name": name, "days": int(days)})
    return {"ok": True, "expired": expired, "today": today, "soon": soon,
            "window_days": window_days, "top": top}


def settings_block(merged: dict, *, units: str, clock_24h: bool) -> dict:
    """The summary's settings echo: the effective content settings plus the
    display conventions (units, clock) the rest of the fleet already uses, so
    the device needs no second endpoint. Pure."""
    return {
        "default_view": merged.get("default_view", "expiring"),
        "timers_take_over": bool(merged.get("timers_take_over", True)),
        "probes_take_over": bool(merged.get("probes_take_over", True)),
        "alerts_take_over": bool(merged.get("alerts_take_over", True)),
        "rotate_seconds": int(merged.get("rotate_seconds", 12)),
        "poll_seconds": int(merged.get("poll_seconds", 15)),
        "units": units,
        "clock_24h": clock_24h,
    }


def global_cub_settings(settings) -> dict:
    """The fleet-wide content settings as bare-name keys, read off the app
    settings object (cub_* fields in config.py)."""
    return {
        "default_view": settings.cub_default_view,
        "timers_take_over": settings.cub_timers_take_over,
        "probes_take_over": settings.cub_probes_take_over,
        "alerts_take_over": settings.cub_alerts_take_over,
        "rotation": list(settings.cub_rotation or []),
        "rotate_seconds": settings.cub_rotate_seconds,
        "poll_seconds": settings.cub_poll_seconds,
    }


# -- registry (services/devices.py pattern) -------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_overrides(raw: str | None) -> dict:
    """The row's overrides JSON as a dict; anything unreadable is {}."""
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record_cub_heartbeat(
    device_id: str,
    *,
    name: str | None = None,
    hardware_profile: str | None = None,
    firmware_version: str | None = None,
    ip: str | None = None,
) -> dict:
    """Upsert a Cub from its summary-poll headers and return the row's stored
    overrides dict (so the summary builder needs no second query).

    The poll IS the heartbeat: there is no separate registration call. An
    empty device_id returns {} without touching the DB, so curl testing the
    summary endpoint never creates a phantom row.
    """
    if not device_id:
        return {}
    db = SessionLocal()
    try:
        dev = db.query(CubDevice).filter_by(device_id=device_id).first()
        if dev is None:
            dev = CubDevice(device_id=device_id, first_seen=_now())
            db.add(dev)
        # The header name seeds the row but never overwrites a user's rename:
        # the card's editable name is authoritative once set.
        if name and not dev.name:
            dev.name = name
        if hardware_profile is not None:
            dev.hardware_profile = hardware_profile
        if firmware_version is not None:
            dev.firmware_version = firmware_version
        if ip:
            dev.ip = ip
        dev.last_seen = _now()
        overrides = _parse_overrides(dev.overrides)
        db.commit()
        return overrides
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
    return age <= CUB_ONLINE_WINDOW_SECONDS


def _serialize(dev: CubDevice) -> dict:
    from ..config import APP_VERSION
    from ..version_compare import compare_to, diff_level
    return {
        "device_id": dev.device_id,
        "name": dev.name or "",
        "hardware_profile": dev.hardware_profile or "",
        "firmware_version": dev.firmware_version or "",
        # Until per-release firmware manifests land, the reference version is
        # the server's own (Cub firmware versioning follows APP_VERSION at tag
        # time), giving the same up-to-date/behind badge satellites get.
        "update_status": compare_to(dev.firmware_version or "", APP_VERSION),
        "version_diff": diff_level(dev.firmware_version or "", APP_VERSION),
        "ip": dev.ip or "",
        "overrides": _parse_overrides(dev.overrides),
        "first_seen": dev.first_seen,
        "last_seen": dev.last_seen,
        "online": _is_online(dev.last_seen),
    }


def list_cubs() -> list[dict]:
    """All known Cubs, most-recently-seen first, with an online flag."""
    db = SessionLocal()
    try:
        rows = db.query(CubDevice).all()
    finally:
        db.close()
    out = [_serialize(r) for r in rows]
    out.sort(key=lambda d: d.get("last_seen") or "", reverse=True)
    return out


def rename_cub(device_id: str, name: str) -> bool:
    db = SessionLocal()
    try:
        dev = db.query(CubDevice).filter_by(device_id=device_id).first()
        if dev is None:
            return False
        dev.name = (name or "").strip() or None
        db.commit()
        return True
    finally:
        db.close()


def set_cub_overrides(device_id: str, overrides: dict) -> bool:
    """Store a Cub's per-device content overrides (any subset of the settings
    merge keys; unknown keys are dropped here so the row never accumulates
    junk). An empty dict clears every override."""
    if not isinstance(overrides, dict):
        return False
    clean = {k: v for k, v in overrides.items() if k in _MERGE_KEYS}
    db = SessionLocal()
    try:
        dev = db.query(CubDevice).filter_by(device_id=device_id).first()
        if dev is None:
            return False
        dev.overrides = json.dumps(clean)
        db.commit()
        return True
    finally:
        db.close()


def forget_cub(device_id: str) -> bool:
    """Drop a Cub from the registry. It reappears on its next poll."""
    db = SessionLocal()
    try:
        dev = db.query(CubDevice).filter_by(device_id=device_id).first()
        if dev is None:
            return False
        db.delete(dev)
        db.commit()
        return True
    finally:
        db.close()
