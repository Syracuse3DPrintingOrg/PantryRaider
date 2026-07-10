"""Local login stays a reliable, clearly-separate fallback from Forager
(FoodAssistant-vg1f).

Covers: the device-password login path succeeds with the Forager server down;
the risky Forager-only-access detector; and that the login page and the
Forager/Security settings panes carry the separation copy.
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.passwords import hash_secret  # noqa: E402

_PW = "kitchen-secret"


@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "gk", raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret(_PW), raising=False)
    monkeypatch.setattr(settings, "viewer_password", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "local_totp_enabled", False, raising=False)
    monkeypatch.setattr(settings, "local_totp_secret", "", raising=False)
    monkeypatch.setattr(settings, "totp_secret", "", raising=False)
    monkeypatch.setattr(settings, "tunnel_enabled", False, raising=False)
    monkeypatch.setattr(settings, "cloud_instance_token", "", raising=False)
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


# --- offline local login ---------------------------------------------------

def test_local_login_works_with_forager_down(client, monkeypatch):
    """Linked to Forager, but the cloud is unreachable: the device password
    still opens a session because that path never touches the cloud."""
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_token", raising=False)
    monkeypatch.setattr(settings, "cloud_base_url", "https://cloud.test", raising=False)

    def _boom(*a, **k):
        raise httpx.ConnectError("no route to cloud")

    # Any cloud call would blow up; the local path must not make one.
    with patch("app.routers.ui.httpx.AsyncClient", side_effect=_boom):
        r = client.post("/ui/login", data={"mode": "local", "password": _PW},
                        follow_redirects=False)
    assert r.status_code == 303
    # A protected page is now reachable with the session cookie.
    assert client.get("/setup", follow_redirects=False).status_code == 200


# --- risky-state detector --------------------------------------------------

def test_forager_only_risk_flagged(monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_token", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    assert settings.forager_only_login_risk() is True
    assert settings.has_reliable_local_login() is False


def test_no_risk_when_device_password_set(monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_token", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret(_PW), raising=False)
    assert settings.forager_only_login_risk() is False
    assert settings.has_reliable_local_login() is True


def test_no_risk_when_not_linked(monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    assert settings.forager_only_login_risk() is False


def test_reliable_when_auth_delegated(monkeypatch):
    # Auth handed to an outer layer: no device password needed, no risk.
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_token", raising=False)
    assert settings.has_reliable_local_login() is True
    assert settings.forager_only_login_risk() is False


# --- login page copy -------------------------------------------------------

def test_login_page_shows_separation_copy_when_linked(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_token", raising=False)
    body = client.get("/ui/login").text
    assert "Sign in with Forager" in body
    assert "always works" in body
    assert "even if Forager is offline" in body


def test_login_page_no_forager_copy_when_unlinked(client):
    body = client.get("/ui/login").text
    assert "Sign in with Forager" not in body
    assert "even if Forager is offline" not in body


# --- settings pane notes ---------------------------------------------------

def _authed(client):
    client.post("/ui/login", data={"mode": "local", "password": _PW},
                follow_redirects=False)


def test_forager_pane_keeps_device_password_note(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_token", raising=False)
    _authed(client)
    body = client.get("/setup").text
    assert "separate from your device" in body


def test_security_pane_note_when_linked(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_token", raising=False)
    _authed(client)
    body = client.get("/setup").text
    assert "Separate from Forager sign-in" in body


def test_panes_carry_the_risky_state_warning_copy():
    # The risky-state warning (shown when forager_only_login_risk() is true) is a
    # {% if %} branch that mirrors the tested detector. That state also leaves the
    # install un-configured, so it cannot be reached through a live /setup render;
    # guard the copy at the source so it is not silently dropped.
    panes = SERVICE / "app" / "templates" / "setup"
    for name in ("_pane_forager.html", "_pane_security.html"):
        src = (panes / name).read_text()
        assert "forager_only_login_risk()" in src
        assert "lock" in src.lower()
