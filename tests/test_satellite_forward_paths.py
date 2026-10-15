"""A Bandit forwards only the path it was asked for (main.forward_native_recipes).

A Bandit (satellite) passes /gadgets, /recipes, /mealie and print requests on
to its main server with its own key. The decision is made on the path as the
request arrived, but the HTTP client that sends it on folds '..' segments, so
'/gadgets/../ha/connect' would leave as '/ha/connect': a caller allowed on the
Bandit's kitchen pages would reach any route on the main server with the
Bandit's key. A path with a dot segment, a backslash or a control character is
now refused before anything is sent, and the path that is sent is exactly the
one that was checked.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings

SERVICE = Path(__file__).resolve().parents[1] / "service"


class FakeUpstream:
    def __init__(self):
        self.calls = []

    async def request(self, method, url, headers=None, params=None, content=None):
        self.calls.append({"method": method, "url": url})
        return httpx.Response(200, json={"forwarded": True},
                              headers={"content-type": "application/json"})


@pytest.fixture
def satellite(monkeypatch, tmp_path):
    from app import main
    monkeypatch.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    monkeypatch.setattr(settings, "remote_server_url", "http://server.test", raising=False)
    monkeypatch.setattr(settings, "upstream_api_key", "up-key", raising=False)
    monkeypatch.setattr(settings, "recipes_backend", "native", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "fleet_relay", {"v": 1, "server_has_password": False},
                        raising=False)
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True, raising=False)
    fake = FakeUpstream()
    monkeypatch.setattr(main, "_satellite_fwd_client", fake)
    return fake


@pytest.mark.parametrize("path", [
    "/gadgets/%2e%2e/ha/connect",
    "/gadgets/%2E%2E/api/config/satellite",
    "/gadgets/devices/%2e%2e/%2e%2e/api/credentials/exchange",
    "/recipes/%2e/%2e%2e/setup/save",
    "/mealie/recipes/%2e%2e%2f%2e%2e/ha/connect",
    "/gadgets/devices%5c..%5cha",
    "/gadgets/devices/%01",
])
def test_a_dot_segment_is_never_forwarded(satellite, path):
    client = TestClient(__import__("app.main", fromlist=["app"]).app)
    for method in ("GET", "POST"):
        r = client.request(method, path, json={} if method == "POST" else None)
        assert r.status_code == 400, (method, path, r.status_code)
    assert satellite.calls == []


def test_ordinary_paths_are_forwarded_as_they_were_checked(satellite):
    from app.main import app
    client = TestClient(app)
    r = client.get("/gadgets/devices")
    assert r.status_code == 200 and r.json() == {"forwarded": True}
    r = client.get("/mealie/recipes/my-stew")
    assert r.status_code == 200
    assert [c["url"] for c in satellite.calls] == [
        "http://server.test/gadgets/devices", "http://server.test/mealie/recipes/my-stew"]


def test_what_is_sent_is_read_the_same_way_on_the_server():
    """The path arrives on the main server exactly as this Bandit read it: a
    percent sign stays a percent sign rather than being decoded a second time,
    and a '?' or '#' that was part of the path stays part of it. (Checked on
    the helper: the test client decodes a path twice, a real server once.)"""
    from app import main
    base = "http://server.test"
    for path in ("/recipes/100%off", "/recipes/a?b#c/image", "/gadgets/devices/AA:BB:CC",
                 "/recipes/crème brûlée"):
        url = main._forward_url(base, path)
        assert httpx.URL(url).path == path, url
        assert httpx.URL(url).query == b""
    assert main._forward_url(base, "/mealie/recipes") == "http://server.test/mealie/recipes"
