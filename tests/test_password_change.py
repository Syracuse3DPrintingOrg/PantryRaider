"""Changing an existing app password requires the current one (FoodAssistant-f403)."""
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
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _save(client, body):
    # Loopback is trusted, so this reaches the handler without a session.
    return client.post("/setup/save", json=body)


def test_first_password_needs_no_current(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    saved = {}
    monkeypatch.setattr(type(settings), "save", lambda self, d: saved.update(d), raising=False)
    r = _save(client, {"auth_password": "first-pass-123"})
    assert r.json().get("ok") is not False
    assert "auth_password" in saved


def test_changing_password_requires_matching_current(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_password", hash_secret("oldpass123"), raising=False)
    monkeypatch.setattr(type(settings), "save", lambda self, d: None, raising=False)
    # Wrong current password is rejected, nothing saved.
    r = _save(client, {"auth_password": "newpass456", "current_password": "wrong"})
    assert r.json().get("ok") is False
    r = _save(client, {"auth_password": "newpass456"})   # missing current
    assert r.json().get("ok") is False


def test_correct_current_allows_change(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_password", hash_secret("oldpass123"), raising=False)
    saved = {}
    monkeypatch.setattr(type(settings), "save", lambda self, d: saved.update(d), raising=False)
    r = _save(client, {"auth_password": "newpass456", "current_password": "oldpass123"})
    assert r.json().get("ok") is not False
    assert saved.get("auth_password")


def test_other_settings_save_without_current_password(client, monkeypatch):
    # A pane that does not touch the password must still save with a password set.
    monkeypatch.setattr(settings, "auth_password", hash_secret("oldpass123"), raising=False)
    saved = {}
    monkeypatch.setattr(type(settings), "save", lambda self, d: saved.update(d), raising=False)
    r = _save(client, {"screensaver_minutes": 5})
    assert r.json().get("ok") is not False
    assert saved.get("screensaver_minutes") == 5
