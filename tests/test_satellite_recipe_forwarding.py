"""Satellite forwarding of the native recipe library (FoodAssistant-g0fd).

On the native recipe backend, recipes and the meal plan live in the MAIN
SERVER's database, so a satellite (pi_remote) forwards every /mealie and
/recipes call upstream, the same way pending scans are forwarded. On the
Mealie backend the forward must NOT engage: those installs keep the existing
path where the local MealieClient reaches the server's Mealie through the
/api/proxy hop.

Covers the pure decision helper and the middleware end to end (the upstream
httpx client is swapped for a fake; no network).
"""
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SERVICE_DIR = Path(__file__).parent.parent / "service"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    cwd = os.getcwd()
    os.chdir(_SERVICE_DIR)
    try:
        from app.config import settings

        data_dir = tmp_path_factory.mktemp("data")
        settings.data_dir = str(data_dir)

        from app.main import app

        settings.auth_required = False
        settings.auth_password = ""
        with TestClient(app) as c:
            yield c
    finally:
        os.chdir(cwd)


@pytest.fixture()
def satellite(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    monkeypatch.setattr(settings, "remote_server_url", "http://server.test")
    monkeypatch.setattr(settings, "upstream_api_key", "up-key")
    monkeypatch.setattr(settings, "recipes_backend", "native")
    monkeypatch.setattr(settings, "mealie_base_url", "")
    monkeypatch.setattr(settings, "mealie_api_key", "")
    yield settings


class FakeUpstream:
    """Stands in for the forwarding httpx client; records what was sent."""

    def __init__(self):
        self.calls = []

    async def request(self, method, url, headers=None, params=None, content=None):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "params": params, "content": content})
        import httpx
        return httpx.Response(200, json={"forwarded": True},
                              headers={"content-type": "application/json"})


def test_decision_helper(satellite):
    from app import main

    assert main._satellite_recipe_upstream("/mealie/recipes") == "http://server.test"
    assert main._satellite_recipe_upstream("/mealie/mealplan/summary") == "http://server.test"
    assert main._satellite_recipe_upstream("/recipes/images/3") == "http://server.test"
    # Unrelated paths stay local.
    assert main._satellite_recipe_upstream("/ui/recipes") is None
    assert main._satellite_recipe_upstream("/pending/scan") is None

    # Mealie backend: the existing MealieClient proxy path stays in charge.
    satellite.recipes_backend = "mealie"
    assert main._satellite_recipe_upstream("/mealie/recipes") is None

    # Not a satellite: nothing forwards.
    satellite.recipes_backend = "native"
    satellite.deployment_mode = "server"
    assert main._satellite_recipe_upstream("/mealie/recipes") is None


def test_gadgets_forward_decision(satellite, monkeypatch):
    """With the gadget relay on (the default, FoodAssistant-me3t) the reader's
    own endpoints are served locally, so the satellite ingests every push and
    forwards a tagged copy through the retry queue; everything else under
    /gadgets still proxies so the server stays the fleet hub. Opting out of
    the relay restores the full proxy."""
    from app import main

    assert main._satellite_recipe_upstream("/gadgets/readings") is None
    assert main._satellite_recipe_upstream("/gadgets/config") is None
    # The state poll is answered locally since t0vp5 (the route fetches the
    # server's snapshot itself and overlays the local accessories).
    assert main._satellite_recipe_upstream("/gadgets/state") is None
    assert main._satellite_recipe_upstream("/gadgets/devices") == "http://server.test"
    # Relay opted out: the old full proxy comes back.
    monkeypatch.setattr(satellite, "relay_gadgets_upstream", False, raising=False)
    assert main._satellite_recipe_upstream("/gadgets/readings") == "http://server.test"
    assert main._satellite_recipe_upstream("/gadgets/config") == "http://server.test"
    # Even on the Mealie recipe backend, gadgets still forward.
    satellite.recipes_backend = "mealie"
    assert main._satellite_recipe_upstream("/gadgets/devices") == "http://server.test"
    # But installing the reader is a local host action on the satellite.
    assert main._satellite_recipe_upstream("/gadgets/install") is None
    # Not a satellite: gadgets stay local (the server reads its own radio).
    satellite.deployment_mode = "server"
    assert main._satellite_recipe_upstream("/gadgets/readings") is None


