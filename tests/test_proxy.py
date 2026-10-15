"""Tests for the satellite backend proxy.

A satellite cannot reach the server's Docker-internal Grocy/Mealie, so it routes
those API calls through the main server's /api/proxy/<backend>/ endpoint with
its key. These tests cover who the server-side hop serves (satellite, legacy
and admin keys, through the app's own sign-in rules), the 401 and 503 answers
every satellite release already knows, the forwarding guards, and the
client-side URL and header switch that points a satellite at the proxy.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402

_PW = "kitchen-secret"


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "extra_api_keys", [], raising=False)
    monkeypatch.setattr(settings, "extra_api_key_names", [], raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    cwd = os.getcwd()
    os.chdir(SERVICE)
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


@pytest.fixture
def forwarded(monkeypatch):
    """Stub the hop's upstream call; returns the captured request."""
    captured = {}

    class _Resp:
        status_code = 200
        content = b'[{"id": 1}]'
        headers = {"content-type": "application/json"}

    async def _fake_request(method, url, headers=None, params=None, content=None):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = headers
        return _Resp()

    from app.routers import proxy as proxy_router
    monkeypatch.setattr(proxy_router._client, "request", _fake_request)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy:80", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "real-grocy-key", raising=False)
    return captured


def _mint(scope, name="Kitchen Pi"):
    from app.services import credentials
    _rec, secret = credentials.mint(scope, name, "manual")
    return secret


# -- server side: /api/proxy/<backend>/<path> --------------------------------

def test_proxy_refuses_without_server_api_key(client, monkeypatch):
    # No key exists anywhere: the old "not set" answer, unchanged.
    monkeypatch.setattr(settings, "api_key", "")
    r = client.get("/api/proxy/grocy/api/stock")
    assert r.status_code == 503
    assert r.json() == {"detail": "Server API key not set"}
    r = client.get("/api/proxy/grocy/api/stock", headers={"X-API-Key": "wrong"})
    assert r.status_code == 503


def test_proxy_rejects_bad_key(client, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "secret-key")
    monkeypatch.setattr(settings, "auth_password", "")
    r = client.get("/api/proxy/grocy/api/stock", headers={"X-API-Key": "wrong"})
    assert r.status_code == 401
    assert r.json() == {"detail": "Unauthorized"}
    assert client.get("/api/proxy/grocy/api/stock").json() == {"detail": "Unauthorized"}


def test_proxy_rejects_bad_key_with_a_password(client, monkeypatch):
    from app.passwords import hash_secret
    monkeypatch.setattr(settings, "api_key", "secret-key")
    monkeypatch.setattr(settings, "auth_password", hash_secret(_PW))
    r = client.get("/api/proxy/grocy/api/stock", headers={"X-API-Key": "wrong"})
    assert r.status_code == 401 and r.json() == {"detail": "Unauthorized"}


def test_proxy_unknown_backend(client, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "secret-key")
    monkeypatch.setattr(settings, "auth_password", "")
    r = client.get("/api/proxy/redis/api/stock", headers={"X-API-Key": "secret-key"})
    assert r.status_code == 404


def test_proxy_backend_not_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "secret-key")
    monkeypatch.setattr(settings, "auth_password", "")
    monkeypatch.setattr(settings, "grocy_base_url", "")
    r = client.get("/api/proxy/grocy/api/stock", headers={"X-API-Key": "secret-key"})
    assert r.status_code == 503


def test_proxy_forwards_to_backend(client, forwarded, monkeypatch):
    """A valid call is forwarded to the server's Grocy with its own credentials."""
    monkeypatch.setattr(settings, "api_key", "secret-key")
    monkeypatch.setattr(settings, "auth_password", "")
    r = client.get("/api/proxy/grocy/api/stock", headers={"X-API-Key": "secret-key"})
    assert r.status_code == 200
    assert r.json() == [{"id": 1}]
    # Server rewrites the path onto its own backend base and supplies its key.
    assert forwarded["url"] == "http://grocy:80/api/stock"
    assert forwarded["headers"]["GROCY-API-KEY"] == "real-grocy-key"
    # The satellite's shared key must never be forwarded to the backend.
    assert "X-API-Key" not in forwarded["headers"]


@pytest.mark.parametrize("password", [False, True])
def test_legacy_and_satellite_keys_forward(client, forwarded, monkeypatch, password):
    from app.passwords import hash_secret
    monkeypatch.setattr(settings, "api_key", "legacy-key")
    monkeypatch.setattr(settings, "auth_password", hash_secret(_PW) if password else "")
    sat = _mint("satellite")
    for key in ("legacy-key", sat):
        forwarded.clear()
        r = client.get("/api/proxy/grocy/api/stock", headers={"X-API-Key": key})
        assert r.status_code == 200, key[:4]
        assert forwarded["url"] == "http://grocy:80/api/stock"


def test_an_admin_key_forwards(client, forwarded, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "legacy-key")
    monkeypatch.setattr(settings, "auth_password", "")
    admin = _mint("admin", "Owner script")
    r = client.get("/api/proxy/grocy/api/stock", headers={"X-API-Key": admin})
    assert r.status_code == 200


