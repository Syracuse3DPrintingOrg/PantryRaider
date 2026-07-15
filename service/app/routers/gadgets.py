"""Bluetooth kitchen thermometer endpoints (FoodAssistant-6ivl).

The host-side reader daemon and the Timers page meet here:

  GET  /gadgets/config            what the reader should do (enabled flag +
                                  configured device list), polled by the daemon
  POST /gadgets/readings          probe readings + discovered devices, pushed
                                  by the daemon every few seconds
  GET  /gadgets/state             the UI snapshot the Timers page polls
  POST /gadgets/devices           add a discovered thermometer (turns the
                                  feature on if it was off)
  DELETE /gadgets/devices/{id}    remove a thermometer
  POST /gadgets/target            set or clear a probe's target temperature

Hygrometers (FoodAssistant-q97i), a separate device class riding the same
reader and readings push:

  POST /gadgets/hygrometers            add a discovered hygrometer
  POST /gadgets/hygrometers/edit       rename, set location, or store thresholds
  DELETE /gadgets/hygrometers/{id}     remove a hygrometer
  GET/POST /gadgets/ha-hygrometers     Home Assistant entity pairs as a source

The Settings pane (FoodAssistant-mnks) adds management endpoints:

  POST /gadgets/install               set the host reader up on a Pi appliance
                                      (via the host bridge), or explain the
                                      manual install on a server
  GET  /gadgets/ha-entities           temperature entities from Home Assistant,
                                      for the entity picker
  POST /gadgets/ha-entities           read one HA entity as a thermometer
  DELETE /gadgets/ha-entities/{id}    stop reading an HA entity

Configuration lives in settings (gadgets_enabled, gadget_devices,
gadget_ha_enabled, gadget_ha_entities), so it round-trips through the normal
settings persistence and the daemon needs no file coupling with the app.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..config import settings
from ..services import gadgets, gadgets_ha, gadgets_esp, gadgets_buttons

router = APIRouter(prefix="/gadgets", tags=["gadgets"])

_HOST_BRIDGE = "http://127.0.0.1:9299"


class DeviceIn(BaseModel):
    id: str
    name: str = ""
    protocol: str = ""


class TargetIn(BaseModel):
    device_id: str
    probe: int
    temp_c: float | None = None   # None clears the target
    direction: str = "above"      # above | below


def _norm_id(value: str) -> str:
    return str(value or "").strip().upper()


@router.get("/config")
async def reader_config():
    """What the host-side reader should do. Devices are listed even while the
    feature is off so a passive scan can keep the add list warm; the daemon
    only connects to devices when enabled is true. A pull also counts as the
    reader's heartbeat, so Settings can tell "reader running, nothing in
    range" apart from "no reader installed"."""
    gadgets.mark_reader_seen()
    return {
        "enabled": bool(settings.gadgets_enabled),
        "devices": gadgets.configured_devices(),
        # Hygrometers are a separate class (FoodAssistant-q97i): the reader
        # decodes them passively from the same scan, no connections, and uses
        # this list only to tell configured devices from discoverable ones.
        "hygrometers_enabled": bool(settings.hygrometers_enabled),
        "hygrometers": gadgets.configured_hygrometers(),
        # Buttons are the third passive class (FoodAssistant-771d): the
        # reader uses this list to tell configured buttons (whose presses it
        # POSTs as events) from discoverable ones.
        "buttons_enabled": bool(settings.buttons_enabled),
        "buttons": gadgets_buttons.configured_buttons(),
        # Door/window contact sensors (FoodAssistant-5c61): decoded passively
        # from the same scan; the list tells configured sensors from
        # discoverable ones.
        "contacts_enabled": bool(settings.contacts_enabled),
        "contacts": gadgets.configured_contacts(),
    }


@router.post("/readings")
async def post_readings(payload: dict):
    """Ingest a reader push: live probe readings plus discovered devices.
    Button entries (kind="button") route to their own handler, which also
    executes the mapped action for each pushed press."""
    payload = payload if isinstance(payload, dict) else {}
    result = gadgets.ingest(payload)
    buttons = await gadgets_buttons.handle_payload(payload)
    result["button_events"] = buttons.get("events", 0)
    return result


@router.get("/state")
async def state():
    """The Timers page snapshot: live probes, targets, and devices to add.
    Button state (FoodAssistant-771d) rides the same payload."""
    data = gadgets.get_state()
    data.update(gadgets_buttons.state_snapshot())
    return data


@router.get("/presets")
async def doneness_presets():
    """The curated doneness targets (name -> Celsius) the target editor offers
    instead of typing a custom temperature (FoodAssistant-42ja). Static data, so
    the Timers page can cache it."""
    return {"presets": gadgets.doneness_presets(),
            "low_battery_pct": gadgets.LOW_BATTERY_PCT}


