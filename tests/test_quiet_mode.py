"""Quiet mode toggle: silence the timer chime, visual-only (FoodAssistant-soj1)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings, _SAVEABLE, SATELLITE_PULL_FIELDS  # noqa: E402


def test_quiet_mode_is_device_local():
    assert settings.quiet_mode is False           # off by default
    assert "quiet_mode" in _SAVEABLE              # persisted
    assert "quiet_mode" not in SATELLITE_PULL_FIELDS  # each device decides


@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "quiet_mode", False, raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_quiet_flag_on_base_pages_and_toggle_on_settings(client, monkeypatch):
    with patch.object(type(settings), "is_configured", lambda self: True):
        # The Settings page carries the toggle control.
        assert 'id="quiet_mode"' in client.get("/setup").text
        # A base-extending page (where timer-chips.js runs) carries the flag on
        # the <html> element so the chime can read it.
        page = client.get("/ui/weather").text
        assert 'data-quiet-mode="false"' in page
        # Saving the toggle persists it and the next render reflects it.
        r = client.post("/setup/save", json={"quiet_mode": True})
        assert r.status_code == 200
        assert settings.quiet_mode is True
        assert 'data-quiet-mode="true"' in client.get("/ui/weather").text
