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

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from ..database import SessionLocal
from ..models.db_models import CubDevice

# Idle views a Cub can be told to show when nothing takes over.
CUB_VIEWS = ("expiring", "rotation", "clock")

# Hardware profiles the flasher page offers and CI builds one factory image per
# (docs/design/bandit-cub.md "Hardware profiles"). A profile is data, so adding
# a board is a new row here plus a CI matrix row, never code. chip_family is the
# ESP Web Tools value used in the manifest; esptool_chip is the --chip argument
# in the copyable command line for people flashing without the browser.
CUB_PROFILES = {
    "tdisplay": {
        "label": "LilyGo T-Display",
        "board": "LilyGo T-Display (ESP32)",
        "chip_family": "ESP32",
        "esptool_chip": "esp32",
    },
    "tdisplay-s3": {
        "label": "LilyGo T-Display S3",
        "board": "LilyGo T-Display S3 (ESP32-S3)",
        "chip_family": "ESP32-S3",
        "esptool_chip": "esp32s3",
    },
    "touch7": {
        "label": "Waveshare ESP32-S3 Touch LCD 7",
        "board": "Waveshare ESP32-S3 Touch LCD 7 (ESP32-S3, touch)",
        "chip_family": "ESP32-S3",
        "esptool_chip": "esp32s3",
    },
}

# Blocks the idle rotation may cycle through. Kept to what phase-1 firmware
# renders; unknown names in cub_rotation are filtered out rather than sent.
CUB_ROTATION_BLOCKS = ("expiring", "pending", "clock")