@router.post("/devices")
async def add_device(payload: DeviceIn):
    """Add a thermometer (usually one the reader discovered). Adding the
    first device is the feature opt-in, so it also flips gadgets_enabled."""
    dev_id = _norm_id(payload.id)
    if not dev_id:
        return {"ok": False, "error": "a device id is required"}
    protocol = payload.protocol if payload.protocol in gadgets.PROTOCOLS else ""
    devices = [dict(d) for d in gadgets.configured_devices()]
    for dev in devices:
        if _norm_id(dev.get("id")) == dev_id:
            if payload.name:
                dev["name"] = payload.name[:60]
            if protocol:
                dev["protocol"] = protocol
            break
    else:
        devices.append({"id": dev_id, "name": payload.name[:60],
                        "protocol": protocol, "targets": {}})
    settings.save({"gadget_devices": devices, "gadgets_enabled": True})
    return {"ok": True, "devices": devices}


@router.delete("/devices/{device_id}")
async def remove_device(device_id: str):
    dev_id = _norm_id(device_id)
    devices = [dict(d) for d in gadgets.configured_devices()
               if _norm_id(d.get("id")) != dev_id]
    settings.save({"gadget_devices": devices})
    return {"ok": True, "devices": devices}


@router.post("/target")
async def set_target(payload: TargetIn):
    """Set or clear one probe's target temperature (stored in Celsius)."""
    dev_id = _norm_id(payload.device_id)
    devices = [dict(d) for d in gadgets.configured_devices()]
    for dev in devices:
        if _norm_id(dev.get("id")) != dev_id:
            continue
        targets = dict(dev.get("targets") or {})
        key = str(int(payload.probe))
        if payload.temp_c is None:
            targets.pop(key, None)
        else:
            direction = "below" if payload.direction == "below" else "above"
            targets[key] = {"temp_c": round(float(payload.temp_c), 1),
                            "direction": direction}
        dev["targets"] = targets
        settings.save({"gadget_devices": devices})
        return {"ok": True, "devices": devices}
    return {"ok": False, "error": "unknown device"}


class NameIn(BaseModel):
    device_id: str
    name: str = ""


class ProbeRoleIn(BaseModel):
    device_id: str
    probe: int
    role: str = ""   # "" clears the override (back to auto); else internal|ambient|food


@router.post("/name")
async def set_name(payload: NameIn):
    """Give a thermometer a friendly name. Auto-added devices arrive with their
    broadcast name or bare address; this lets any of them become "Grill" or
    "Smoker". An empty name falls back to the broadcast name in the UI."""
    dev_id = _norm_id(payload.device_id)
    devices = [dict(d) for d in gadgets.configured_devices()]
    for dev in devices:
        if _norm_id(dev.get("id")) != dev_id:
            continue
        dev["name"] = (payload.name or "").strip()[:60]
        settings.save({"gadget_devices": devices})
        return {"ok": True, "devices": devices}
    return {"ok": False, "error": "unknown device"}


@router.post("/probe-role")
async def set_probe_role(payload: ProbeRoleIn):
    """Override one probe's role (internal/ambient/food), or clear it back to
    auto with an empty role. Auto detection labels a TempSpike's leads
    internal + ambient; this is the escape hatch when that guess is wrong or a
    lead is repurposed."""
    dev_id = _norm_id(payload.device_id)
    devices = [dict(d) for d in gadgets.configured_devices()]
    for dev in devices:
        if _norm_id(dev.get("id")) != dev_id:
            continue
        roles = dict(dev.get("roles") or {})
        key = str(int(payload.probe))
        if payload.role in gadgets.PROBE_ROLES:
            roles[key] = payload.role
        else:
            roles.pop(key, None)
        dev["roles"] = roles
        settings.save({"gadget_devices": devices})
        return {"ok": True, "devices": devices}
    return {"ok": False, "error": "unknown device"}


@router.post("/install")
async def install_reader():
    """Set the Bluetooth reader up on this device (FoodAssistant-mnks).

    On a Pi appliance this asks the on-device helper to install the reader
    service (the same helper ENABLE_GADGETS runs at install time); it is safe
    to run more than once. On a plain server the app runs in a container and
    cannot install a host service, so this returns the manual steps instead.
    Mirrors POST /printing/install."""
    if settings.is_pi_appliance():
        from ..services.bridge import bridge_client
        try:
            # apt plus a pip install can take a couple of minutes on a Pi;
            # give the bridge call room so a slow run is not cut off.
            async with bridge_client(timeout=610.0) as client:
                r = await client.post(f"{_HOST_BRIDGE}/gadgets-setup")
            body = r.json()
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"ok": False, "message":
                 "Could not reach the device helper to set up the reader. "
                 f"Make sure the device is fully started, then try again. ({e})"},
                status_code=502,
            )
        if r.status_code == 200 and body.get("ok"):
            return {"ok": True, "message":
                    "The reader is set up. Turn a thermometer on nearby and "
                    "it appears below within a minute.",
                    "log": body.get("log", "")}
        return JSONResponse(
            {"ok": False, "message": body.get(
                "error", "The device could not finish setting up the reader."),
             "log": body.get("log", "")},
            status_code=502,
        )

    # Server (Docker) mode: the reader runs on the server itself and needs a
    # Bluetooth radio there; the app cannot install a host service for you.
    return {"ok": False, "message":
            "The reader runs on the server itself and needs a Bluetooth radio "
            "there. To install it, run this on the server, in the folder with "
            "your Pantry Raider checkout:\n"
            "  sudo ./scripts/image-build/foodassistant-gadgets-setup\n"
            "The gadgets/README.md file in that folder covers the details. No "
            "Bluetooth radio on the server? If Home Assistant already sees "
            "your thermometers, use the From Home Assistant section instead."}


