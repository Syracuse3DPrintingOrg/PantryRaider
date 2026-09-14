"""Tests for read-only kiosk mode while PIN-locked (FoodAssistant-yxu).

When kiosk_readonly_when_locked is True and a kiosk PIN is set, unauthenticated
GET requests pass through (read-only browsing), while write methods are blocked
with 403. When the flag is False the normal PIN-gate redirect applies.
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
    # Configured satellite so the setup-redirect does not interfere.
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    monkeypatch.setattr(settings, "remote_server_url", "http://server:9284")
    monkeypatch.setattr(settings, "upstream_api_key", "shared-key")
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "auth_password", "")
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_readonly_flag_off_unauthenticated_get_redirects(client, monkeypatch):
    """With readonly mode disabled, unauthenticated GET redirects to /ui/pin."""
    monkeypatch.setattr(settings, "kiosk_pin", "1234")
    monkeypatch.setattr(settings, "kiosk_readonly_when_locked", False)
    r = client.get("/ui/", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert "/ui/pin" in r.headers["location"]


def test_readonly_flag_on_unauthenticated_get_passes(client, monkeypatch):
    """With readonly mode enabled, unauthenticated GET returns 200 (passes through)."""
    monkeypatch.setattr(settings, "kiosk_pin", "1234")
    monkeypatch.setattr(settings, "kiosk_readonly_when_locked", True)
    # /ui/ now redirects to the Glance home; use a content page to see the
    # middleware pass-through as a 200 (FoodAssistant-gg33).
    r = client.get("/ui/inventory", follow_redirects=False)
    assert r.status_code == 200


def test_readonly_flag_on_unauthenticated_post_returns_403(client, monkeypatch):
    """With readonly mode enabled, unauthenticated POST is rejected with 403."""
    monkeypatch.setattr(settings, "kiosk_pin", "1234")
    monkeypatch.setattr(settings, "kiosk_readonly_when_locked", True)
    # POST to a write endpoint under /ui (not in the bypass list) must be blocked.
    # /ui/consume/1 is a real route handled after the middleware runs.
    r = client.post("/ui/consume/1", data={"amount": "1"}, follow_redirects=False)
    assert r.status_code == 403


def test_readonly_flag_on_no_pin_no_effect(client, monkeypatch):
    """With readonly mode enabled but no PIN set, no gate applies -- GET returns 200."""
    monkeypatch.setattr(settings, "kiosk_pin", "")
    monkeypatch.setattr(settings, "kiosk_readonly_when_locked", True)
    # /ui/ now redirects to the Glance home; a content page shows the 200.
    r = client.get("/ui/inventory", follow_redirects=False)
    assert r.status_code == 200


def test_readonly_pin_bypass_paths_still_work(client, monkeypatch):
    """PIN page and login are always reachable regardless of readonly mode."""
    monkeypatch.setattr(settings, "kiosk_pin", "1234")
    monkeypatch.setattr(settings, "kiosk_readonly_when_locked", True)
    r = client.get("/ui/pin", follow_redirects=False)
    # /ui/pin is in the bypass list so it must be reachable (200 or redirect away
    # if pin_lock_active returns false for some reason, but here it is active so
    # it shows the pin form).
    assert r.status_code == 200
