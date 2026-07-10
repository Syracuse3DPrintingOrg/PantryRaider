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


def test_gadgets_forward_decision(satellite):
    """Thermometer gadgets always forward on a satellite, regardless of the
    recipe backend (the server owns the probe config and readings)."""
    from app import main

    assert main._satellite_recipe_upstream("/gadgets/readings") == "http://server.test"
    assert main._satellite_recipe_upstream("/gadgets/state") == "http://server.test"
    assert main._satellite_recipe_upstream("/gadgets/config") == "http://server.test"
    # Even on the Mealie recipe backend, gadgets still forward.
    satellite.recipes_backend = "mealie"
    assert main._satellite_recipe_upstream("/gadgets/readings") == "http://server.test"
    # But installing the reader is a local host action on the satellite.
    assert main._satellite_recipe_upstream("/gadgets/install") is None
    # Not a satellite: gadgets stay local (the server reads its own radio).
    satellite.deployment_mode = "server"
    assert main._satellite_recipe_upstream("/gadgets/readings") is None


def test_middleware_forwards_gadgets_readings(client, satellite, monkeypatch):
    """A satellite's reader push to localhost is relayed to the server with the
    upstream key, so the server (and the satellite's kiosk) show the probes."""
    from app import main

    fake = FakeUpstream()
    monkeypatch.setattr(main, "_satellite_fwd_client", fake)

    r = client.post("/gadgets/readings", json={"devices": []})
    assert r.status_code == 200 and r.json() == {"forwarded": True}
    assert len(fake.calls) == 1
    assert fake.calls[0]["url"] == "http://server.test/gadgets/readings"
    assert fake.calls[0]["headers"]["X-API-Key"] == "up-key"


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
