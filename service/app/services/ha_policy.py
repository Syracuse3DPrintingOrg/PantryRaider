"""Which Home Assistant service calls a Pantry Raider key may make.

A key (a Stream Deck key, a Start Page key, an ESP or Bluetooth button)
names an entity and a service. Most keys toggle a light or run a scene and
are fine to run from anything in the kitchen. A few reach further: they
unlock a door, disarm an alarm, open a garage or restart Home Assistant.
Those are "elevated" and run only after the owner allows them on the key in
Settings, Start Page. Bluetooth buttons never run them, because anyone
nearby can fake a press.

classify() sorts a call into ok, elevated or invalid. Invalid is kept for
shapes Home Assistant itself refuses or that could reach into the request
URL ("all", "none", path characters, an empty item), so no key that works
today becomes invalid. permitted() turns the verdict into a yes or no for
one press from one source.

This file is pure and uses only the standard library, so it runs on the
Stream Deck's Python 3.9 as well. The deck package carries a byte-identical
copy at streamdeck/foodassistant_streamdeck/ha_policy.py; a test compares
the two, so change both together.
"""
from __future__ import annotations

import re

HA_POLICY_VERSION = 1

# The most entity ids one call may name. The HA proxy uses the same limit.
MAX_ENTITIES = 20

# A Home Assistant service or entity id, after normalize().
ID_RE = re.compile("^[a-z0-9_]+[.][a-z0-9_]+$")

# Services that are everyday kitchen controls when called on an entity of
# their own domain.
STANDARD_SERVICES = {
    "light": {"turn_on", "turn_off", "toggle"},
    "switch": {"turn_on", "turn_off", "toggle"},
    "fan": {"turn_on", "turn_off", "toggle"},
    "input_boolean": {"turn_on", "turn_off", "toggle"},
    "humidifier": {"turn_on", "turn_off", "toggle"},
    "climate": {"turn_on", "turn_off", "toggle"},
    "button": {"press"},
    "input_button": {"press"},
    "scene": {"turn_on"},
    "script": {"turn_on", "turn_off", "toggle"},
    "automation": {"trigger", "turn_on", "turn_off", "toggle"},
    "media_player": {
        "media_play_pause", "media_play", "media_pause", "media_stop",
        "media_next_track", "media_previous_track", "volume_up",
        "volume_down", "volume_mute", "turn_on", "turn_off", "toggle",
    },
    "vacuum": {"start", "stop", "pause", "return_to_base", "locate"},
    "timer": {"start", "pause", "cancel"},
    "counter": {"increment", "decrement", "reset"},
    "input_select": {"select_next", "select_previous"},
}

# Entity domains homeassistant.toggle, turn_on and turn_off may switch freely.
STANDARD_ENTITY_DOMAINS = frozenset({
    "light", "switch", "fan", "input_boolean", "input_button", "button",
    "scene", "script", "automation", "media_player", "climate", "vacuum",
    "timer", "counter", "input_select", "humidifier",
})

# Domains that guard the home or the Home Assistant install itself. Any call
# into them, or any call aimed at one of their entities, is elevated.
HIGH_RISK = frozenset({
    "lock", "alarm_control_panel", "cover", "valve", "siren", "camera",
    "water_heater", "hassio", "shell_command", "python_script", "pyscript",
    "rest_command", "recorder", "backup", "update", "zwave_js", "zha", "mqtt",
    "esphome", "system_log", "logger", "input_text", "notify",
})

HA_GENERIC = frozenset({"toggle", "turn_on", "turn_off"})

MSG_INVALID = "This key's Home Assistant action is not valid. Edit it in Settings, Start Page."
MSG_HIGH_RISK = ("This key controls a lock, alarm, door or something similar, so it only "
                 "runs after you allow it in Settings, Start Page.")
MSG_UNLISTED = ("This key calls a Home Assistant action Pantry Raider does not know, so it "
                "only runs after you allow it in Settings, Start Page.")
MSG_BLE = "Bluetooth buttons can't run this: anyone nearby could fake the press."

