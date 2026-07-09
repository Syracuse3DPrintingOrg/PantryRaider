"""Back up to Forager, app side (FoodAssistant-kzjz).

Covers the Backups pane gating (the Forager controls render only when the
install is linked, and are unhidden only for a Premium account via the status
endpoint), the push path (the app zips its data dir with the existing builder
and POSTs it to the cloud), the pull path (download from the cloud runs through
the existing guarded restore, which still needs the device password), and the
fail-soft behaviour when not linked, not Premium, or the cloud is down. All HTTP
is mocked; no network, matching the cloud contract without importing cloud/.
"""
from __future__ import annotations

import io
import json
import os
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

_SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(_SERVICE))

from app.config import settings  # noqa: E402


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app

    cwd = os.getcwd()
    os.chdir(_SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "auth_password", "")
    monkeypatch.setattr(settings, "cloud_base_url", "https://cloud.test")
    monkeypatch.setattr(settings, "deployment_mode", "server")
    # A little data so the backup builder has something to zip.
    (tmp_path / "settings.json").write_text(json.dumps({"app_name": "x"}))
    try:
        with patch.object(type(settings), "is_configured", lambda self: True):
            yield TestClient(app)
    finally:
        os.chdir(cwd)


class _FakeAsyncClient:
    """Stand-in for httpx.AsyncClient serving canned cloud replies."""

    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kwargs):
        self.post_url, self.post_kwargs = url, kwargs
        if self._error:
            raise self._error
        return self._response

    async def get(self, url, **kwargs):
        self.get_url, self.get_kwargs = url, kwargs
        if self._error:
            raise self._error
        return self._response


def _resp(status, *, body=None, content=None):
    req = httpx.Request("GET", "https://cloud.test")
    if content is not None:
        return httpx.Response(status, content=content, request=req)
    return httpx.Response(status, json=body or {}, request=req)


def _patch(fake):
    from app.routers import setup as setup_router
    return patch.object(setup_router.httpx, "AsyncClient", fake)


def _valid_backup_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("foodassistant-data/settings.json",
                    json.dumps({"app_name": "restored"}))
    return buf.getvalue()


# --- pane gating -----------------------------------------------------------

def test_pane_hides_forager_backup_when_not_linked(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "")
    with patch.object(type(settings), "is_configured", lambda self: True):
        html = client.get("/setup").text
    assert 'id="forager-backup-panel"' not in html


