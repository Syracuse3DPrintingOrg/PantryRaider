"""Every Home Assistant integration already in the field keeps working.

HACS updates lag app updates by months, so an install on this release still
serves integrations from the first one (c9517f9, 2026-07-14) to v0.20.0. The
requests those versions send were recorded once at build time by
scripts/compat/capture-ha-component-requests.py into
tests/data/compat_ha_component_requests.json (header names only, secrets as
placeholders). This replays them against the current app from a Home
Assistant box on the home network:

- an upgraded install with no password answers every keyless request the way
  the app intends;
- with a password, every keyed request made with the install's shared
  (legacy) key answers 200;
- the recorded pairing request (no kind) opens a request suggested as a Home
  Assistant key;
- the recorded /ha/connect body (base_url and token only) is accepted and
  stored as the owner's token.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402

FIXTURE = ROOT / "tests" / "data" / "compat_ha_component_requests.json"
LEGACY_KEY = "legacy-shared-key-for-tests"
HA_PEER = ("192.168.1.20", 50000)


def _calls():
    data = json.loads(FIXTURE.read_text())
    out = []
    for version in data["versions"]:
        out.extend(version["calls"])
    return out


CALLS = _calls()
PAIRING_PATHS = ("/api/pairing/",)


def _is_pairing(call) -> bool:
    return call["path"].startswith(PAIRING_PATHS)


class _FakeProbe:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        class _R:
            status_code = 200
        return _R()


@pytest.fixture
def ha_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.routers import expiring as expiring_router
    from app.services import deck_config, gadgets, pairing, printing
    from app.services import devices as devices_svc
    from app.services.grocy import GrocyClient

    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "api_key", LEGACY_KEY, raising=False)
    monkeypatch.setattr(settings, "extra_api_keys", [], raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "local_device_pairing_enabled", True, raising=False)
    monkeypatch.setattr(settings, "ha_token_kind", "", raising=False)
    monkeypatch.setattr("app.hardware.is_raspberry_pi", lambda: False)

    async def no_expiring(self, days=7):
        return []

    async def no_push(*a, **kw):
        return False

    monkeypatch.setattr(GrocyClient, "get_expiring", no_expiring)
    monkeypatch.setattr(gadgets, "get_state", lambda: {"devices": []})
    monkeypatch.setattr(devices_svc, "list_devices", lambda: [])
    monkeypatch.setattr(printing, "list_queues", lambda: [])
    monkeypatch.setattr(printing, "local_label_queue", lambda choice: "")
    monkeypatch.setattr(printing, "resolve_effective_queue", lambda local, inherited: "")
    monkeypatch.setattr(deck_config, "apush_to_deck", no_push, raising=False)
    monkeypatch.setattr("app.services.egress.is_safe_public_url", lambda url, **kw: True)
    monkeypatch.setattr("app.services.egress.guarded_async_client", lambda **kw: _FakeProbe())
    pairing.reset()
    expiring_router._count_items_cache.invalidate()
    with TestClient(app, client=HA_PEER) as c:
        yield c
    pairing.reset()
    expiring_router._count_items_cache.invalidate()


def _send(client, call, key=None):
    headers = {}
    if "X-API-Key" in call["headers"] and key:
        headers["X-API-Key"] = key
    if call["method"] == "GET":
        return client.get(call["path"], headers=headers)
    return client.post(call["path"], headers=headers, json=call["json"])


def test_fixture_covers_both_versions():
    versions = {call["version"] for call in CALLS}
    assert versions == {"c9517f9", "v0.20.0"}
    for call in CALLS:
        for name in call["headers"]:
            assert name == "X-API-Key"
        assert "<api-key>" not in json.dumps(call["json"])


@pytest.mark.parametrize("call", [c for c in CALLS if not c["keyed"]],
                         ids=lambda c: f"{c['version']}-{c['method']}-{c['path']}")
def test_keyless_requests_on_an_upgraded_open_install(ha_client, call):
    resp = _send(ha_client, call)
    assert resp.status_code == 200, resp.text
    if call["path"] in ("/ha/state",):
        assert resp.json()["app"] == "pantryraider"


@pytest.mark.parametrize("call", [c for c in CALLS if c["keyed"] and not _is_pairing(c)],
                         ids=lambda c: f"{c['version']}-{c['method']}-{c['path']}")
def test_keyed_requests_with_a_password_and_the_legacy_key(ha_client, monkeypatch, call):
    monkeypatch.setattr(settings, "auth_password", "hunter2", raising=False)
    resp = _send(ha_client, call, key=LEGACY_KEY)
    assert resp.status_code == 200, resp.text


def test_recorded_pairing_request_suggests_a_home_assistant_key(ha_client):
    from app.services import pairing

    call = next(c for c in CALLS if c["path"] == "/api/pairing/request")
    assert "kind" not in call["json"]
    resp = _send(ha_client, call)
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("ok") and body.get("request_id")
    pending = pairing.pending_requests()
    assert len(pending) == 1
    assert pending[0]["suggested_kind"] == "ha"


def test_recorded_connect_body_is_stored_as_the_owner_token(ha_client):
    call = next(c for c in CALLS if c["path"] == "/ha/connect" and c["version"] == "v0.20.0")
    assert set(call["json"]) == {"base_url", "token"}
    resp = _send(ha_client, call)
    assert resp.status_code == 200
    assert settings.ha_token_kind == "owner-legacy"
    assert settings.streamdeck_ha_token == call["json"]["token"]