SOURCES = ("deck", "screen", "esp", "ble", "satellite", "proxy")


def normalize(entity_id, service):
    """(entity, service) the way Home Assistant reads them.

    The service is stripped and lowercased. The entity string is split on
    commas, each item stripped and lowercased, and the items joined again
    with ','. An empty item stays empty, so classify() still sees it.
    Home Assistant lowercases both itself, so a key saved as 'Light.Kitchen'
    keeps doing what it did. A list or tuple of ids (the form Home Assistant
    also accepts) is read as the same ids joined with ','."""
    svc = str(service or "").strip().lower()
    if isinstance(entity_id, (list, tuple)):
        raw = ",".join("" if item is None else str(item) for item in entity_id)
    else:
        raw = str(entity_id or "")
    items = [part.strip().lower() for part in raw.split(",")]
    return ",".join(items), svc


def split_entities(entity_id) -> list:
    """The normalized entity ids in an entity string, empty items dropped."""
    entity, _svc = normalize(entity_id, "")
    return [item for item in entity.split(",") if item]


def _classify_one(entity: str, service: str, sdomain: str, sname: str) -> str:
    """'ok', 'high_risk' or 'unlisted' for one well-formed entity."""
    edomain = entity.split(".", 1)[0]
    if sdomain == "script" and sname not in HA_GENERIC:
        # A script runs with the entity passed as a variable. Called on
        # itself it is an ordinary scene-like key. script.reload is Home
        # Assistant's own reload action (no script may take that name), so
        # it never counts as a script calling itself.
        if entity == service and sname != "reload":
            return "ok"
        return "high_risk" if edomain in HIGH_RISK else "unlisted"
    if sdomain == "homeassistant":
        if sname in HA_GENERIC:
            if edomain in STANDARD_ENTITY_DOMAINS:
                return "ok"
            if edomain in HIGH_RISK:
                return "high_risk"
            return "unlisted"
        return "high_risk"
    if sdomain in HIGH_RISK or edomain in HIGH_RISK:
        return "high_risk"
    if sdomain == edomain and sname in STANDARD_SERVICES.get(sdomain, ()):
        return "ok"
    return "unlisted"


def classify(entity_id, service) -> tuple:
    """(verdict, reason) for a call: ('ok', ''), ('elevated', 'high_risk'
    or 'unlisted') or ('invalid', 'service' or 'entity')."""
    entity, svc = normalize(entity_id, service)
    if not ID_RE.fullmatch(svc):
        return "invalid", "service"
    items = entity.split(",")
    if (not entity or len(items) > MAX_ENTITIES
            or any(not ID_RE.fullmatch(item) for item in items)):
        return "invalid", "entity"
    sdomain, sname = svc.split(".", 1)
    verdicts = [_classify_one(item, svc, sdomain, sname) for item in items]
    if "high_risk" in verdicts:
        return "elevated", "high_risk"
    if "unlisted" in verdicts:
        return "elevated", "unlisted"
    return "ok", ""


def permitted(entity_id, service, entry, source, *, enforce) -> tuple:
    """(allowed, message) for one press of a key from one source.

    enforce has no default on purpose: every caller says whether its config
    source has turned the policy on, so nothing is refused by omission.
    Invalid calls are always refused. Elevated calls are always refused from
    Bluetooth buttons, allowed while enforce is False, and otherwise allowed
    only when the key's entry carries allow_elevated True. An unknown source
    is treated like 'proxy'."""
    verdict, reason = classify(entity_id, service)
    if verdict == "invalid":
        return False, MSG_INVALID
    if verdict == "ok":
        return True, ""
    src = str(source or "").strip().lower()
    if src not in SOURCES:
        src = "proxy"
    if src == "ble":
        return False, MSG_BLE
    if not enforce:
        return True, ""
    if isinstance(entry, dict) and entry.get("allow_elevated") is True:
        return True, ""
    return False, (MSG_HIGH_RISK if reason == "high_risk" else MSG_UNLISTED)
