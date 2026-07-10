"""Bluetooth kitchen thermometer state and target alerts (FoodAssistant-6ivl).

The host-side reader (gadgets/foodassistant_gadgets) POSTs probe readings to
/gadgets/readings; the Timers page polls /gadgets/state. Readings live in a
small state file under data_dir, the same pattern as timers.py and
scanner_mode.py: a server running multiple uvicorn workers must share the
readings, or a reading pushed through one worker never reaches the kiosk
polling another. Reads check the file's mtime and only re-parse when it
changed; writes are atomic (temp file + os.replace); if data_dir is not
writable (tests, a read-only mount) the module quietly degrades to
process-local in-memory behavior.

Target-temperature alerts are evaluated on ingest: each configured probe can
carry a target (Celsius) and a direction, and when a reading crosses it a
toast is queued through ha_events. The crossing state persists in the same
state file, so an alert fires once per crossing (with a hysteresis re-arm and
a cooldown), not once per reading, and not again after a worker or app
restart while the roast is still above target.

The normalization and alert logic are pure functions so they test without
hardware or a running app.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

# A device that has not reported for this long is shown as stale; after the
# longer prune window it is dropped from the state entirely.
STALE_SECONDS = 60
PRUNE_SECONDS = 15 * 60
# A discovered-but-unconfigured device disappears from the "available to add"
# list this long after its last sighting.
DISCOVERED_TTL = 5 * 60

# Alert semantics: fire once per crossing. After firing, the probe must come
# back past the target by the hysteresis margin to re-arm, and even a genuine
# new crossing within the cooldown stays quiet (a lid opening should not
# retrigger the kitchen).
ALERT_COOLDOWN_SECONDS = 300
ALERT_HYSTERESIS_C = 0.5

# "home_assistant" is the virtual protocol for entities read from Home
# Assistant (services/gadgets_ha.py); the BLE reader daemon has no decoder for
# it, so it simply never connects to those ids.
PROTOCOLS = ("inkbird", "thermopro", "combustion", "bluedot", "tempspike",
             "govee_grill", "home_assistant")

# Probe roles the UI can show and the user can override to. "internal" is the
# tip in the food, "ambient" the pit/oven air around it, "food" a generic meat
# probe; "" means no role (a plain numbered probe). A two-lead device like the
# TempSpike reports internal + ambient; a grill controller like the Govee has
# two food leads. The user can override any probe's role when the auto guess is
# wrong or a lead is repurposed.
PROBE_ROLES = ("internal", "ambient", "food")
_ROLE_LABELS = {"internal": "Internal", "ambient": "Ambient", "food": "Food"}


def role_label(role: str) -> str:
    """Human label for a probe role ("" for an unlabeled numbered probe)."""
    return _ROLE_LABELS.get(role or "", "")


def default_probe_role(protocol: str, index: int) -> str:
    """The role a probe carries before any user override, from its protocol.

    A TempSpike's first lead sits in the food (internal) and its second reads
    the ambient/pit; every other device is left as a plain numbered probe. Pure
    so it tests without hardware."""
    if protocol == "tempspike":
        return "internal" if index == 1 else "ambient"
    return ""

_lock = threading.Lock()
# In-process view of the state file: devices/discovered/alerts plus the
# reader heartbeat, and the file mtime the view corresponds to (None = never
# seen). reader_seen is the epoch of the host reader's last contact (a config
# pull or a readings push); 0 means it has never checked in, which is what
# the Settings pane uses to show setup guidance instead of a device list.
_state: dict = {"devices": {}, "discovered": {}, "alerts": {}, "reader_seen": 0.0,
                "bluetooth": {"available": True, "detail": ""}}
_mtime: int | None = None


def _state_file() -> Path:
    # Resolved per call (not at import) so tests that repoint data_dir work.
    from ..config import settings
    return Path(settings.data_dir) / "gadgets.json"


def _load_locked() -> None:
    global _state, _mtime
    try:
        sf = _state_file()
        mtime = sf.stat().st_mtime_ns
    except OSError:
        return  # no file yet (fresh install, or unwritable data_dir)
    if mtime == _mtime:
        return
    try:
        data = json.loads(sf.read_text())
    except (OSError, ValueError):
        return  # a torn or corrupt file never breaks a poll; keep what we have
    if isinstance(data, dict):
        _mtime = mtime
        seen = data.get("reader_seen")
        bt = data.get("bluetooth")
        _state = {
            "devices": data.get("devices") if isinstance(data.get("devices"), dict) else {},
            "discovered": data.get("discovered") if isinstance(data.get("discovered"), dict) else {},
            "alerts": data.get("alerts") if isinstance(data.get("alerts"), dict) else {},
            "reader_seen": float(seen) if isinstance(seen, (int, float)) else 0.0,
            "bluetooth": bt if isinstance(bt, dict) else {"available": True, "detail": ""},
        }


def _save_locked() -> None:
    global _mtime
    sf = _state_file()
    try:
        tmp = sf.with_name(sf.name + ".tmp")
        tmp.write_text(json.dumps(_state))
        os.replace(tmp, sf)
        _mtime = sf.stat().st_mtime_ns
    except OSError:
        pass  # data_dir not writable: fall back to process-local behavior


def _norm_id(value) -> str:
    return str(value or "").strip().upper()


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

def normalize_reading(entry: dict, now: float) -> dict | None:
    """Reduce one pushed device reading to the shape the state file keeps.

    Pure: unknown fields are dropped, temperatures must be numbers (or None
    for an empty probe socket), and anything without an id or probes is
    rejected with None rather than raising.
    """
    if not isinstance(entry, dict):
        return None
    dev_id = _norm_id(entry.get("id"))
    if not dev_id:
        return None
    probes = []
    for probe in entry.get("probes") or []:
        if not isinstance(probe, dict):
            continue
        try:
            index = int(probe.get("index"))
        except (TypeError, ValueError):
            continue
        temp = probe.get("temp_c")
        if temp is not None:
            try:
                temp = round(float(temp), 2)
            except (TypeError, ValueError):
                continue
            if not (-100.0 <= temp <= 600.0):
                temp = None
        entry_probe = {"index": index, "temp_c": temp}
        # The reader may tag a probe with a role ("internal"/"ambient") and a
        # target the device itself reports (a Govee grill's on-device alarm).
        role = probe.get("role")
        if role in PROBE_ROLES:
            entry_probe["role"] = role
        dev_target = probe.get("device_target_c")
        if dev_target is not None:
            try:
                dev_target = round(float(dev_target), 2)
            except (TypeError, ValueError):
                dev_target = None
            if dev_target is not None and -100.0 <= dev_target <= 600.0:
                entry_probe["device_target_c"] = dev_target
        probes.append(entry_probe)
    if not probes:
        return None
    protocol = str(entry.get("protocol") or "")
    battery = entry.get("battery")
    if battery is not None:
        try:
            battery = max(0, min(100, int(battery)))
        except (TypeError, ValueError):
            battery = None
    rssi = entry.get("rssi")
    if rssi is not None:
        try:
            rssi = int(rssi)
        except (TypeError, ValueError):
            rssi = None
    return {
        "id": dev_id,
        "name": str(entry.get("name") or "")[:60],
        "protocol": protocol if protocol in PROTOCOLS else "",
        "probes": probes,
        "battery": battery,
        "rssi": rssi,
        "ts": now,
    }


def evaluate_alerts(targets: dict, readings: dict, alert_state: dict,
                    now: float, cooldown: float = ALERT_COOLDOWN_SECONDS,
                    hysteresis: float = ALERT_HYSTERESIS_C) -> tuple[dict, list]:
    """Decide which probe targets fire, once per crossing.

    ``targets`` maps "DEVICE:probe" to {"temp_c": float, "direction":
    "above"|"below"}; ``readings`` maps the same keys to the current Celsius
    reading; ``alert_state`` is the persisted map of {"reached": bool,
    "fired_ts": float} per key. Returns (new_alert_state, fired) where each
    fired entry is {"key", "temp_c", "target_c", "direction"}.

    Semantics: a probe fires when its reading crosses the target in the
    configured direction (first sighting already past target counts as a
    crossing, so setting a target below the current temperature alerts right
    away). It stays quiet until the reading comes back past the target by the
    hysteresis margin (re-arm), and a re-crossing within the cooldown stays
    quiet too. Pure: no clocks, no I/O.
    """
    new_state: dict = {}
    fired: list = []
    for key, target in targets.items():
        try:
            target_c = float(target.get("temp_c"))
        except (TypeError, ValueError):
            continue
        direction = "below" if target.get("direction") == "below" else "above"
        temp = readings.get(key)
        prev = alert_state.get(key) if isinstance(alert_state.get(key), dict) else {}
        reached = bool(prev.get("reached"))
        fired_ts = float(prev.get("fired_ts") or 0)
        if temp is None:
            # No reading (probe unplugged, device asleep): keep the state so a
            # brief dropout mid-roast cannot re-fire on return.
            new_state[key] = {"reached": reached, "fired_ts": fired_ts}
            continue
        if direction == "above":
            crossed = temp >= target_c
            rearmed = temp < target_c - hysteresis
        else:
            crossed = temp <= target_c
            rearmed = temp > target_c + hysteresis
        if crossed and not reached:
            reached = True
            # fired_ts of 0 means this target has never fired, so the
            # cooldown cannot apply yet.
            if not fired_ts or now - fired_ts >= cooldown:
                fired_ts = now
                fired.append({"key": key, "temp_c": temp,
                              "target_c": target_c, "direction": direction})
        elif not crossed and rearmed:
            reached = False
        new_state[key] = {"reached": reached, "fired_ts": fired_ts}
    return new_state, fired


def format_temp(temp_c: float, unit: str) -> str:
    """Format a Celsius temperature for display in the configured unit."""
    if unit == "c":
        return f"{round(temp_c)}°C"
    return f"{round(temp_c * 9.0 / 5.0 + 32.0)}°F"


# --------------------------------------------------------------------------
# Settings glue
# --------------------------------------------------------------------------

def configured_devices() -> list[dict]:
    """The sanitized gadget_devices list from settings."""
    from ..config import settings
    out = []
    for dev in settings.gadget_devices or []:
        if isinstance(dev, dict) and _norm_id(dev.get("id")):
            out.append(dev)
    return out


def _targets_map(devices: list[dict]) -> dict:
    """Flatten per-device probe targets into the {key: target} map the alert
    evaluator takes."""
    targets: dict = {}
    for dev in devices:
        dev_id = _norm_id(dev.get("id"))
        probe_targets = dev.get("targets")
        if not isinstance(probe_targets, dict):
            continue
        for probe, target in probe_targets.items():
            if isinstance(target, dict) and target.get("temp_c") is not None:
                targets[f"{dev_id}:{probe}"] = target
    return targets


def display_unit() -> str:
    """The unit probes display in: the same one the weather surfaces use, so
    one preference drives every temperature on screen."""
    from ..config import settings
    return "c" if str(settings.streamdeck_weather_units).lower() == "c" else "f"


# --------------------------------------------------------------------------
# Ingest and read
# --------------------------------------------------------------------------

def mark_reader_seen(now: float | None = None) -> None:
    """Record host-reader contact (a /gadgets/config pull). Throttled so a
    frequent poll does not rewrite the state file every few seconds."""
    now = time.time() if now is None else now
    with _lock:
        _load_locked()
        if now - float(_state.get("reader_seen") or 0.0) >= 5.0:
            _state["reader_seen"] = now
            _save_locked()


def ingest(payload: dict, now: float | None = None, *,
           mark_reader: bool = True) -> dict:
    """Store a reading push (readings + discovered devices) and fire alerts.

    mark_reader=True (the host reader's POST path) also refreshes the reader
    heartbeat; the Home Assistant poller passes False so an HA-only setup
    never reads as a connected Bluetooth reader in Settings."""
    now = time.time() if now is None else now
    devices_in = payload.get("devices") if isinstance(payload, dict) else None
    discovered_in = payload.get("discovered") if isinstance(payload, dict) else None
    bluetooth_in = payload.get("bluetooth") if isinstance(payload, dict) else None

    configured = configured_devices()
    configured_ids = {_norm_id(d.get("id")) for d in configured}
    targets = _targets_map(configured)

    with _lock:
        _load_locked()
        for entry in devices_in or []:
            reading = normalize_reading(entry, now)
            if reading:
                _state["devices"][reading["id"]] = reading
        for entry in discovered_in or []:
            if not isinstance(entry, dict):
                continue
            dev_id = _norm_id(entry.get("id"))
            if not dev_id or dev_id in configured_ids:
                continue
            protocol = str(entry.get("protocol") or "")
            _state["discovered"][dev_id] = {
                "id": dev_id,
                "name": str(entry.get("name") or "")[:60],
                "protocol": protocol if protocol in PROTOCOLS else "",
                "rssi": entry.get("rssi") if isinstance(entry.get("rssi"), int) else None,
                # supported=False marks a probe-looking device we have no
                # decoder for (shown as "seen nearby, not supported yet").
                "supported": entry.get("supported") is not False,
                "ts": now,
            }
        # Prune what is long gone so the file cannot grow without bound.
        _state["devices"] = {k: v for k, v in _state["devices"].items()
                             if now - v.get("ts", 0) <= PRUNE_SECONDS}
        _state["discovered"] = {k: v for k, v in _state["discovered"].items()
                                if now - v.get("ts", 0) <= DISCOVERED_TTL
                                and k not in configured_ids}

        readings: dict = {}
        for dev in _state["devices"].values():
            for probe in dev.get("probes", []):
                readings[f"{dev['id']}:{probe['index']}"] = probe.get("temp_c")
        new_alert_state, fired = evaluate_alerts(
            targets, readings, _state.get("alerts") or {}, now)
        _state["alerts"] = new_alert_state
        if mark_reader:
            _state["reader_seen"] = now
            # Only the host Bluetooth reader reports adapter health; the Home
            # Assistant poller (mark_reader=False) leaves it untouched.
            if isinstance(bluetooth_in, dict):
                _state["bluetooth"] = {
                    "available": bluetooth_in.get("available") is not False,
                    "detail": str(bluetooth_in.get("detail") or "")[:200],
                }
        _save_locked()

    if fired:
        _fire_toasts(fired, configured)
    return {"ok": True, "stored": len(devices_in or []), "alerts": len(fired)}


def _fire_toasts(fired: list, configured: list[dict]) -> None:
    """Queue an on-screen toast per fired target.

    Uses the warning channel on purpose: like a device-health alert, a "pull
    the roast" moment must show on the kiosk even when on-screen Home
    Assistant events are turned off. The key carries the probe so a client
    can tell alerts apart."""
    from . import ha_events
    unit = display_unit()
    names = {_norm_id(d.get("id")): (d.get("name") or "") for d in configured}
    for alert in fired:
        dev_id, _, probe = alert["key"].rpartition(":")
        name = names.get(dev_id) or dev_id
        verb = "reached" if alert["direction"] == "above" else "dropped to"
        message = (f"Probe {probe} is at {format_temp(alert['temp_c'], unit)} "
                   f"and {verb} its {format_temp(alert['target_c'], unit)} target.")
        ha_events.add_warning(message, title=name, key=f"gadget:{alert['key']}",
                              level="warning", timeout=30)


def get_state(now: float | None = None) -> dict:
    """The UI-facing snapshot: configured devices with live readings merged
    with their targets, plus discovered devices available to add."""
    from ..config import settings
    now = time.time() if now is None else now
    configured = configured_devices()
    configured_ids = {_norm_id(d.get("id")) for d in configured}
    with _lock:
        _load_locked()
        devices_raw = {k: dict(v) for k, v in _state["devices"].items()}
        discovered_raw = [dict(v) for v in _state["discovered"].values()
                          if now - v.get("ts", 0) <= DISCOVERED_TTL
                          and v.get("id") not in configured_ids]
        reader_seen = float(_state.get("reader_seen") or 0.0)
        bluetooth = _state.get("bluetooth") if isinstance(_state.get("bluetooth"), dict) else {}

    devices = []
    for dev in configured:
        dev_id = _norm_id(dev.get("id"))
        live = devices_raw.get(dev_id) or {}
        probe_targets = dev.get("targets") if isinstance(dev.get("targets"), dict) else {}
        probe_roles = dev.get("roles") if isinstance(dev.get("roles"), dict) else {}
        protocol = dev.get("protocol") or live.get("protocol") or ""
        probes = []
        for probe in live.get("probes", []):
            target = probe_targets.get(str(probe["index"]))
            # Role: a user override wins, then whatever the reader tagged, then
            # the protocol default (TempSpike's internal/ambient).
            override = probe_roles.get(str(probe["index"]))
            role = (override if override in PROBE_ROLES
                    else probe.get("role")
                    if probe.get("role") in PROBE_ROLES
                    else default_probe_role(protocol, probe["index"]))
            probes.append({
                "index": probe["index"],
                "temp_c": probe.get("temp_c"),
                "role": role,
                "role_label": role_label(role),
                "role_source": ("you" if override in PROBE_ROLES else "auto"),
                "target_c": (target or {}).get("temp_c") if isinstance(target, dict) else None,
                "direction": (target or {}).get("direction", "above") if isinstance(target, dict) else "above",
                # The setpoint the device itself broadcasts (Govee grill alarm),
                # shown when the user has not set their own target.
                "device_target_c": probe.get("device_target_c"),
            })
        age = (now - live["ts"]) if live.get("ts") else None
        devices.append({
            "id": dev_id,
            "name": dev.get("name") or "",
            "protocol": dev.get("protocol") or live.get("protocol") or "",
            "probes": probes,
            "battery": live.get("battery"),
            "rssi": live.get("rssi"),
            "age_seconds": round(age, 1) if age is not None else None,
            "stale": age is None or age > STALE_SECONDS,
        })
    return {
        "enabled": bool(settings.gadgets_enabled),
        "unit": display_unit(),
        "devices": devices,
        "discovered": discovered_raw,
        # Host-reader heartbeat for the Settings pane: None means the reader
        # has never checked in on this device (show setup guidance), a number
        # is the seconds since its last config pull or readings push.
        "reader_age_seconds": (round(now - reader_seen, 1)
                               if reader_seen > 0 else None),
        # False when the host reader last reported its Bluetooth radio off or
        # missing, so the card can say "Bluetooth is turned off on this device"
        # rather than showing a silent empty list.
        "bluetooth_available": bluetooth.get("available") is not False,
    }


def reset() -> None:
    """Clear all state and drop the state file (used by tests)."""
    global _state, _mtime
    with _lock:
        _state = {"devices": {}, "discovered": {}, "alerts": {}, "reader_seen": 0.0,
                  "bluetooth": {"available": True, "detail": ""}}
        _mtime = None
        try:
            _state_file().unlink(missing_ok=True)
        except OSError:
            pass