# -- Hygrometers (FoodAssistant-q97i) ----------------------------------------
#
# A separate device class from the probe thermometers: ambient temperature +
# humidity sensors for a fridge, freezer, pantry, or room. Same reader, same
# readings push, their own registry (settings.hygrometer_devices) and their
# own block on the Time & Temp page. Thresholds are stored for the alarms
# follow-up (FoodAssistant-5c61) and not yet acted on.


class HygroDeviceIn(BaseModel):
    id: str
    name: str = ""
    protocol: str = ""
    location: str = ""


class HygroEditIn(BaseModel):
    device_id: str
    name: str | None = None
    location: str | None = None
    min_temp_c: float | None = None
    max_temp_c: float | None = None
    min_humidity: float | None = None
    max_humidity: float | None = None
    # Protection alarm settings (FoodAssistant-5c61): how long a reading must
    # sit outside its range before the alarm fires (null = the 5 minute
    # default), and the per-device "sensor stopped reporting" window (null or
    # 0 = off, the default).
    alarm_grace_seconds: float | None = None
    stale_alarm_seconds: float | None = None


@router.post("/hygrometers")
async def add_hygrometer(payload: HygroDeviceIn):
    """Add a hygrometer (usually one the reader discovered). Adding the first
    one is the class's opt-in, so it also flips hygrometers_enabled."""
    dev_id = _norm_id(payload.id)
    if not dev_id:
        return {"ok": False, "error": "a device id is required"}
    protocol = payload.protocol if payload.protocol in gadgets.HYGRO_PROTOCOLS else ""
    devices = [dict(d) for d in gadgets.configured_hygrometers()]
    for dev in devices:
        if _norm_id(dev.get("id")) == dev_id:
            if payload.name:
                dev["name"] = payload.name[:60]
            if payload.location:
                dev["location"] = payload.location[:40]
            if protocol:
                dev["protocol"] = protocol
            break
    else:
        devices.append({"id": dev_id, "name": payload.name[:60],
                        "protocol": protocol,
                        "location": payload.location[:40], "thresholds": {}})
    settings.save({"hygrometer_devices": devices, "hygrometers_enabled": True})
    return {"ok": True, "hygrometers": devices}


@router.post("/hygrometers/edit")
async def edit_hygrometer(payload: HygroEditIn):
    """Rename a hygrometer, set its location label, or store its min/max
    temperature and humidity thresholds. Only the fields present in the
    request are applied, so a rename never touches thresholds; passing a
    threshold field as null clears that one threshold."""
    dev_id = _norm_id(payload.device_id)
    provided = payload.model_dump(exclude_unset=True)
    devices = [dict(d) for d in gadgets.configured_hygrometers()]
    for dev in devices:
        if _norm_id(dev.get("id")) != dev_id:
            continue
        if "name" in provided:
            dev["name"] = str(provided["name"] or "").strip()[:60]
        if "location" in provided:
            dev["location"] = str(provided["location"] or "").strip()[:40]
        thresholds = gadgets.normalize_hygro_thresholds(dev.get("thresholds"))
        for key in gadgets.HYGRO_THRESHOLD_KEYS:
            if key in provided:
                if provided[key] is None:
                    thresholds.pop(key, None)
                else:
                    thresholds[key] = provided[key]
        dev["thresholds"] = gadgets.normalize_hygro_thresholds(thresholds)
        for key in ("alarm_grace_seconds", "stale_alarm_seconds"):
            if key in provided:
                value = gadgets.normalize_alarm_seconds(provided[key])
                if value is None:
                    dev.pop(key, None)   # back to the class default
                else:
                    dev[key] = value
        settings.save({"hygrometer_devices": devices})
        return {"ok": True, "hygrometers": devices}
    return {"ok": False, "error": "unknown device"}


