"""Home Assistant entity discovery for custom action keys (FoodAssistant-yzjr).

Covers the pure filtering (actionable domains only), the domain grouping, the
domain-to-default-service mapping, the mocked fetch, the no-token degrade, and
the retirement of the fixed ha_1..ha_5 slot keys: legacy-flagged in the deck
catalog (hidden from the palettes) but still registered and resolvable so a
saved layout keeps rendering and firing. No network or Home Assistant needed.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services import ha_actions  # noqa: E402


# -- default_service (domain -> service) --------------------------------------

def test_default_service_toggles_stateful_domains():
    for entity in ("light.kitchen", "switch.fan_plug", "fan.bedroom",
                   "input_boolean.guest_mode", "media_player.living_room",
                   "climate.thermostat"):
        assert ha_actions.default_service(entity) == "homeassistant.toggle"


def test_default_service_runs_one_shot_domains():
    assert ha_actions.default_service("scene.movie_night") == "scene.turn_on"
    assert ha_actions.default_service("script.goodnight") == "script.turn_on"
    assert ha_actions.default_service("button.doorbell") == "button.press"
    assert ha_actions.default_service("automation.lights") == "automation.trigger"
    assert ha_actions.default_service("cover.garage") == "cover.toggle"
    assert ha_actions.default_service("lock.front_door") == "lock.lock"
    assert ha_actions.default_service("vacuum.robo") == "vacuum.start"


def test_default_service_falls_back_to_toggle():
    # Matches resolve_ha_call's own no-service default in start_actions.
    assert ha_actions.default_service("weird.thing") == "homeassistant.toggle"
    assert ha_actions.default_service("") == "homeassistant.toggle"


def test_every_actionable_domain_has_a_default_and_common_services():
    for domain in ha_actions.ACTIONABLE_DOMAINS:
        default = ha_actions.DEFAULT_SERVICE_BY_DOMAIN[domain]
        common = ha_actions.COMMON_SERVICES_BY_DOMAIN[domain]
        assert common and common[0] == default


# -- actionable_entity (row filter) --------------------------------------------

def test_actionable_entity_keeps_actionable_domains():
    row = {"entity_id": "light.kitchen", "state": "on",
           "attributes": {"friendly_name": "Kitchen Light"}}
    assert ha_actions.actionable_entity(row) == {
        "entity_id": "light.kitchen", "name": "Kitchen Light",
        "domain": "light", "state": "on",
        "default_service": "homeassistant.toggle",
    }


def test_actionable_entity_drops_sensors_and_junk():
    assert ha_actions.actionable_entity(
        {"entity_id": "sensor.temp", "state": "72"}) is None
    assert ha_actions.actionable_entity(
        {"entity_id": "binary_sensor.door", "state": "off"}) is None
    assert ha_actions.actionable_entity(
        {"entity_id": "weather.home", "state": "sunny"}) is None
    assert ha_actions.actionable_entity({"entity_id": "nodot"}) is None
    assert ha_actions.actionable_entity("not a dict") is None
    assert ha_actions.actionable_entity({}) is None


def test_actionable_entity_falls_back_to_id_for_name():
    row = {"entity_id": "switch.plug_3", "state": None, "attributes": {}}
    entry = ha_actions.actionable_entity(row)
    assert entry["name"] == "switch.plug_3"
    assert entry["state"] == ""


# -- group_by_domain ------------------------------------------------------------

def test_group_by_domain_orders_and_sorts():
    entities = [
        {"entity_id": "scene.b", "name": "Zeta Scene", "domain": "scene"},
        {"entity_id": "light.b", "name": "Zeta Light", "domain": "light"},
        {"entity_id": "light.a", "name": "alpha light", "domain": "light"},
    ]
    groups = ha_actions.group_by_domain(entities)
    # Domains come out in ACTIONABLE_DOMAINS order (light before scene) and
    # each group's entities sort by name, case-insensitively.
    assert [g["domain"] for g in groups] == ["light", "scene"]
    assert [e["name"] for e in groups[0]["entities"]] == ["alpha light", "Zeta Light"]
    # Each group carries the picker's common services, default first.
    assert groups[0]["services"][0] == "homeassistant.toggle"
    assert groups[1]["services"] == ["scene.turn_on"]


def test_group_by_domain_empty_and_malformed():
    assert ha_actions.group_by_domain([]) == []
    assert ha_actions.group_by_domain([{"no": "domain"}, "junk"]) == []


# -- list_actionable_entities (mocked HTTP) --------------------------------------

class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _FakeClient:
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


def _configure(monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_ha_base_url",
                        "http://ha.local:8123", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "tok", raising=False)


def test_list_actionable_entities_filters_states(monkeypatch):
    _configure(monkeypatch)
    client = _FakeClient({
        "/api/states": _FakeResponse(200, [
            {"entity_id": "light.kitchen", "state": "on",
             "attributes": {"friendly_name": "Kitchen Light"}},
            {"entity_id": "sensor.temp", "state": "72", "attributes": {}},
            {"entity_id": "script.goodnight", "state": "off",
             "attributes": {"friendly_name": "Goodnight"}},
        ]),
    })
    rows = asyncio.run(ha_actions.list_actionable_entities(client=client))
    assert [r["entity_id"] for r in rows] == ["light.kitchen", "script.goodnight"]
    assert rows[1]["default_service"] == "script.turn_on"
    # The bearer token went out on the request.
    assert client.requested[0][1] == {"Authorization": "Bearer tok"}


def test_list_actionable_entities_empty_when_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "", raising=False)
    assert asyncio.run(ha_actions.list_actionable_entities(
        client=_FakeClient({}))) == []


def test_list_actionable_entities_degrades_on_errors(monkeypatch):
    _configure(monkeypatch)
    assert asyncio.run(ha_actions.list_actionable_entities(
        client=_FakeClient({"/api/states": ConnectionError("down")}))) == []
    assert asyncio.run(ha_actions.list_actionable_entities(
        client=_FakeClient({"/api/states": _FakeResponse(401, {})}))) == []


# -- the /setup/ha/entities endpoint ---------------------------------------------

def _setup_client(monkeypatch):
    """A TestClient that reaches /setup/* directly: auth off and the app
    treated as configured, so the setup-redirect middleware stays out of the
    way (it would otherwise render the wizard)."""
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True)
    return TestClient(app)


def test_endpoint_hints_when_ha_not_connected(monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "", raising=False)
    r = _setup_client(monkeypatch).get("/setup/ha/entities")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["configured"] is False
    assert body["groups"] == []
    assert "Home Assistant" in body["hint"]


def test_endpoint_groups_entities(monkeypatch):
    client = _setup_client(monkeypatch)
    _configure(monkeypatch)

    async def fake_list(http_client=None):
        return [
            {"entity_id": "light.a", "name": "A", "domain": "light",
             "state": "on", "default_service": "homeassistant.toggle"},
        ]

    monkeypatch.setattr(ha_actions, "list_actionable_entities", fake_list)
    body = client.get("/setup/ha/entities").json()
    assert body["configured"] is True
    assert body["groups"][0]["domain"] == "light"
    assert body["groups"][0]["entities"][0]["entity_id"] == "light.a"


# -- ha_1..ha_5 retirement compat --------------------------------------------------

def test_ha_slot_keys_are_legacy_in_catalog_but_still_registered():
    """The fixed slot keys are hidden from the palettes (legacy flag) yet stay
    registered and in the catalog, so a deployed deck.toml or Start Page layout
    that references ha_1 keeps its face and its fire path."""
    from foodassistant_streamdeck import actions as deck_actions

    cat = {a["name"]: a for a in deck_actions.catalog()}
    for name in ("ha_1", "ha_2", "ha_3", "ha_4", "ha_5"):
        assert cat[name].get("legacy") is True
        assert deck_actions.resolve(name) is not None
    # Nothing else got flagged.
    assert [n for n, a in cat.items() if a.get("legacy")] == [
        "ha_1", "ha_2", "ha_3", "ha_4", "ha_5"]


def test_start_page_still_renders_a_saved_ha_slot_key():
    from app.services import start_page

    keys = start_page.resolve_layout(["ha_1"], 6, overrides=[],
                                     catalog=start_page.bundled_catalog())
    # ha_1 has no on-screen page of its own but fires server-side; it must not
    # collapse to a blank key just because it left the palette.
    assert keys[0]["kind"] in ("builtin", "deckonly")
    assert keys[0]["key"] == "ha_1"
    assert keys[0]["label"] == "HA 1"