def test_middleware_serves_readings_locally_and_queues_relay(client, satellite,
                                                             monkeypatch):
    """A satellite's reader push is ingested by the satellite itself, and a
    tagged copy is queued for the server (FoodAssistant-me3t) instead of the
    request being proxied raw."""
    from app import main
    from app.services import gadgets_relay

    fake = FakeUpstream()
    monkeypatch.setattr(main, "_satellite_fwd_client", fake)
    monkeypatch.setattr(gadgets_relay, "_ensure_worker", lambda: None)
    gadgets_relay.reset()
    try:
        r = client.post("/gadgets/readings", json={"devices": [
            {"id": "AA:11", "kind": "hygrometer", "temp_c": 4.0}]})
        assert r.status_code == 200 and r.json().get("ok") is True
        assert fake.calls == []  # answered locally, not proxied
        assert gadgets_relay.pending() == 1
        assert gadgets_relay._queue[0].get("source")
    finally:
        gadgets_relay.reset()


def test_middleware_forwards_with_upstream_key(client, satellite, monkeypatch):
    from app import main

    fake = FakeUpstream()
    monkeypatch.setattr(main, "_satellite_fwd_client", fake)

    r = client.get("/mealie/recipes", params={"search": "stew"})
    assert r.status_code == 200 and r.json() == {"forwarded": True}
    (call,) = fake.calls
    assert call["url"] == "http://server.test/mealie/recipes"
    assert call["headers"]["X-API-Key"] == "up-key"
    assert call["params"] == {"search": "stew"}


def test_middleware_forwards_writes_with_body(client, satellite, monkeypatch):
    from app import main

    fake = FakeUpstream()
    monkeypatch.setattr(main, "_satellite_fwd_client", fake)

    r = client.post("/mealie/mealplan",
                    json={"date": "2026-07-09", "title": "Leftovers"})
    assert r.status_code == 200 and r.json() == {"forwarded": True}
    (call,) = fake.calls
    assert call["method"] == "POST"
    assert b"Leftovers" in call["content"]
    assert call["headers"]["Content-Type"].startswith("application/json")


def test_unreachable_server_answers_502(client, satellite, monkeypatch):
    from app import main

    class Dead:
        async def request(self, *a, **k):
            raise RuntimeError("down")

    monkeypatch.setattr(main, "_satellite_fwd_client", Dead())
    r = client.get("/mealie/recipes")
    assert r.status_code == 502
    assert "main server" in r.json()["detail"].lower()


def test_print_actions_forward_but_local_bits_stay(satellite):
    """Printers are system-level: a satellite relays print jobs to the server's
    one printer, but the Bluetooth bridge setup and discovery stay local, since
    the satellite is the device physically hosting the printer (eml9)."""
    from app import main
    # Print jobs go to the server.
    assert main._satellite_recipe_upstream("/printing/label") == "http://server.test"
    assert main._satellite_recipe_upstream("/printing/label/batch") == "http://server.test"
    assert main._satellite_recipe_upstream("/printing/decorative") == "http://server.test"
    assert main._satellite_recipe_upstream("/printing/document") == "http://server.test"
    # Local device bits stay local: the satellite hosts the bridge and renders
    # its own previews.
    assert main._satellite_recipe_upstream("/printing/bluetooth/setup") is None
    assert main._satellite_recipe_upstream("/printing/bluetooth/status") is None
    assert main._satellite_recipe_upstream("/printing/label/preview") is None
    assert main._satellite_recipe_upstream("/printing/discover") is None


# -- Plug-in accessories stay on the device that holds them (t0vp5) -----------

