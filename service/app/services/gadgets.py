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
                "history": {}, "bluetooth": {"available": True, "detail": ""}}
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
            "history": data.get("history") if isinstance(data.get("history"), dict) else {},
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
# Time-to-target ready estimate (FoodAssistant-1d1g)
# --------------------------------------------------------------------------

# A probe only measures, it cannot drive an actuator, so there is no literal
# PID loop to run. The genuinely useful thing is a Meater-style "ready in ~20
# min": from a short history of readings and the probe's target, project when
# the temperature will get there. The estimate is intentionally conservative,
# it is omitted whenever the trend is flat, going the wrong way, already past
# the target, or so slow the number would be meaningless.

# How many recent samples to keep per probe for the slope estimate: enough to
# smooth sensor jitter without letting a stale trend dominate a changed one.
HISTORY_SAMPLES = 8

# EMA smoothing factor for the rate of change: higher reacts faster to a real
# shift, lower rejects more noise. 0.4 is a gentle middle.
_RATE_EMA_ALPHA = 0.4

# A rate of change slower than this (Celsius per second, about 0.06 C/min) is
# treated as flat: no meaningful climb toward target, so no estimate.
_MIN_RATE_C_PER_S = 0.001

# Beyond this the estimate is not useful ("ready in 9 hours"), so we say we
# cannot estimate rather than show a wild number.
_MAX_ESTIMATE_SECONDS = 6 * 3600


def _ema_rate(history: list, alpha: float) -> float | None:
    """EMA-smoothed rate of change (Celsius per second) over consecutive
    samples, or None when there is not enough clean data.

    ``history`` is a list of (timestamp, temp_c) samples, oldest first. Pairs
    with a non-positive time gap are skipped (a duplicate or out-of-order
    timestamp never divides by zero). Pure."""
    clean = []
    for item in history or []:
        try:
            ts = float(item[0])
            temp = float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        clean.append((ts, temp))
    clean.sort(key=lambda x: x[0])
    rate: float | None = None
    for (t0, v0), (t1, v1) in zip(clean, clean[1:]):
        dt = t1 - t0
        if dt <= 0:
            continue
        slope = (v1 - v0) / dt
        rate = slope if rate is None else alpha * slope + (1.0 - alpha) * rate
    return rate


def estimate_ready_seconds(history: list, target_c: float,
                           direction: str = "above", *,
                           alpha: float = _RATE_EMA_ALPHA) -> int | None:
    """Estimate seconds until a probe reaches its target, or None.

    ``history`` is a list of (timestamp, temp_c) samples in time order (oldest
    first); ``target_c`` is the goal in Celsius; ``direction`` "above" is a
    heating cook (temperature rising to the target), "below" a chilling one
    (falling to it). Returns a positive whole-seconds estimate, or None when no
    sane estimate exists: fewer than two clean samples, already at or past the
    target, a flat or wrong-way trend, or a projection so far out it is
    meaningless.

    The rate of change is an EMA-smoothed slope over consecutive samples, so a
    single noisy jump does not blow up the estimate; genuinely noisy data with
    no net trend smooths toward flat and yields None. Pure: no clocks, no
    I/O, the projection is measured from the latest sample."""
    try:
        goal = float(target_c)
    except (TypeError, ValueError):
        return None
    clean = []
    for item in history or []:
        try:
            clean.append((float(item[0]), float(item[1])))
        except (TypeError, ValueError, IndexError):
            continue
    if len(clean) < 2:
        return None
    clean.sort(key=lambda x: x[0])
    latest = clean[-1][1]
    rate = _ema_rate(clean, alpha)
    if rate is None:
        return None
    if direction == "below":
        remaining = latest - goal
        approach = -rate  # a chilling cook needs a falling temperature
    else:
        remaining = goal - latest
        approach = rate
    if remaining <= 0:
        return None  # already at or past the target: nothing to predict
    if approach < _MIN_RATE_C_PER_S:
        return None  # flat or heading the wrong way
    seconds = remaining / approach
    if seconds <= 0 or seconds > _MAX_ESTIMATE_SECONDS:
        return None
    return int(round(seconds))


