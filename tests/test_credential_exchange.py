"""POST /api/credentials/exchange: Home Assistant swaps an older key for its own."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.passwords import hash_secret
from app.services import access, credentials

SERVICE = Path(__file__).resolve().parents[1] / "service"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret("hunter2"), raising=False)
    monkeypatch.setattr(settings, "api_key", "", raising=False)
    monkeypatch.setattr(settings, "extra_api_keys", ["ha-legacy-key"], raising=False)
    monkeypatch.setattr(settings, "extra_api_key_names", ["Home Assistant"], raising=False)
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True, raising=False)
    credentials.reset(None, persist=True)
    from fastapi.testclient import TestClient
    from app.main import app
    yield TestClient(app)
    credentials.reset(persist=False)


H = {"X-API-Key": "ha-legacy-key", "User-Agent": "HomeAssistant/2026.9 aiohttp/3.10",
     "X-PR-Client": "ha-component/0.21.0"}


def test_a_legacy_key_gets_its_own_ha_key(client):
    # The integration used the shared key before (a holder is recorded).
    assert client.get("/ha/state", headers=H).status_code != 401
    r = client.post("/api/credentials/exchange", json={"kind": "ha"}, headers=H)
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["ok"] and body["key"].startswith("pr1.")
    rec = credentials.lookup(body["key"])
    assert rec["scope"] == "ha" and rec["kind"] == "exchanged"
    assert rec["name"].startswith("Home Assistant (")
    # The old key keeps working, and its holder is marked as moved.
    assert client.get("/ha/state", headers={"X-API-Key": "ha-legacy-key"}).status_code != 401
    legacy = credentials.get(credentials.legacy_id("ha-legacy-key"))
    assert any(h.get("moved") for h in legacy["holders"])


def test_only_a_legacy_key_can_swap(client):
    _rec, ha = credentials.mint("ha", "HA", "manual")
    r = client.post("/api/credentials/exchange", json={"kind": "ha"}, headers={"X-API-Key": ha})
    assert r.status_code == 403
    assert client.post("/api/credentials/exchange", json={"kind": "ha"}).status_code == 401


def test_swap_from_outside_is_refused(client):
    from fastapi.testclient import TestClient
    from app.main import app
    outside = TestClient(app, client=("203.0.113.9", 50000))
    r = outside.post("/api/credentials/exchange", json={"kind": "ha"}, headers=H)
    assert r.status_code == 403 and r.json()["error"] == access.EXCHANGE_LAN_MSG


def test_swaps_are_limited_per_key(client):
    for _ in range(5):
        assert client.post("/api/credentials/exchange", json={"kind": "ha"},
                           headers=H).status_code == 200
    r = client.post("/api/credentials/exchange", json={"kind": "ha"}, headers=H)
    assert r.status_code == 429 and r.json()["error"] == access.EXCHANGE_RATE_MSG


def test_unknown_kind_is_refused(client):
    assert client.post("/api/credentials/exchange", json={"kind": "satellite"},
                       headers=H).status_code == 400
