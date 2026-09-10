"""Restore-from-backup tests (FoodAssistant-bkpp).

Covers the app-data restore that mirrors GET /admin/backup: round-trip,
secret-preserve-on-blank for a redacted backup, and the zip-slip / bad-archive
guards. Also pins what a redacted backup must never carry (the copies
settings.json spawns, unscrubbed logs), where the pre-restore safety copy lives,
and the settings a restore always keeps from the device it runs on.
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException

import app.routers.admin as admin
from app.config import settings


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(settings, "data_dir", str(d), raising=False)
    return d


def _write_settings(d: Path, values: dict) -> None:
    (d / "settings.json").write_text(json.dumps(values, indent=2))


def test_restore_round_trip(data_dir):
    _write_settings(data_dir, {"grocy_base_url": "http://original", "staple_items": "miso"})
    (data_dir / "staples.txt").write_text("nori\n")
    zip_bytes, _ = admin._build_zip(include_secrets=True)

    # Drift the live state away from the backup, then restore.
    settings.apply({"grocy_base_url": "http://changed", "staple_items": "changed"})
    out = admin._restore_zip(zip_bytes)

    assert out["ok"] is True
    assert out["restored_files"] >= 2
    assert settings.grocy_base_url == "http://original"
    assert settings.staple_items == "miso"
    # The snapshot of the pre-restore data dir exists alongside it.
    assert out["snapshot"] and Path(out["snapshot"]).exists()


def test_restore_preserves_secret_when_backup_is_redacted(data_dir):
    # A redacted backup blanks secrets; a working key must not be wiped.
    _write_settings(data_dir, {"gemini_api_key": "should-be-stripped", "grocy_base_url": "http://x"})
    redacted_zip, _ = admin._build_zip(include_secrets=False)

    settings.apply({"gemini_api_key": "live-key"})
    out = admin._restore_zip(redacted_zip)

    assert out["secrets_preserved"] >= 1
    assert settings.gemini_api_key == "live-key"
    saved = json.loads((data_dir / "settings.json").read_text())
    assert saved["gemini_api_key"] == "live-key"


def _zip_with(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in members.items():
            zf.writestr(name, body)
    return buf.getvalue()


def test_safe_members_rejects_zip_slip(data_dir):
    z = _zip_with({
        "foodassistant-data/settings.json": "{}",
        "foodassistant-data/../evil.txt": "pwned",
    })
    zf = zipfile.ZipFile(io.BytesIO(z))
    resolved = admin._safe_members(zf, data_dir)
    names = [arc for arc, _ in resolved]
    assert "foodassistant-data/settings.json" in names
    assert all("evil" not in n for n in names)


def test_restore_rejects_non_backup_zip(data_dir):
    z = _zip_with({"random/other.txt": "nope"})
    with pytest.raises(HTTPException) as ei:
        admin._restore_zip(z)
    assert ei.value.status_code == 400


def test_restore_rejects_bad_zip(data_dir):
    with pytest.raises(HTTPException) as ei:
        admin._restore_zip(b"not a zip at all")
    assert ei.value.status_code == 400


# --- What a redacted backup may carry ---------------------------------------

SECRET = "sk-live-abcdef0123456789"


def _members(zip_bytes: bytes) -> dict:
    """Every member of a zip, decompressed, so a search sees the real bytes."""
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    return {name: zf.read(name) for name in zf.namelist()}


def test_redacted_backup_leaves_out_every_settings_copy(data_dir):
    # Saving settings leaves copies beside the live file: a .bak, the atomic
    # write's .tmp, and a .corrupt.N when the old file would not parse. Each one
    # holds the same keys, and the unparseable one cannot be redacted at all.
    _write_settings(data_dir, {"gemini_api_key": SECRET, "grocy_base_url": "http://x"})
    (data_dir / "settings.json.bak").write_text(json.dumps({"gemini_api_key": SECRET}))
    (data_dir / "settings.json.tmp").write_text(json.dumps({"gemini_api_key": SECRET}))
    (data_dir / "settings.json.corrupt.1").write_text("{broken " + SECRET)
    (data_dir / "settings.json.lock").write_text(SECRET)
    settings.apply({"gemini_api_key": SECRET})

    members = _members(admin._build_zip(include_secrets=False)[0])

    assert "foodassistant-data/settings.json" in members
    assert not [n for n in members if n.startswith("foodassistant-data/settings.json.")]
    assert not [n for n, body in members.items() if SECRET.encode() in body]


def test_redacted_backup_scrubs_the_captured_log(data_dir):
    _write_settings(data_dir, {"gemini_api_key": SECRET})
    logs = data_dir / "logs"
    logs.mkdir()
    (logs / "foodassistant.log").write_text(f"INFO calling provider key={SECRET}\n")
    (logs / "foodassistant.log.1").write_text(f"INFO older line key={SECRET}\n")
    settings.apply({"gemini_api_key": SECRET})

    members = _members(admin._build_zip(include_secrets=False)[0])

    assert not [n for n, body in members.items() if SECRET.encode() in body]
    live = members["foodassistant-data/logs/foodassistant.log"].decode()
    assert "[redacted]" in live


def test_full_backup_still_carries_everything(data_dir):
    # The restore-complete backup is the one the user is told to store somewhere
    # trusted, so nothing is stripped from it.
    _write_settings(data_dir, {"gemini_api_key": SECRET})
    (data_dir / "settings.json.bak").write_text(json.dumps({"gemini_api_key": SECRET}))
    settings.apply({"gemini_api_key": SECRET})

    members = _members(admin._build_zip(include_secrets=True)[0])

    assert "foodassistant-data/settings.json.bak" in members
    assert SECRET.encode() in members["foodassistant-data/settings.json"]


# --- The pre-restore safety copy --------------------------------------------


def test_safety_copy_lands_in_the_data_dir_and_stays_out_of_backups(data_dir):
    _write_settings(data_dir, {"grocy_base_url": "http://original"})
    (data_dir / "staples.txt").write_text("nori\n")
    out = admin._restore_zip(admin._build_zip(include_secrets=True)[0])

    snapshot = Path(out["snapshot"])
    assert snapshot.parent == data_dir / ".pre-restore"
    assert (snapshot / "settings.json").exists()

    # A later backup must skip it, or each backup swallows the last copy and
    # doubles in size every cycle.
    members = _members(admin._build_zip(include_secrets=True)[0])
    assert not [n for n in members if ".pre-restore" in n]


def test_only_the_newest_safety_copies_are_kept(tmp_path):
    root = tmp_path / ".pre-restore"
    for stamp in ("20250101-000000", "20250102-000000", "20250103-000000"):
        (root / stamp).mkdir(parents=True)

    admin._prune_snapshots(root, 2)

    assert sorted(p.name for p in root.iterdir()) == ["20250102-000000", "20250103-000000"]


def test_safe_members_refuses_to_write_into_the_safety_copies(data_dir):
    z = _zip_with({
        "foodassistant-data/settings.json": "{}",
        "foodassistant-data/.pre-restore/20250101-000000/settings.json": "{}",
    })
    zf = zipfile.ZipFile(io.BytesIO(z))
    names = [arc for arc, _ in admin._safe_members(zf, data_dir)]
    assert names == ["foodassistant-data/settings.json"]


def test_second_restore_is_refused_while_one_is_running(data_dir):
    _write_settings(data_dir, {"grocy_base_url": "http://original"})
    zip_bytes, _ = admin._build_zip(include_secrets=True)

    admin._RESTORE_LOCK.acquire()
    try:
        with pytest.raises(HTTPException) as ei:
            admin._restore_zip(zip_bytes)
    finally:
        admin._RESTORE_LOCK.release()
    assert ei.value.status_code == 409


# --- Device identity ---------------------------------------------------------


def test_restore_keeps_this_devices_identity(data_dir):
    _write_settings(data_dir, {
        "deployment_mode": "pi_hosted", "device_id": "the-other-box",
        "remote_server_url": "http://elsewhere:9284",
        "grocy_base_url": "http://original",
    })
    zip_bytes, _ = admin._build_zip(include_secrets=True)

    settings.apply({"deployment_mode": "pi_remote", "device_id": "this-box",
                    "remote_server_url": "http://my-server:9284"})
    out = admin._restore_zip(zip_bytes)

    assert settings.deployment_mode == "pi_remote"
    assert settings.device_id == "this-box"
    assert settings.remote_server_url == "http://my-server:9284"
    assert out["identity_kept"] >= 3
    saved = json.loads((data_dir / "settings.json").read_text())
    assert saved["deployment_mode"] == "pi_remote"
    assert saved["device_id"] == "this-box"
    # The data and preferences in the backup still land.
    assert settings.grocy_base_url == "http://original"


def test_restore_onto_a_fresh_install_adopts_the_backups_mode(data_dir):
    # Nothing to keep: a box being set up from a backup takes what the archive
    # has rather than staying blank.
    _write_settings(data_dir, {"deployment_mode": "pi_hosted", "device_id": "restored-box"})
    zip_bytes, _ = admin._build_zip(include_secrets=True)

    settings.apply({"deployment_mode": "", "device_id": ""})
    admin._restore_zip(zip_bytes)

    assert settings.deployment_mode == "pi_hosted"
    assert settings.device_id == "restored-box"


# --- The restored database ---------------------------------------------------


def test_restored_database_is_brought_up_to_this_builds_schema(data_dir, monkeypatch):
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.orm import sessionmaker

    import app.database as database

    # A database as it was several releases ago: the table exists but without
    # the columns added since, and the newer tables are missing entirely.
    db_path = data_dir / "foodassistant.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE pending_items (id INTEGER PRIMARY KEY, name VARCHAR)"))
        conn.commit()
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))

    admin._upgrade_restored_database()

    with engine.connect() as conn:
        columns = {row[1] for row in conn.execute(text('PRAGMA table_info("pending_items")'))}
        seeded = conn.execute(text("SELECT COUNT(*) FROM expiry_defaults")).scalar()
    assert "best_by_source" in columns  # a column added after that table shipped
    assert "recipes" in inspect(engine).get_table_names()  # a table added later
    assert seeded and seeded > 0  # seed rules topped up, not left empty
    engine.dispose()