@router.delete("/hygrometers/{device_id:path}")
async def remove_hygrometer(device_id: str):
    dev_id = _norm_id(device_id)
    devices = [dict(d) for d in gadgets.configured_hygrometers()
               if _norm_id(d.get("id")) != dev_id]
    # An HA- or ESP-sourced hygrometer also leaves its polled list, so its
    # readings stop arriving rather than haunting a removed device.
    save: dict = {"hygrometer_devices": devices}
    pairs = [dict(p) for p in gadgets_ha.configured_hygro_pairs()
             if gadgets_ha.hygro_device_id_for(p.get("temperature")) != dev_id]
    if len(pairs) != len(gadgets_ha.configured_hygro_pairs()):
        save["gadget_ha_hygrometers"] = pairs
    esp_devices = [dict(d) for d in gadgets_esp.configured_devices()
                   if gadgets_esp.device_id_for(d.get("host"), d.get("sensor"))
                   != dev_id]
    if len(esp_devices) != len(gadgets_esp.configured_devices()):
        save["gadget_esp_devices"] = esp_devices
    settings.save(save)
    return {"ok": True, "hygrometers": devices}


class HygroHaPairIn(BaseModel):
    temperature: str
    humidity: str = ""
    name: str = ""
    location: str = ""


@router.get("/ha-hygrometers")
async def ha_hygrometers():
    """Temperature and humidity entities from Home Assistant for the
    hygrometer pair picker. connected=False (with empty lists) when the HA
    connection is not set, so the picker degrades to plain text fields."""
    base, token = gadgets_ha.ha_connection()
    if not (base and token):
        return {"ok": True, "connected": False, "temperature": [],
                "humidity": [], "configured": gadgets_ha.configured_hygro_pairs()}
    temp_entities, hum_entities = await gadgets_ha.list_hygro_entities()
    return {"ok": True, "connected": True, "temperature": temp_entities,
            "humidity": hum_entities,
            "configured": gadgets_ha.configured_hygro_pairs()}


@router.post("/ha-hygrometers")
async def add_ha_hygrometer(payload: HygroHaPairIn):
    """Read a Home Assistant temperature entity (with an optional humidity
    companion) as a hygrometer. Adds the pair to the polled list and a
    matching device to hygrometer_devices, and turns the HA source and the
    hygrometer class on: adding a pair is the opt-in."""
    temp_entity = str(payload.temperature or "").strip().lower()
    hum_entity = str(payload.humidity or "").strip().lower()
    if not gadgets_ha.valid_entity_id(temp_entity):
        return {"ok": False,
                "error": "enter a temperature entity id like sensor.fridge_temperature"}
    if hum_entity and not gadgets_ha.valid_entity_id(hum_entity):
        return {"ok": False,
                "error": "enter a humidity entity id like sensor.fridge_humidity"}
    name = (payload.name or temp_entity)[:60]
    pairs = [dict(p) for p in gadgets_ha.configured_hygro_pairs()]
    entry = {"temperature": temp_entity, "humidity": hum_entity, "name": name}
    for i, pair in enumerate(pairs):
        if pair.get("temperature") == temp_entity:
            pairs[i] = entry
            break
    else:
        pairs.append(entry)
    dev_id = gadgets_ha.hygro_device_id_for(temp_entity)
    devices = [dict(d) for d in gadgets.configured_hygrometers()]
    for dev in devices:
        if _norm_id(dev.get("id")) == dev_id:
            if payload.name:
                dev["name"] = name
            if payload.location:
                dev["location"] = payload.location[:40]
            dev["protocol"] = "home_assistant"
            break
    else:
        devices.append({"id": dev_id, "name": name,
                        "protocol": "home_assistant",
                        "location": payload.location[:40], "thresholds": {}})
    settings.save({"gadget_ha_hygrometers": pairs,
                   "hygrometer_devices": devices,
                   "gadget_ha_enabled": True, "hygrometers_enabled": True})
    return {"ok": True, "pairs": pairs, "hygrometers": devices}


# -- Shelf buttons (FoodAssistant-771d) ---------------------------------------
#
# Stick-anywhere BLE push buttons whose presses the same reader decodes
# passively. Each button binds its press types (single/double/long) to an
# action: add a Grocy product to the shopping list, or fire a Start Page
# action token. Registry lives in settings.button_devices; live state and
# event execution in services/gadgets_buttons.py.


class ButtonIn(BaseModel):
    id: str
    name: str = ""
    protocol: str = ""


class ButtonEditIn(BaseModel):
    device_id: str
    name: str = ""


class ButtonMappingIn(BaseModel):
    device_id: str
    event: str                    # single | double | long
    action: str = ""              # "" clears | shopping_add | esp_action
    product_id: int | None = None
    product_name: str = ""
    token: str = ""


class ButtonTestIn(BaseModel):
    device_id: str
    event: str = "single"


