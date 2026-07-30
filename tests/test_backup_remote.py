"""The rclone backup remote is validated before it ever reaches a command
line (security review, Jul 2026): settings.rclone_remote becomes a positional
argument to rclone, so a value shaped like a flag must never be stored or
executed."""
import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services.backup_remote import valid_remote  # noqa: E402


def test_remote_path_shapes_are_accepted():
    assert valid_remote("s3:mybucket/foodassistant")
    assert valid_remote("gdrive:")
    assert valid_remote("b2:bucket/deep/path")
    assert valid_remote("my remote.1:backups")   # spaces, dots, digits in the name
    assert valid_remote("  s3:bucket  ")         # surrounding whitespace is fine


def test_absolute_paths_are_accepted():
    assert valid_remote("/mnt/backups/pantry")


def test_flag_shaped_values_are_rejected():
    assert not valid_remote("--config=/etc/passwd")
    assert not valid_remote("-o")
    assert not valid_remote("- s3:bucket")


def test_other_junk_is_rejected():
    assert not valid_remote("")
    assert not valid_remote("   ")
    assert not valid_remote("no-colon-no-slash")
    assert not valid_remote("relative/path")
    assert not valid_remote(":s3,env_auth:bucket")   # connection strings stay out


def test_setup_save_drops_a_flag_shaped_remote(monkeypatch, tmp_path):
    """The settings boundary: a bad remote is dropped on save, a good one and
    an explicit clear are kept."""
    import os
    from app.config import settings
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "rclone_remote", "", raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        client = TestClient(app)
        # Unconfigured, so /setup/save is reachable without a session.
        client.post("/setup/save", json={"rclone_remote": "--config=/etc/passwd"})
        assert settings.rclone_remote == ""
        client.post("/setup/save", json={"rclone_remote": "s3:bucket/pantry"})
        assert settings.rclone_remote == "s3:bucket/pantry"
        client.post("/setup/save", json={"rclone_remote": "-flag:oops"})
        assert settings.rclone_remote == "s3:bucket/pantry"   # bad value dropped
    finally:
        os.chdir(cwd)
