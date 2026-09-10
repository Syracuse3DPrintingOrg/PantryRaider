"""Password gate on the settings backup download (FoodAssistant-16cj).

GET /admin/backup used to stream a zip of the app data (the SQLite database and,
with include_secrets, the raw API keys and passwords) to any authenticated
session, so a walk-up at an already-open Settings page could exfiltrate every
secret. The download is now a POST that re-confirms the current app password.

These tests drive the endpoint through TestClient (authenticated as the admin)
and assert: no password is refused with no zip bytes, a wrong password is
refused, the correct password streams a real zip, and an open install (no
auth_password) still streams without a password rather than locking anyone out.

The support bundle is deliberately not gated: test_support_bundle already proves
it blanks every SECRET_SETTING_KEYS field by name and scrubs all text by value,
so it cannot carry a secret and needs no password. A smoke check of that here.

Run: python -m pytest tests/test_backup_gate.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.passwords import hash_secret  # noqa: E402
import app.routers.admin as admin  # noqa: E402

_ZIP_MAGIC = b"PK\x03\x04"
PASSWORD = "hunter2-long-enough"


@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    data = tmp_path / "data"
    data.mkdir()
    (data / "settings.json").write_text('{"grocy_base_url": "http://grocy.test"}')
    monkeypatch.setattr(settings, "data_dir", str(data), raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        # host "testclient" is not loopback, so the middleware's loopback trust
        # does not mask the admin auth the download sits behind.
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _configured(monkeypatch, password=PASSWORD):
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "totp_secret", "", raising=False)
    monkeypatch.setattr(settings, "auth_password",
                        hash_secret(password) if password else "", raising=False)


def _login(client, password):
    return client.post("/ui/login", data={"password": password},
                       follow_redirects=False)


# --- unit tests on the gate helper ------------------------------------------

def test_gate_open_install_never_raises(monkeypatch):
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    # No password configured: nothing to verify, so any input passes.
    admin._require_backup_password("")
    admin._require_backup_password("anything")


def test_gate_wrong_password_raises(monkeypatch):
    monkeypatch.setattr(settings, "auth_password", hash_secret(PASSWORD), raising=False)
    for bad in ("", "nope", PASSWORD + "x"):
        with pytest.raises(HTTPException) as ei:
            admin._require_backup_password(bad)
        assert ei.value.status_code == 403
        assert "password" in ei.value.detail.lower()


def test_gate_correct_password_passes(monkeypatch):
    monkeypatch.setattr(settings, "auth_password", hash_secret(PASSWORD), raising=False)
    admin._require_backup_password(PASSWORD)  # does not raise


# --- integration tests through the endpoint ---------------------------------

def test_backup_without_password_refused(client, monkeypatch):
    _configured(monkeypatch)
    _login(client, PASSWORD)
    r = client.post("/admin/backup", data={"include_secrets": "true"})
    assert r.status_code == 403
    assert _ZIP_MAGIC not in r.content  # streamed nothing


def test_backup_wrong_password_refused(client, monkeypatch):
    _configured(monkeypatch)
    _login(client, PASSWORD)
    r = client.post("/admin/backup",
                    data={"backup_password": "wrong", "include_secrets": "true"})
    assert r.status_code == 403
    assert _ZIP_MAGIC not in r.content


def test_backup_correct_password_succeeds(client, monkeypatch):
    _configured(monkeypatch)
    _login(client, PASSWORD)
    r = client.post("/admin/backup",
                    data={"backup_password": PASSWORD, "include_secrets": "true"})
    assert r.status_code == 200
    assert r.content.startswith(_ZIP_MAGIC)


def test_backup_open_install_streams_without_password(client, monkeypatch):
    _configured(monkeypatch, password="")  # no auth_password: open install
    # is_configured must be true or the setup-redirect intercepts /admin.
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.post("/admin/backup", data={})
    assert r.status_code == 200
    assert r.content.startswith(_ZIP_MAGIC)


def test_support_bundle_needs_no_password_and_hides_secrets(client, monkeypatch):
    secret = "sk-supersecret-abcdef123456"
    _configured(monkeypatch)
    monkeypatch.setattr(settings, "gemini_api_key", secret, raising=False)
    _login(client, PASSWORD)
    # No password field is sent: the bundle is redacted, not gated.
    r = client.get("/admin/support-bundle")
    assert r.status_code == 200
    assert r.content.startswith(_ZIP_MAGIC)
    assert secret.encode() not in r.content