@router.post("/buttons")
async def add_button(payload: ButtonIn):
    """Add a button (usually one the reader discovered from a press). Adding
    the first one is the class's opt-in, so it also flips buttons_enabled."""
    dev_id = _norm_id(payload.id)
    if not dev_id:
        return {"ok": False, "error": "a device id is required"}
    protocol = (payload.protocol
                if payload.protocol in gadgets_buttons.BUTTON_PROTOCOLS else "")
    devices = [dict(d) for d in gadgets_buttons.configured_buttons()]
    for dev in devices:
        if _norm_id(dev.get("id")) == dev_id:
            if payload.name:
                dev["name"] = payload.name[:60]
            if protocol:
                dev["protocol"] = protocol
            break
    else:
        devices.append({"id": dev_id, "name": payload.name[:60],
                        "protocol": protocol, "mappings": {}})
    settings.save({"button_devices": devices, "buttons_enabled": True})
    return {"ok": True, "buttons": devices}


@router.post("/buttons/edit")
async def edit_button(payload: ButtonEditIn):
    """Rename a button. An empty name falls back to the id in the UI."""
    dev_id = _norm_id(payload.device_id)
    devices = [dict(d) for d in gadgets_buttons.configured_buttons()]
    for dev in devices:
        if _norm_id(dev.get("id")) != dev_id:
            continue
        dev["name"] = (payload.name or "").strip()[:60]
        settings.save({"button_devices": devices})
        return {"ok": True, "buttons": devices}
    return {"ok": False, "error": "unknown device"}


@router.post("/buttons/mapping")
async def set_button_mapping(payload: ButtonMappingIn):
    """Set or clear one press type's action. action "" clears the mapping;
    shopping_add needs a product name (with the Grocy product id when picked
    from the search); esp_action needs a Start Page action token."""
    dev_id = _norm_id(payload.device_id)
    if payload.event not in gadgets_buttons.BUTTON_EVENT_TYPES:
        return {"ok": False, "error": "unknown press type"}
    devices = [dict(d) for d in gadgets_buttons.configured_buttons()]
    for dev in devices:
        if _norm_id(dev.get("id")) != dev_id:
            continue
        mappings = gadgets_buttons.normalize_mappings(dev.get("mappings"))
        mapping = gadgets_buttons.normalize_mapping({
            "action": payload.action,
            "product_id": payload.product_id,
            "product_name": payload.product_name,
            "token": payload.token,
        })
        if mapping:
            mappings[payload.event] = mapping
        elif payload.action:
            need = ("pick a product" if payload.action == "shopping_add"
                    else "enter an action token")
            return {"ok": False, "error": f"{need} first"}
        else:
            mappings.pop(payload.event, None)
        dev["mappings"] = mappings
        settings.save({"button_devices": devices})
        return {"ok": True, "buttons": devices}
    return {"ok": False, "error": "unknown device"}


@router.delete("/buttons/{device_id:path}")
async def remove_button(device_id: str):
    dev_id = _norm_id(device_id)
    devices = [dict(d) for d in gadgets_buttons.configured_buttons()
               if _norm_id(d.get("id")) != dev_id]
    settings.save({"button_devices": devices})
    return {"ok": True, "buttons": devices}


@router.post("/buttons/test")
async def test_button(payload: ButtonTestIn):
    """Run a button's mapping without pressing it, so a mapping can be
    checked from Settings. Bypasses the press cooldown."""
    return await gadgets_buttons.test_fire(payload.device_id, payload.event)


@router.get("/product-search")
async def product_search(q: str = ""):
    """Grocy products matching a typed prefix, for the button mapping picker
    (id + name, so the mapping links the exact product). Empty and error
    cases return an empty list so the picker degrades to plain text."""
    key = (q or "").strip().lower()
    if not key:
        return {"ok": True, "products": []}
    try:
        from ..services.grocy import GrocyClient
        products = await GrocyClient().get_products()
    except Exception as e:  # noqa: BLE001 - the picker degrades, never 500s
        return {"ok": False, "products": [], "error": str(e)}
    rows = [{"id": p.get("id"), "name": str(p.get("name") or "").strip()}
            for p in products if p.get("name")]
    starts = sorted((r for r in rows if r["name"].lower().startswith(key)),
                    key=lambda r: r["name"].lower())
    contains = sorted((r for r in rows if key in r["name"].lower()
                       and not r["name"].lower().startswith(key)),
                      key=lambda r: r["name"].lower())
    return {"ok": True, "products": (starts + contains)[:10]}


# -- Door/window contact sensors (FoodAssistant-5c61) -------------------------
#
# A fourth device class: a magnet sensor on a fridge or freezer door,
# open/closed instead of a temperature. Same reader, same readings push
# (kind="contact"), their own registry (settings.contact_devices). A door
# open longer than its per-device threshold raises an on-screen alarm that
# clears when it closes (services/gadgets.py protection sweep).


class ContactDeviceIn(BaseModel):
    id: str
    name: str = ""
    protocol: str = ""
    location: str = ""


class ContactEditIn(BaseModel):
    device_id: str
    name: str | None = None
    location: str | None = None
    # Null restores the 3 minute default.
    open_alarm_seconds: float | None = None