def test_accessory_registry_and_state_poll_stay_local(satellite, monkeypatch):
    """The STEMMA registry is device-local by design (services/stemma.py): a
    board is plugged into ONE device, so every registry call and the state
    poll are answered by the satellite itself. The Bluetooth classes are
    fleet state and keep forwarding, whatever the relay setting."""
    from app import main

    local = ("/gadgets/stemma", "/gadgets/stemma/edit", "/gadgets/stemma/test",
             "/gadgets/stemma/i2c:1:0x30", "/gadgets/state")
    forwarded = ("/gadgets/devices", "/gadgets/hygrometers", "/gadgets/buttons",
                 "/gadgets/outputs", "/gadgets/target", "/gadgets/stemmax")
    for relay in (True, False):
        monkeypatch.setattr(satellite, "relay_gadgets_upstream", relay, raising=False)
        for path in local:
            assert main._satellite_recipe_upstream(path) is None, (path, relay)
        for path in forwarded:
            assert main._satellite_recipe_upstream(path) == "http://server.test", (path, relay)
    # Off a satellite nothing forwards, as before.
    satellite.deployment_mode = "server"
    for path in local + forwarded:
        assert main._satellite_recipe_upstream(path) is None


def test_stemma_add_on_a_satellite_writes_the_local_registry(client, satellite,
                                                             monkeypatch):
    """Regression for t0vp5: adding the pad from a satellite's Accessories
    pane used to land in the SERVER's registry (where there is no bus), while
    the satellite's own agent pulled an empty list and drove nothing."""
    from app import main

    fake = FakeUpstream()
    monkeypatch.setattr(main, "_satellite_fwd_client", fake)
    monkeypatch.setattr(satellite, "stemma_devices", [], raising=False)
    monkeypatch.setattr(satellite, "stemma_enabled", False, raising=False)

    r = client.post("/gadgets/stemma",
                    json={"id": "i2c:1:0x30", "kind": "neokey", "name": "Pad"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert fake.calls == []  # never left this device
    assert [d["id"] for d in satellite.stemma_devices] == ["i2c:1:0x30"]
    assert satellite.stemma_enabled is True
    # The local agent's config pull now carries the pad.
    cfg = client.get("/gadgets/config").json()
    assert [d["id"] for d in cfg["stemma"]["devices"]] == ["i2c:1:0x30"]
    # Edit, key test, and delete stay local too.
    r = client.post("/gadgets/stemma/edit",
                    json={"device_id": "i2c:1:0x30", "brightness": 55})
    assert r.json()["ok"] is True
    assert satellite.stemma_devices[0]["options"]["brightness"] == 55
    assert client.post("/gadgets/stemma/test",
                       json={"device_id": "i2c:1:0x30", "key": 1}).json()["ok"] is True
    assert client.delete("/gadgets/stemma/i2c:1:0x30").json()["ok"] is True
    assert satellite.stemma_devices == []
    assert fake.calls == []


def test_overlay_local_accessories_is_pure_and_keeps_the_fleet_sections():
    from app.routers.gadgets import overlay_local_accessories

    upstream = {"devices": [{"id": "SRV"}], "hygrometers": [{"id": "H"}],
                "buttons": [{"id": "B"}], "relay_source": "bandit-1",
                "stemma": [{"id": "i2c:1:0x30", "name": "phantom"}],
                "stemma_discovered": [], "stemma_choices": [],
                "i2c_available": False, "i2c_detail": "no bus on the server",
                "reader_age_seconds": None}
    local = {"devices": [], "stemma": [{"id": "i2c:1:0x30", "name": "Pad"}],
             "stemma_discovered": [{"id": "i2c:1:0x38"}],
             "stemma_choices": [{"value": ""}], "stemma_enabled": True,
             "i2c_available": True, "i2c_detail": "", "reader_age_seconds": 2.0}
    merged = overlay_local_accessories(upstream, local)
    # Fleet sections are the server's, untouched.
    assert merged["devices"] == [{"id": "SRV"}]
    assert merged["hygrometers"] == [{"id": "H"}]
    assert merged["buttons"] == [{"id": "B"}]
    assert merged["relay_source"] == "bandit-1"
    # The accessory section is this device's.
    assert merged["stemma"] == [{"id": "i2c:1:0x30", "name": "Pad"}]
    assert merged["stemma_discovered"] == [{"id": "i2c:1:0x38"}]
    assert merged["stemma_enabled"] is True
    assert merged["i2c_available"] is True and merged["i2c_detail"] == ""
    assert merged["reader_age_seconds"] == 2.0
    # Inputs are not mutated.
    assert upstream["stemma"][0]["name"] == "phantom"


def test_state_on_a_satellite_overlays_the_local_accessories(client, satellite,
                                                             monkeypatch):
    """GET /gadgets/state on a satellite: the server's fleet snapshot with
    this device's own configured pad, its own discoveries, and its own bus
    health laid over it, so the Accessories pane on the device holding the
    pad shows that pad instead of the server's phantom entry."""
    import httpx
    from app import main
    from app.services import gadgets, gadgets_relay

    server_state = {
        "devices": [{"id": "SRV:PROBE", "probes": []}],
        "hygrometers": [{"id": "SRV:HYGRO"}],
        "stemma": [{"id": "i2c:1:0x30", "name": "phantom", "stale": True}],
        "stemma_discovered": [],
        "stemma_enabled": True,
        "i2c_available": False,
        "i2c_detail": "The accessory reader is not set up on this device yet.",
        "reader_age_seconds": None,
    }

    class Upstream(FakeUpstream):
        async def request(self, method, url, headers=None, params=None, content=None):
            self.calls.append({"method": method, "url": url, "headers": headers})
            return httpx.Response(200, json=server_state,
                                  headers={"content-type": "application/json"})

    fake = Upstream()
    monkeypatch.setattr(main, "_satellite_fwd_client", fake)
    main._FWD_CACHE_PATHS["/gadgets/state"].invalidate()
    monkeypatch.setattr(gadgets_relay, "_ensure_worker", lambda: None)
    monkeypatch.setattr(satellite, "stemma_enabled", True, raising=False)
    monkeypatch.setattr(satellite, "stemma_devices", [
        {"id": "i2c:1:0x30", "kind": "neokey", "name": "Pad"}], raising=False)
    gadgets.reset()
    gadgets_relay.reset()
    try:
        # The local agent checks in with a heartbeat for the pad, a board it
        # found that is not added yet, and a healthy bus.
        client.post("/gadgets/readings", json={
            "devices": [{"id": "i2c:1:0x30", "kind": "stemma", "model": "neokey"}],
            "discovered": [{"id": "i2c:1:0x38", "kind": "stemma", "model": "aht20",
                            "supported": False}],
            "i2c": {"available": True, "detail": ""},
        })
        r = client.get("/gadgets/state")
        assert r.status_code == 200
        state = r.json()
        # Fetched from the server with the satellite's key, and the fleet
        # sections are the server's.
        (call,) = fake.calls
        assert call["url"] == "http://server.test/gadgets/state"
        assert call["headers"]["X-API-Key"] == "up-key"
        assert [d["id"] for d in state["devices"]] == ["SRV:PROBE"]
        assert [d["id"] for d in state["hygrometers"]] == ["SRV:HYGRO"]
        # The accessory section is this device's own.
        assert [d["name"] for d in state["stemma"]] == ["Pad"]
        assert state["stemma"][0]["stale"] is False
        assert [d["id"] for d in state["stemma_discovered"]] == ["i2c:1:0x38"]
        assert state["i2c_available"] is True and state["i2c_detail"] == ""
        assert state["reader_age_seconds"] is not None
        assert state["stemma_choices"]
        # A second poll inside the cache window never re-asks the server.
        client.get("/gadgets/state")
        assert len(fake.calls) == 1
    finally:
        main._FWD_CACHE_PATHS["/gadgets/state"].invalidate()
        gadgets.reset()
        gadgets_relay.reset()


def test_state_on_a_satellite_stands_alone_when_the_server_is_down(client, satellite,
                                                                   monkeypatch):
    from app import main
    from app.services import gadgets

    class Dead:
        async def request(self, *a, **k):
            raise RuntimeError("down")

    monkeypatch.setattr(main, "_satellite_fwd_client", Dead())
    main._FWD_CACHE_PATHS["/gadgets/state"].invalidate()
    monkeypatch.setattr(satellite, "stemma_devices", [
        {"id": "i2c:1:0x30", "kind": "neokey", "name": "Pad"}], raising=False)
    gadgets.reset()
    try:
        r = client.get("/gadgets/state")
        assert r.status_code == 200
        assert [d["name"] for d in r.json()["stemma"]] == ["Pad"]
    finally:
        gadgets.reset()
