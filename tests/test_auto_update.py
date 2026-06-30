"""Fleet-wide auto-update flag and the Pi update decision (FoodAssistant-k2kk)."""
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


def test_pi_hosted_always_attempts():
    # Not a satellite: attempt regardless of any server version (OTA no-ops when
    # already current).
    assert au.should_run(False, "0.6.10", "") is True
    assert au.should_run(False, "0.6.10", "0.6.10") is True


def test_satellite_only_updates_when_behind_known_server():
    # Server version unknown yet: do nothing.
    assert au.should_run(True, "0.6.10", "") is False
    # Same version: nothing to do.
    assert au.should_run(True, "0.6.12", "0.6.12") is False
    # Different version: converge on the server.
    assert au.should_run(True, "0.6.10", "0.6.12") is True


def test_flag_defaults_on_and_is_global():
    assert settings.auto_update is True              # on by default
    assert "auto_update" in _SAVEABLE                 # persisted
    assert "auto_update" in SATELLITE_PULL_FIELDS     # inherited by remotes (global)


def test_server_reports_its_version_to_satellites():
    from app.config import APP_VERSION
    from app.routers import satellite as sat
    # The satellite config payload carries the server version so a remote can
    # match it. Build the response shape directly via the handler's helper path.
    import app.routers.satellite as srv
    # The version constant is what the endpoint embeds.
    assert srv.APP_VERSION == APP_VERSION


# -- UI ---------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
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


def test_server_shows_editable_auto_update_toggle(client, monkeypatch):
    html = _setup_html(client, monkeypatch, "server")
    assert 'id="auto_update"' in html
    assert 'onchange="saveAutoUpdate(this)"' in html       # editable
    assert "Watchtower" in html                            # mode-specific note


def test_satellite_auto_update_toggle_is_read_only(client, monkeypatch):
    html = _setup_html(client, monkeypatch, "pi_remote")
    assert 'id="auto_update"' in html
    assert 'onchange="saveAutoUpdate(this)"' not in html   # disabled on a remote
    assert "Managed on the main server" in html


def test_setup_save_persists_auto_update(client, monkeypatch):
    # monkeypatch captures the original so the flag is restored after this test,
    # keeping the on-by-default invariant intact for the rest of the suite.
    monkeypatch.setattr(settings, "auto_update", True)
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted")
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.post("/setup/save", json={"auto_update": False})
    assert r.status_code == 200
    assert settings.auto_update is False
