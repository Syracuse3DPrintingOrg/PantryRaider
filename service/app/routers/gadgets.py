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
from ..services import gadgets, gadgets_ha

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
    }


@router.post("/readings")
async def post_readings(payload: dict):
    """Ingest a reader push: live probe readings plus discovered devices."""
    return gadgets.ingest(payload if isinstance(payload, dict) else {})


@router.get("/state")
async def state():
    """The Timers page snapshot: live probes, targets, and devices to add."""
    return gadgets.get_state()


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