# --------------------------------------------------------------------------
# Demo sample thermometer (FoodAssistant-qqcq)
# --------------------------------------------------------------------------

# The public demo has no Bluetooth hardware, so the Temperatures section would
# sit empty. When settings.demo_mode is on, get_state adds this one clearly
# labeled sample grill so a visitor can see the feature working: two probes on
# a namespaced fake device (id "DEMO:GRILL"), the food lead climbing toward a
# target so the ready-in estimate shows, the pit lead drifting a little so it
# reads as live. It is generated fresh from the clock (never stored, never
# ingested), so it can never collide with or displace a real probe.
DEMO_DEVICE_ID = "DEMO:GRILL"


def demo_sample_device(now: float) -> dict:
    """A deterministic sample grill for demo mode, in get_state device shape.

    Given a clock (epoch seconds) it returns the same device every time for the
    same instant: a two-probe grill whose food probe rises through a plausible
    band toward a 63 C target (so estimate_ready_seconds yields a ready-in) and
    whose ambient probe hovers around pit temperature with a gentle drift, so
    the card looks alive without any hardware. Pure: clock in, device out."""
    import math

    target = 63.0
    # Food probe: rises through 44..62 C on a 20-minute loop, always below the
    # target so the ready-in estimate stays positive and visibly counts down.
    phase = (now % 1200.0) / 1200.0
    internal = round(44.0 + 18.0 * phase, 1)
    # A short synthetic rising history fed to the real predictor, so the demo
    # exercises the same estimate the live path would show.
    rise = 0.03  # Celsius per second (about 1.8 C/min)
    history = [(now - 40.0 + i * 10.0,
                round(internal - rise * (40.0 - i * 10.0), 2))
               for i in range(5)]
    ready = estimate_ready_seconds(history, target, "above")
    # Ambient/pit probe: drifts a few degrees around 108 C so it reads live.
    ambient = round(108.0 + 4.0 * math.sin(now / 300.0), 1)
    return {
        "id": DEMO_DEVICE_ID,
        "name": "Sample Grill (demo)",
        "protocol": "govee_grill",
        "probes": [
            {"index": 1, "temp_c": internal, "role": "food",
             "role_label": role_label("food"), "role_source": "auto",
             "target_c": target, "direction": "above",
             "device_target_c": None, "ready_in_seconds": ready},
            {"index": 2, "temp_c": ambient, "role": "ambient",
             "role_label": role_label("ambient"), "role_source": "auto",
             "target_c": None, "direction": "above",
             "device_target_c": None, "ready_in_seconds": None},
        ],
        "battery": 85,
        "battery_low": False,
        "rssi": -58,
        "age_seconds": 2.0,
        "stale": False,
        "demo": True,
    }


# --------------------------------------------------------------------------
# Battery (FoodAssistant-oyt9)
# --------------------------------------------------------------------------

# At or below this percentage a probe or device battery is treated as low, so
# every surface (Timers page, Settings pane) flags it the same way. A device
# that reports no battery at all (many probes do not) is never "low": None is
# just unknown, not empty.
LOW_BATTERY_PCT = 20


def is_low_battery(pct, threshold: int = LOW_BATTERY_PCT) -> bool:
    """True when a reported battery percentage is at or below the low-battery
    threshold. None (no battery data) is never low, so a device that simply
    does not report a battery is not flagged. Pure."""
    if pct is None:
        return False
    try:
        return int(pct) <= int(threshold)
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------
# Doneness presets (FoodAssistant-42ja)
# --------------------------------------------------------------------------

