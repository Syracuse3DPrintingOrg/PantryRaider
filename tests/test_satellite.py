"""Satellite config-federation tests.

Exercise the server-side config endpoint (what a main server hands out) and the
pull-side apply logic (how a satellite mirrors it), without real network or a
second running instance. Pure logic + FastAPI TestClient.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings, SATELLITE_PULL_FIELDS  # noqa: E402


# -- server side: GET /api/config/satellite ----------------------------------

@pytest.fixture
def client():
    # Templates load from the relative path "app/templates", so run from service/.
    from fastapi.testclient import TestClient
    from app.main import app
    cwd = os.getcwd()
    os.chdir(SERVICE)
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_config_endpoint_refuses_without_server_api_key(client, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "")
    r = client.get("/api/config/satellite")
    assert r.status_code == 503


def test_config_endpoint_rejects_bad_key(client, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "secret-key")
    monkeypatch.setattr(settings, "auth_password", "")  # avoid auth middleware
    r = client.get("/api/config/satellite", headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


def test_config_endpoint_serves_shareable_fields(client, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "secret-key")
    monkeypatch.setattr(settings, "auth_password", "")
    monkeypatch.setattr(settings, "grocy_base_url", "http://server:9383")
    monkeypatch.setattr(settings, "grocy_api_key", "grocy-key")
    r = client.get("/api/config/satellite", headers={"X-API-Key": "secret-key"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # Every shareable field is present; no device-local secret leaks.
    assert set(body["config"].keys()) == set(SATELLITE_PULL_FIELDS)
    assert body["config"]["grocy_base_url"] == "http://server:9383"
    assert "secret_key" not in body["config"]
    assert "auth_password" not in body["config"]
    assert "api_key" not in body["config"]
    assert isinstance(body["expiry_defaults"], list)


# -- pull side: apply config onto live settings ------------------------------

def test_apply_config_sets_only_shareable_fields(monkeypatch):
    from app.services.satellite import _apply_config
    monkeypatch.setattr(settings, "grocy_base_url", "")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    applied = _apply_config({
        "grocy_base_url": "http://server:9383",
        "gemini_api_key": "pulled-key",
        "secret_key": "SHOULD-NOT-APPLY",  # not in SATELLITE_PULL_FIELDS
    })
    assert "grocy_base_url" in applied
    assert "gemini_api_key" in applied
    assert "secret_key" not in applied
    assert settings.grocy_base_url == "http://server:9383"
    assert settings.gemini_api_key == "pulled-key"
    assert getattr(settings, "secret_key") != "SHOULD-NOT-APPLY"
    assert settings.server_sourced_fields >= {"grocy_base_url", "gemini_api_key"}


def test_sync_noops_when_not_satellite(monkeypatch):
    from app.services.satellite import sync_from_upstream
    monkeypatch.setattr(settings, "deployment_mode", "server")
    out = sync_from_upstream()
    assert out["ok"] is False
    assert out["error"] == "not a satellite"


def test_sync_requires_url_and_key(monkeypatch):
    from app.services.satellite import sync_from_upstream
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    monkeypatch.setattr(settings, "remote_server_url", "")
    monkeypatch.setattr(settings, "upstream_api_key", "")
    out = sync_from_upstream()
    assert out["ok"] is False
    assert "missing" in out["error"]


# -- mode semantics ----------------------------------------------------------

def test_satellite_is_configured_needs_url_and_key(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "remote_server_url", "http://server:9284")
    monkeypatch.setattr(settings, "upstream_api_key", "")
    assert settings.is_configured() is False
    monkeypatch.setattr(settings, "upstream_api_key", "k")
    assert settings.is_configured() is True


def test_satellite_features_show_backend_panes(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    f = settings.features()
    assert f["satellite"] is True
    assert f["manages_stack"] is False
    assert f["ai"] is True


# -- integration: sync_from_upstream against a mocked HTTP layer -------------
#
# These drive the real sync_from_upstream code path (header build, status
# handling, _apply_config, defaults, provider invalidation) but swap the
# module-level httpx.get for a fake so nothing touches the network. The DB-
# backed _apply_defaults and the provider cache reset_providers are also
# patched so the test stays pure logic, while still letting us assert that
# sync invalidates providers on a successful pull.

def _httpx_connect_error():
    """A representative httpx network failure for the unreachable-server case."""
    import httpx
    return httpx.ConnectError("connection refused")


class _FakeResponse:
    """Minimal stand-in for httpx.Response: just what sync_from_upstream reads."""

    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


@pytest.fixture
def satellite_mode(monkeypatch):
    """Put settings into a fully configured satellite state for sync tests."""
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    monkeypatch.setattr(settings, "remote_server_url", "http://server:9284")
    monkeypatch.setattr(settings, "upstream_api_key", "upstream-secret")
    monkeypatch.setattr(settings, "device_id", "dev-abc")
    # Start from blanks so we can prove which fields the pull wrote.
    monkeypatch.setattr(settings, "grocy_base_url", "")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    # server_sourced_fields is not a declared pydantic field; the production
    # code sets it via object.__setattr__, so do the same here and restore it.
    prior = getattr(settings, "server_sourced_fields", set())
    object.__setattr__(settings, "server_sourced_fields", set())
    yield
    object.__setattr__(settings, "server_sourced_fields", prior)


def test_sync_happy_path_applies_fields_and_invalidates_providers(satellite_mode):
    from app.services import satellite as sat

    payload = {
        "ok": True,
        "config": {
            "grocy_base_url": "http://server:9383",
            "gemini_api_key": "pulled-key",
        },
        "expiry_defaults": [
            {"category": "dairy", "name_pattern": "milk", "storage_type": "fridge",
             "default_days": 7, "priority": 1},
        ],
        "command": None,
    }

    with patch.object(sat.httpx, "get", return_value=_FakeResponse(200, payload)) as mock_get, \
            patch.object(sat, "_apply_defaults", return_value=1) as mock_defaults, \
            patch("app.dependencies.reset_providers") as mock_reset:
        out = sat.sync_from_upstream()

    assert out["ok"] is True
    assert set(out["applied"]) >= {"grocy_base_url", "gemini_api_key"}
    assert out["defaults"] == 1
    assert out["error"] is None
    assert settings.grocy_base_url == "http://server:9383"
    assert settings.gemini_api_key == "pulled-key"
    assert settings.server_sourced_fields >= {"grocy_base_url", "gemini_api_key"}
    # The pull hit the expected endpoint with the upstream key, and providers
    # were invalidated so freshly pulled keys take effect.
    called_url = mock_get.call_args.args[0]
    assert called_url == "http://server:9284/api/config/satellite"
    headers = mock_get.call_args.kwargs["headers"]
    assert headers["X-API-Key"] == "upstream-secret"
    mock_defaults.assert_called_once()
    mock_reset.assert_called_once()


def test_sync_unreachable_server_keeps_existing_config(satellite_mode):
    from app.services import satellite as sat

    monkeypatch_value = "http://existing:9383"
    object.__setattr__(settings, "grocy_base_url", monkeypatch_value)

    with patch.object(sat.httpx, "get", side_effect=_httpx_connect_error()) as mock_get, \
            patch.object(sat, "_apply_defaults") as mock_defaults, \
            patch("app.dependencies.reset_providers") as mock_reset:
        out = sat.sync_from_upstream()

    assert out["ok"] is False
    assert "cannot reach server" in out["error"]
    assert out["applied"] == []
    assert out["defaults"] == 0
    # Existing config is untouched and nothing downstream ran.
    assert settings.grocy_base_url == monkeypatch_value
    mock_defaults.assert_not_called()
    mock_reset.assert_not_called()
    assert mock_get.called


def test_sync_bad_api_key_401_handled_gracefully(satellite_mode):
    from app.services import satellite as sat

    resp = _FakeResponse(401, {"detail": "bad api key"})
    with patch.object(sat.httpx, "get", return_value=resp), \
            patch.object(sat, "_apply_defaults") as mock_defaults, \
            patch("app.dependencies.reset_providers") as mock_reset:
        out = sat.sync_from_upstream()

    assert out["ok"] is False
    assert "401" in out["error"]
    assert "bad api key" in out["error"]
    assert out["applied"] == []
    # A rejected pull applies nothing and leaves the provider cache alone.
    assert settings.grocy_base_url == ""
    mock_defaults.assert_not_called()
    mock_reset.assert_not_called()


def test_sync_partial_payload_without_defaults_still_applies_config(satellite_mode):
    from app.services import satellite as sat

    # No expiry_defaults key at all: config should apply, defaults step is a no-op.
    payload = {
        "ok": True,
        "config": {"grocy_base_url": "http://server:9383"},
        "command": None,
    }

    with patch.object(sat.httpx, "get", return_value=_FakeResponse(200, payload)), \
            patch.object(sat, "_apply_defaults", return_value=0) as mock_defaults, \
            patch("app.dependencies.reset_providers") as mock_reset:
        out = sat.sync_from_upstream()

    assert out["ok"] is True
    assert "grocy_base_url" in out["applied"]
    assert out["defaults"] == 0
    assert settings.grocy_base_url == "http://server:9383"
    # _apply_defaults is called with the empty default and must not error.
    mock_defaults.assert_called_once_with([])
    mock_reset.assert_called_once()
