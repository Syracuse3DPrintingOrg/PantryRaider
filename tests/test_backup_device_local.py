"""Files that belong to one device never travel in a backup (I-18).

The host bridge's token and its handshake markers, the login lockout state, the
kiosk launch tickets, the setup code and the device key describe the running
device, not the user's data. A backup leaves them out whatever include_secrets
says, and a restore never writes them, so an old archive that still carries a
bridge-token restores everything else and leaves the device's own token alone.
"""
from __future__ import annotations

import io
import json
import zipfile

import pytest

import app.routers.admin as admin
from app.config import settings

_PREFIX = "foodassistant-data"


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(settings, "data_dir", str(d), raising=False)
    return d


def _names(zip_bytes: bytes) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        return {n.split("/", 1)[1] for n in zf.namelist()}


def _zip_with(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in members.items():
            zf.writestr(name, body)
    return buf.getvalue()


def test_the_lists_match_the_contract():
    assert admin.DEVICE_LOCAL_FILES == {
        "bridge-token", "bridge-token-enforced", "bridge-token-grace-started",
        "bridge-identity-verified", "auth-state.json", "kiosk-launch-nonces.json",
        "setup-code", "device.key",
    }
    assert {"rclone.conf", "credentials.json", "identity.key"} <= admin._SECRET_FILES
    # A device-local file is never merely a secret: include_secrets must not
    # bring it back.
    assert not (admin.DEVICE_LOCAL_FILES & admin._SECRET_FILES)


@pytest.mark.parametrize("include_secrets", [False, True])
def test_device_local_files_are_never_archived(data_dir, include_secrets):
    (data_dir / "settings.json").write_text(json.dumps({"grocy_base_url": "http://x"}))
    (data_dir / "staples.txt").write_text("nori\n")
    for name in admin.DEVICE_LOCAL_FILES:
        (data_dir / name).write_text("device-own-" + name)

    names = _names(admin._build_zip(include_secrets=include_secrets)[0])

    assert "staples.txt" in names
    assert "settings.json" in names
    assert not (names & admin.DEVICE_LOCAL_FILES), names & admin.DEVICE_LOCAL_FILES


def test_new_secret_files_follow_include_secrets(data_dir):
    (data_dir / "settings.json").write_text("{}")
    (data_dir / "credentials.json").write_text('{"k": "v"}')
    (data_dir / "identity.key").write_text("private")
    (data_dir / "rclone.conf").write_text("[remote]")

    redacted = _names(admin._build_zip(include_secrets=False)[0])
    full = _names(admin._build_zip(include_secrets=True)[0])

    for name in ("credentials.json", "identity.key", "rclone.conf"):
        assert name not in redacted
        assert name in full


def test_old_archive_with_bridge_token_leaves_the_device_token_alone(data_dir):
    """An archive made before this change carries bridge-token (and possibly
    the other device files). The restore takes everything else from it and
    keeps the running device's own copies, byte for byte."""
    (data_dir / "settings.json").write_text(json.dumps({"grocy_base_url": "http://live"}))
    (data_dir / "bridge-token").write_text("live-token")
    (data_dir / "setup-code").write_text("live-code")
    # auth-state.json is absent on this device: the archive's copy must not
    # appear either.
    old = _zip_with({
        f"{_PREFIX}/settings.json": json.dumps({"grocy_base_url": "http://restored",
                                                "staple_items": "miso"}),
        f"{_PREFIX}/staples.txt": "nori\n",
        f"{_PREFIX}/bridge-token": "stale-token-from-another-day",
        f"{_PREFIX}/bridge-token-enforced": "1",
        f"{_PREFIX}/setup-code": "old-code",
        f"{_PREFIX}/auth-state.json": '{"failures": 99}',
        f"{_PREFIX}/kiosk-launch-nonces.json": "{}",
        f"{_PREFIX}/device.key": "old-key",
    })

    out = admin._restore_zip(old)

    assert out["ok"] is True
    assert out["restored_files"] == 2  # settings.json and staples.txt
    assert settings.grocy_base_url == "http://restored"
    assert (data_dir / "staples.txt").read_text() == "nori\n"
    assert (data_dir / "bridge-token").read_text() == "live-token"
    assert (data_dir / "setup-code").read_text() == "live-code"
    for name in ("bridge-token-enforced", "auth-state.json",
                 "kiosk-launch-nonces.json", "device.key"):
        assert not (data_dir / name).exists(), name


def test_restore_skips_device_files_however_the_path_is_spelled(data_dir):
    z = _zip_with({
        f"{_PREFIX}/./bridge-token": "x",
        f"{_PREFIX}/sub/../bridge-token": "y",
        f"{_PREFIX}/staples.txt": "ok",
    })
    with zipfile.ZipFile(io.BytesIO(z)) as zf:
        dests = [d for _, d in admin._safe_members(zf, data_dir)]
    assert dests == [(data_dir / "staples.txt").resolve()]


def test_a_same_named_file_below_the_top_level_still_travels(data_dir):
    """Only the device's own files at the top of the data dir are held back,
    so nothing nested (a future folder of its own) disappears by accident."""
    nested = data_dir / "notes"
    nested.mkdir()
    (nested / "setup-code").write_text("a recipe called setup-code")
    names = _names(admin._build_zip(include_secrets=False)[0])
    assert "notes/setup-code" in names


def test_secret_files_restore_only_from_an_archive_that_has_them(data_dir):
    (data_dir / "settings.json").write_text("{}")
    (data_dir / "credentials.json").write_text("live")
    redacted = admin._build_zip(include_secrets=False)[0]
    admin._restore_zip(redacted)
    assert (data_dir / "credentials.json").read_text() == "live"

    full = _zip_with({f"{_PREFIX}/settings.json": "{}",
                      f"{_PREFIX}/credentials.json": "from-archive"})
    admin._restore_zip(full)
    assert (data_dir / "credentials.json").read_text() == "from-archive"
