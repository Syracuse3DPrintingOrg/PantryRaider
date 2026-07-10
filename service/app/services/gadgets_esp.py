"""ESPHome WiFi sensors as a thermometer source (FoodAssistant-0oq3).

A cheap ESP32 or ESP8266 flashed with ESPHome, a temperature sensor (a
DS18B20 probe, a DHT22, a BME280, whatever), and the ``web_server`` component
turns into a little REST endpoint on your LAN. Each sensor is readable at
``http://<device>/sensor/<object_id>`` and returns JSON like
``{"id": "sensor-fridge_temp", "value": 4.2, "state": "4.2 °C"}``.

This module polls those endpoints and feeds the readings through the same
gadgets ingest path the host Bluetooth reader uses, so a WiFi fridge, freezer,
or room probe shows up on the Timers page and raises the same target-temp
alerts, with no Bluetooth radio and no Home Assistant in the middle. It is the
DIY-hardware sibling of gadgets_ha.py and follows the same shape.

Each configured device becomes one virtual single-probe thermometer:

  id        "ESP:<HOST>:<SENSOR>" (ingest uppercases ids, so this is stable)
  protocol  "esphome"
  probe 1   the sensor value, converted to Celsius when it reports Fahrenheit
  battery   a companion "battery"/"battery_level" sensor when the device has one

The poll loop runs as a background task from app startup (main.py) and is a
no-op until the source is enabled and at least one device is configured. It
never raises: an unreachable ESP or a bad sensor is skipped quietly and
retried on the next pass.

The parsing helpers are pure so they test without a network.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

import httpx

from . import gadgets

log = logging.getLogger(__name__)

POLL_SECONDS = 10
DISCOVER_SECONDS = 3.0

# A hostname or IPv4/IPv6 literal, optionally with :port. Deliberately loose:
# ESP devices use .local mDNS names, bare IPs, and static hostnames alike.
_HOST_RE = re.compile(r"^[A-Za-z0-9._\-\[\]:]{1,255}$")
# ESPHome object ids are lowercase slugs (letters, digits, underscores).
_SENSOR_RE = re.compile(r"^[a-z0-9_]+$")
# Sensor ids that look like a battery level, used to attach battery% to a
# device's reading when the ESP exposes one alongside its temperature.
_BATTERY_RE = re.compile(r"battery")


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

def valid_host(host: str) -> bool:
    """True for a plausible ESP host: an IP, an mDNS name, or a hostname,
    with no scheme or path. The user types the bare address."""
    host = str(host or "").strip()
    if not host or "/" in host or " " in host:
        return False
    return bool(_HOST_RE.match(host))


def valid_sensor(sensor: str) -> bool:
    """True for a well-formed ESPHome sensor object id like "fridge_temp"."""
    return bool(_SENSOR_RE.match(str(sensor or "").strip().lower()))


def normalize_host(host: str) -> str:
    """Strip an accidental scheme, trailing slash, or /sensor/... path a user
    may paste, leaving the bare host[:port] ESPHome is reachable at."""
    host = str(host or "").strip()
    host = re.sub(r"^https?://", "", host, flags=re.IGNORECASE)
    host = host.split("/", 1)[0]
    return host.strip().rstrip(".")


def device_id_for(host: str, sensor: str) -> str:
    """The gadgets device id an ESP sensor maps to (ids are kept uppercase)."""
    host = normalize_host(host).upper()
    sensor = str(sensor or "").strip().upper()
    return f"ESP:{host}:{sensor}"


def base_url(host: str) -> str:
    """The http base URL for an ESP host (ESPHome web_server is plain HTTP)."""
    return f"http://{normalize_host(host)}"


def _auth_for(auth) -> tuple[str, str] | None:
    """(user, password) from a stored "user:pass" string, or None. ESPHome's
    web_server optional auth is HTTP Basic."""
    if not auth:
        return None
    text = str(auth)
    if ":" not in text:
        return None
    user, _, pw = text.partition(":")
    return (user, pw)


def _celsius_from(value, state) -> float | None:
    """A Celsius float from an ESPHome sensor's value/state pair. ESPHome puts
    the raw number in ``value`` and a formatted "<n> <unit>" in ``state``; we
    read the number from ``value`` and sniff the unit from ``state`` so a
    Fahrenheit sensor is converted. Pure; None when there is no usable number."""
    try:
        temp = float(value)
    except (TypeError, ValueError):
        return None
    if temp != temp:  # NaN, ESPHome's "no reading yet"
        return None
    unit = str(state or "")
    if "°F" in unit or re.search(r"\bF\b", unit):
        temp = (temp - 32.0) * 5.0 / 9.0
    return round(temp, 2)


def sensor_object_id(raw_id) -> str:
    """The bare object id from an ESPHome entity id. ESPHome prefixes entity
    ids with their domain and a dash ("sensor-fridge_temp"); strip it."""
    text = str(raw_id or "").strip()
    if "-" in text:
        return text.split("-", 1)[1]
    return text


def sensor_reading(host: str, sensor: str, data: dict, now: float,
                   battery=None) -> dict | None:
    """Turn one GET /sensor/<id> body into a gadgets device entry.

    Pure. Returns None for anything not usable as a probe reading (a missing or
    non-numeric value, or a NaN "no reading yet"). ``battery`` is an optional
    already-parsed 0-100 int from a companion battery sensor. Fahrenheit is
    converted; everything downstream is Celsius."""
    if not isinstance(data, dict):
        return None
    temp = _celsius_from(data.get("value"), data.get("state"))
    if temp is None:
        return None
    if battery is not None:
        try:
            battery = max(0, min(100, int(battery)))
        except (TypeError, ValueError):
            battery = None
    return {
        "id": device_id_for(host, sensor),
        "name": str(sensor)[:60],
        "protocol": "esphome",
        "probes": [{"index": 1, "temp_c": temp}],
        "battery": battery,
    }


def battery_pct_from(value, state) -> int | None:
    """A 0-100 battery percentage from an ESPHome battery sensor's value/state,
    or None. Pure; clamps out-of-range and rejects non-numbers."""
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return None
    if pct != pct:
        return None
    return max(0, min(100, int(round(pct))))


def is_temperature_event(payload: dict) -> bool:
    """Whether one ESPHome /events "state" payload looks like a temperature
    sensor, for the discovery picker. Pure: a sensor-domain entity whose state
    string carries a degree unit."""
    if not isinstance(payload, dict):
        return False
    raw_id = str(payload.get("id") or "")
    if not raw_id.startswith("sensor-"):
        return False
    state = str(payload.get("state") or "")
    return "°" in state or bool(re.search(r"\b[CF]\b", state))


def parse_events_stream(text: str) -> list[dict]:
    """Collect discovered temperature sensors from a captured chunk of an
    ESPHome web_server ``/events`` SSE stream. ESPHome emits one
    ``event: state`` block per entity with a JSON ``data:`` line. Pure: returns
    a de-duplicated list of {"sensor", "name", "state"} for the temperature
    sensors seen, in first-seen order."""
    out: list[dict] = []
    seen: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        body = line[len("data:"):].strip()
        if not body:
            continue
        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            continue
        if not is_temperature_event(payload):
            continue
        sensor = sensor_object_id(payload.get("id"))
        if not sensor or sensor in seen:
            continue
        seen.add(sensor)
        out.append({
            "sensor": sensor,
            "name": str(payload.get("name") or sensor)[:60],
            "state": str(payload.get("state") or ""),
        })
    return out


# --------------------------------------------------------------------------
# Settings glue
# --------------------------------------------------------------------------

def configured_devices() -> list[dict]:
    """The sanitized gadget_esp_devices list from settings: each a dict with a
    valid host and sensor. Malformed rows are dropped."""
    from ..config import settings
    out: list[dict] = []
    seen: set[str] = set()
    for dev in settings.gadget_esp_devices or []:
        if not isinstance(dev, dict):
            continue
        host = normalize_host(dev.get("host"))
        sensor = str(dev.get("sensor") or "").strip().lower()
        if not (valid_host(host) and valid_sensor(sensor)):
            continue
        key = device_id_for(host, sensor)
        if key in seen:
            continue
        seen.add(key)
        clean = {"host": host, "sensor": sensor,
                 "name": str(dev.get("name") or sensor)[:60]}
        if dev.get("auth"):
            clean["auth"] = str(dev.get("auth"))
        if dev.get("battery"):
            clean["battery"] = str(dev.get("battery")).strip().lower()
        out.append(clean)
    return out


def source_active() -> bool:
    """Whether the ESP source has everything it needs to poll."""
    from ..config import settings
    return bool(settings.gadget_esp_enabled and configured_devices())


# --------------------------------------------------------------------------
# Polling
# --------------------------------------------------------------------------

async def _read_sensor(client: httpx.AsyncClient, host: str, sensor: str,
                       auth) -> dict | None:
    """GET one ESPHome sensor body, or None on any failure."""
    try:
        r = await client.get(f"{base_url(host)}/sensor/{sensor}",
                             auth=_auth_for(auth))
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:  # noqa: BLE001
        return None


async def poll_once(client: httpx.AsyncClient | None = None,
                    now: float | None = None) -> int:
    """Read every configured ESP device once and ingest what parsed. Returns
    the number of readings ingested. Never raises past a single device: one
    unreachable ESP cannot starve the rest."""
    now = time.time() if now is None else now
    devices = configured_devices()
    if not devices:
        return 0
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=8.0)
    readings = []
    try:
        for dev in devices:
            host, sensor = dev["host"], dev["sensor"]
            body = await _read_sensor(client, host, sensor, dev.get("auth"))
            if body is None:
                continue
            battery = None
            if dev.get("battery"):
                bat_body = await _read_sensor(client, host, dev["battery"],
                                              dev.get("auth"))
                if isinstance(bat_body, dict):
                    battery = battery_pct_from(bat_body.get("value"),
                                               bat_body.get("state"))
            reading = sensor_reading(host, sensor, body, now, battery=battery)
            if reading:
                reading["name"] = dev.get("name") or sensor
                readings.append(reading)
    finally:
        if own_client:
            await client.aclose()
    if readings:
        # mark_reader=False: an ESP reading is not proof the host Bluetooth
        # reader is running (same reasoning as the Home Assistant source).
        gadgets.ingest({"devices": readings}, now, mark_reader=False)
    return len(readings)


async def discover_sensors(host: str,
                           client: httpx.AsyncClient | None = None) -> list[dict]:
    """List the temperature sensors an ESP device exposes, for the Settings
    picker. Reads a few seconds of its ESPHome web_server ``/events`` SSE
    stream, which emits one state event per entity. Returns [] when the device
    is unreachable or exposes no temperature sensors (the picker degrades to a
    plain text field)."""
    host = normalize_host(host)
    if not valid_host(host):
        return []
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=DISCOVER_SECONDS + 3.0)
    chunks: list[str] = []
    try:
        deadline = time.time() + DISCOVER_SECONDS
        async with client.stream("GET", f"{base_url(host)}/events") as resp:
            if resp.status_code != 200:
                return []
            async for chunk in resp.aiter_text():
                chunks.append(chunk)
                if time.time() >= deadline:
                    break
    except Exception:  # noqa: BLE001
        return parse_events_stream("".join(chunks))
    finally:
        if own_client:
            await client.aclose()
    return parse_events_stream("".join(chunks))


async def poll_loop() -> None:
    """Background task: poll ESP devices every POLL_SECONDS while the source is
    configured, sit quietly otherwise. Cancellable; never crashes."""
    while True:
        try:
            if source_active():
                await poll_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.debug("ESPHome thermometer poll failed: %s", exc)
        await asyncio.sleep(POLL_SECONDS)
