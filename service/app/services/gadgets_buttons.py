"""BLE shelf buttons: registry, press-to-action mapping, and execution
(FoodAssistant-771d).

A cheap Bluetooth button stuck on a pantry shelf broadcasts its presses; the
host reader (gadgets/foodassistant_gadgets) decodes and dedupes them and
POSTs each press through the same /gadgets/readings ingest the thermometers
use, as a kind="button" entry carrying an event. This module owns everything
app-side:

* the registry: settings.button_devices, each button with a name, protocol,
  and a mapping per press type (single / double / long) to either a Grocy
  product added to the shopping list or a Start Page action token (the same
  vocabulary /gadgets/esp-action and the Stream Deck use);
* live state (battery, last seen, last press) and the discovered list, in
  their own small state file under data_dir, the same atomic-write
  mtime-cached pattern as gadgets.py, so every worker agrees;
* execution: on a press, resolve the mapping and run it, with a short
  per-button per-press-type cooldown so a radio repeat or a nervous double
  tap cannot add the same item twice, and a kiosk toast so the presser sees
  "Paper Towels added to the shopping list" on screen.

The normalization, mapping, and cooldown logic are pure functions so they
test without hardware, Grocy, or a running app.

Concurrency (FoodAssistant-k7cw): writes are atomic (temp file + os.replace),
and the ingest read-modify-write additionally holds the shared cross-process
file lock (services/state_lock.py), so two workers handling a press push at
the same time can no longer lose one of the updates (or fire a mapping
twice). Reads stay lock-free.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

from .state_lock import state_write_lock

log = logging.getLogger(__name__)

# The press types a mapping can bind. Decoders also report triple/hold
# variants; those show as the last press but fire nothing.
BUTTON_EVENT_TYPES = ("single", "double", "long")

# Mapping actions: add a Grocy product to the shopping list, or fire a Start
# Page action token (a timer key, an ha_1..ha_5 slot, a custom key id).
BUTTON_ACTIONS = ("shopping_add", "esp_action")

# Protocols the reader decodes. Kept separate from the thermometer and
# hygrometer protocol lists: a button is its own device class.
BUTTON_PROTOCOLS = ("bthome_button", "xiaomi_button")

# A second press of the same type inside this window is ignored: BLE bursts
# repeat packets, and nobody means to add the same product twice in five
# seconds.
BUTTON_COOLDOWN_SECONDS = 5.0

# A discovered-but-unconfigured button leaves the "available to add" list
# this long after its last press.
DISCOVERED_TTL = 5 * 60
# A configured button's live entry (battery, last seen) is kept this long.
PRUNE_SECONDS = 7 * 24 * 3600

_lock = threading.Lock()
# In-process view of the state file. "buttons" holds live info per configured
# button id (battery, rssi, ts, last_event); "discovered" the unconfigured
# ones; "fired" the cooldown bookkeeping ({id:type -> epoch}).
_state: dict = {"buttons": {}, "discovered": {}, "fired": {}}
_mtime: int | None = None


def _state_file() -> Path:
    # Resolved per call (not at import) so tests that repoint data_dir work.
    from ..config import settings
    return Path(settings.data_dir) / "gadget-buttons.json"


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
        _state = {
            "buttons": data.get("buttons") if isinstance(data.get("buttons"), dict) else {},
            "discovered": data.get("discovered") if isinstance(data.get("discovered"), dict) else {},
            "fired": data.get("fired") if isinstance(data.get("fired"), dict) else {},
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

def normalize_mapping(raw) -> dict | None:
    """Sanitize one press-type mapping, or None for "do nothing".

    A shopping_add mapping needs a product name (the id is kept when known so
    Grocy can link the exact product); an esp_action mapping needs a token.
    Anything else is None. Pure."""
    if not isinstance(raw, dict):
        return None
    action = str(raw.get("action") or "")
    if action == "shopping_add":
        name = str(raw.get("product_name") or "").strip()[:80]
        if not name:
            return None
        out = {"action": "shopping_add", "product_name": name}
        pid = raw.get("product_id")
        if pid is not None:
            try:
                out["product_id"] = int(pid)
            except (TypeError, ValueError):
                pass
        return out
    if action == "esp_action":
        token = str(raw.get("token") or "").strip()[:60]
        if not token:
            return None
        return {"action": "esp_action", "token": token}
    return None


def normalize_mappings(raw) -> dict:
    """Sanitize a button's whole mappings dict: only the known press types
    survive, each through normalize_mapping. Pure."""
    out: dict = {}
    if not isinstance(raw, dict):
        return out
    for event_type in BUTTON_EVENT_TYPES:
        mapping = normalize_mapping(raw.get(event_type))
        if mapping:
            out[event_type] = mapping
    return out


def mapping_label(mapping: dict | None) -> str:
    """A short human label for a mapping, for the Settings card and the
    confirmation toast. Pure."""
    if not isinstance(mapping, dict):
        return "Nothing"
    if mapping.get("action") == "shopping_add":
        return f"Add {mapping.get('product_name') or 'a product'} to the shopping list"
    if mapping.get("action") == "esp_action":
        return f"Run action {mapping.get('token') or ''}".strip()
    return "Nothing"


def cooldown_ok(fired: dict, key: str, now: float,
                cooldown: float = BUTTON_COOLDOWN_SECONDS) -> bool:
    """Whether a press may execute: True when this (button, press type) key
    has not fired within the cooldown. Pure."""
    try:
        last = float(fired.get(key) or 0.0)
    except (TypeError, ValueError):
        last = 0.0
    return now - last >= cooldown


def normalize_event(raw) -> dict | None:
    """Reduce one pushed press event to {"button": int, "type": str}. Unknown
    press types (triple, hold) are kept so the card can show the last press,
    but only single/double/long can carry a mapping. Pure."""
    if not isinstance(raw, dict):
        return None
    etype = str(raw.get("type") or "").strip().lower()
    if not etype:
        return None
    try:
        button = max(1, int(raw.get("button") or 1))
    except (TypeError, ValueError):
        button = 1
    return {"button": button, "type": etype[:20]}


# --------------------------------------------------------------------------
# Settings glue
# --------------------------------------------------------------------------

def configured_buttons() -> list[dict]:
    """The sanitized button_devices list from settings. Each entry keeps its
    id, name, protocol, and normalized mappings."""
    from ..config import settings
    out = []
    for dev in settings.button_devices or []:
        if isinstance(dev, dict) and _norm_id(dev.get("id")):
            out.append(dev)
    return out


# --------------------------------------------------------------------------
# Ingest (called from the /gadgets/readings route for kind="button" entries)
# --------------------------------------------------------------------------

async def handle_payload(payload: dict, now: float | None = None, *,
                         execute: bool = True) -> dict:
    """Process the button entries of one reader push.

    Updates live state (battery, last seen, last press) and the discovered
    list, then executes the mapping for each pushed press that survives the
    per-button per-press-type cooldown. Returns {"events": n, "executed": n}.
    Never raises: a failed action logs and toasts instead of failing the
    reader's POST.

    execute=False (a satellite whose gadget relay is on, FoodAssistant-me3t)
    records state and the last press but runs NO mapping and burns no
    cooldown: the press is forwarded upstream and the main server's mapping
    is the only one that fires, so a shelf button can never add the same
    item twice. A push relayed FROM a satellite carries a "source" tag,
    stored per button as "via"."""
    from ..config import settings
    now = time.time() if now is None else now
    devices_in = payload.get("devices") if isinstance(payload, dict) else None
    discovered_in = payload.get("discovered") if isinstance(payload, dict) else None
    source = (str(payload.get("source") or "").strip()[:60]
              if isinstance(payload, dict) else "")

    configured = configured_buttons()
    by_id = {_norm_id(d.get("id")): d for d in configured}

    to_execute: list[tuple[dict, dict]] = []  # (configured entry, event)
    events = 0
    with _lock, state_write_lock(_state_file()):
        _load_locked()
        for entry in devices_in or []:
            if not (isinstance(entry, dict) and entry.get("kind") == "button"):
                continue
            dev_id = _norm_id(entry.get("id"))
            if not dev_id:
                continue
            live = dict(_state["buttons"].get(dev_id) or {})
            live["id"] = dev_id
            protocol = str(entry.get("protocol") or "")
            if protocol in BUTTON_PROTOCOLS:
                live["protocol"] = protocol
            battery = entry.get("battery")
            if battery is not None:
                try:
                    live["battery"] = max(0, min(100, int(battery)))
                except (TypeError, ValueError):
                    pass
            rssi = entry.get("rssi")
            if isinstance(rssi, int):
                live["rssi"] = rssi
            live["ts"] = now
            if source:
                live["via"] = source
            event = normalize_event(entry.get("event"))
            if event:
                events += 1
                live["last_event"] = {**event, "ts": now}
                dev = by_id.get(dev_id)
                if (dev and execute and bool(settings.buttons_enabled)
                        and event["type"] in BUTTON_EVENT_TYPES):
                    mapping = normalize_mapping(
                        (dev.get("mappings") or {}).get(event["type"]))
                    key = f"{dev_id}:{event['type']}"
                    if mapping and cooldown_ok(_state["fired"], key, now):
                        _state["fired"][key] = now
                        to_execute.append((dev, event))
            _state["buttons"][dev_id] = live
        for entry in discovered_in or []:
            if not (isinstance(entry, dict) and entry.get("kind") == "button"):
                continue
            dev_id = _norm_id(entry.get("id"))
            if not dev_id or dev_id in by_id:
                continue
            protocol = str(entry.get("protocol") or "")
            _state["discovered"][dev_id] = {
                "id": dev_id,
                "name": str(entry.get("name") or "")[:60],
                "protocol": protocol if protocol in BUTTON_PROTOCOLS else "",
                "rssi": entry.get("rssi") if isinstance(entry.get("rssi"), int) else None,
                "ts": now,
            }
            if source:
                _state["discovered"][dev_id]["via"] = source
        # Prune so the file cannot grow without bound.
        _state["buttons"] = {k: v for k, v in _state["buttons"].items()
                             if now - v.get("ts", 0) <= PRUNE_SECONDS}
        _state["discovered"] = {k: v for k, v in _state["discovered"].items()
                                if now - v.get("ts", 0) <= DISCOVERED_TTL
                                and k not in by_id}
        _state["fired"] = {k: v for k, v in _state["fired"].items()
                           if now - float(v or 0) <= 3600}
        if devices_in or discovered_in:
            _save_locked()

    executed = 0
    for dev, event in to_execute:
        mapping = normalize_mapping((dev.get("mappings") or {}).get(event["type"]))
        if await _execute(dev, mapping):
            executed += 1
    return {"events": events, "executed": executed}


async def _execute(dev: dict, mapping: dict | None) -> bool:
    """Run one mapped action and toast the outcome on the kiosk. Returns True
    when the action succeeded. Never raises."""
    from . import ha_events
    if not mapping:
        return False
    name = str(dev.get("name") or dev.get("id") or "Button")[:60]
    try:
        if mapping["action"] == "shopping_add":
            from . import shopping_source
            product = mapping.get("product_name") or ""
            list_name = await shopping_source.quick_add(product)
            ha_events.add_confirmation(
                f"{product} added to {list_name or 'the shopping list'}.",
                title=name)
            return True
        if mapping["action"] == "esp_action":
            from . import start_actions
            result = await start_actions.fire_key(mapping.get("token") or "")
            if result.get("ok"):
                detail = str(result.get("detail") or "Done.")
                ha_events.add_confirmation(detail, title=name)
                return True
            ha_events.add_warning(
                str(result.get("detail") or "The action could not run."),
                title=name, key=f"button:{_norm_id(dev.get('id'))}")
            return False
    except Exception as exc:  # noqa: BLE001 - a press must never 500 the reader
        log.warning("Button action failed (%s): %s", name, exc)
        try:
            ha_events.add_warning(
                f"{mapping_label(mapping)} did not work: {exc}",
                title=name, key=f"button:{_norm_id(dev.get('id'))}")
        except Exception:  # noqa: BLE001
            pass
    return False


async def test_fire(device_id: str, event_type: str) -> dict:
    """Run one button's mapping on demand (the Settings test link). Bypasses
    the cooldown: an explicit click is not a radio repeat."""
    dev_id = _norm_id(device_id)
    dev = next((d for d in configured_buttons()
                if _norm_id(d.get("id")) == dev_id), None)
    if not dev:
        return {"ok": False, "detail": "Unknown button."}
    if event_type not in BUTTON_EVENT_TYPES:
        return {"ok": False, "detail": "Unknown press type."}
    mapping = normalize_mapping((dev.get("mappings") or {}).get(event_type))
    if not mapping:
        return {"ok": False,
                "detail": "This press has nothing mapped to it yet."}
    ok = await _execute(dev, mapping)
    return {"ok": ok, "detail": mapping_label(mapping)}


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------

def state_snapshot(now: float | None = None) -> dict:
    """The UI-facing button state, merged into GET /gadgets/state by the
    router: configured buttons with live battery/last-press info, plus
    discovered buttons available to add."""
    from ..config import settings
    now = time.time() if now is None else now
    configured = configured_buttons()
    configured_ids = {_norm_id(d.get("id")) for d in configured}
    with _lock:
        _load_locked()
        live_raw = {k: dict(v) for k, v in _state["buttons"].items()}
        discovered_raw = [dict(v) for v in _state["discovered"].values()
                          if now - v.get("ts", 0) <= DISCOVERED_TTL
                          and v.get("id") not in configured_ids]

    from .gadgets import is_low_battery
    buttons = []
    for dev in configured:
        dev_id = _norm_id(dev.get("id"))
        live = live_raw.get(dev_id) or {}
        age = (now - live["ts"]) if live.get("ts") else None
        last = live.get("last_event") if isinstance(live.get("last_event"), dict) else None
        if last is not None:
            last = dict(last)
            try:
                last["age_seconds"] = round(now - float(last.get("ts") or now), 1)
            except (TypeError, ValueError):
                last["age_seconds"] = None
        mappings = normalize_mappings(dev.get("mappings"))
        buttons.append({
            "id": dev_id,
            "name": dev.get("name") or "",
            "protocol": dev.get("protocol") or live.get("protocol") or "",
            "battery": live.get("battery"),
            "battery_low": is_low_battery(live.get("battery")),
            "rssi": live.get("rssi"),
            "age_seconds": round(age, 1) if age is not None else None,
            "via": str(live.get("via") or ""),
            "last_event": last,
            "mappings": {t: {**m, "label": mapping_label(m)}
                         for t, m in mappings.items()},
        })
    for entry in discovered_raw:
        age = now - entry.get("ts", 0)
        entry["age_seconds"] = round(age, 1)
        entry.pop("ts", None)
    return {
        "buttons_enabled": bool(settings.buttons_enabled),
        "buttons": buttons,
        "button_discovered": discovered_raw,
    }


def reset() -> None:
    """Clear all state and drop the state file (used by tests)."""
    global _state, _mtime
    with _lock:
        _state = {"buttons": {}, "discovered": {}, "fired": {}}
        _mtime = None
        try:
            _state_file().unlink(missing_ok=True)
        except OSError:
            pass
