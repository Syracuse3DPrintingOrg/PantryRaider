"""Tests for the satellite backend proxy.

A satellite cannot reach the server's Docker-internal Grocy/Mealie, so it routes
those API calls through the main server's /api/proxy/<backend>/ endpoint
(authenticated with the shared X-API-Key). These tests cover the server-side
auth/forwarding guards and the client-side URL/header switch that points a
satellite at the proxy.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    cwd = os.getcwd()
    os.chdir(SERVICE)
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


# -- server side: /api/proxy/<backend>/<path> --------------------------------

def test_proxy_refuses_without_server_api_key(client, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "")
    r = client.get("/api/proxy/grocy/api/stock")
    assert r.status_code == 503


def test_proxy_rejects_bad_key(client, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "secret-key")
    monkeypatch.setattr(settings, "auth_password", "")  # avoid auth middleware
    r = client.get("/api/proxy/grocy/api/stock", headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


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


def test_proxy_forwards_to_backend(client, monkeypatch):
    """A valid call is forwarded to the server's Grocy with its own credentials."""
    monkeypatch.setattr(settings, "api_key", "secret-key")
    monkeypatch.setattr(settings, "auth_password", "")
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy:80")
    monkeypatch.setattr(settings, "grocy_api_key", "real-grocy-key")

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

    r = client.get("/api/proxy/grocy/api/stock", headers={"X-API-Key": "secret-key"})
    assert r.status_code == 200
    assert r.json() == [{"id": 1}]
    # Server rewrites the path onto its own backend base and supplies its key.
    assert captured["url"] == "http://grocy:80/api/stock"
    assert captured["headers"]["GROCY-API-KEY"] == "real-grocy-key"
    # The satellite's shared key must never be forwarded to the backend.
    assert "X-API-Key" not in captured["headers"]


# -- client side: GrocyClient / MealieClient switch to the proxy -------------

def test_grocy_client_uses_proxy_on_satellite(monkeypatch):
    from app.services.grocy import GrocyClient
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    monkeypatch.setattr(settings, "remote_server_url", "http://server:9284")
    monkeypatch.setattr(settings, "upstream_api_key", "shared-key")
    c = GrocyClient()
    assert c.base == "http://server:9284/api/proxy/grocy/api"
    assert c.headers["X-API-Key"] == "shared-key"
    assert "GROCY-API-KEY" not in c.headers


def test_grocy_client_direct_when_not_satellite(monkeypatch):
    from app.services.grocy import GrocyClient
    monkeypatch.setattr(settings, "deployment_mode", "server")
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy:80")
    monkeypatch.setattr(settings, "grocy_api_key", "k")
    c = GrocyClient()
    assert c.base == "http://grocy:80/api"
    assert c.headers["GROCY-API-KEY"] == "k"


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
