"""Restore-from-backup tests (FoodAssistant-bkpp).

Covers the app-data restore that mirrors GET /admin/backup: round-trip,
secret-preserve-on-blank for a redacted backup, and the zip-slip / bad-archive
guards.
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
