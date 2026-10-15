"""Thermometers do not open other Home Assistant states to the fleet hop.

GET /api/proxy/ha/api/states/<entity> answers for the entities the owner's
keys, cameras, thermometers and hygrometers use. Anything that can open a
kitchen page (a viewer, a Bandit's key, the Stream Deck) can add a
thermometer, so the thermometer list alone must not let a key read, say, a
person's location or a lock: only sensors count, and their state is passed on
only when it is a temperature or humidity reading.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.routers import proxy
from app.services import gadgets_ha, ha_allowlist

SERVICE = Path(__file__).resolve().parents[1] / "service"

STATES = {
    "sensor.grill_probe": {"entity_id": "sensor.grill_probe", "state": "71.5",
                           "attributes": {"device_class": "temperature",
                                          "unit_of_measurement": "°C"}},
    "sensor.fridge_rh": {"entity_id": "sensor.fridge_rh", "state": "40",
                         "attributes": {"device_class": "humidity", "unit_of_measurement": "%"}},
    "sensor.phone_geocoded_location": {"entity_id": "sensor.phone_geocoded_location",
                                       "state": "12 Main St", "attributes": {}},
    "person.owner": {"entity_id": "person.owner", "state": "home",
                     "attributes": {"latitude": 40.0, "longitude": -76.0}},
    "lock.front_door": {"entity_id": "lock.front_door", "state": "locked", "attributes": {}},
}


def test_only_sensors_count_from_the_thermometer_lists(monkeypatch):
    monkeypatch.setattr(gadgets_ha, "configured_hygro_pairs", lambda: [
        {"temperature": "sensor.fridge_t", "humidity": "lock.front_door"}])
    monkeypatch.setattr(settings, "gadget_ha_entities",
                        ["sensor.grill_probe", "person.owner", "device_tracker.phone"],
                        raising=False)
    monkeypatch.setattr(settings, "streamdeck_key_overrides", [], raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_slots", [], raising=False)
    monkeypatch.setattr(settings, "streamdeck_cameras", [], raising=False)
    assert ha_allowlist.sensor_entities(settings) == {"sensor.grill_probe", "sensor.fridge_t"}
    readable = ha_allowlist.readable_entities(settings, [])
    assert "person.owner" not in readable and "lock.front_door" not in readable


@pytest.fixture
def hop(monkeypatch, tmp_path):
    from app.services import ha_client
    monkeypatch.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "http://ha.test:8123", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "ha-token", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_slots", [], raising=False)
    monkeypatch.setattr(settings, "streamdeck_cameras", [], raising=False)
    monkeypatch.setattr(settings, "streamdeck_key_overrides", [
        {"id": "k", "type": "ha_action", "entity_id": "sensor.phone_geocoded_location",
         "service": "homeassistant.update_entity"}], raising=False)
    # Whoever adds thermometers can name anything here.
    monkeypatch.setattr(settings, "gadget_ha_entities",
                        ["sensor.grill_probe", "sensor.fridge_rh", "person.owner",
                         "sensor.phone_geocoded_location", "lock.front_door"], raising=False)
    monkeypatch.setattr(gadgets_ha, "configured_hygro_pairs", lambda: [])
    monkeypatch.setattr(proxy, "_ha_profiles", lambda: [])

    async def _get_state(entity_id):
        return STATES.get(entity_id)

    monkeypatch.setattr(ha_client, "mode", lambda: "direct")
    monkeypatch.setattr(ha_client, "get_state", _get_state)
    from app.services import credentials
    return {"X-API-Key": credentials.mint("satellite", "Bandit", "manual")[1]}


def _read(headers, entity):
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app).get(f"/api/proxy/ha/api/states/{entity}", headers=headers)


def test_thermometer_and_hygrometer_readings_pass(hop):
    assert _read(hop, "sensor.grill_probe").json()["state"] == "71.5"
    assert _read(hop, "sensor.fridge_rh").json()["state"] == "40"


def test_other_states_named_as_thermometers_do_not(hop):
    for entity in ("person.owner", "lock.front_door"):
        r = _read(hop, entity)
        assert r.status_code == 403 and "latitude" not in r.text
    # A sensor that is not a temperature or humidity reading.
    r = _read(hop, "sensor.phone_geocoded_location")
    assert r.status_code == 200      # a key of the owner's uses it, so it is set up


def test_a_non_reading_sensor_listed_only_as_a_thermometer_is_refused(hop, monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_key_overrides", [], raising=False)
    r = _read(hop, "sensor.phone_geocoded_location")
    assert r.status_code == 403 and "Main St" not in r.text
