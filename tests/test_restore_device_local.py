"""Device-local files survive a full-stack restore (FoodAssistant-rbxcd).

The in-app restore has always skipped DEVICE_LOCAL_FILES. The snapshot path
(scripts/backup.sh, the bridge's USB backup, foodassistant-restore behind the
bridge's POST /restore, and scripts/restore.sh) used to carry another device's
bridge-token and auth-state.json straight over this device's own. These tests
pin that the shell copies of the list match the app's, that backups leave the
files out, that a restore keeps the device's own copies, and that a restore
with different settings is recorded by the app on its next start.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

import app.routers.admin as admin
from app.config import settings
from app.services import auth_state

REPO = Path(__file__).resolve().parents[1]
RESTORE = REPO / "scripts" / "image-build" / "foodassistant-restore"
BACKUP = REPO / "scripts" / "backup.sh"
SERVER_RESTORE = REPO / "scripts" / "restore.sh"
BRIDGE = REPO / "scripts" / "image-build" / "foodassistant-host-bridge"

needs_shell = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("tar")),
    reason="bash and tar are required to exercise the scripts",
)


def _shell_list(path: Path) -> set[str]:
    m = re.search(r'^DEVICE_LOCAL_FILES="([^"]*)"', path.read_text(), re.M)
    assert m, f"{path.name} has no DEVICE_LOCAL_FILES list"
    return set(m.group(1).split())


@pytest.mark.parametrize("script", [RESTORE, BACKUP, SERVER_RESTORE], ids=lambda p: p.name)
def test_shell_lists_match_the_app(script):
    assert _shell_list(script) == set(admin.DEVICE_LOCAL_FILES)


def test_bridge_usb_list_matches_the_app():
    import importlib.machinery
    import importlib.util
    spec = importlib.util.spec_from_loader(
        "fa_bridge_dl", importlib.machinery.SourceFileLoader("fa_bridge_dl", str(BRIDGE)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert set(mod._DEVICE_LOCAL_FILES) == set(admin.DEVICE_LOCAL_FILES)
    args = mod._usb_tar_excludes(["data", "grocy", "mealie"])
    assert args[:2] == ["--anchored", "--ignore-case"]
    assert "--exclude=data/bridge-token" in args
    assert not any(a.startswith("--exclude=grocy") for a in args)


def test_restore_marker_is_device_local():
    assert auth_state.RESTORE_MARKER in admin.DEVICE_LOCAL_FILES


# --- backup.sh ---------------------------------------------------------------


@needs_shell
def test_backup_sh_leaves_device_local_files_out(tmp_path):
    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(BACKUP, root / "scripts" / "backup.sh")
    data = root / "service" / "data"
    data.mkdir(parents=True)
    (data / "settings.json").write_text("{}")
    (data / "staples.txt").write_text("nori\n")
    for name in admin.DEVICE_LOCAL_FILES:
        (data / name).write_text("device-own")
    (data / "Bridge-Token").write_text("odd case")
    nested = data / "sub"
    nested.mkdir()
    (nested / "bridge-token").write_text("not at the top, kept")
    dest = tmp_path / "out"
    proc = subprocess.run(["bash", str(root / "scripts" / "backup.sh"), str(dest)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    [archive] = list(dest.glob("foodassistant-*.tar.gz"))
    with tarfile.open(archive) as tf:
        names = {m.name for m in tf.getmembers()}
    assert "service/data/settings.json" in names
    assert "service/data/staples.txt" in names
    assert "service/data/sub/bridge-token" in names
    tops = {n.split("/", 2)[2].casefold() for n in names
            if n.startswith("service/data/") and n.count("/") == 2}
    assert not (tops & admin.DEVICE_LOCAL_FILES), tops & admin.DEVICE_LOCAL_FILES


# --- foodassistant-restore ---------------------------------------------------


def _archive(path: Path, files: dict[str, str]) -> Path:
    with tarfile.open(path, "w:gz") as tf:
        dirs = set()
        for name in files:
            parts = name.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                d = "/".join(parts[:i])
                if d != "service":  # backup.sh archives service/data, not service
                    dirs.add(d)
        for d in sorted(dirs):
            info = tarfile.TarInfo(d)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            tf.addfile(info)
        for name, body in files.items():
            data = body.encode()
            info = tarfile.TarInfo(name)
            info.mode = 0o644
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


def _run_restore(proj: Path, source: str, data_dirs: str | None = None):
    env = {**os.environ, "INSTALL_DIR": str(proj), "COMPOSE": "true"}
    env.pop("REPO_DIR", None)
    env.pop("DATA_DIRS", None)
    if data_dirs:
        env["DATA_DIRS"] = data_dirs
    proc = subprocess.run(["bash", str(RESTORE), source], env=env,
                          capture_output=True, text=True)
    return proc.returncode, json.loads(proc.stdout.strip().splitlines()[-1]), proc.stdout


@pytest.fixture()
def proj(tmp_path):
    p = tmp_path / "proj"
    data = p / "service" / "data"
    data.mkdir(parents=True)
    (data / "settings.json").write_text('{"live": true}\n')
    (data / "bridge-token").write_text("this-device-token\n")
    os.chmod(data / "bridge-token", 0o640)
    (data / "auth-state.json").write_text('{"origin": "fresh"}\n')
    (data / "bridge-token-enforced").write_text("1\n")
    return p


@needs_shell
def test_restore_keeps_this_devices_own_files(proj, tmp_path):
    archive = _archive(tmp_path / "snap.tar.gz", {
        "service/data/settings.json": '{"restored": true}\n',
        "service/data/staples.txt": "nori\n",
        "service/data/bridge-token": "other-device-token\n",
        "service/data/Bridge-Token": "odd-case-token\n",
        "service/data/auth-state.json": '{"origin": "other"}\n',
        "service/data/setup-code": "123456\n",
        "service/data/restore-note-pending": "planted\n",
    })
    rc, result, out = _run_restore(proj, str(archive))
    assert rc == 0 and result["ok"] is True, out
    data = proj / "service" / "data"
    assert (data / "settings.json").read_text() == '{"restored": true}\n'
    assert (data / "staples.txt").read_text() == "nori\n"
    assert (data / "bridge-token").read_text() == "this-device-token\n"
    assert (data / "bridge-token").stat().st_mode & 0o777 == 0o640
    assert (data / "auth-state.json").read_text() == '{"origin": "fresh"}\n'
    assert (data / "bridge-token-enforced").read_text() == "1\n"
    # Another device's files this device never had do not appear either.
    names = {p.name for p in data.iterdir()}
    assert "setup-code" not in names
    assert "Bridge-Token" not in names
    # Settings changed, so the app gets its note (written here, not planted).
    marker = data / auth_state.RESTORE_MARKER
    assert marker.exists() and marker.read_text().strip().isdigit()
    # The device's previous data is still set aside, untouched.
    [prev] = list((proj / "service").glob("data.pre-restore-*"))
    assert (prev / "bridge-token").read_text() == "this-device-token\n"


@needs_shell
def test_restore_with_the_same_settings_leaves_no_note(proj, tmp_path):
    archive = _archive(tmp_path / "snap.tar.gz", {
        "service/data/settings.json": '{"live": true}\n',
        "service/data/staples.txt": "nori\n",
    })
    rc, result, out = _run_restore(proj, str(archive))
    assert rc == 0 and result["ok"] is True, out
    data = proj / "service" / "data"
    assert not (data / auth_state.RESTORE_MARKER).exists()
    assert (data / "bridge-token").read_text() == "this-device-token\n"


@needs_shell
def test_restore_handles_the_appliance_data_dir(tmp_path):
    p = tmp_path / "proj"
    (p / "data").mkdir(parents=True)
    (p / "data" / "settings.json").write_text("{}\n")
    (p / "data" / "bridge-token").write_text("mine\n")
    archive = _archive(tmp_path / "snap.tar.gz", {
        "data/settings.json": '{"x": 1}\n',
        "data/bridge-token": "theirs\n",
    })
    rc, result, out = _run_restore(p, str(archive), data_dirs="data grocy mealie")
    assert rc == 0 and result["ok"] is True, out
    assert (p / "data" / "bridge-token").read_text() == "mine\n"
    assert (p / "data" / auth_state.RESTORE_MARKER).exists()


@needs_shell
def test_server_restore_sh_keeps_this_machines_own_files(tmp_path):
    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SERVER_RESTORE, root / "scripts" / "restore.sh")
    data = root / "service" / "data"
    data.mkdir(parents=True)
    (data / "settings.json").write_text('{"live": true}\n')
    (data / "bridge-token").write_text("mine\n")
    archive = _archive(tmp_path / "snap.tar.gz", {
        "service/data/settings.json": '{"restored": true}\n',
        "service/data/bridge-token": "theirs\n",
        "service/data/auth-state.json": "{}\n",
    })
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text("#!/bin/sh\nexit 0\n")
    os.chmod(bindir / "docker", 0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    proc = subprocess.run(["bash", str(root / "scripts" / "restore.sh"), str(archive)],
                          env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (data / "settings.json").read_text() == '{"restored": true}\n'
    assert (data / "bridge-token").read_text() == "mine\n"
    assert not (data / "auth-state.json").exists()
    assert (data / auth_state.RESTORE_MARKER).exists()


# --- The app records the restore on its next start ----------------------------


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(settings, "data_dir", str(d), raising=False)
    return d


def test_consume_restore_marker_notes_the_restore_once(data_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(auth_state, "note_restore", lambda now=None: calls.append(now))
    (data_dir / auth_state.RESTORE_MARKER).write_text("1700000000\n")
    assert auth_state.consume_restore_marker() is True
    assert calls == [None]
    assert not (data_dir / auth_state.RESTORE_MARKER).exists()
    # A second worker, or the next start, finds nothing to do.
    assert auth_state.consume_restore_marker() is False
    assert calls == [None]


def test_consume_restore_marker_without_a_marker_does_nothing(data_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(auth_state, "note_restore", lambda now=None: calls.append(now))
    assert auth_state.consume_restore_marker() is False
    assert calls == []


def test_a_marker_that_cannot_be_removed_never_reopens_the_grace(data_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(auth_state, "note_restore", lambda now=None: calls.append(now))
    (data_dir / auth_state.RESTORE_MARKER).write_text("1\n")
    real_unlink = Path.unlink

    def deny(self, *a, **kw):
        if self.name == auth_state.RESTORE_MARKER:
            raise PermissionError("read-only")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", deny)
    assert auth_state.consume_restore_marker() is False
    assert calls == []


def test_consume_restore_marker_marks_the_install_upgraded(data_dir, monkeypatch):
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    t0 = 1_800_000_000.0
    auth_state.reset(auth_state.test_state(now=t0, origin="fresh"), persist=True)
    try:
        (data_dir / auth_state.RESTORE_MARKER).write_text("1\n")
        assert auth_state.consume_restore_marker(now=t0 + 60) is True
        doc = json.loads((data_dir / auth_state.FILENAME).read_text())
        assert doc["origin"] == "upgraded"
        assert doc.get("restored_at")
    finally:
        auth_state.reset(auth_state.test_state(), persist=False)


def test_startup_consumes_the_marker():
    src = (REPO / "service" / "app" / "main.py").read_text()
    body = src.split("async def _access_startup", 1)[1].split("\nasync def ", 1)[0]
    assert "auth_state.consume_restore_marker()" in body