# The content settings a Cub cares about, as they appear in the summary's
# settings block (bare names). Per-Cub overrides may hold any subset of these.
_MERGE_KEYS = {
    "default_view", "timers_take_over", "probes_take_over",
    "alerts_take_over",
    "rotation", "rotate_seconds", "poll_seconds",
    "auto_update",
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
    id, label, and the epoch deadline the device ticks against locally. Pure.

    The deadline goes out as a whole number of seconds (FoodAssistant-8qtx).
    Timers carry a float deadline internally (time.time() plus the duration),
    and a firmware JSON reader that asks for an integer gets nothing back from
    a value with a decimal point in it: the Cub read every deadline as zero and
    showed a stuck 0:00. Sub-second precision means nothing to a display that
    ticks once a second, so the wire format is the honest place to round. Keep
    this an int even if a device seems to cope with a float.
    """
    out = []
    for t in all_timers:
        if not isinstance(t, dict):
            continue
        deadline = t.get("deadline_epoch")
        out.append({
            "id": t.get("id"),
            "label": t.get("label", ""),
            "deadline_epoch": (round(deadline)
                               if isinstance(deadline, (int, float))
                               and not isinstance(deadline, bool) else 0),
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
        # Whether this Cub may install new firmware by itself. Additive, so
        # firmware too old to know about it simply ignores the key.
        "auto_update": bool(merged.get("auto_update", True)),
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
        "auto_update": settings.cub_auto_update,
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


# -- firmware: manifest, asset naming, cache paths -----------------------------
# The flasher page (GET /ui/cubs) flashes over Web Serial with ESP Web Tools,
# which reads an ESP Web Tools manifest and fetches the firmware parts from the
# manifest's own origin. Both are served same-origin under /cub/firmware/*
# (routers/cub.py); these helpers stay pure so the shapes and paths unit-test
# without a server, network, or the data directory.


def firmware_asset_name(profile: str, version: str) -> str:
    """The release-asset / cache filename for a profile at a version. This is
    the exact name CI attaches to a GitHub release
    (bandit-cub-<profile>-<version>.factory.bin)."""
    return f"bandit-cub-{profile}-{version}.factory.bin"


def firmware_dir(data_dir) -> Path:
    """Where fetched firmware is cached (and where a locally built image can be
    dropped for testing): data_dir/cub-firmware/."""
    return Path(data_dir) / "cub-firmware"


def local_override_path(data_dir, profile: str) -> Path:
    """A locally built image that wins over any release fetch, for development
    and testing: data_dir/cub-firmware/<profile>.factory.bin. Drop a file here
    and the .bin endpoint serves it directly, no release needed."""
    return firmware_dir(data_dir) / f"{profile}.factory.bin"


def cached_firmware_path(data_dir, profile: str, version: str) -> Path:
    """The on-disk cache of a fetched release asset, keyed by version+profile so
    repeat flashes are local and work offline after the first download."""
    return firmware_dir(data_dir) / firmware_asset_name(profile, version)


def release_download_url(repo: str, version: str, profile: str) -> str:
    """The pinned public-repo release-asset URL for a profile at a version. The
    host is always github.com (constructed from constants, never user input), so
    the firmware proxy has no SSRF surface."""
    return (f"https://github.com/{repo}/releases/download/"
            f"v{version}/{firmware_asset_name(profile, version)}")


# A factory image is the whole flash layout: bootloader, partition table, then
# the app. An update over the air replaces only the app, so the image a Cub
# downloads for itself is the factory image from the app's flash offset on.
# Every board in the fleet uses ESPHome's default partition table, which puts
# the app at 0x10000. 0xE9 is the magic byte every ESP app image starts with,
# which is what makes the slice checkable rather than hopeful.
CUB_APP_OFFSET = 0x10000
_ESP_IMAGE_MAGIC = 0xE9


def ota_image_from_factory(data: bytes | None) -> bytes | None:
    """The app-only image an over-the-air update flashes, sliced out of a
    factory image. None when the bytes are not a factory image with an app
    where one belongs, so a truncated or unexpected download is skipped
    instead of sent to a device. Pure."""
    if not data or len(data) <= CUB_APP_OFFSET:
        return None
    app = data[CUB_APP_OFFSET:]
    if app[0] != _ESP_IMAGE_MAGIC:
        return None
    return app


def firmware_ota_block(profile: str, factory_image: bytes | None) -> dict | None:
    """The manifest's ota block for one profile: where a Cub fetches the app
    image and the md5 it must match. None when there is no usable image, in
    which case the manifest simply goes out without it. Pure.

    The path is absolute rather than relative to the manifest URL, because the
    Cub asks for the manifest with a ?profile= query and only an absolute path
    resolves the same either way.
    """
    app = ota_image_from_factory(factory_image)
    if app is None:
        return None
    return {
        "path": f"/cub/firmware/{profile}.ota.bin",
        "md5": hashlib.md5(app).hexdigest(),
    }


def firmware_manifest(profile: str, version: str,
                      ota: dict | None = None) -> dict | None:
    """The firmware manifest for one profile, or None for an unknown profile.

    Two readers share it. ESP Web Tools (the browser flasher on /ui/cubs)
    reads "parts", whose path is same-origin-relative to the manifest URL so
    the binary comes back through our own proxy (a cross-origin part would be
    refused). A Cub already in the kitchen reads "ota", which is what lets it
    update itself; it is left out when no image is available, and a reader
    that does not know the key ignores it.
    """
    meta = CUB_PROFILES.get(profile)
    if not meta:
        return None
    build = {
        "chipFamily": meta["chip_family"],
        "parts": [{"path": f"{profile}.bin", "offset": 0}],
    }
    if ota:
        build["ota"] = ota
    return {
        "name": f"Bandit Cub ({meta['label']})",
        "version": version,
        "new_install_prompt_erase": True,
        "builds": [build],
    }


def esptool_command(profile: str, version: str) -> str:
    """The copyable one-line flash command for people not using the browser
    flasher (Firefox/Safari, or a headless box). Empty for an unknown profile."""
    meta = CUB_PROFILES.get(profile)
    if not meta:
        return ""
    return (f"esptool.py --chip {meta['esptool_chip']} --baud 460800 "
            f"write_flash 0x0 {firmware_asset_name(profile, version)}")


# --------------------------------------------------------------------------
# BLE advertisement relay (FoodAssistant-nn3u)
# --------------------------------------------------------------------------
#
# A Cub sits in the kitchen with a live radio. A server install (Docker on a
# NAS) usually has no Bluetooth radio at all, so it cannot see the fridge
# hygrometer, the door sensor, or the shelf button that a Cub hears perfectly
# well. The relay closes that gap: the Cub forwards the raw advertisement
# bytes to POST /cub/ble-adv and the server decodes them HERE, with the same
# decoders the reader daemon uses (gadgets/foodassistant_gadgets/decoders.py),
# so a sensor heard by a Cub and a sensor heard by a Pi's own radio are one
# code path and one set of alarms.
#
# What the Cub filters on is not baked into firmware: the server hands it an
# allowlist in /cub/summary, derived below from what the decoders actually
# match. Adding a decoder therefore reaches the whole Cub fleet on the next
# poll, with no reflash, and the allowlist cannot drift away from the
# decoders because it is built from their own constants.

# The decoders live in the reader package, which is installed on the HOST of a
# Pi appliance (/opt/foodassistant), not inside the app container. So the
# import is guarded: where the package is absent the relay reports itself
# unavailable, publishes no allowlist, and accepts nothing, rather than
# pretending to work. See ble_relay_available().
try:  # pragma: no cover - exercised by both branches in the test suite
    from foodassistant_gadgets import decoders as _decoders
except Exception:  # noqa: BLE001 - any import trouble means "no decoders here"
    _decoders = None


def _norm_id(value) -> str:
    """A device id in the one canonical form every gadgets surface stores."""
    return str(value or "").strip().upper()


def _uuid16(uuid: str) -> int:
    """The 16-bit id inside a full 128-bit Bluetooth base UUID string, which is
    the form the decoders keep their service UUIDs in and the form a radio
    actually advertises. Pure."""
    return int(str(uuid)[4:8], 16)


def _relay_allowlist_source() -> dict:
    """The allowlist, derived from the decoders' own matching constants.

    Every entry below exists because some identify_* function keys on it, and
    nothing is listed that the decoders cannot read from an advertisement
    alone. Deliberately absent, per docs/design/bandit-cub.md: the
    connect-and-notify brands (Inkbird iBBQ on 0xFFF0, ThermoPro TP25,
    BlueDot). Their advertisement carries no reading, so relaying it would put
    a device on screen that a radio-less server can never read. Those need a
    reader in range, and the docs say so plainly.
    """
    d = _decoders
    if d is None:
        return {"company_ids": [], "service_uuids": [], "names": []}
    # Manufacturer company ids an identify_* keys on directly.
    company_ids = [
        d.COMBUSTION_MANUFACTURER_ID,        # identify() -> combustion probes
        d.GOVEE_HYGROMETER_MANUFACTURER_ID,  # identify_hygrometer() -> Govee
    ]
    # Service-data UUIDs an identify_* keys on. 16-bit, which is how they are
    # advertised and how the firmware compares them.
    service_uuids = [
        _uuid16(d.BTHOME_SERVICE_UUID),   # 0xFCD2: BTHome v2 buttons, contacts
        _uuid16(d.XIAOMI_SERVICE_UUID),   # 0xFE95: MiBeacon buttons, contacts
        _uuid16(d.ATC_SERVICE_UUID),      # 0x181A: Xiaomi on ATC/pvvx firmware
    ] + [_uuid16(u) for u in d.SWITCHBOT_SERVICE_UUIDS]  # 0xFD3D, 0x0D00
    # Name prefixes. These are not decoration: a TempSpike and an Inkbird
    # IBS-TH roll their temperature bytes THROUGH the company id, so every
    # advertisement carries a different one and no company-id allowlist can
    # ever catch them; a Govee grill is matched by payload shape under any id.
    # For that whole family the advertised name is the only stable filter.
    names = (list(d._ROOM_SENSOR_NAME_PREFIXES)      # Govee GVH50xx hygrometers
             + list(d._ATC_NAME_PREFIXES)            # atc_, lywsd03
             + list(d._INKBIRD_HYGRO_NAME_PREFIXES)  # ibs-th, sps, tps, ith-
             + list(d._TEMPSPIKE_NAME_PREFIXES)      # tp96, tp97, tempspike
             + list(d._GOVEE_GRILL_NAME_PREFIXES))   # gvh518x grills
    return {
        "company_ids": sorted(set(company_ids)),
        "service_uuids": sorted(set(service_uuids)),
        "names": sorted(set(names)),
    }


# How many packets one POST may carry, and how long a Cub may sit on a part
# full batch. The firmware batches to whichever comes first; both ride the
# summary so the pacing is tunable server-side without a reflash.
BLE_RELAY_BATCH_MAX = 10
BLE_RELAY_BATCH_MS = 2000
# Hard caps the endpoint enforces. The batch cap is what a well-behaved Cub
# sends; the accept cap is what the endpoint will tolerate before saying no,
# with room for a firmware that batches a little differently.
BLE_RELAY_MAX_PACKETS = 25
# A BLE advertisement plus its scan response: 31 + 31 bytes is the whole
# addressable space, so anything longer is not an advertisement.
BLE_RELAY_MAX_ADV_BYTES = 62
# Per-Cub rate limit: a token bucket refilled at a steady rate. A Cub batching
# 10 packets every 2 seconds needs 0.5 posts/s; the ceiling leaves generous
# headroom for a busy kitchen and still bounds a misbehaving or hostile
# device to something the server shrugs off.
BLE_RELAY_RATE_PER_SEC = 2.0
BLE_RELAY_BURST = 20.0


def ble_relay_available() -> bool:
    """Whether this install can actually decode a relayed advertisement.

    False where the reader package is not importable (the stock app container
    today), which is the one honest answer: the endpoint then declines and the
    summary publishes no allowlist, so no Cub ever burns radio time relaying
    into a void."""
    return _decoders is not None


def ble_relay_enabled(settings) -> bool:
    """Whether the relay is on: the setting AND a decoder package to feed."""
    return bool(getattr(settings, "cub_ble_relay", False)) and ble_relay_available()


def ble_relay_block(settings) -> dict | None:
    """The ble_relay block of /cub/summary, or None when the relay is off (so
    the key is simply absent and a Cub keeps its radio to itself).

    Additive: firmware that predates the relay ignores the block, and firmware
    that supports it treats an absent block as "do not relay"."""
    if not ble_relay_enabled(settings):
        return None
    allow = _relay_allowlist_source()
    return {
        "enabled": True,
        "company_ids": allow["company_ids"],
        "service_uuids": allow["service_uuids"],
        "names": allow["names"],
        "max_packets": BLE_RELAY_BATCH_MAX,
        "interval_ms": BLE_RELAY_BATCH_MS,
    }


# -- Raw advertisement parsing -------------------------------------------------
#
# A Cub sends the advertisement exactly as its radio received it: the flat list
# of AD structures (length, type, data), advertisement and scan response
# concatenated. The reader daemon never sees this because bleak parses it
# first, so the relay parses it here into the same shapes bleak produces
# (a name, {company_id: bytes}, {uuid_string: bytes}, [uuid_string]) and then
# hands those to the very same identify_*/decode_* functions. Pure.

# Bluetooth Core Supplement, Part A, section 1: the AD types we read.
_AD_NAME_SHORT = 0x08
_AD_NAME_COMPLETE = 0x09
_AD_UUID16_PARTIAL = 0x02
_AD_UUID16_COMPLETE = 0x03
_AD_UUID128_PARTIAL = 0x06
_AD_UUID128_COMPLETE = 0x07
_AD_SERVICE_DATA_16 = 0x16
_AD_SERVICE_DATA_128 = 0x21
_AD_MANUFACTURER = 0xFF

_UUID_BASE = "-0000-1000-8000-00805f9b34fb"


def _uuid16_str(value: int) -> str:
    """A 16-bit UUID as the full base-UUID string the decoders match on."""
    return f"0000{value:04x}{_UUID_BASE}"


def _uuid128_str(raw: bytes) -> str:
    """A 128-bit UUID from its little-endian advertisement bytes."""
    h = raw[::-1].hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def parse_advertisement(raw: bytes) -> dict:
    """Split raw advertisement bytes into the shapes the decoders expect.

    Returns {"name", "manufacturer_data", "service_data", "service_uuids"}.
    Malformed input is not an error: a truncated or nonsense AD structure ends
    the walk and whatever parsed cleanly so far is returned, exactly as a real
    radio stack shrugs off a bad advertisement. Pure, and never raises.
    """
    name = ""
    manufacturer_data: dict = {}
    service_data: dict = {}
    service_uuids: list = []
    raw = bytes(raw or b"")
    i = 0
    while i < len(raw):
        length = raw[i]
        if length == 0:
            break  # the standard end-of-data marker (and what padding looks like)
        if i + 1 + length > len(raw):
            break  # truncated structure: trust nothing past here
        ad_type = raw[i + 1]
        data = raw[i + 2:i + 1 + length]
        if ad_type in (_AD_NAME_COMPLETE, _AD_NAME_SHORT):
            if not name:  # a complete name never loses to a later short one
                name = data.decode("utf-8", "replace")
        elif ad_type == _AD_MANUFACTURER and len(data) >= 2:
            # The company id is the first two bytes, little-endian, and bleak
            # strips it into the dict key. The TempSpike and the Inkbird
            # IBS-TH roll temperature bytes through that id, so a single
            # advertisement can carry several keys; insertion order is
            # preserved because their decoders read the last one.
            company = int.from_bytes(data[0:2], "little")
            manufacturer_data[company] = data[2:]
        elif ad_type == _AD_SERVICE_DATA_16 and len(data) >= 2:
            service_data[_uuid16_str(int.from_bytes(data[0:2], "little"))] = data[2:]
        elif ad_type == _AD_SERVICE_DATA_128 and len(data) >= 16:
            service_data[_uuid128_str(data[0:16])] = data[16:]
        elif ad_type in (_AD_UUID16_COMPLETE, _AD_UUID16_PARTIAL):
            for off in range(0, len(data) - 1, 2):
                service_uuids.append(
                    _uuid16_str(int.from_bytes(data[off:off + 2], "little")))
        elif ad_type in (_AD_UUID128_COMPLETE, _AD_UUID128_PARTIAL):
            for off in range(0, len(data) - 15, 16):
                service_uuids.append(_uuid128_str(data[off:off + 16]))
        i += 1 + length
    return {
        "name": name,
        "manufacturer_data": manufacturer_data,
        "service_data": service_data,
        "service_uuids": service_uuids,
    }


def normalize_packet(raw) -> dict | None:
    """Validate one relayed packet, or None when it is not usable.

    Hard rules, because this arrives from a device on the LAN: the MAC must
    look like a MAC, the advertisement must be valid hex no longer than a real
    advertisement can be, and the RSSI must be a plausible dBm. Unknown fields
    are ignored so a newer firmware can add one without a server update. Pure,
    and never raises: a bad packet is a counted drop, not an error.
    """
    if not isinstance(raw, dict):
        return None
    mac = _norm_id(raw.get("mac"))
    if len(mac) != 17 or any(
            len(part) != 2 or any(c not in "0123456789ABCDEF" for c in part)
            for part in mac.split(":")) or mac.count(":") != 5:
        return None
    adv_hex = str(raw.get("adv") or "").strip()
    if not adv_hex or len(adv_hex) % 2 or len(adv_hex) > BLE_RELAY_MAX_ADV_BYTES * 2:
        return None
    try:
        adv = bytes.fromhex(adv_hex)
    except ValueError:
        return None
    rssi = raw.get("rssi")
    if not isinstance(rssi, int) or isinstance(rssi, bool) or not -127 <= rssi <= 20:
        rssi = None
    return {"mac": mac, "rssi": rssi, "adv": adv}


# -- Decode-through ------------------------------------------------------------
#
# The routing below deliberately mirrors the reader daemon's _on_advertisement
# (gadgets/foodassistant_gadgets/daemon.py), class for class and in the same
# order: hygrometer, contact, button, then the advertising probes. A device
# already configured yields a reading; an unknown one yields a discovered
# entry, so a fridge sensor can be found and added through a Cub exactly as it
# is through a Pi's own radio.

# Button presses repeat: the radio hears one press as a burst of identical
# advertisements, and a batch can straddle two posts. The same dedupe the
# daemon uses runs here, keyed per device, so one press is one press no matter
# how many Cubs heard it or how the batches fell.
_relay_button_seen: dict = {}


def reset_relay_state() -> None:
    """Clear the relay's dedupe and rate-limit memory (used by tests)."""
    _relay_button_seen.clear()
    _relay_buckets.clear()


def _configured_ids(devices) -> dict:
    return {_norm_id(d.get("id")): d for d in devices or []
            if isinstance(d, dict) and _norm_id(d.get("id"))}


def decode_packets(packets: list, source: str, now: float | None = None) -> dict:
    """Decode a batch of relayed advertisements into one gadgets push.

    Returns {"payload", "accepted", "dropped", "matched"}: the payload is
    exactly the shape POST /gadgets/readings takes (devices + discovered +
    a "source" tag), so the caller hands it to the ordinary ingest and the
    readings, the "via Cub" tag, and the alarms all behave as they do for
    every other source. Never raises: a packet that will not decode is
    counted and skipped.
    """
    import time as _time

    from . import gadgets, gadgets_buttons

    now = _time.time() if now is None else now
    out_devices: list = []
    out_discovered: list = []
    accepted = 0
    dropped = 0
    matched = 0

    if not ble_relay_available():
        return {"payload": {}, "accepted": 0,
                "dropped": len(packets or []), "matched": 0}

    d = _decoders
    hygros = _configured_ids(gadgets.configured_hygrometers())
    contacts = _configured_ids(gadgets.configured_contacts())
    buttons = _configured_ids(gadgets_buttons.configured_buttons())
    probes = _configured_ids(gadgets.configured_devices())

    def _discovered(dev_id, protocol, name, rssi, kind="", supported=True):
        entry = {"id": dev_id, "protocol": protocol, "name": name or "",
                 "rssi": rssi, "supported": supported, "ts": now}
        if kind:
            entry["kind"] = kind
        out_discovered.append(entry)

    for raw in packets or []:
        pkt = normalize_packet(raw)
        if pkt is None:
            dropped += 1
            continue
        accepted += 1
        try:
            adv = parse_advertisement(pkt["adv"])
        except Exception:  # noqa: BLE001 - a parse can never break the batch
            dropped += 1
            continue
        name = adv["name"]
        md = adv["manufacturer_data"]
        sd = adv["service_data"]
        dev_id = _norm_id(pkt["mac"])
        rssi = pkt["rssi"]
        try:
            hygro = d.identify_hygrometer(name, md, sd)
            if hygro:
                matched += 1
                if dev_id in hygros:
                    decoded = d.decode_hygrometer(hygro, md, sd)
                    if decoded:
                        out_devices.append({
                            "id": dev_id, "kind": "hygrometer",
                            "protocol": hygro,
                            "name": hygros[dev_id].get("name") or name,
                            "temp_c": decoded.get("temp_c"),
                            "humidity": decoded.get("humidity_pct"),
                            "battery": decoded.get("battery_pct"),
                            "rssi": rssi, "ts": now,
                        })
                else:
                    _discovered(dev_id, hygro, name, rssi, kind="hygrometer")
                continue
            contact = d.identify_contact(name, md, sd)
            if contact:
                matched += 1
                if dev_id in contacts:
                    decoded = d.decode_contact(contact, md, sd)
                    if decoded and decoded.get("open") is not None:
                        out_devices.append({
                            "id": dev_id, "kind": "contact",
                            "protocol": contact,
                            "name": contacts[dev_id].get("name") or name,
                            "open": bool(decoded["open"]),
                            "battery": decoded.get("battery_pct"),
                            "rssi": rssi, "ts": now,
                        })
                else:
                    _discovered(dev_id, contact, name, rssi, kind="contact")
                continue
            button = d.identify_button(name, md, sd)
            if button:
                matched += 1
                decoded = d.decode_button(button, sd) or {}
                if dev_id in buttons:
                    label = buttons[dev_id].get("name") or name
                    out_devices.append({
                        "id": dev_id, "kind": "button", "protocol": button,
                        "name": label, "battery": decoded.get("battery"),
                        "rssi": rssi, "ts": now,
                    })
                    for ev in d.dedupe_button_events(_relay_button_seen, dev_id,
                                                     decoded, now):
                        out_devices.append({
                            "id": dev_id, "kind": "button", "protocol": button,
                            "name": label, "battery": decoded.get("battery"),
                            "rssi": rssi,
                            "event": {"button": ev.get("button"),
                                      "type": ev.get("event"),
                                      "counter": decoded.get("counter")},
                        })
                else:
                    _discovered(dev_id, button, name, rssi, kind="button")
                continue
            if d.is_room_sensor(name, md):
                continue
            protocol = d.identify(name, md, adv["service_uuids"])
            if not protocol:
                if d.looks_like_probe(name):
                    _discovered(dev_id, "", name, rssi, supported=False)
                continue
            matched += 1
            if protocol == d.PROTOCOL_COMBUSTION:
                decoded = d.decode_combustion_advertising(
                    md.get(d.COMBUSTION_MANUFACTURER_ID) or b"")
                if not decoded:
                    continue
                serial = _norm_id(decoded["serial"])
                if serial in probes:
                    out_devices.append({
                        "id": serial, "protocol": protocol,
                        "name": (probes[serial].get("name")
                                 or f"Combustion {decoded['probe_id']}"),
                        "probes": [{"index": i + 1, "temp_c": t}
                                   for i, t in enumerate(decoded["temps_c"])],
                        "battery": 5 if decoded["battery_low"] else 100,
                        "rssi": rssi, "ts": now,
                        "instant_read": decoded["instant_read"],
                    })
                else:
                    _discovered(serial, protocol,
                                name or f"Combustion probe {decoded['probe_id']}",
                                rssi)
                continue
            if protocol == d.PROTOCOL_TEMPSPIKE:
                decoded = d.decode_tempspike_from_manufacturer(md)
                if not decoded or dev_id not in probes:
                    _discovered(dev_id, protocol, name, rssi)
                    continue
                out_devices.append({
                    "id": dev_id, "protocol": protocol,
                    "name": probes[dev_id].get("name") or name,
                    # Probe 1 is the tip in the food, probe 2 the ambient/pit.
                    "probes": [{"index": 1, "temp_c": decoded["tip_c"],
                                "role": "internal"},
                               {"index": 2, "temp_c": decoded["ambient_c"],
                                "role": "ambient"}],
                    "battery": decoded["battery"], "rssi": rssi, "ts": now,
                })
                continue
            if protocol == d.PROTOCOL_GOVEE_GRILL:
                value = d._govee_grill_value(md)
                decoded = d.decode_govee_grill(value) if value else None
                if not decoded or dev_id not in probes:
                    _discovered(dev_id, protocol, name, rssi)
                    continue
                targets = decoded.get("targets") or []
                entry_probes = []
                for i, t in enumerate(decoded["probes"]):
                    probe = {"index": i + 1, "temp_c": t}
                    if i < len(targets) and targets[i] is not None:
                        probe["device_target_c"] = targets[i]
                    entry_probes.append(probe)
                out_devices.append({
                    "id": dev_id, "protocol": protocol,
                    "name": probes[dev_id].get("name") or name,
                    "probes": entry_probes, "rssi": rssi, "ts": now,
                })
                continue
            # A connect-only brand (iBBQ, TP25). Its advertisement carries no
            # reading, so there is nothing to relay; surfacing it as
            # discovered would promise a reading this server cannot get.
            if dev_id not in probes:
                _discovered(dev_id, protocol, name, rssi)
        except Exception:  # noqa: BLE001 - one odd packet never sinks a batch
            dropped += 1
            continue

    payload: dict = {}
    if out_devices or out_discovered:
        payload = {"devices": out_devices, "discovered": out_discovered}
        source = str(source or "").strip()[:60]
        if source:
            payload["source"] = source
    return {"payload": payload, "accepted": accepted,
            "dropped": dropped, "matched": matched}


# -- Rate limiting -------------------------------------------------------------

# One token bucket per Cub id, {cub_id: (tokens, last_seen_epoch)}. Bounded by
# the registry in practice; a flood of invented ids is pruned below.
_relay_buckets: dict = {}
_RELAY_BUCKET_MAX = 200


def relay_rate_ok(cub_id: str, now: float | None = None) -> bool:
    """Whether this Cub may post right now, spending a token if so.

    A steady relay never notices this; a device stuck in a retry loop, or one
    somebody points at the endpoint on purpose, is bounded to a rate the
    server does not care about."""
    import time as _time

    now = _time.time() if now is None else now
    key = _norm_id(cub_id) or "-"
    tokens, last = _relay_buckets.get(key, (BLE_RELAY_BURST, now))
    tokens = min(BLE_RELAY_BURST, tokens + (now - last) * BLE_RELAY_RATE_PER_SEC)
    if tokens < 1.0:
        _relay_buckets[key] = (tokens, now)
        return False
    _relay_buckets[key] = (tokens - 1.0, now)
    if len(_relay_buckets) > _RELAY_BUCKET_MAX:
        # Drop the buckets nobody has used lately rather than growing forever.
        for stale, _ in sorted(_relay_buckets.items(), key=lambda kv: kv[1][1])[:50]:
            _relay_buckets.pop(stale, None)
    return True