# A curated table of safe/typical internal temperatures (Celsius), so a user
# can pick "Chicken" instead of remembering 74. These are pull-from-the-heat
# targets, kept as a plain data structure so it is trivially testable and can
# be served to the UI unchanged. "min_safe" marks the USDA safe minimums the
# UI can flag; the doneness levels for red meat are cook's-preference points.
DONENESS_PRESETS: tuple[dict, ...] = (
    {"category": "Beef", "name": "Rare", "temp_c": 52.0},
    {"category": "Beef", "name": "Medium rare", "temp_c": 57.0},
    {"category": "Beef", "name": "Medium", "temp_c": 63.0},
    {"category": "Beef", "name": "Medium well", "temp_c": 69.0},
    {"category": "Beef", "name": "Well done", "temp_c": 71.0},
    {"category": "Ground beef", "name": "Well done", "temp_c": 71.0, "min_safe": True},
    {"category": "Pork", "name": "Medium", "temp_c": 63.0, "min_safe": True},
    {"category": "Pork", "name": "Well done", "temp_c": 71.0},
    {"category": "Chicken", "name": "Cooked through", "temp_c": 74.0, "min_safe": True},
    {"category": "Turkey", "name": "Cooked through", "temp_c": 74.0, "min_safe": True},
    {"category": "Ground poultry", "name": "Cooked through", "temp_c": 74.0, "min_safe": True},
    {"category": "Fish", "name": "Cooked through", "temp_c": 63.0, "min_safe": True},
    {"category": "Lamb", "name": "Medium rare", "temp_c": 57.0},
    {"category": "Lamb", "name": "Medium", "temp_c": 63.0},
)


def doneness_presets() -> list[dict]:
    """The doneness preset table as a fresh list of dicts, for the UI picker."""
    return [dict(p) for p in DONENESS_PRESETS]


def _preset_key(text: str) -> str:
    """Normalize a preset name for lookup: lowercase, collapse spaces/hyphens."""
    import re as _re
    return _re.sub(r"[\s_-]+", " ", str(text or "").strip().lower())


def doneness_preset_c(name: str) -> float | None:
    """The Celsius target for a doneness preset by name, or None if unknown.

    Accepts either the bare doneness name ("medium rare") when it is unique, or
    a "Category name" pairing ("beef medium rare", "chicken"). Case- and
    separator-insensitive so "Medium-Rare" and "medium rare" both resolve.
    Pure: a name -> Celsius lookup with no I/O."""
    want = _preset_key(name)
    if not want:
        return None
    # Exact "category name", exact "category", then a unique bare name.
    bare: dict[str, list[float]] = {}
    for p in DONENESS_PRESETS:
        cat = _preset_key(p["category"])
        nm = _preset_key(p["name"])
        if want in (f"{cat} {nm}", cat):
            return float(p["temp_c"])
        bare.setdefault(nm, []).append(float(p["temp_c"]))
    temps = bare.get(want)
    if temps and len(set(temps)) == 1:
        return temps[0]
    return None


# --------------------------------------------------------------------------
# Recipe-driven target suggestion (FoodAssistant-42ja)
# --------------------------------------------------------------------------

import re as _re_mod

# An explicit internal temperature the recipe text names, e.g. "internal
# temperature of 165°F", "until it reaches 74C", "thermometer reads 165 F".
_TEMP_RE = _re_mod.compile(
    r"(?P<value>-?\d{2,3})\s*(?:°|degrees?\s*)?(?P<unit>[FC])\b", _re_mod.IGNORECASE)

# Doneness phrases in recipe text mapped to a preset lookup name. Ordered most
# specific first so "medium rare" is not shadowed by "medium".
_DONENESS_PHRASES: tuple[tuple[str, str], ...] = (
    ("medium-well", "beef medium well"),
    ("medium well", "beef medium well"),
    ("medium-rare", "beef medium rare"),
    ("medium rare", "beef medium rare"),
    ("well-done", "beef well done"),
    ("well done", "beef well done"),
    ("rare", "beef rare"),
    ("medium", "beef medium"),
)

