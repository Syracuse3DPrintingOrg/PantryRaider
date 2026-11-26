"""Status icons for kitchen displays.

A kitchen screen can show a short row of small, see-through icons in a top
corner, each one there only while something in the kitchen is true: a
thermometer is reporting, the fridge door is open, the oven or the sous vide
is on. The owner sets the list up in Settings, Home Assistant, as status
items saved in ``settings.status_icons``:

  {id, name, icon, source, entity_id, match, state, above, device_id}

- ``source`` "thermometer": shown while a kitchen thermometer is heard from
  (``device_id`` names one, blank means any of them).
- ``source`` "ha": shown while a Home Assistant entity matches. ``match`` is
  "on" (an on, open or heating style state), "off", "state" (one of the
  comma-separated states in ``state``) or "above" (a number above ``above``,
  for an appliance on a power-metering plug).

normalize_item() and item_active() are pure. active_items() reads the states
through services/ha_client.py, the one path the app uses to reach Home
Assistant (its own two-second cache per entity), and the whole answer is
cached for a few seconds more so a room full of screens costs one read.
The entities these items name count as ones the app reads
(ha_allowlist.status_entities), so they are allowed through the Home
Assistant hop like a thermometer's sensors.

On a Bandit the list comes from the main server, which holds the Home
Assistant connection and the thermometer readings (routers/status_icons.py).
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets

from .ttl_cache import TTLCache

log = logging.getLogger(__name__)

ICONS = ("thermometer", "fridge", "oven", "pot", "plug", "door", "fan", "generic")
ICON_LABELS = {
    "thermometer": "Thermometer", "fridge": "Fridge", "oven": "Oven",
    "pot": "Sous vide or pot", "plug": "Plug", "door": "Door", "fan": "Fan",
    "generic": "Dot",
}
SOURCES = ("thermometer", "ha")
MATCHES = ("on", "off", "state", "above")
POSITIONS = ("top_right", "top_left")
DEFAULT_POSITION = "top_right"

MAX_ITEMS = 12
NAME_MAX = 40
STATE_MAX = 80
CACHE_SECONDS = 3.0

# States that mean "on" for the default match: switches, doors and windows,
# appliances that report what they are doing, and the climate modes.
ACTIVE_STATES = frozenset({
    "on", "open", "opening", "heat", "heating", "heat_cool", "cool", "cooling",
    "auto", "dry", "fan_only", "cook", "cooking", "preheat", "preheating",
    "running", "active", "playing", "detected",
})
OFF_STATES = frozenset({"off", "closed"})
# Home Assistant's own "no reading" states never count, whatever the match.
NO_READING = frozenset({"unavailable", "unknown", "none", ""})

_ID_RE = re.compile(r"^[a-z0-9_]+[.][a-z0-9_]+$")
_ITEM_ID_RE = re.compile(r"^[a-z0-9]{1,16}$")


def _text(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def normalize_item(raw) -> dict | None:
    """One status item cleaned to its saved shape, or None when it cannot
    work (not a dict, or a Home Assistant item without a well-formed entity
    id, or an "above" match without a number). Pure."""
    if not isinstance(raw, dict):
        return None
    source = str(raw.get("source") or "").strip().lower()
    if source not in SOURCES:
        return None
    icon = str(raw.get("icon") or "").strip().lower()
    if icon not in ICONS:
        icon = "thermometer" if source == "thermometer" else "generic"
    item_id = str(raw.get("id") or "").strip().lower()
    if not _ITEM_ID_RE.fullmatch(item_id):
        item_id = secrets.token_hex(4)
    item = {
        "id": item_id,
        "name": _text(raw.get("name"), NAME_MAX),
        "icon": icon,
        "source": source,
        "entity_id": "",
        "match": "on",
        "state": "",
        "above": None,
        "device_id": "",
    }
    if source == "thermometer":
        item["device_id"] = _text(raw.get("device_id"), 80).upper()
        if not item["name"]:
            item["name"] = "Thermometer"
        return item
    entity = str(raw.get("entity_id") or "").strip().lower()
    if not _ID_RE.fullmatch(entity):
        return None
    item["entity_id"] = entity
    match = str(raw.get("match") or "on").strip().lower()
    if match not in MATCHES:
        match = "on"
    item["match"] = match
    if match == "state":
        states = [s for s in (_text(p, STATE_MAX).lower()
                              for p in str(raw.get("state") or "").split(",")) if s]
        if not states:
            return None
        item["state"] = ", ".join(states)[:STATE_MAX]
    elif match == "above":
        above = _number(raw.get("above"))
        if above is None:
            return None
        item["above"] = above
    if not item["name"]:
        item["name"] = entity
    return item


def normalize_items(raw) -> list | None:
    """The saved list: every item cleaned, broken ones dropped, a repeated
    id given a new one, at most MAX_ITEMS. None when ``raw`` is not a list,
    so a save keeps what is stored. Pure apart from new ids."""
    if not isinstance(raw, list):
        return None
    out: list = []
    seen: set = set()
    for entry in raw:
        item = normalize_item(entry)
        if not item:
            continue
        while item["id"] in seen:
            item["id"] = secrets.token_hex(4)
        seen.add(item["id"])
        out.append(item)
        if len(out) >= MAX_ITEMS:
            break
    return out


def normalize_position(value) -> str:
    value = str(value or "").strip().lower()
    return value if value in POSITIONS else DEFAULT_POSITION


def configured_items(settings) -> list:
    """The items in settings, cleaned again on the way out (a hand-edited
    settings file can hold anything)."""
    return normalize_items(getattr(settings, "status_icons", None) or []) or []


def entities(settings) -> set:
    """The Home Assistant entities the configured items read."""
    return {item["entity_id"] for item in configured_items(settings)
            if item["source"] == "ha" and item["entity_id"]}


def _state_value(state) -> str:
    if isinstance(state, dict):
        state = state.get("state")
    return str(state if state is not None else "").strip().lower()


def item_active(item: dict, state) -> bool:
    """Whether a Home Assistant item shows for this entity state (the state
    dict Home Assistant returns, or just its state text). Pure."""
    value = _state_value(state)
    if value in NO_READING:
        return False
    match = item.get("match") or "on"
    if match == "off":
        return value in OFF_STATES
    if match == "state":
        wanted = {s.strip().lower() for s in str(item.get("state") or "").split(",")
                  if s.strip()}
        return value in wanted
    if match == "above":
        number, limit = _number(value), _number(item.get("above"))
        return number is not None and limit is not None and number > limit
    return value in ACTIVE_STATES


# Binary sensor classes whose "on" means open, as Home Assistant shows them.
_OPENING_CLASSES = frozenset({"door", "window", "opening", "garage_door"})


def _format_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def ha_detail(item: dict, state) -> str:
    """The short text beside an active Home Assistant item: the number and
    its unit for an "above" match, otherwise the state in plain words. Pure."""
    value = _state_value(state)
    if item.get("match") == "above":
        number = _number(value)
        unit = ""
        if isinstance(state, dict) and isinstance(state.get("attributes"), dict):
            unit = _text(state["attributes"].get("unit_of_measurement"), 12)
        if number is None:
            return ""
        return (_format_number(number) + (" " + unit if unit else "")).strip()
    if value in ("on", "off") and isinstance(state, dict):
        attrs = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
        if str(attrs.get("device_class") or "").lower() in _OPENING_CLASSES:
            return "open" if value == "on" else "closed"
    return value.replace("_", " ")


def thermometer_detail(found: list, unit: str) -> str:
    """'63°F' for one live thermometer, '2 thermometers' for more. Pure."""
    from .gadgets import format_temp
    if not found:
        return ""
    if len(found) > 1:
        return f"{len(found)} thermometers"
    temp = found[0].get("temp_c")
    return format_temp(temp, unit) if isinstance(temp, (int, float)) else "connected"


def evaluate(items: list, states: dict, thermometers: list, unit: str = "f") -> list:
    """The items that show right now, in their saved order, each as
    {id, name, icon, detail}. ``states`` maps entity id to its Home Assistant
    state; ``thermometers`` is gadgets.live_thermometers(). Pure."""
    out = []
    for item in items:
        if item["source"] == "thermometer":
            wanted = item.get("device_id") or ""
            found = [t for t in thermometers
                     if not wanted or str(t.get("id") or "").upper() == wanted]
            if not found:
                continue
            detail = thermometer_detail(found, unit)
        else:
            state = states.get(item["entity_id"])
            if state is None or not item_active(item, state):
                continue
            detail = ha_detail(item, state)
        out.append({"id": item["id"], "name": item["name"], "icon": item["icon"],
                    "detail": detail})
    return out


# What the Settings entity picker offers: things that are on or off, open or
# closed, or heating, plus sensors that measure power (a metering plug).
PICKER_DOMAINS = ("binary_sensor", "switch", "input_boolean", "climate",
                  "water_heater", "fan", "sensor")
_POWER_UNITS = frozenset({"w", "kw", "a", "ma"})
_POWER_CLASSES = frozenset({"power", "current"})


def picker_entity(row) -> dict | None:
    """One GET /api/states row as an entity picker entry, or None when it is
    not something a status item watches. Pure."""
    if not isinstance(row, dict):
        return None
    entity_id = str(row.get("entity_id") or "").strip().lower()
    if not _ID_RE.fullmatch(entity_id):
        return None
    domain = entity_id.split(".", 1)[0]
    if domain not in PICKER_DOMAINS:
        return None
    attrs = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
    unit = _text(attrs.get("unit_of_measurement"), 12)
    device_class = _text(attrs.get("device_class"), 30).lower()
    if domain == "sensor" and (unit.lower() not in _POWER_UNITS
                               and device_class not in _POWER_CLASSES):
        return None
    return {
        "entity_id": entity_id,
        "name": _text(attrs.get("friendly_name") or entity_id, 60),
        "domain": domain,
        "device_class": device_class,
        "state": _text(row.get("state"), 30),
        "unit": unit,
        # The match that suits it: a number for a power sensor, else on.
        "match": "above" if domain == "sensor" else "on",
    }


_cache = TTLCache(CACHE_SECONDS)


def invalidate() -> None:
    _cache.invalidate()


async def active_items() -> dict:
    """What a display shows: {enabled, position, items}. Never raises: an
    unreachable Home Assistant or a broken reading leaves those items out."""
    from ..config import settings
    enabled = bool(getattr(settings, "status_icons_enabled", False))
    position = normalize_position(getattr(settings, "status_icons_position", ""))
    if not enabled:
        return {"enabled": False, "position": position, "items": []}
    hit = _cache.get()
    if hit is not None:
        return hit
    items = configured_items(settings)
    states: dict = {}
    thermometers: list = []
    unit = "f"
    try:
        wanted = [i["entity_id"] for i in items if i["source"] == "ha"]
        if wanted:
            from . import ha_client
            states = await ha_client.get_states(wanted)
    except Exception:  # noqa: BLE001 - a display poll never fails on Home Assistant
        log.debug("status icons: Home Assistant read failed", exc_info=True)
        states = {}
    try:
        if any(i["source"] == "thermometer" for i in items):
            from . import gadgets
            thermometers = await asyncio.to_thread(gadgets.live_thermometers)
            unit = gadgets.display_unit()
    except Exception:  # noqa: BLE001
        log.debug("status icons: thermometer read failed", exc_info=True)
        thermometers = []
    try:
        shown = evaluate(items, states, thermometers, unit)
    except Exception:  # noqa: BLE001
        log.debug("status icons: evaluation failed", exc_info=True)
        shown = []
    result = {"enabled": True, "position": position, "items": shown}
    _cache.set(result)
    return result
