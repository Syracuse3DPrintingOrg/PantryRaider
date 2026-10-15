"""Fleet-wide update channel: stable (releases) vs main (FoodAssistant-wkwx)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import auto_update as au  # noqa: E402
from app.config import settings, SATELLITE_PULL_FIELDS, _SAVEABLE  # noqa: E402


def test_channel_defaults_to_stable_and_is_global():
    # Signed releases are the default from 0.21; an install that saved "main"
    # (or sets UPDATE_CHANNEL) keeps its choice. Checked on a fresh instance so
    # a value another test or a host settings.json put on the shared object
    # cannot mask the default.
    from app.config import Settings
    assert Settings.model_fields["update_channel"].default == "stable"
    assert "update_channel" in _SAVEABLE                 # persisted
    assert "update_channel" in SATELLITE_PULL_FIELDS     # inherited by remotes


def test_decision_is_the_same_on_both_channels():
    # The channel picks WHAT the OTA installs, not WHETHER an attempt runs: the
    # helper resolves main tip vs newest release itself and no-ops when current,
    # and a satellite converges on its server's version on either channel.
    for channel in ("main", "stable"):
        assert au.should_run(False, "0.6.10", "", channel) is True
        assert au.should_run(True, "0.6.10", "", channel) is False
        assert au.should_run(True, "0.6.10", "0.6.12", channel) is True
        assert au.should_run(True, "0.6.12", "0.6.12", channel) is False


# -- UI and save --------------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    # Configured so the setup-redirect middleware lets /setup/* through.
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _setup_html(client, monkeypatch, mode):
    monkeypatch.setattr(settings, "deployment_mode", mode)
    with patch.object(type(settings), "is_configured", lambda self: True):
        return client.get("/setup").text


def test_server_shows_editable_channel_select(client, monkeypatch):
    html = _setup_html(client, monkeypatch, "server")
    assert 'id="update_channel"' in html
    assert 'data-on-change="saveUpdateChannel(this)"' in html   # editable
    assert "Releases only" in html


def test_satellite_channel_select_is_read_only(client, monkeypatch):
    html = _setup_html(client, monkeypatch, "pi_remote")
    assert 'id="update_channel"' in html
    assert 'data-on-change="saveUpdateChannel(this)"' not in html  # disabled on a remote
    assert "Managed on the main server" in html


def test_setup_save_persists_the_channel(client, monkeypatch):
    # Through the instance dict, so the shared settings never record the
    # channel as chosen (model_fields_set) for the tests that follow.
    monkeypatch.setitem(settings.__dict__, "update_channel", "main")
    monkeypatch.setattr(settings, "deployment_mode", "server")
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.post("/setup/save", json={"update_channel": "stable"})
    assert r.status_code == 200
    assert settings.update_channel == "stable"


def test_setup_save_drops_an_unknown_channel(client, monkeypatch):
    # Through the instance dict, so the shared settings never record the
    # channel as chosen (model_fields_set) for the tests that follow.
    monkeypatch.setitem(settings.__dict__, "update_channel", "stable")
    monkeypatch.setattr(settings, "deployment_mode", "server")
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.post("/setup/save", json={"update_channel": "nightly"})
    assert r.status_code == 200
    assert settings.update_channel == "stable"  # stored value kept