@pytest.mark.parametrize("scope", ["ha", "ha_rest", "display"])
def test_narrower_keys_are_refused(client, forwarded, monkeypatch, scope):
    from app.passwords import hash_secret
    from app.services import access
    monkeypatch.setattr(settings, "api_key", "legacy-key")
    for pw in ("", hash_secret(_PW)):
        monkeypatch.setattr(settings, "auth_password", pw)
        forwarded.clear()
        key = _mint(scope, f"{scope} key")
        r = client.get("/api/proxy/grocy/api/stock", headers={"X-API-Key": key})
        assert r.status_code == 403, (scope, bool(pw))
        assert r.json()["detail"] == access.SCOPE_MSG
        assert forwarded == {}


def test_an_admin_session_without_a_key_is_refused(client, forwarded, monkeypatch):
    # The hop always needed a key: a signed-in browser gets the plain 401.
    from app.passwords import hash_secret
    monkeypatch.setattr(settings, "api_key", "legacy-key")
    monkeypatch.setattr(settings, "auth_password", hash_secret(_PW))
    monkeypatch.setattr(settings, "local_totp_enabled", False, raising=False)
    monkeypatch.setattr(settings, "local_totp_secret", "", raising=False)
    monkeypatch.setattr(settings, "totp_secret", "", raising=False)
    r = client.post("/ui/login", data={"mode": "local", "password": _PW},
                    follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/setup", follow_redirects=False).status_code == 200
    r = client.get("/api/proxy/grocy/api/stock")
    assert r.status_code == 401 and r.json() == {"detail": "Unauthorized"}
    assert forwarded == {}


def test_legacy_loopback_trust_does_not_open_the_hop(forwarded, monkeypatch, tmp_path):
    # A program on this device without a key is trusted on kitchen routes
    # while the old trust lasts, but never on the Grocy and Mealie hop.
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "api_key", "legacy-key")
    monkeypatch.setattr(settings, "auth_password", "")
    loop = TestClient(app, client=("127.0.0.1", 50000), base_url="http://localhost")
    r = loop.get("/api/proxy/grocy/api/stock")
    assert r.status_code == 401
    assert forwarded == {}


# -- client side: GrocyClient / MealieClient switch to the proxy -------------

def test_grocy_client_uses_proxy_on_satellite(monkeypatch):
    from app.services.grocy import GrocyClient
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    monkeypatch.setattr(settings, "remote_server_url", "http://server:9284")
    monkeypatch.setattr(settings, "upstream_api_key", "shared-key")
    c = GrocyClient()
    assert c.base == "http://server:9284/api/proxy/grocy/api"
    assert c.headers["X-API-Key"] == "shared-key"
    assert c.headers["X-PR-Caps"] == "relay1"
    assert "GROCY-API-KEY" not in c.headers


def test_grocy_client_direct_when_not_satellite(monkeypatch):
    from app.services.grocy import GrocyClient
    monkeypatch.setattr(settings, "deployment_mode", "server")
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy:80")
    monkeypatch.setattr(settings, "grocy_api_key", "k")
    c = GrocyClient()
    assert c.base == "http://grocy:80/api"
    assert c.headers["GROCY-API-KEY"] == "k"
    assert "X-PR-Caps" not in c.headers


def test_satellite_linked_by_credential_id_alone_uses_the_proxy(monkeypatch):
    # A later release links a satellite without a shared key; the clients
    # already follow upstream_linked(), not the key itself.
    from app.services.grocy import GrocyClient
    from app.services.mealie import MealieClient
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    monkeypatch.setattr(settings, "remote_server_url", "http://server:9284")
    monkeypatch.setattr(settings, "upstream_api_key", "")
    monkeypatch.setattr(settings, "upstream_credential_id", "c_0123456789abcdef",
                        raising=False)
    monkeypatch.setattr(settings, "mealie_base_url", "http://mealie:80")
    assert GrocyClient().base == "http://server:9284/api/proxy/grocy/api"
    assert MealieClient().configured is True


def test_mealie_client_uses_proxy_on_satellite(monkeypatch):
    from app.services.mealie import MealieClient
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    monkeypatch.setattr(settings, "remote_server_url", "http://server:9284")
    monkeypatch.setattr(settings, "upstream_api_key", "shared-key")
    monkeypatch.setattr(settings, "mealie_base_url", "http://mealie:80")
    c = MealieClient()
    # Mealie's _request inserts /api, so the proxy base stops before it.
    assert c.base == "http://server:9284/api/proxy/mealie"
    assert c.headers["X-API-Key"] == "shared-key"
    assert c.configured is True


def test_mealie_not_configured_on_satellite_without_server_mealie(monkeypatch):
    from app.services.mealie import MealieClient
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    monkeypatch.setattr(settings, "remote_server_url", "http://server:9284")
    monkeypatch.setattr(settings, "upstream_api_key", "shared-key")
    monkeypatch.setattr(settings, "mealie_base_url", "")  # server has no Mealie
    assert MealieClient().configured is False
