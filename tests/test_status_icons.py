"""Status icons on kitchen displays (FoodAssistant-vsh2c).

Which items show for given Home Assistant states and thermometer readings,
how a saved item is cleaned, the /status-icons route (cached, never fails,
the main server's answer on a Bandit), the settings round trip, the entity
picker, the access rules, the Home Assistant read allowlist, and that the
display pages carry the script only while the feature is on.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

_SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(_SERVICE))

from app.config import settings  # noqa: E402
from app.services import access, gadgets, ha_allowlist, ha_client, status_icons  # noqa: E402

FRIDGE = {"id": "fridge1", "name": "Fridge door", "icon": "fridge", "source": "ha",
          "entity_id": "binary_sensor.fridge_door", "match": "on"}
OVEN = {"id": "oven1", "name": "Oven", "icon": "oven", "source": "ha",
        "entity_id": "climate.oven", "match": "state", "state": "Preheating, heating"}
PLUG = {"id": "plug1", "name": "Sous vide", "icon": "pot", "source": "ha",
        "entity_id": "sensor.sous_vide_power", "match": "above", "above": 10}
THERMO = {"id": "therm1", "name": "Thermometer", "icon": "thermometer",
          "source": "thermometer"}


def _state(value, unit=None):
    attrs = {"unit_of_measurement": unit} if unit else {}
    return {"state": value, "attributes": attrs}


# --------------------------------------------------------------------------
# Pure logic
# --------------------------------------------------------------------------

def test_normalize_keeps_a_good_item():
    item = status_icons.normalize_item(dict(FRIDGE, entity_id=" Binary_Sensor.Fridge_Door "))
    assert item == {"id": "fridge1", "name": "Fridge door", "icon": "fridge", "source": "ha",
                    "entity_id": "binary_sensor.fridge_door", "match": "on", "state": "",
                    "above": None, "device_id": ""}


@pytest.mark.parametrize("raw", [
    None, "x", {}, {"source": "weather"},
    {"source": "ha", "entity_id": "not an entity"},
    {"source": "ha", "entity_id": "sensor.x", "match": "above", "above": "lots"},
    {"source": "ha", "entity_id": "sensor.x", "match": "state", "state": " , "},
])
def test_normalize_drops_items_that_cannot_work(raw):
    assert status_icons.normalize_item(raw) is None


def test_normalize_fills_in_what_it_can():
    item = status_icons.normalize_item({"source": "ha", "entity_id": "switch.kettle",
                                        "icon": "rocket", "match": "sometimes", "id": "BAD ID!"})
    assert item["icon"] == "generic" and item["match"] == "on"
    assert item["name"] == "switch.kettle"
    assert status_icons._ITEM_ID_RE.fullmatch(item["id"])
    therm = status_icons.normalize_item({"source": "thermometer", "device_id": "aa:bb"})
    assert therm["icon"] == "thermometer" and therm["name"] == "Thermometer"
    assert therm["device_id"] == "AA:BB"
    oven = status_icons.normalize_item(OVEN)
    assert oven["state"] == "preheating, heating"


def test_normalize_items_dedupes_ids_and_caps_the_list():
    assert status_icons.normalize_items("nope") is None
    out = status_icons.normalize_items([FRIDGE, FRIDGE, {"source": "bad"}])
    assert len(out) == 2 and out[0]["id"] == "fridge1" and out[1]["id"] != "fridge1"
    many = status_icons.normalize_items([dict(THERMO, id=f"t{i}") for i in range(30)])
    assert len(many) == status_icons.MAX_ITEMS


@pytest.mark.parametrize("item,state,shown", [
    (FRIDGE, "on", True),
    (FRIDGE, "open", True),
    (FRIDGE, "off", False),
    (FRIDGE, "unavailable", False),
    (FRIDGE, "unknown", False),
    (dict(FRIDGE, match="off"), "off", True),
    (dict(FRIDGE, match="off"), "closed", True),
    (dict(FRIDGE, match="off"), "on", False),
    (dict(FRIDGE, match="off"), "unavailable", False),
    ({"match": "on"}, "heat", True),
    ({"match": "on"}, "idle", False),
    (status_icons.normalize_item(OVEN), "heating", True),
    (status_icons.normalize_item(OVEN), "Preheating", True),
    (status_icons.normalize_item(OVEN), "off", False),
    (PLUG, "1500.5", True),
    (PLUG, "10", False),
    (PLUG, "2", False),
    (PLUG, "unavailable", False),
    (PLUG, "nan", False),
])
def test_item_active(item, state, shown):
    assert status_icons.item_active(item, _state(state)) is shown
    assert status_icons.item_active(item, state) is shown


def test_evaluate_keeps_the_saved_order_and_details():
    items = status_icons.normalize_items([THERMO, PLUG, FRIDGE, OVEN])
    states = {"binary_sensor.fridge_door": _state("on"),
              "sensor.sous_vide_power": _state("812", "W"),
              "climate.oven": _state("off")}
    thermos = [{"id": "AA", "name": "Grill", "temp_c": 63.0}]
    shown = status_icons.evaluate(items, states, thermos, "f")
    assert [s["id"] for s in shown] == ["therm1", "plug1", "fridge1"]
    assert shown[0]["detail"] == "145°F"
    assert shown[1]["detail"] == "812 W"
    assert shown[2]["detail"] == "on"
    assert set(shown[0]) == {"id", "name", "icon", "detail"}


def test_evaluate_thermometers():
    items = status_icons.normalize_items([THERMO, dict(THERMO, id="t2", device_id="BB")])
    assert status_icons.evaluate(items, {}, [], "c") == []
    two = [{"id": "AA", "name": "", "temp_c": 50.0}, {"id": "BB", "name": "", "temp_c": None}]
    shown = status_icons.evaluate(items, {}, two, "c")
    assert shown[0]["detail"] == "2 thermometers"
    assert shown[1]["id"] == "t2" and shown[1]["detail"] == "connected"
    only_a = status_icons.evaluate(items, {}, two[:1], "c")
    assert [s["id"] for s in only_a] == ["therm1"] and only_a[0]["detail"] == "50°C"


def test_a_door_reads_open_and_closed():
    door = status_icons.normalize_item(FRIDGE)
    state = {"state": "on", "attributes": {"device_class": "door"}}
    assert status_icons.ha_detail(door, state) == "open"
    assert status_icons.ha_detail(dict(door, match="off"),
                                  {"state": "off", "attributes": {"device_class": "door"}}) == "closed"
    assert status_icons.ha_detail(door, {"state": "on", "attributes": {}}) == "on"
    assert status_icons.ha_detail(door, {"state": "fan_only"}) == "fan only"


def test_missing_state_hides_the_item():
    items = status_icons.normalize_items([FRIDGE])
    assert status_icons.evaluate(items, {}, [], "f") == []


def test_picker_entity():
    door = status_icons.picker_entity({"entity_id": "binary_sensor.fridge_door", "state": "off",
                                       "attributes": {"friendly_name": "Fridge door",
                                                      "device_class": "door"}})
    assert door["match"] == "on" and door["name"] == "Fridge door"
    power = status_icons.picker_entity({"entity_id": "sensor.plug_power", "state": "3",
                                        "attributes": {"unit_of_measurement": "W"}})
    assert power["match"] == "above" and power["unit"] == "W"
    assert status_icons.picker_entity({"entity_id": "sensor.kitchen_temp", "state": "21",
                                       "attributes": {"unit_of_measurement": "°C"}}) is None
    assert status_icons.picker_entity({"entity_id": "light.kitchen", "state": "on"}) is None
    assert status_icons.picker_entity("junk") is None


def test_live_thermometers(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "gadgets_enabled", True, raising=False)
    monkeypatch.setattr(settings, "gadget_devices",
                        [{"id": "AA:01", "name": "Grill"}, {"id": "AA:02", "name": "Oven probe"}],
                        raising=False)
    gadgets.reset()
    try:
        now = time.time()
        gadgets.ingest({"devices": [
            {"id": "AA:01", "probes": [{"index": 1, "temp_c": 60.0}]},
        ]}, now=now)
        found = gadgets.live_thermometers(now=now + 5)
        assert [t["id"] for t in found] == ["AA:01"]
        assert found[0]["temp_c"] == 60.0 and found[0]["name"] == "Grill"
        assert gadgets.live_thermometers(now=now + gadgets.STALE_SECONDS + 5) == []
        monkeypatch.setattr(settings, "gadgets_enabled", False, raising=False)
        assert gadgets.live_thermometers(now=now + 5) == []
    finally:
        gadgets.reset()


# --------------------------------------------------------------------------
# active_items: caching and never failing
# --------------------------------------------------------------------------

@pytest.fixture
def feature(monkeypatch):
    monkeypatch.setattr(settings, "status_icons_enabled", True, raising=False)
    monkeypatch.setattr(settings, "status_icons", [FRIDGE, PLUG], raising=False)
    monkeypatch.setattr(settings, "status_icons_position", "top_right", raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    status_icons.invalidate()
    yield
    status_icons.invalidate()


def test_active_items_reads_once_per_cache_window(feature, monkeypatch):
    calls = []

    async def fake_states(ids):
        calls.append(list(ids))
        return {"binary_sensor.fridge_door": _state("on"),
                "sensor.sous_vide_power": _state("0", "W")}

    monkeypatch.setattr(ha_client, "get_states", fake_states)
    first = asyncio.run(status_icons.active_items())
    second = asyncio.run(status_icons.active_items())
    assert first == second == {"enabled": True, "position": "top_right",
                               "items": [{"id": "fridge1", "name": "Fridge door",
                                          "icon": "fridge", "detail": "on"}]}
    assert calls == [["binary_sensor.fridge_door", "sensor.sous_vide_power"]]


def test_active_items_survives_home_assistant_failing(feature, monkeypatch):
    async def boom(ids):
        raise RuntimeError("down")

    monkeypatch.setattr(ha_client, "get_states", boom)
    assert asyncio.run(status_icons.active_items())["items"] == []


def test_active_items_off(feature, monkeypatch):
    monkeypatch.setattr(settings, "status_icons_enabled", False, raising=False)

    async def never(ids):
        raise AssertionError("Home Assistant must not be asked while the feature is off")

    monkeypatch.setattr(ha_client, "get_states", never)
    assert asyncio.run(status_icons.active_items()) == {
        "enabled": False, "position": "top_right", "items": []}


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app

    cwd = os.getcwd()
    os.chdir(_SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "auth_password", "")
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    status_icons.invalidate()
    try:
        yield TestClient(app)
    finally:
        status_icons.invalidate()
        os.chdir(cwd)


def _configured():
    return patch.object(type(settings), "is_configured", lambda self: True)


def test_route_lists_active_items(client, feature, monkeypatch):
    async def fake_states(ids):
        return {"binary_sensor.fridge_door": _state("open"),
                "sensor.sous_vide_power": _state("950", "W")}

    monkeypatch.setattr(ha_client, "get_states", fake_states)
    with _configured():
        r = client.get("/status-icons")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert [(i["id"], i["icon"], i["detail"]) for i in body["items"]] == [
        ("fridge1", "fridge", "open"), ("plug1", "pot", "950 W")]


def test_route_never_fails(client, feature, monkeypatch):
    async def broken():
        raise RuntimeError("bad")

    monkeypatch.setattr(status_icons, "active_items", broken)
    with _configured():
        r = client.get("/status-icons")
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_route_on_a_bandit_shows_the_servers_answer(client, feature, monkeypatch):
    import app.main as main_mod

    asked = []

    async def upstream(path):
        asked.append(path)
        return {"enabled": True, "position": "top_left",
                "items": [{"id": "fridge1", "name": "Fridge door", "icon": "fridge",
                           "detail": "open", "extra": "dropped"},
                          {"id": "x", "name": "Odd", "icon": "<script>", "detail": ""},
                          "junk"]}

    async def never(ids):
        raise AssertionError("a Bandit asks its server, not Home Assistant")

    monkeypatch.setattr(main_mod, "satellite_upstream_get", upstream)
    monkeypatch.setattr(ha_client, "get_states", never)
    monkeypatch.setattr(type(settings), "is_satellite", lambda self: True, raising=False)
    monkeypatch.setattr(type(settings), "upstream_linked", lambda self: True, raising=False)
    from app.routers import status_icons as route
    data = asyncio.run(route.get_status_icons())
    assert asked == ["/status-icons"]
    assert data == {"enabled": True, "position": "top_left", "items": [
        {"id": "fridge1", "name": "Fridge door", "icon": "fridge", "detail": "open"},
        {"id": "x", "name": "Odd", "icon": "generic", "detail": ""}]}

    async def silent(path):
        return None

    monkeypatch.setattr(main_mod, "satellite_upstream_get", silent)
    assert asyncio.run(route.get_status_icons())["items"] == []


def test_settings_round_trip(client, monkeypatch):
    monkeypatch.setattr(settings, "status_icons", [], raising=False)
    monkeypatch.setattr(settings, "status_icons_enabled", False, raising=False)
    with _configured():
        r = client.post("/setup/save", json={
            "status_icons_enabled": True,
            "status_icons_position": "middle",
            "status_icons": [FRIDGE, {"source": "ha", "entity_id": "nope"}, PLUG],
        })
    assert r.status_code == 200
    assert settings.status_icons_enabled is True
    assert settings.status_icons_position == "top_right"
    assert [i["id"] for i in settings.status_icons] == ["fridge1", "plug1"]
    assert settings.status_icons[1]["above"] == 10.0
    # A save from another section leaves the items alone.
    with _configured():
        client.post("/setup/save", json={"quiet_mode": True})
    assert [i["id"] for i in settings.status_icons] == ["fridge1", "plug1"]
    # Not a list keeps what is stored; an empty list clears it.
    with _configured():
        client.post("/setup/save", json={"status_icons": "x"})
    assert len(settings.status_icons) == 2
    with _configured():
        client.post("/setup/save", json={"status_icons": []})
    assert settings.status_icons == []


def test_entity_picker(client, monkeypatch):
    monkeypatch.setattr(ha_client, "mode", lambda: "direct")

    async def rows(*a, **k):
        return [{"entity_id": "sensor.plug_power", "state": "3",
                 "attributes": {"unit_of_measurement": "W", "friendly_name": "Plug"}},
                {"entity_id": "binary_sensor.fridge_door", "state": "off",
                 "attributes": {"friendly_name": "Fridge door"}},
                {"entity_id": "light.kitchen", "state": "on", "attributes": {}}]

    monkeypatch.setattr(ha_client, "list_states", rows)
    with _configured():
        body = client.get("/setup/status-icons/entities").json()
    assert body["ok"] and body["configured"]
    assert [e["entity_id"] for e in body["entities"]] == [
        "binary_sensor.fridge_door", "sensor.plug_power"]
    monkeypatch.setattr(ha_client, "mode", lambda: "none")
    with _configured():
        assert client.get("/setup/status-icons/entities").json()["entities"] == []


# --------------------------------------------------------------------------
# Access, allowlist and pages
# --------------------------------------------------------------------------

def test_access_rules():
    assert access.route_family("GET", "/status-icons") == "kitchen"
    assert access.route_family("GET", "/setup/status-icons/entities") == "admin"
    for scope in ("screen", "screen_app", "viewer", "satellite", "kiosk", "deck"):
        assert access.allows_raw(scope, "GET", "/status-icons", kiosk_is_admin=False), scope
    for scope in ("screen", "viewer", "satellite", "kiosk", "reader", "display"):
        assert not access.allows_raw(scope, "GET", "/setup/status-icons/entities",
                                     kiosk_is_admin=False), scope
    assert not access.allows_raw("reader", "GET", "/status-icons", kiosk_is_admin=False)


def test_status_entities_are_readable(monkeypatch):
    monkeypatch.setattr(settings, "status_icons",
                        [FRIDGE, THERMO, dict(PLUG, entity_id="Sensor.Sous_Vide_Power")],
                        raising=False)
    found = ha_allowlist.status_entities(settings)
    assert found == {"binary_sensor.fridge_door", "sensor.sous_vide_power"}
    assert found <= ha_allowlist.readable_entities(settings, [])


def test_pages_carry_the_script_only_while_on(client, monkeypatch):
    monkeypatch.setattr(settings, "status_icons_enabled", False, raising=False)
    with _configured():
        off = client.get("/ui/start").text
    assert "status-icons.js" not in off
    monkeypatch.setattr(settings, "status_icons_enabled", True, raising=False)
    monkeypatch.setattr(settings, "status_icons_position", "top_left", raising=False)
    with _configured():
        start = client.get("/ui/start").text
        inventory = client.get("/ui/inventory").text
    assert 'id="status-icons-config" data-position="top_left" data-gap="52"' in start
    assert "static/js/status-icons.js" in start
    assert 'id="status-icons-config" data-position="top_left"' in inventory