# Proteins whose safe-cooked target we can suggest when the recipe names them
# with a doneness/temperature cue but no explicit number.
_PROTEIN_PRESETS: tuple[tuple[str, str], ...] = (
    ("chicken", "chicken"),
    ("turkey", "turkey"),
    ("pork", "pork medium"),
    ("salmon", "fish"),
    ("fish", "fish"),
)

# Words that signal the recipe is talking about a probe/internal temperature,
# so a bare number nearby is a doneness target and not an oven setting.
_INTERNAL_CUES = ("internal", "thermometer", "probe", "instant-read",
                  "instant read", "reaches", "registers", "reads", "doneness",
                  "until it reaches", "core temp")


def _recipe_text(recipe: dict) -> str:
    """Flatten a serialized recipe (title/notes/steps) into one lowercase blob
    for phrase scanning. Ingredients are skipped: an ingredient line naming a
    protein is not a doneness instruction."""
    if not isinstance(recipe, dict):
        return ""
    parts = [str(recipe.get("title") or ""), str(recipe.get("notes") or "")]
    parts.extend(str(s) for s in (recipe.get("steps") or []))
    return "  ".join(parts).lower()


def suggest_target_from_recipe(recipe: dict) -> dict | None:
    """Best-effort probe target pulled from the active recipe, or None.

    Looks first for an explicit internal temperature the recipe names (near an
    "internal"/"thermometer"/"reaches" cue), converting Fahrenheit to Celsius;
    failing that, a doneness word ("medium rare") or a protein ("chicken") maps
    to a preset. Returns {"temp_c", "label", "source"} where source is "recipe
    temperature", "doneness", or "protein". Pure: text in, suggestion out, no
    clocks or I/O. Skips (returns None) when the recipe names no cue at all."""
    text = _recipe_text(recipe)
    if not text:
        return None
    title = str((recipe or {}).get("title") or "").strip()
    # 1) An explicit number near an internal-temperature cue.
    for m in _TEMP_RE.finditer(text):
        start = max(0, m.start() - 40)
        window = text[start:m.end() + 10]
        if not any(cue in window for cue in _INTERNAL_CUES):
            continue
        try:
            value = float(m.group("value"))
        except (TypeError, ValueError):
            continue
        unit = m.group("unit").lower()
        temp_c = (value - 32.0) * 5.0 / 9.0 if unit == "f" else value
        if -20.0 <= temp_c <= 150.0:
            return {"temp_c": round(temp_c, 1),
                    "label": title or "recipe target",
                    "source": "recipe temperature"}
    # 2) A doneness phrase.
    for phrase, preset in _DONENESS_PHRASES:
        if phrase in text:
            c = doneness_preset_c(preset)
            if c is not None:
                return {"temp_c": c, "label": phrase, "source": "doneness"}
    # 3) A named protein, only when a doneness/internal cue is present so we do
    #    not fire on any mention of chicken in an unrelated line.
    if any(cue in text for cue in _INTERNAL_CUES) or "cook" in text or "done" in text:
        for word, preset in _PROTEIN_PRESETS:
            if word in text:
                c = doneness_preset_c(preset)
                if c is not None:
                    return {"temp_c": c, "label": word, "source": "protein"}
    return None


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

        # Keep a bounded ring of recent (timestamp, temperature) samples per
        # probe so get_state can project a ready-in estimate (FoodAssistant-1d1g).
        # A probe reporting no temperature (empty socket) is skipped; history
        # for a probe no longer present is dropped so the map cannot grow
        # without bound.
        history = _state.get("history")
        if not isinstance(history, dict):
            history = {}
        for key, temp in readings.items():
            if temp is None:
                continue
            ring = history.get(key)
            ring = list(ring) if isinstance(ring, list) else []
            ring.append([now, temp])
            if len(ring) > HISTORY_SAMPLES:
                ring = ring[-HISTORY_SAMPLES:]
            history[key] = ring
        _state["history"] = {k: v for k, v in history.items() if k in readings}

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
    # Also fire onto the Home Assistant event bus when HA is connected, so an
    # automation can react to the same "pull the roast" moment (FoodAssistant-42ja).
    # Best-effort and non-blocking; a no-op when HA is not configured.
    try:
        from . import gadgets_ha
        gadgets_ha.notify_probe_alerts(fired, configured, unit)
    except Exception:  # noqa: BLE001 - an outbound notify never blocks ingest
        pass


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
        history_raw = {k: list(v) for k, v in (_state.get("history") or {}).items()
                       if isinstance(v, list)}
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
            target_c = (target or {}).get("temp_c") if isinstance(target, dict) else None
            direction = (target or {}).get("direction", "above") if isinstance(target, dict) else "above"
            # Ready-in projection: only when a target is set and the recent
            # trend gives a sane estimate (FoodAssistant-1d1g). None (omitted on
            # the card) when cooling, flat, already past target, or too noisy.
            ready_in = None
            if target_c is not None:
                key = f"{dev_id}:{probe['index']}"
                ready_in = estimate_ready_seconds(
                    history_raw.get(key) or [], target_c, direction)
            probes.append({
                "index": probe["index"],
                "temp_c": probe.get("temp_c"),
                "role": role,
                "role_label": role_label(role),
                "role_source": ("you" if override in PROBE_ROLES else "auto"),
                "target_c": target_c,
                "direction": direction,
                # The setpoint the device itself broadcasts (Govee grill alarm),
                # shown when the user has not set their own target.
                "device_target_c": probe.get("device_target_c"),
                "ready_in_seconds": ready_in,
            })
        age = (now - live["ts"]) if live.get("ts") else None
        devices.append({
            "id": dev_id,
            "name": dev.get("name") or "",
            "protocol": dev.get("protocol") or live.get("protocol") or "",
            "probes": probes,
            "battery": live.get("battery"),
            # Consistent low-battery flag for every surface (FoodAssistant-oyt9);
            # None battery stays False (unknown, not low).
            "battery_low": is_low_battery(live.get("battery")),
            "rssi": live.get("rssi"),
            "age_seconds": round(age, 1) if age is not None else None,
            "stale": age is None or age > STALE_SECONDS,
        })
    # Demo mode has no Bluetooth hardware, so add one clearly labeled sample
    # grill (FoodAssistant-qqcq). It is namespaced and generated from the
    # clock, so it never collides with or displaces a real probe; real devices
    # above always take precedence.
    if bool(settings.demo_mode):
        devices.append(demo_sample_device(now))
    return {
        "enabled": bool(settings.gadgets_enabled),
        "unit": display_unit(),
        "devices": devices,
        "discovered": discovered_raw,
        # A best-effort target the active cook recipe implies (a doneness word
        # or an internal temperature it names), so the Timers page can offer
        # "use recipe target" (FoodAssistant-42ja). None when no recipe is
        # active or it names no temperature cue.
        "recipe_suggestion": _active_recipe_suggestion(),
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


def _active_recipe_suggestion() -> dict | None:
    """The recipe-implied probe target for the active cook recipe, or None.

    A thin, best-effort wrapper around suggest_target_from_recipe: it reads the
    single active recipe and never raises, so a missing recipe module or a
    read error just yields no suggestion."""
    try:
        from . import current_recipe
        active = current_recipe.get_active()
    except Exception:  # noqa: BLE001 - a suggestion is optional, never fatal
        return None
    return suggest_target_from_recipe(active) if active else None


def reset() -> None:
    """Clear all state and drop the state file (used by tests)."""
    global _state, _mtime
    with _lock:
        _state = {"devices": {}, "discovered": {}, "alerts": {}, "reader_seen": 0.0,
                  "history": {}, "bluetooth": {"available": True, "detail": ""}}
        _mtime = None
        try:
            _state_file().unlink(missing_ok=True)
        except OSError:
            pass
