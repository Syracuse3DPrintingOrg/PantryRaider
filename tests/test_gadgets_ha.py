"""Home Assistant thermometer source (FoodAssistant-mnks).

Covers the pure entity parsing (unit conversion, unavailable states, battery,
stale last_updated), the poll gating, one mocked poll pass end to end into the
gadgets state, and the picker filter. No network or Home Assistant needed.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services import gadgets, gadgets_ha  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    gadgets.reset()
    yield
    gadgets.reset()


NOW = 1_700_000_000.0


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


# -- entity_reading -----------------------------------------------------------

def test_entity_reading_celsius_passthrough():
    reading = gadgets_ha.entity_reading("sensor.grill", {
        "state": "74.5",
        "attributes": {"unit_of_measurement": "°C", "friendly_name": "Grill"},
        "last_updated": _iso(NOW - 5),
    }, NOW)
    assert reading == {
        "id": "HA:SENSOR.GRILL", "name": "Grill",
        "protocol": "home_assistant",
        "probes": [{"index": 1, "temp_c": 74.5}], "battery": None,
    }


def test_entity_reading_converts_fahrenheit():
    reading = gadgets_ha.entity_reading("sensor.grill", {
        "state": "212", "attributes": {"unit_of_measurement": "°F"},
        "last_updated": _iso(NOW),
    }, NOW)
    assert reading["probes"][0]["temp_c"] == 100.0


def test_entity_reading_battery_attribute_clamped():
    reading = gadgets_ha.entity_reading("sensor.grill", {
        "state": "20", "attributes": {"battery_level": 150},
    }, NOW)
    assert reading["battery"] == 100
    reading = gadgets_ha.entity_reading("sensor.grill", {
        "state": "20", "attributes": {"battery_level": "62"},
    }, NOW)
    assert reading["battery"] == 62


def test_entity_reading_skips_unavailable_and_junk():
    for state in ("unavailable", "unknown", "", None, "not-a-number"):
        assert gadgets_ha.entity_reading(
            "sensor.grill", {"state": state}, NOW) is None
    assert gadgets_ha.entity_reading("sensor.grill", "junk", NOW) is None


def test_entity_reading_skips_stale_last_updated():
    body = {"state": "50", "last_updated": _iso(NOW - 3600)}
    assert gadgets_ha.entity_reading("sensor.grill", body, NOW) is None
    # A missing or unparseable timestamp is not treated as stale.
    assert gadgets_ha.entity_reading("sensor.grill", {"state": "50"}, NOW)


def test_valid_entity_id():
    assert gadgets_ha.valid_entity_id("sensor.grill_probe_1")
    assert not gadgets_ha.valid_entity_id("grill")
    assert not gadgets_ha.valid_entity_id("sensor.grill probe")
    assert not gadgets_ha.valid_entity_id("")
    assert not gadgets_ha.valid_entity_id("sensor.grill.probe")


def test_is_temperature_entity_filter():
    assert gadgets_ha.is_temperature_entity({
        "entity_id": "sensor.probe",
        "attributes": {"device_class": "temperature"}})
    assert gadgets_ha.is_temperature_entity({
        "entity_id": "sensor.probe",
        "attributes": {"unit_of_measurement": "°F"}})
    assert not gadgets_ha.is_temperature_entity({
        "entity_id": "binary_sensor.door",
        "attributes": {"device_class": "temperature"}})
    assert not gadgets_ha.is_temperature_entity({
        "entity_id": "sensor.humidity",
        "attributes": {"unit_of_measurement": "%"}})


# -- gating and polling -------------------------------------------------------

def _configure(monkeypatch, tmp_path, entities):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "gadget_ha_enabled", True, raising=False)
    monkeypatch.setattr(settings, "gadget_ha_entities", entities, raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_base_url",
                        "http://ha.local:8123", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "tok", raising=False)
    monkeypatch.setattr(settings, "gadgets_enabled", True, raising=False)
    monkeypatch.setattr(settings, "gadget_devices", [
        {"id": gadgets_ha.device_id_for(e), "name": e,
         "protocol": "home_assistant", "targets": {}} for e in entities
    ], raising=False)


def test_source_active_gating(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, ["sensor.grill"])
    assert gadgets_ha.source_active()
    monkeypatch.setattr(settings, "gadget_ha_enabled", False, raising=False)
    assert not gadgets_ha.source_active()
    monkeypatch.setattr(settings, "gadget_ha_enabled", True, raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "", raising=False)
    assert not gadgets_ha.source_active()
    monkeypatch.setattr(settings, "streamdeck_ha_token", "tok", raising=False)
    monkeypatch.setattr(settings, "gadget_ha_entities", [], raising=False)
    assert not gadgets_ha.source_active()


def test_configured_entities_sanitizes(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "gadget_ha_entities",
                        ["sensor.grill", "Sensor.Grill", "junk", "", None],
                        raising=False)
    assert gadgets_ha.configured_entities() == ["sensor.grill"]


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _FakeClient:
    """A stand-in for httpx.AsyncClient: entity url -> canned response."""

    def __init__(self, responses):
        self.responses = responses
        self.requested = []

    async def get(self, url, headers=None):
        self.requested.append((url, headers))
        for suffix, resp in self.responses.items():
            if url.endswith(suffix):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return _FakeResponse(404, {})


def test_poll_once_ingests_readings_without_reader_heartbeat(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, ["sensor.grill", "sensor.oven"])
    client = _FakeClient({
        "/api/states/sensor.grill": _FakeResponse(200, {
            "state": "150.0",
            "attributes": {"unit_of_measurement": "°F",
                           "friendly_name": "Grill", "battery_level": 80},
            "last_updated": _iso(time.time()),
        }),
        # One dead entity must not starve the rest.
        "/api/states/sensor.oven": ConnectionError("down"),
    })
    count = asyncio.run(gadgets_ha.poll_once(client=client))
    assert count == 1
    # The bearer token went out on the request.
    assert client.requested[0][1] == {"Authorization": "Bearer tok"}

    state = gadgets.get_state()
    dev = {d["id"]: d for d in state["devices"]}["HA:SENSOR.GRILL"]
    assert dev["probes"][0]["temp_c"] == pytest.approx(65.56, abs=0.01)
    assert dev["battery"] == 80
    assert dev["stale"] is False
    # HA readings are not the Bluetooth reader: no reader heartbeat.
    assert state["reader_age_seconds"] is None


def test_poll_once_noop_when_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "", raising=False)
    monkeypatch.setattr(settings, "gadget_ha_entities", [], raising=False)
    assert asyncio.run(gadgets_ha.poll_once(client=_FakeClient({}))) == 0


def test_target_alert_fires_on_ha_reading(monkeypatch, tmp_path):
    """Alerts evaluate on ingest, so an HA-sourced reading crossing its target
    fires the same toast as a reader-sourced one."""
    from app.services import ha_events
    ha_events.reset()
    _configure(monkeypatch, tmp_path, ["sensor.grill"])
    monkeypatch.setattr(settings, "gadget_devices", [
        {"id": "HA:SENSOR.GRILL", "name": "Grill",
         "protocol": "home_assistant",
         "targets": {"1": {"temp_c": 60.0, "direction": "above"}}},
    ], raising=False)
    client = _FakeClient({
        "/api/states/sensor.grill": _FakeResponse(200, {
            "state": "74", "attributes": {"unit_of_measurement": "°C"},
            "last_updated": _iso(time.time()),
        }),
    })
    asyncio.run(gadgets_ha.poll_once(client=client))
    events = ha_events.poll(0)["events"]
    assert any("target" in e.get("message", "")
               and e.get("title") == "Grill" for e in events)
    ha_events.reset()


def test_list_temperature_entities_filters_and_sorts(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, ["sensor.grill"])
    client = _FakeClient({
        "/api/states": _FakeResponse(200, [
            {"entity_id": "sensor.zeta_probe", "state": "50",
             "attributes": {"device_class": "temperature",
                            "friendly_name": "Zeta"}},
            {"entity_id": "sensor.alpha_probe", "state": "60",
             "attributes": {"unit_of_measurement": "°F",
                            "friendly_name": "Alpha"}},
            {"entity_id": "sensor.humidity", "state": "40",
             "attributes": {"unit_of_measurement": "%"}},
            {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
        ]),
    })
    rows = asyncio.run(gadgets_ha.list_temperature_entities(client=client))
    assert [r["entity_id"] for r in rows] == [
        "sensor.alpha_probe", "sensor.zeta_probe"]
    assert rows[0]["unit"] == "°F"


def test_list_temperature_entities_empty_when_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "", raising=False)
    assert asyncio.run(gadgets_ha.list_temperature_entities(
        client=_FakeClient({}))) == []