def test_pane_renders_forager_backup_when_linked(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_tok")
    with patch.object(type(settings), "is_configured", lambda self: True):
        html = client.get("/setup").text
    # The panel renders (hidden) when linked; JS reveals it only for Premium.
    assert 'id="forager-backup-panel"' in html
    assert 'foragerBackupNow' in html
    assert 'foragerRestore' in html
    assert "prc_tok" not in html  # the token never renders into the page


def test_status_reports_premium_when_the_cloud_lists(client, monkeypatch):
    # The cloud's own gate is the truth: /v1/backup/list answering 200 means
    # Premium, and the app relays the stored backups.
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_tok")
    fake = _FakeAsyncClient(_resp(200, body={"backups": [
        {"id": 5, "filename": "b.zip", "size_bytes": 1024,
         "created_at": "2026-07-07T12:00:00+00:00"}]}))
    with _patch(fake):
        d = client.get("/setup/cloud/backup/status").json()
    assert fake.get_url == "https://cloud.test/v1/backup/list"
    assert d["linked"] and d["premium"] is True
    assert d["backups"][0]["id"] == 5


def test_status_reports_not_premium_on_402(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_tok")
    fake = _FakeAsyncClient(_resp(402, body={"detail": {"error": "premium_required"}}))
    with _patch(fake):
        d = client.get("/setup/cloud/backup/status").json()
    assert d["linked"] is True and d["premium"] is False
    assert d["backups"] == []


def test_status_unlinked(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "")
    d = client.get("/setup/cloud/backup/status").json()
    assert d == {"ok": True, "linked": False, "premium": False, "backups": []}


def test_status_survives_unreachable_cloud(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_tok")
    fake = _FakeAsyncClient(error=httpx.ConnectError("down"))
    with _patch(fake):
        r = client.get("/setup/cloud/backup/status")
    d = r.json()
    assert r.status_code == 200
    assert d["linked"] and d["reachable"] is False and d["premium"] is False
    assert "prc_tok" not in json.dumps(d)  # token never leaks into the error


# --- push ------------------------------------------------------------------

def test_upload_posts_the_zip_to_the_cloud(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_tok")
    fake = _FakeAsyncClient(_resp(200, body={
        "id": 9, "filename": "backup-x.zip", "size_bytes": 200,
        "created_at": "2026-07-07T12:00:00+00:00"}))
    with _patch(fake):
        d = client.post("/setup/cloud/backup/upload").json()
    assert d["ok"] is True and d["backup"]["id"] == 9
    assert fake.post_url == "https://cloud.test/v1/backup/upload"
    # The existing builder's zip rides as a multipart file part.
    files = fake.post_kwargs["files"]
    assert "file" in files
    name, blob, mime = files["file"]
    assert name.endswith(".zip") and mime == "application/zip"
    assert zipfile.ZipFile(io.BytesIO(blob))  # a real zip
    assert fake.post_kwargs["headers"]["Authorization"] == "Bearer prc_tok"


def test_upload_refused_when_not_premium(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_tok")
    fake = _FakeAsyncClient(_resp(402, body={"detail": {"error": "premium_required"}}))
    with _patch(fake):
        d = client.post("/setup/cloud/backup/upload").json()
    assert d["ok"] is False and d["premium"] is False
    assert "Premium" in d["error"]


def test_upload_not_linked_is_honest(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "")
    d = client.post("/setup/cloud/backup/upload").json()
    assert d["ok"] is False and "not linked" in d["error"]


def test_upload_survives_unreachable_cloud(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_tok")
    fake = _FakeAsyncClient(error=httpx.ConnectError("down"))
    with _patch(fake):
        d = client.post("/setup/cloud/backup/upload").json()
    assert d["ok"] is False
    assert "could not be reached" in d["error"]
    assert "local backup" in d["error"]  # points at the still-working fallback


# --- pull (restore) --------------------------------------------------------

def test_restore_downloads_and_runs_the_guarded_restore(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_tok")
    fake = _FakeAsyncClient(_resp(200, content=_valid_backup_zip()))
    with _patch(fake):
        d = client.post("/setup/cloud/backup/restore", json={}).json()
    assert d["ok"] is True
    assert d["restored_files"] == 1
    # Latest is pulled when no id is given.
    assert fake.get_url == "https://cloud.test/v1/backup/latest"
    # The restore actually rewrote the data dir with the archive's contents.
    saved = json.loads((Path(settings.data_dir) / "settings.json").read_text())
    assert saved["app_name"] == "restored"


def test_restore_of_a_specific_backup_uses_its_id(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_tok")
    fake = _FakeAsyncClient(_resp(200, content=_valid_backup_zip()))
    with _patch(fake):
        d = client.post("/setup/cloud/backup/restore", json={"backup_id": 42}).json()
    assert d["ok"] is True
    assert fake.get_url == "https://cloud.test/v1/backup/download/42"


def test_restore_requires_the_device_password(client, monkeypatch):
    # The password gate is enforced before any download, so a wrong password
    # changes nothing and never reaches the cloud.
    from app.passwords import hash_secret
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_tok")
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "totp_secret", "")
    monkeypatch.setattr(settings, "auth_password", hash_secret("correct-pass"))
    # Authenticate the session (the route sits behind auth), then send a wrong
    # backup password so only the restore gate refuses.
    client.post("/ui/login", data={"password": "correct-pass"},
                follow_redirects=False)
    fake = _FakeAsyncClient(_resp(200, content=_valid_backup_zip()))
    with _patch(fake):
        d = client.post("/setup/cloud/backup/restore",
                        json={"restore_password": "wrong"}).json()
    assert d["ok"] is False
    assert "current password" in d["error"]
    assert not hasattr(fake, "get_url")  # never downloaded


def test_restore_survives_unreachable_cloud(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_tok")
    fake = _FakeAsyncClient(error=httpx.ConnectError("down"))
    with _patch(fake):
        d = client.post("/setup/cloud/backup/restore", json={}).json()
    assert d["ok"] is False
    assert "could not be reached" in d["error"]
