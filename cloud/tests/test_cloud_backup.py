"""Cloud backup: Premium gate, size cap, retention, and IDOR scoping."""
import io

import pytest

from app.config import settings
from app.database import SessionLocal
from app.models import Account, CloudBackup
from tests.conftest import activate_entitlement


@pytest.fixture(autouse=True)
def backup_dir(tmp_path, monkeypatch):
    """Point cloud backup storage at a throwaway dir and shrink the caps so the
    size and retention tests are cheap and deterministic."""
    monkeypatch.setattr(settings, "backup_storage_dir", str(tmp_path / "backups"))
    monkeypatch.setattr(settings, "backup_max_bytes", 1000)
    monkeypatch.setattr(settings, "backup_retention_count", 3)
    yield


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _zip(nbytes=50, marker=b"data"):
    body = marker + b"x" * max(0, nbytes - len(marker))
    return {"file": ("backup.zip", io.BytesIO(body), "application/zip")}


def _second_account(client):
    """A second signed-up + paired account, for the IDOR checks. Returns its
    instance token."""
    resp = client.post("/v1/accounts/signup",
                       json={"email": "eve@example.com", "password": "hunter2222"})
    session = resp.json()["session_token"]
    code = client.post("/v1/pairing/code", headers=_auth(session))
    redeem = client.post("/v1/pairing/redeem",
                         json={"code": code.json()["code"], "name": "Eve Pi"})
    return redeem.json()["instance_token"]


# --- Premium gate ---------------------------------------------------------

def test_premium_can_upload_list_download(client, instance_token):
    activate_entitlement(plan="premium")
    up = client.post("/v1/backup/upload", files=_zip(marker=b"hello"),
                     headers=_auth(instance_token))
    assert up.status_code == 200
    bid = up.json()["id"]
    assert up.json()["size_bytes"] > 0

    lst = client.get("/v1/backup/list", headers=_auth(instance_token))
    assert lst.status_code == 200
    ids = [b["id"] for b in lst.json()["backups"]]
    assert ids == [bid]

    dl = client.get(f"/v1/backup/download/{bid}", headers=_auth(instance_token))
    assert dl.status_code == 200
    assert dl.content.startswith(b"hello")

    latest = client.get("/v1/backup/latest", headers=_auth(instance_token))
    assert latest.status_code == 200
    assert latest.content.startswith(b"hello")


def test_trial_account_is_refused(client, instance_token):
    # A fresh account is on the trial (premium quota but plan "trial"): not
    # Premium, so cloud backup is refused with the upgrade message.
    up = client.post("/v1/backup/upload", files=_zip(),
                     headers=_auth(instance_token))
    assert up.status_code == 402
    assert "Premium" in up.json()["detail"]["message"]
    assert client.get("/v1/backup/list",
                      headers=_auth(instance_token)).status_code == 402


def test_basic_account_is_refused(client, instance_token):
    # Cloud Basic is a paid plan but not Premium, so it cannot back up either.
    activate_entitlement(plan="basic")
    up = client.post("/v1/backup/upload", files=_zip(),
                     headers=_auth(instance_token))
    assert up.status_code == 402
    assert up.json()["detail"]["error"] == "premium_required"


def test_requires_instance_token(client):
    assert client.get("/v1/backup/list").status_code == 401
    assert client.post("/v1/backup/upload", files=_zip()).status_code == 401


# --- Size cap + retention -------------------------------------------------

def test_size_cap_rejects_large_upload(client, instance_token):
    activate_entitlement(plan="premium")
    big = client.post("/v1/backup/upload", files=_zip(nbytes=2000),
                      headers=_auth(instance_token))
    assert big.status_code == 413
    assert big.json()["detail"]["error"] == "too_large"


def test_retention_evicts_oldest(client, instance_token):
    activate_entitlement(plan="premium")
    ids = []
    for i in range(4):
        up = client.post("/v1/backup/upload",
                         files=_zip(marker=f"n{i}".encode()),
                         headers=_auth(instance_token))
        assert up.status_code == 200
        ids.append(up.json()["id"])

    lst = client.get("/v1/backup/list", headers=_auth(instance_token))
    kept = [b["id"] for b in lst.json()["backups"]]
    # Only the newest three survive; the first upload was evicted.
    assert kept == ids[:0:-1]  # ids[3], ids[2], ids[1]
    assert ids[0] not in kept

    # The evicted backup's file and row are both gone.
    gone = client.get(f"/v1/backup/download/{ids[0]}",
                      headers=_auth(instance_token))
    assert gone.status_code == 404
    db = SessionLocal()
    try:
        assert db.query(CloudBackup).count() == 3
    finally:
        db.close()


# --- IDOR scoping ---------------------------------------------------------

def test_cannot_list_or_download_another_account(client, instance_token):
    activate_entitlement(plan="premium")
    # Account one stores a backup.
    up = client.post("/v1/backup/upload", files=_zip(marker=b"mine"),
                     headers=_auth(instance_token))
    bid = up.json()["id"]

    # Account two is also Premium but must never see account one's backup.
    eve = _second_account(client)
    db = SessionLocal()
    try:
        eve_acct = db.query(Account).filter_by(email="eve@example.com").first().id
    finally:
        db.close()
    activate_entitlement(account_email="eve@example.com", plan="premium")

    eve_list = client.get("/v1/backup/list", headers=_auth(eve))
    assert eve_list.status_code == 200
    assert eve_list.json()["backups"] == []  # scoped to eve, sees nothing

    # A direct fetch by id answers 404 (same as not existing), not 200.
    stolen = client.get(f"/v1/backup/download/{bid}", headers=_auth(eve))
    assert stolen.status_code == 404
    # And eve cannot delete it either.
    assert client.delete(f"/v1/backup/{bid}", headers=_auth(eve)).status_code == 404
    # Account one still has it.
    assert client.get(f"/v1/backup/download/{bid}",
                      headers=_auth(instance_token)).status_code == 200
    assert eve_acct  # sanity: the second account really exists


def test_delete_removes_backup(client, instance_token):
    activate_entitlement(plan="premium")
    up = client.post("/v1/backup/upload", files=_zip(),
                     headers=_auth(instance_token))
    bid = up.json()["id"]
    dele = client.delete(f"/v1/backup/{bid}", headers=_auth(instance_token))
    assert dele.status_code == 200
    assert client.get("/v1/backup/list",
                      headers=_auth(instance_token)).json()["backups"] == []