@router.post("/contacts")
async def add_contact(payload: ContactDeviceIn):
    """Add a door sensor (usually one the reader discovered). Adding the
    first one is the class's opt-in, so it also flips contacts_enabled."""
    dev_id = _norm_id(payload.id)
    if not dev_id:
        return {"ok": False, "error": "a device id is required"}
    protocol = payload.protocol if payload.protocol in gadgets.CONTACT_PROTOCOLS else ""
    devices = [dict(d) for d in gadgets.configured_contacts()]
    for dev in devices:
        if _norm_id(dev.get("id")) == dev_id:
            if payload.name:
                dev["name"] = payload.name[:60]
            if payload.location:
                dev["location"] = payload.location[:40]
            if protocol:
                dev["protocol"] = protocol
            break
    else:
        devices.append({"id": dev_id, "name": payload.name[:60],
                        "protocol": protocol,
                        "location": payload.location[:40]})
    settings.save({"contact_devices": devices, "contacts_enabled": True})
    return {"ok": True, "contacts": devices}


@router.post("/contacts/edit")
async def edit_contact(payload: ContactEditIn):
    """Rename a door sensor, set its location label, or change how long it
    may stay open before the alarm. Only the fields present in the request
    are applied; passing open_alarm_seconds as null restores the default."""
    dev_id = _norm_id(payload.device_id)
    provided = payload.model_dump(exclude_unset=True)
    devices = [dict(d) for d in gadgets.configured_contacts()]
    for dev in devices:
        if _norm_id(dev.get("id")) != dev_id:
            continue
        if "name" in provided:
            dev["name"] = str(provided["name"] or "").strip()[:60]
        if "location" in provided:
            dev["location"] = str(provided["location"] or "").strip()[:40]
        if "open_alarm_seconds" in provided:
            value = gadgets.normalize_alarm_seconds(provided["open_alarm_seconds"])
            if value is None:
                dev.pop("open_alarm_seconds", None)
            else:
                dev["open_alarm_seconds"] = value
        settings.save({"contact_devices": devices})
        return {"ok": True, "contacts": devices}
    return {"ok": False, "error": "unknown device"}


@router.delete("/contacts/{device_id:path}")
async def remove_contact(device_id: str):
    dev_id = _norm_id(device_id)
    devices = [dict(d) for d in gadgets.configured_contacts()
               if _norm_id(d.get("id")) != dev_id]
    settings.save({"contact_devices": devices})
    return {"ok": True, "contacts": devices}


# -- Home Assistant source (FoodAssistant-mnks) ------------------------------

class HaEntityIn(BaseModel):
    entity_id: str
    name: str = ""


@router.get("/ha-entities")
async def ha_entities():
    """Temperature entities from Home Assistant for the Settings picker.
    connected=False (with no entities) when the HA connection is not set, so
    the picker degrades to a plain text field."""
    base, token = gadgets_ha.ha_connection()
    if not (base and token):
        return {"ok": True, "connected": False, "entities": [],
                "configured": gadgets_ha.configured_entities()}
    entities = await gadgets_ha.list_temperature_entities()
    return {"ok": True, "connected": True, "entities": entities,
            "configured": gadgets_ha.configured_entities(),
            "grouped": gadgets_ha.group_entities_into_devices(entities)}


@router.post("/ha-entities")
async def add_ha_entity(payload: HaEntityIn):
    """Read one Home Assistant entity as a thermometer. Adds the entity to
    the polled list and a matching virtual device to gadget_devices (so the
    Timers page and target alerts treat it like any thermometer), and turns
    both the feature and the HA source on: adding an entity is the opt-in."""
    entity_id = str(payload.entity_id or "").strip().lower()
    if not gadgets_ha.valid_entity_id(entity_id):
        return {"ok": False,
                "error": "enter an entity id like sensor.grill_probe_1"}
    entities = gadgets_ha.configured_entities()
    if entity_id not in entities:
        entities.append(entity_id)
    dev_id = gadgets_ha.device_id_for(entity_id)
    name = (payload.name or entity_id)[:60]
    devices = [dict(d) for d in gadgets.configured_devices()]
    for dev in devices:
        if _norm_id(dev.get("id")) == dev_id:
            if payload.name:
                dev["name"] = name
            dev["protocol"] = "home_assistant"
            break
    else:
        devices.append({"id": dev_id, "name": name,
                        "protocol": "home_assistant", "targets": {}})
    settings.save({"gadget_ha_entities": entities, "gadget_devices": devices,
                   "gadget_ha_enabled": True, "gadgets_enabled": True})
    return {"ok": True, "entities": entities, "devices": devices}


class HaDeviceIn(BaseModel):
    device_name: str = ""
    entities: list[HaEntityIn] = []


