"""Support bundle builder (FoodAssistant-w7mb): contents and redaction.

The builder is pure (it takes a settings-like object), so these tests run it
against a tmp data dir with planted files, including a known secret, and check
the bundle shape plus the hard guarantee: the secret never appears anywhere in
the produced zip bytes.

Run: python -m pytest tests/test_support_bundle.py -q
"""
from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import support_bundle as sb  # noqa: E402

SECRET = "sk-verysecret-1234567890abcdef"


def _settings(tmp_path: Path, **extra) -> SimpleNamespace:
    return SimpleNamespace(
        data_dir=str(tmp_path),
        deployment_mode="server",
        gemini_api_key=SECRET,
        auth_password="hunter2-long-enough",
        update_last_checked=123.0,
        update_last_latest="0.9.9",
        update_last_available=True,
        auto_update=True,
        **extra,
    )


def _plant_files(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(json.dumps({
        "gemini_api_key": SECRET,
        "auth_password": "hunter2-long-enough",
        "grocy_url": "http://grocy:80",
    }))
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "foodassistant.log").write_text(
        f"INFO connecting with key {SECRET} to provider\n")
    (tmp_path / "scanner_mode.json").write_text(json.dumps({"mode": "consume"}))
    (tmp_path / "audit_session.json").write_text(json.dumps(
        {"active": True, "location": "Pantry", "scans": {}}))


def test_bundle_contains_expected_files(tmp_path):
    _plant_files(tmp_path)
    files = sb.build_bundle_files(_settings(tmp_path))
    assert "manifest.json" in files
    assert "settings.redacted.json" in files
    assert "logs/foodassistant.log" in files
    assert "state/scanner_mode.json" in files
    assert "state/audit_session.json" in files
    assert "state/timers.json" in files
    assert "update-check.json" in files
    assert "python-environment.txt" in files
    manifest = json.loads(files["manifest.json"])
    assert manifest["deployment_mode"] == "server"
    assert manifest["version"]
    assert "is_raspberry_pi" in manifest
    update = json.loads(files["update-check.json"])
    assert update["last_latest"] == "0.9.9"
    assert "python:" in files["python-environment.txt"]


def test_settings_dump_blanks_secret_keys(tmp_path):
    _plant_files(tmp_path)
    files = sb.build_bundle_files(_settings(tmp_path))
    dumped = json.loads(files["settings.redacted.json"])
    assert dumped["gemini_api_key"] == "[redacted]"
    assert dumped["auth_password"] == "[redacted]"
    assert dumped["grocy_url"] == "http://grocy:80"  # non-secrets survive


def test_secret_never_appears_in_zip_bytes(tmp_path):
    _plant_files(tmp_path)
    files = sb.build_bundle_files(
        _settings(tmp_path),
        host_sections={"bridge-journal": f"unit logged token {SECRET} once"},
    )
    zip_bytes = sb.build_zip_bytes(files)
    assert SECRET.encode() not in zip_bytes
    # And after decompression of every member, for a compressed match too.
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            assert SECRET.encode() not in zf.read(name), name


def test_host_sections_land_under_host_dir(tmp_path):
    _plant_files(tmp_path)
    files = sb.build_bundle_files(
        _settings(tmp_path),
        host_sections={"systemd-units": "ok", "weird/../name": "x"},
    )
    assert files["host/systemd-units.txt"] == "ok"
    # Section names are sanitized so they cannot escape the host/ folder.
    assert "host/weird-..-name.txt" in files
    assert not any(".." in Path(n).parts for n in files)


def test_missing_state_files_are_skipped(tmp_path):
    (tmp_path / "settings.json").write_text("{}")
    files = sb.build_bundle_files(_settings(tmp_path))
    assert "state/scanner_mode.json" not in files
    assert "state/audit_session.json" not in files
    assert "state/timers.json" in files  # in-memory snapshot always present


def test_unparseable_settings_passthrough():
    text = "not json {"
    assert sb.redacted_settings_dump(text) == text
