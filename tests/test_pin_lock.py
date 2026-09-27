"""Tests for the optional satellite kiosk PIN lock.

The PIN is a lightweight gate for a satellite's touchscreen, applied only when a
PIN is set and deployment_mode=pi_remote. It guards the browser UI (/ui and the
root redirect) but leaves /setup reachable for recovery.
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
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    # A configured satellite so the setup-redirect does not interfere.
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    monkeypatch.setattr(settings, "remote_server_url", "http://server:9284")
    monkeypatch.setattr(settings, "upstream_api_key", "shared-key")
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "auth_password", "")
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_pin_lock_active_only_on_satellite_with_pin(monkeypatch):
    monkeypatch.setattr(settings, "kiosk_pin", "1234")
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    assert settings.pin_lock_active() is True
    monkeypatch.setattr(settings, "deployment_mode", "server")
    assert settings.pin_lock_active() is False
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    monkeypatch.setattr(settings, "kiosk_pin", "")
    assert settings.pin_lock_active() is False


def test_ui_redirects_to_pin_when_locked(client, monkeypatch):
    monkeypatch.setattr(settings, "kiosk_pin", "1234")
    r = client.get("/ui/", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert "/ui/pin" in r.headers["location"]


def test_setup_reachable_while_locked(client, monkeypatch):
    # Recovery path: settings must stay reachable so the PIN can be cleared.
    monkeypatch.setattr(settings, "kiosk_pin", "1234")
    r = client.get("/setup", follow_redirects=False)
    assert r.status_code == 200


def test_wrong_pin_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "kiosk_pin", "1234")
    r = client.post("/ui/pin/verify", data={"pin": "0000"}, follow_redirects=False)
    assert r.status_code == 401


def test_correct_pin_unlocks(client, monkeypatch):
    monkeypatch.setattr(settings, "kiosk_pin", "1234")
    r = client.post("/ui/pin/verify", data={"pin": "1234"}, follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    # The session cookie now carries pin_ok, so a content page loads without the
    # pin redirect. (/ui/ itself now redirects to the Glance home, gg33.)
    r2 = client.get("/ui/inventory", follow_redirects=False)
    assert r2.status_code == 200


def test_no_lock_when_pin_unset(client, monkeypatch):
    monkeypatch.setattr(settings, "kiosk_pin", "")
    # /ui/ now redirects to the Glance home; a content page shows the 200.
    r = client.get("/ui/inventory", follow_redirects=False)
    assert r.status_code == 200