@router.post("/ha-devices")
async def add_ha_device(payload: HaDeviceIn):
    """Add every probe entity of one discovered grill/multi-probe device at
    once, from the "Discover grills" list. Same effect as calling
    POST /gadgets/ha-entities once per probe, but in a single settings save
    so the Timers page sees the whole device appear together."""
    entities = gadgets_ha.configured_entities()
    devices = [dict(d) for d in gadgets.configured_devices()]
    added = 0
    for item in payload.entities:
        entity_id = str(item.entity_id or "").strip().lower()
        if not gadgets_ha.valid_entity_id(entity_id):
            continue
        if entity_id not in entities:
            entities.append(entity_id)
        dev_id = gadgets_ha.device_id_for(entity_id)
        name = (item.name or entity_id)[:60]
        for dev in devices:
            if _norm_id(dev.get("id")) == dev_id:
                if item.name:
                    dev["name"] = name
                dev["protocol"] = "home_assistant"
                break
        else:
            devices.append({"id": dev_id, "name": name,
                            "protocol": "home_assistant", "targets": {}})
        added += 1
    if not added:
        return {"ok": False, "error": "no valid entities to add"}
    settings.save({"gadget_ha_entities": entities, "gadget_devices": devices,
                   "gadget_ha_enabled": True, "gadgets_enabled": True})
    return {"ok": True, "added": added, "entities": entities, "devices": devices}


@router.delete("/ha-entities/{entity_id:path}")
async def remove_ha_entity(entity_id: str):
    """Stop reading an HA entity: drop it from the polled list and remove its
    virtual device (targets included)."""
    entity_id = str(entity_id or "").strip().lower()
    entities = [e for e in gadgets_ha.configured_entities() if e != entity_id]
    dev_id = gadgets_ha.device_id_for(entity_id)
    devices = [dict(d) for d in gadgets.configured_devices()
               if _norm_id(d.get("id")) != dev_id]
    settings.save({"gadget_ha_entities": entities, "gadget_devices": devices})
    return {"ok": True, "entities": entities, "devices": devices}


# --------------------------------------------------------------------------
# ESPHome WiFi sensors as a source (FoodAssistant-0oq3)
# --------------------------------------------------------------------------

class EspDeviceIn(BaseModel):
    host: str
    sensor: str
    name: str = ""
    auth: str = ""       # optional "user:pass" for ESPHome web_server auth
    battery: str = ""    # optional companion battery sensor object id
    kind: str = ""       # "" thermometer (default) | "hygrometer"
    humidity: str = ""   # hygrometer only: companion humidity sensor id
    location: str = ""   # hygrometer only: location label (Fridge, Freezer)


@router.get("/esp-sensors")
async def esp_sensors(host: str = ""):
    """Temperature sensors an ESP device exposes, for the Settings picker.
    Reads a few seconds of the device's ESPHome web_server /events stream.
    Returns connected=False with no sensors when the host is unreachable, so
    the picker degrades to a plain text field."""
    host = gadgets_esp.normalize_host(host)
    if not gadgets_esp.valid_host(host):
        return {"ok": False, "connected": False, "sensors": [],
                "error": "enter the ESP device address, e.g. 192.168.1.50 "
                         "or fridge.local"}
    sensors = await gadgets_esp.discover_sensors(host)
    return {"ok": True, "connected": bool(sensors), "sensors": sensors,
            "host": host}


@router.post("/esp-devices")
async def add_esp_device(payload: EspDeviceIn):
    """Read one ESPHome sensor as a thermometer. Adds the device to the polled
    list and a matching virtual device to gadget_devices (so the Timers page
    and target alerts treat it like any thermometer), and turns both the
    feature and the ESP source on: adding a device is the opt-in."""
    host = gadgets_esp.normalize_host(payload.host)
    sensor = str(payload.sensor or "").strip().lower()
    if not gadgets_esp.valid_host(host):
        return {"ok": False,
                "error": "enter the ESP device address, e.g. 192.168.1.50"}
    if not gadgets_esp.valid_sensor(sensor):
        return {"ok": False,
                "error": "enter a sensor id like fridge_temp"}
    dev_id = gadgets_esp.device_id_for(host, sensor)
    name = (payload.name or sensor)[:60]
    hygro = payload.kind == "hygrometer"
    esp_devices = [dict(d) for d in gadgets_esp.configured_devices()]
    entry = {"host": host, "sensor": sensor, "name": name}
    if payload.auth and ":" in payload.auth:
        entry["auth"] = payload.auth
    if payload.battery and gadgets_esp.valid_sensor(payload.battery.strip().lower()):
        entry["battery"] = payload.battery.strip().lower()
    if hygro:
        entry["kind"] = "hygrometer"
        humidity = payload.humidity.strip().lower()
        if humidity and gadgets_esp.valid_sensor(humidity):
            entry["humidity"] = humidity
    for i, dev in enumerate(esp_devices):
        if gadgets_esp.device_id_for(dev.get("host"), dev.get("sensor")) == dev_id:
            esp_devices[i] = entry
            break
    else:
        esp_devices.append(entry)
    save: dict = {"gadget_esp_devices": esp_devices, "gadget_esp_enabled": True}
    if hygro:
        # A WiFi fridge/room sensor joins the hygrometer class, not the
        # cooking probes (FoodAssistant-q97i).
        hygros = [dict(d) for d in gadgets.configured_hygrometers()]
        for dev in hygros:
            if _norm_id(dev.get("id")) == dev_id:
                if payload.name:
                    dev["name"] = name
                if payload.location:
                    dev["location"] = payload.location[:40]
                dev["protocol"] = "esphome"
                break
        else:
            hygros.append({"id": dev_id, "name": name, "protocol": "esphome",
                           "location": payload.location[:40], "thresholds": {}})
        save.update({"hygrometer_devices": hygros, "hygrometers_enabled": True})
        settings.save(save)
        return {"ok": True, "esp_devices": esp_devices, "hygrometers": hygros}
    devices = [dict(d) for d in gadgets.configured_devices()]
    for dev in devices:
        if _norm_id(dev.get("id")) == dev_id:
            if payload.name:
                dev["name"] = name
            dev["protocol"] = "esphome"
            break
    else:
        devices.append({"id": dev_id, "name": name,
                        "protocol": "esphome", "targets": {}})
    save.update({"gadget_devices": devices, "gadgets_enabled": True})
    settings.save(save)
    return {"ok": True, "esp_devices": esp_devices, "devices": devices}


