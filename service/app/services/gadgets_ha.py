"""Home Assistant as a thermometer source (FoodAssistant-mnks).

Home Assistant already integrates many Bluetooth thermometers and sensors
(directly or through ESPHome Bluetooth proxies) and exposes them as
temperature entities. This module polls those entities over the HA REST API
using the Home Assistant connection the app already stores
(streamdeck_ha_base_url / streamdeck_ha_token) and feeds the readings through
the same gadgets ingest path the host BLE reader uses, so a server with no
Bluetooth radio still gets live probes on the Timers page and the same
target-temperature alerts (alerts evaluate on ingest, so they apply to these
readings automatically).

Each configured entity becomes one virtual single-probe device:

  id        "HA:<ENTITY_ID>" (ingest uppercases ids, so this is stable)
  protocol  "home_assistant"
  probe 1   the entity state, converted to Celsius when HA reports Fahrenheit
  battery   attributes.battery_level when present

The poll loop runs as a background task from app startup (main.py) and is a
no-op until the source is enabled, at least one entity is configured, and the
Home Assistant connection is set. It never raises: an unreachable HA or a bad
entity is skipped quietly and retried on the next pass.

The parsing helpers are pure so they test without a network.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime

import httpx

from . import gadgets

log = logging.getLogger(__name__)

POLL_SECONDS = 10
# An entity whose last_updated is older than this is treated as gone (the
# thermometer was switched off); skipping it lets the normal stale/prune
# handling in the gadgets state age it out instead of showing a frozen value.
ENTITY_STALE_SECONDS = 15 * 60

_ENTITY_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

def valid_entity_id(entity_id: str) -> bool:
    """True for a well-formed HA entity id like "sensor.grill_probe_1"."""
    return bool(_ENTITY_RE.match(str(entity_id or "").strip().lower()))


def device_id_for(entity_id: str) -> str:
    """The gadgets device id an entity maps to (ids are kept uppercase)."""
    return f"HA:{str(entity_id or '').strip().upper()}"


def _parse_ha_ts(value) -> float | None:
    """Epoch seconds from an HA ISO timestamp, or None when unparseable."""
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def entity_reading(entity_id: str, data: dict, now: float,
                   stale_after: float = ENTITY_STALE_SECONDS) -> dict | None:
    """Turn one GET /api/states/<entity> body into a gadgets device entry.

    Pure. Returns None for anything not usable as a probe reading: a
    non-numeric or unavailable/unknown state, or an entity whose last_updated
    is older than stale_after (the sensor is gone, not at that temperature).
    Fahrenheit states are converted; everything downstream is Celsius.
    """
    if not isinstance(data, dict):
        return None
    state = data.get("state")
    if state in (None, "", "unavailable", "unknown"):
        return None
    try:
        temp = float(state)
    except (TypeError, ValueError):
        return None
    attrs = data.get("attributes") if isinstance(data.get("attributes"), dict) else {}
    unit = str(attrs.get("unit_of_measurement") or "")
    if "F" in unit.upper():
        temp = (temp - 32.0) * 5.0 / 9.0
    updated = _parse_ha_ts(data.get("last_updated"))
    if updated is not None and now - updated > stale_after:
        return None
    battery = attrs.get("battery_level")
    if battery is not None:
        try:
            battery = max(0, min(100, int(battery)))
        except (TypeError, ValueError):
            battery = None
    return {
        "id": device_id_for(entity_id),
        "name": str(attrs.get("friendly_name") or entity_id)[:60],
        "protocol": "home_assistant",
        "probes": [{"index": 1, "temp_c": round(temp, 2)}],
        "battery": battery,
    }


def has_current_value(state) -> bool:
    """True when a raw HA state represents a real reading right now: a
    finite number, not "unavailable"/"unknown"/""/None. Pure; used both to
    flag rows for the Settings entity picker and, indirectly, by
    entity_reading above."""
    if state in (None, "", "unavailable", "unknown"):
        return False
    try:
        float(state)
    except (TypeError, ValueError):
        return False
    return True


# A trailing "Probe 1" / "_probe_2" / plain " 3" suffix on a name or entity
# id is what most multi-probe grills use to tell their leads apart; strip it
# to get the shared device name.
_PROBE_SUFFIX_RE = re.compile(r"[\s_-]*probe[\s_-]*\d*$|[\s_-]+\d+$",
                              re.IGNORECASE)


def _device_name_prefix(entity_id: str, name: str) -> str:
    """The common device name a probe entity belongs to, e.g. "Grill Probe 1"
    and "sensor.grill_probe_2" both reduce to "Grill". Falls back to the full
    name (or entity id) when there is nothing to strip."""
    base = str(name or entity_id or "").strip()
    stripped = _PROBE_SUFFIX_RE.sub("", base).strip(" _-")
    return stripped or base


def group_entities_into_devices(entities: list[dict]) -> list[dict]:
    """Cluster HA temperature entities (as returned by
    list_temperature_entities) into suggested physical devices, for the
    Settings "Discover grills" action. Many multi-probe grills and smokers
    expose one HA entity per probe; this groups the ones that belong
    together so the user can add a whole grill instead of hunting probes
    one at a time.

    Entities that carry a "device_id" (HA's device registry id) group by
    that; note the Settings entity picker's /api/states data does not
    currently include one, so in practice every group here comes from the
    name/entity-id prefix fallback below, stripping a trailing "Probe N",
    "_probe_N", or plain number suffix (e.g. "Grill Probe 1" and
    "Grill Probe 2" both fall under "Grill").

    Only groups of 2 or more entities are returned. Singletons already have
    an obvious one-at-a-time add path and would just be noise here. Pure;
    returns [] for no groups, sorted by device_name."""
    groups: dict[str, dict] = {}
    order: list[str] = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        entity_id = str(entity.get("entity_id") or "").strip()
        if not entity_id:
            continue
        name = str(entity.get("name") or entity_id)
        device_id = str(entity.get("device_id") or "").strip()
        prefix = _device_name_prefix(entity_id, name)
        key = f"device:{device_id}" if device_id else f"prefix:{prefix.lower()}"
        if key not in groups:
            groups[key] = {"device_name": prefix, "entity_ids": []}
            order.append(key)
        groups[key]["entity_ids"].append(entity_id)
    result = [groups[k] for k in order if len(groups[k]["entity_ids"]) >= 2]
    result.sort(key=lambda g: str(g["device_name"]).lower())
    return result


def is_temperature_entity(entity: dict) -> bool:
    """Whether a GET /api/states row looks like a temperature sensor, for the
    Settings entity picker. Pure: device_class temperature, or a sensor with a
    degree unit."""
    if not isinstance(entity, dict):
        return False
    entity_id = str(entity.get("entity_id") or "")
    if not entity_id.startswith("sensor."):
        return False
    attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
    if attrs.get("device_class") == "temperature":
        return True
    unit = str(attrs.get("unit_of_measurement") or "").upper()
    return unit in ("°F", "°C", "F", "C")


# --------------------------------------------------------------------------
# Settings glue
# --------------------------------------------------------------------------

def ha_connection() -> tuple[str, str]:
    """(base_url, token) of the app's Home Assistant connection, "" when unset."""
    from ..config import settings
    base = str(settings.streamdeck_ha_base_url or "").rstrip("/")
    token = str(settings.streamdeck_ha_token or "")
    return base, token


def configured_entities() -> list[str]:
    """The sanitized gadget_ha_entities list from settings."""
    from ..config import settings
    out: list[str] = []
    for entity in settings.gadget_ha_entities or []:
        entity = str(entity or "").strip().lower()
        if valid_entity_id(entity) and entity not in out:
            out.append(entity)
    return out


def source_active() -> bool:
    """Whether the HA source has everything it needs to poll."""
    from ..config import settings
    base, token = ha_connection()
    return bool(settings.gadget_ha_enabled and base and token
                and configured_entities())


# --------------------------------------------------------------------------
# Polling
# --------------------------------------------------------------------------

async def poll_once(client: httpx.AsyncClient | None = None,
                    now: float | None = None) -> int:
    """Read every configured entity once and ingest what parsed. Returns the
    number of readings ingested. Never raises past a single entity: one bad
    entity cannot starve the rest."""
    now = time.time() if now is None else now
    base, token = ha_connection()
    entities = configured_entities()
    if not (base and token and entities):
        return 0
    headers = {"Authorization": f"Bearer {token}"}
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=8.0)
    readings = []
    try:
        for entity_id in entities:
            try:
                r = await client.get(f"{base}/api/states/{entity_id}",
                                     headers=headers)
                if r.status_code != 200:
                    continue
                reading = entity_reading(entity_id, r.json(), now)
                if reading:
                    readings.append(reading)
            except Exception:  # noqa: BLE001
                continue
    finally:
        if own_client:
            await client.aclose()
    if readings:
        # mark_reader=False: these are Home Assistant readings, not proof the
        # host Bluetooth reader is running.
        gadgets.ingest({"devices": readings}, now, mark_reader=False)
    return len(readings)


