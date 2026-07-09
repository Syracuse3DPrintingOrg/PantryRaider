"""The optional viewer password (RBAC-lite, security review Jul 2026).

A second password opens a kitchen-only session: every normal page and kitchen
action works, but the settings surface (/setup) and the admin surface (/admin)
require the main password. A blank viewer password turns the feature off.
"""
import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.passwords import hash_secret  # noqa: E402


@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        # client_host defaults to "testclient", NOT loopback, so the loopback
        # trust path does not mask the role checks under test.
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _configured(monkeypatch, admin="hunter2", viewer="kitchen"):
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "totp_secret", "", raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret(admin), raising=False)
    monkeypatch.setattr(settings, "viewer_password",
                        hash_secret(viewer) if viewer else "", raising=False)


def _login(client, password):
    return client.post("/ui/login", data={"password": password},
                       follow_redirects=False)


def test_viewer_login_opens_a_session(client, monkeypatch):
    _configured(monkeypatch)
    r = _login(client, "kitchen")
    assert r.status_code in (302, 303, 307)
    # Kitchen surfaces work: the shared timers API answers.
    assert client.get("/timers").status_code == 200


def test_viewer_cannot_reach_settings_or_admin(client, monkeypatch):
    _configured(monkeypatch)
    _login(client, "kitchen")
    # The settings page bounces a viewer's browser back to the app.
    r = client.get("/setup", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert "/ui" in r.headers.get("location", "")
    # Writes and downloads are refused outright.
    assert client.post("/setup/save", json={"ui_theme": "dark"}).status_code == 403
    assert client.get("/admin/backup").status_code == 403
    assert client.post("/admin/restore").status_code == 403
    assert client.post("/setup/deployment/to-satellite", json={}).status_code == 403


def test_viewer_password_does_not_match_admin_paths_after_logout(client, monkeypatch):
    _configured(monkeypatch)
    _login(client, "kitchen")
    client.get("/ui/logout", follow_redirects=False)
    # Session gone: back to unauthorized like any anonymous client.
    assert client.get("/timers").status_code == 401


def test_admin_login_is_unaffected(client, monkeypatch):
    _configured(monkeypatch)
    r = _login(client, "hunter2")
    assert r.status_code in (302, 303, 307)
    assert client.get("/timers").status_code == 200
    assert client.get("/setup").status_code == 200


def test_blank_viewer_password_turns_the_feature_off(client, monkeypatch):
    _configured(monkeypatch, viewer="")
    r = _login(client, "kitchen")
    assert r.status_code == 401
    assert client.get("/timers").status_code == 401


def test_wrong_password_still_fails(client, monkeypatch):
    _configured(monkeypatch)
    assert _login(client, "nope").status_code == 401


def test_api_key_keeps_full_access(client, monkeypatch):
    """API keys drive HA automations and satellite syncs: they stay admin."""
    _configured(monkeypatch)
    monkeypatch.setattr(settings, "api_key", "sesame", raising=False)
    r = client.post("/setup/test/grocy", json={},
                    headers={"X-API-Key": "sesame"}, follow_redirects=False)
    assert r.status_code not in (302, 303, 307, 401, 403)