@router.delete("/esp-devices/{device_id:path}")
async def remove_esp_device(device_id: str):
    """Stop reading an ESP sensor: drop it from the polled list and remove its
    virtual device (targets included). device_id is the ESP:<HOST>:<SENSOR>
    gadgets id."""
    dev_id = _norm_id(device_id)
    esp_devices = [dict(d) for d in gadgets_esp.configured_devices()
                   if gadgets_esp.device_id_for(d.get("host"), d.get("sensor"))
                   != dev_id]
    devices = [dict(d) for d in gadgets.configured_devices()
               if _norm_id(d.get("id")) != dev_id]
    hygros = [dict(d) for d in gadgets.configured_hygrometers()
              if _norm_id(d.get("id")) != dev_id]
    settings.save({"gadget_esp_devices": esp_devices, "gadget_devices": devices,
                   "hygrometer_devices": hygros})
    return {"ok": True, "esp_devices": esp_devices, "devices": devices}


class EspActionIn(BaseModel):
    button: str          # a Start Page / deck action token
    long: bool = False   # long-press semantics (a timer key cancels/resets)


@router.post("/esp-action")
async def esp_action(payload: EspActionIn):
    """Fire an action from an ESP device button (FoodAssistant-k4wc).

    A DIY ESP (ESPHome ``http_request`` on a button press) POSTs the action
    token that button is wired to, with the app API key in ``X-API-Key`` (the
    global auth middleware enforces it, the same way it does for Home Assistant
    and any other headless client). The token is the SAME vocabulary the
    on-screen Start Page and the Stream Deck use, so nothing new to learn: a
    timer key (``timer_1``/``timer_2``/``timer_3`` cycle through 5/10/15/30/60,
    the ``timer_eggs``/``timer_pasta``/``timer_rice`` presets, or a custom timer
    key id), an ``ha_1``..``ha_5`` Home Assistant slot, or a custom key id
    (Home Assistant action, media, macro, quick shopping-add). One physical
    button can start a kitchen timer or fire a Home Assistant action with a
    single POST. ``long`` true is a long press: a timer key cancels/resets,
    matching the deck. Unknown tokens are reported, never raised."""
    from ..services import start_actions
    token = str(payload.button or "").strip()
    if not token:
        return {"ok": False, "detail": "No button action was specified."}
    return await start_actions.fire_key(token, long=payload.long)


@router.get("/esp-screen")
async def esp_screen(lines: int = 4):
    """Compact status for a small ESP screen (FoodAssistant-k4wc follow-on).

    Read-only, and built only from local state (running timers plus the active
    recipe), so it is instant and never waits on Grocy or the network, which
    matters for a screen that polls every few seconds. An ESP with an OLED or
    e-ink display fetches this and prints ``lines`` straight through, or reads
    the structured ``timers`` for its own layout. Auth is the same X-API-Key as
    the other gadget endpoints."""
    import time as _time
    from ..services import timers as timers_svc, current_recipe
    now = _time.time()
    tlist = timers_svc.list_timers()
    active = current_recipe.get_active() or {}
    recipe_title = str(active.get("title") or "")
    return {
        "ok": True,
        "generated": int(now),
        "lines": gadgets_esp.compose_screen(tlist, recipe_title, max_lines=lines),
        "timers": [
            {"label": t.get("label"),
             "remaining": int(t.get("remaining_seconds") or 0),
             "expired": bool(t.get("expired"))}
            for t in tlist
        ],
        "recipe": recipe_title,
    }
