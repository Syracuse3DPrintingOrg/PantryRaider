"""Tests for read-only DEMO MODE (FoodAssistant-pxp0).

When settings.demo_mode is on, the app is a public, fully-navigable demo that
refuses every state-changing request: GET pages still render (and show the demo
banner), while POST/PUT/PATCH/DELETE are blocked before they reach a route, so
the demo's Grocy/Mealie data is never written. When demo_mode is off the
middleware is a no-op and no banner appears.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings, Settings, _SAVEABLE  # noqa: E402
from app.services import demo  # noqa: E402


# --------------------------------------------------------------------------
# Pure helpers (is_mutating_request / is_allowed_in_demo)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["POST", "put", "Patch", "DELETE"])
def test_mutating_methods(method):
    assert demo.is_mutating_method(method) is True


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", ""])
def test_non_mutating_methods(method):
    assert demo.is_mutating_method(method) is False


def test_allowlist_membership():
    assert demo.is_allowed_in_demo("/ui/login") is True
    assert demo.is_allowed_in_demo("/ui/pin/verify") is True
    assert demo.is_allowed_in_demo("/setup/kiosk/activity") is True
    assert demo.is_allowed_in_demo("/setup/save") is False
    assert demo.is_allowed_in_demo("/ui/consume/1") is False


def test_is_blocked_in_demo():
    # Reads are never blocked.
    assert demo.is_blocked_in_demo("GET", "/ui/consume/1") is False
    assert demo.is_blocked_in_demo("GET", "/setup/save") is False
    # Writes to non-allowlisted paths are blocked.
    assert demo.is_blocked_in_demo("POST", "/ui/consume/1") is True
    assert demo.is_blocked_in_demo("POST", "/setup/save") is True
    assert demo.is_blocked_in_demo("DELETE", "/admin/restore") is True
    # Writes to allowlisted paths pass.
    assert demo.is_blocked_in_demo("POST", "/ui/login") is False
    assert demo.is_blocked_in_demo("POST", "/setup/kiosk/activity") is False


# --------------------------------------------------------------------------
# Config: env-only flag, off by default, cannot be saved
# --------------------------------------------------------------------------

def test_demo_mode_off_by_default():
    assert settings.demo_mode is False


@pytest.mark.parametrize("env", ["DEMO_MODE", "FOODASSISTANT_DEMO_MODE"])
def test_demo_mode_env_override(monkeypatch, env):
    monkeypatch.setenv(env, "true")
    s = Settings()
    assert s.demo_mode is True


def test_demo_mode_not_saveable():
    # The safety guarantee: demo_mode is deliberately absent from _SAVEABLE, so
    # the settings-save path can never turn it on or off from inside the demo.
    assert "demo_mode" not in _SAVEABLE


def test_demo_mode_cannot_be_flipped_via_apply(monkeypatch):
    # Even a crafted save payload naming demo_mode is ignored by apply(), because
    # apply() only writes keys in _SAVEABLE.
    monkeypatch.setattr(settings, "demo_mode", False)
    settings.apply({"demo_mode": True})
    assert settings.demo_mode is False


# --------------------------------------------------------------------------
# Middleware behavior
# --------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    # A configured satellite so the setup-redirect never interferes, and auth
    # off so requests reach the demo middleware (which runs regardless of auth).
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    monkeypatch.setattr(settings, "remote_server_url", "http://server:9284")
    monkeypatch.setattr(settings, "upstream_api_key", "shared-key")
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "auth_password", "")
    monkeypatch.setattr(settings, "kiosk_pin", "")
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_demo_off_get_has_no_banner(client, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", False)
    r = client.get("/ui/")
    assert r.status_code == 200
    assert "exploring a live Pantry Raider demo" not in r.text


def test_demo_off_post_not_demo_blocked(client, monkeypatch):
    # With demo off the middleware is a no-op: a write is never answered with the
    # demo error body (it reaches the normal request pipeline instead).
    monkeypatch.setattr(settings, "demo_mode", False)
    r = client.post("/setup/save", json={"ui_theme": "dark"},
                    follow_redirects=False)
    assert b"demo_read_only" not in r.content


def test_demo_on_get_renders_with_banner(client, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    # The banner is part of the normal page chrome; /ui/ now redirects to the
    # chrome-free Glance home, so check a content page (FoodAssistant-gg33).
    r = client.get("/ui/inventory")
    assert r.status_code == 200
    assert "exploring a live Pantry Raider demo" in r.text


def test_demo_on_json_post_refused(client, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    r = client.post("/ui/consume/1", data={"amount": "1"},
                    headers={"Accept": "application/json"},
                    follow_redirects=False)
    assert r.status_code == 403
    body = r.json()
    assert body["error"] == "demo_read_only"
    assert "demo" in body["message"].lower()


def test_demo_on_html_post_redirects_with_message(client, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    r = client.post("/ui/consume/1", data={"amount": "1"},
                    headers={"Accept": "text/html",
                             "Referer": "http://testserver/ui/inventory"},
                    follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert "/ui/inventory" in loc
    assert "msg" in loc


def test_demo_on_settings_save_blocked(client, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    r = client.post("/setup/save", json={"ui_theme": "light"},
                    headers={"Accept": "application/json"},
                    follow_redirects=False)
    assert r.status_code == 403
    assert r.json()["error"] == "demo_read_only"


def test_demo_on_admin_write_blocked(client, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    r = client.post("/admin/restore",
                    headers={"Accept": "application/json"},
                    follow_redirects=False)
    assert r.status_code == 403
    assert r.json()["error"] == "demo_read_only"


def test_demo_on_allowlisted_post_passes(client, monkeypatch):
    # The kiosk display-wake ping is display-only, so it is not demo-blocked.
    monkeypatch.setattr(settings, "demo_mode", True)
    r = client.post("/setup/kiosk/activity", json={},
                    headers={"Accept": "application/json"},
                    follow_redirects=False)
    assert b"demo_read_only" not in r.content


def test_demo_on_get_never_blocked(client, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    # A representative read endpoint still answers normally.
    r = client.get("/health")
    assert r.status_code == 200