async def list_temperature_entities(client: httpx.AsyncClient | None = None) -> list[dict]:
    """Temperature-looking sensors from HA, for the Settings entity picker.
    Returns [] when HA is unconfigured or unreachable (the picker degrades to
    a plain text field)."""
    base, token = ha_connection()
    if not (base and token):
        return []
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=8.0)
    try:
        r = await client.get(f"{base}/api/states",
                             headers={"Authorization": f"Bearer {token}"})
        if r.status_code != 200:
            return []
        rows = r.json()
    except Exception:  # noqa: BLE001
        return []
    finally:
        if own_client:
            await client.aclose()
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not is_temperature_entity(row):
            continue
        attrs = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
        out.append({
            "entity_id": row.get("entity_id"),
            "name": str(attrs.get("friendly_name") or row.get("entity_id"))[:60],
            "state": row.get("state"),
            "unit": attrs.get("unit_of_measurement") or "",
            "has_value": has_current_value(row.get("state")),
        })
    out.sort(key=lambda e: str(e.get("name") or "").lower())
    return out


async def poll_loop() -> None:
    """Background task: poll HA every POLL_SECONDS while the source is
    configured, sit quietly otherwise. Cancellable; never crashes."""
    while True:
        try:
            if source_active():
                await poll_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.debug("Home Assistant thermometer poll failed: %s", exc)
        await asyncio.sleep(POLL_SECONDS)
