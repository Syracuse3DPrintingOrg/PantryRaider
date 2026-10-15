"""Snapshot layouts in the appliance restore helper (FoodAssistant-7qw44).

A Pi's USB backup tars the install dir's data, grocy and mealie; a server's
scripts/backup.sh tars service/data, grocy/config and mealie/data. These tests
run the real foodassistant-restore in a sandbox (COMPOSE stubbed, INSTALL_DIR
a temp tree, tiny fixture tarballs of both layouts) and pin that either
snapshot lands in the dirs the device's compose file mounts, that this
device's own files survive either way, and that an archive mixing the two
layouts or matching neither is refused before the stack is touched.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

SCRIPT = (Path(__file__).resolve().parents[1]
          / "scripts" / "image-build" / "foodassistant-restore")

pytestmark = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("tar")),
    reason="bash and tar are required to exercise the restore script",
)

APPLIANCE_COMPOSE = """services:
  foodassistant:
    volumes:
      - ./data:/app/data
  grocy:
    volumes:
      - ./grocy:/config
  mealie:
    volumes:
      - ./mealie:/app/data
"""

SERVER_COMPOSE = """services:
  foodassistant:
    volumes:
      - ./service/app:/app/app
      - ./service/data:/app/data
  grocy:
    volumes:
      - ./grocy/config:/config
"""


def snapshot(path: Path, files: dict[str, str], dirs: tuple[str, ...] = ()) -> Path:
    with tarfile.open(path, "w:gz") as tf:
        for d in dirs:
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


APPLIANCE_SNAP = {
    "data/settings.json": '{"from": "pi"}\n',
    "data/bridge-token": "other-device\n",
    "grocy/data/grocy.db": "grocy-pi\n",
    "mealie/mealie.db": "mealie-pi\n",
}

SERVER_SNAP = {
    "service/data/settings.json": '{"from": "server"}\n',
    "service/data/bridge-token": "other-device\n",
    "grocy/config/data/grocy.db": "grocy-server\n",
    "mealie/data/mealie.db": "mealie-server\n",
}


@pytest.fixture()
def calls(tmp_path):
    """A stand-in for docker compose that records each call."""
    log = tmp_path / "compose-calls"
    stub = tmp_path / "compose"
    stub.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexit 0\n')
    stub.chmod(0o755)
    return stub, log


def run(proj: Path, archive: Path, calls):
    stub, _ = calls
    env = {**os.environ, "INSTALL_DIR": str(proj), "COMPOSE": str(stub)}
    for k in ("REPO_DIR", "DATA_DIRS"):
        env.pop(k, None)
    proc = subprocess.run(["bash", str(SCRIPT), str(archive)], env=env,
                          capture_output=True, text=True)
    return proc.returncode, json.loads(proc.stdout.strip().splitlines()[-1]), proc.stdout


@pytest.fixture()
def appliance(tmp_path):
    p = tmp_path / "opt-foodassistant"
    (p / "data").mkdir(parents=True)
    (p / "data" / "settings.json").write_text('{"live": true}\n')
    (p / "data" / "bridge-token").write_text("this-device\n")
    (p / "grocy" / "data").mkdir(parents=True)
    (p / "grocy" / "data" / "grocy.db").write_text("grocy-live\n")
    (p / "mealie").mkdir()
    (p / "mealie" / "mealie.db").write_text("mealie-live\n")
    (p / "docker-compose.yml").write_text(APPLIANCE_COMPOSE)
    return p


@pytest.fixture()
def server(tmp_path):
    p = tmp_path / "checkout"
    (p / "service" / "data").mkdir(parents=True)
    (p / "service" / "data" / "settings.json").write_text('{"live": true}\n')
    (p / "service" / "data" / "bridge-token").write_text("this-device\n")
    (p / "grocy" / "config" / "data").mkdir(parents=True)
    (p / "grocy" / "config" / "data" / "grocy.db").write_text("grocy-live\n")
    (p / "mealie" / "data").mkdir(parents=True)
    (p / "mealie" / "data" / "mealie.db").write_text("mealie-live\n")
    (p / "docker-compose.yml").write_text(SERVER_COMPOSE)
    return p


def test_appliance_snapshot_restores_on_an_appliance(appliance, tmp_path, calls):
    archive = snapshot(tmp_path / "usb.tar.gz", APPLIANCE_SNAP)
    rc, result, out = run(appliance, archive, calls)
    assert rc == 0 and result["ok"] is True, out
    assert result["restored_dirs"] == ["data", "grocy", "mealie"]
    assert (appliance / "data" / "settings.json").read_text() == '{"from": "pi"}\n'
    assert (appliance / "data" / "bridge-token").read_text() == "this-device\n"
    assert (appliance / "data" / "restore-note-pending").exists()
    assert (appliance / "grocy" / "data" / "grocy.db").read_text() == "grocy-pi\n"
    assert (appliance / "mealie" / "mealie.db").read_text() == "mealie-pi\n"
    # Nothing landed in the layout this device does not mount.
    assert not (appliance / "service").exists()
    assert list(appliance.glob("grocy.pre-restore-*"))


def test_server_snapshot_lands_where_the_appliance_mounts(appliance, tmp_path, calls):
    archive = snapshot(tmp_path / "server.tar.gz", SERVER_SNAP)
    rc, result, out = run(appliance, archive, calls)
    assert rc == 0 and result["ok"] is True, out
    assert result["restored_dirs"] == ["data", "grocy", "mealie"]
    assert (appliance / "data" / "settings.json").read_text() == '{"from": "server"}\n'
    assert (appliance / "data" / "bridge-token").read_text() == "this-device\n"
    assert (appliance / "data" / "restore-note-pending").exists()
    assert (appliance / "grocy" / "data" / "grocy.db").read_text() == "grocy-server\n"
    assert (appliance / "mealie" / "mealie.db").read_text() == "mealie-server\n"
    assert not (appliance / "service").exists()
    assert not (appliance / "grocy" / "config").exists()
    # The previous data is set aside, not deleted.
    [prev] = list(appliance.glob("data.pre-restore-*"))
    assert (prev / "settings.json").read_text() == '{"live": true}\n'
    assert not list(appliance.glob(".restore-unpack-*"))


def test_appliance_snapshot_lands_where_the_server_mounts(server, tmp_path, calls):
    archive = snapshot(tmp_path / "usb.tar.gz", APPLIANCE_SNAP)
    rc, result, out = run(server, archive, calls)
    assert rc == 0 and result["ok"] is True, out
    assert result["restored_dirs"] == ["service/data", "grocy/config", "mealie/data"]
    data = server / "service" / "data"
    assert (data / "settings.json").read_text() == '{"from": "pi"}\n'
    assert (data / "bridge-token").read_text() == "this-device\n"
    assert (server / "grocy" / "config" / "data" / "grocy.db").read_text() == "grocy-pi\n"
    assert (server / "mealie" / "data" / "mealie.db").read_text() == "mealie-pi\n"
    assert not (server / "data").exists()


def test_partial_server_snapshot_leaves_other_dirs_alone(appliance, tmp_path, calls):
    archive = snapshot(tmp_path / "grocy.tar.gz",
                       {"grocy/config/data/grocy.db": "grocy-server\n"},
                       dirs=("grocy", "grocy/config"))
    rc, result, out = run(appliance, archive, calls)
    assert rc == 0 and result["ok"] is True, out
    assert result["restored_dirs"] == ["grocy"]
    assert (appliance / "grocy" / "data" / "grocy.db").read_text() == "grocy-server\n"
    assert (appliance / "data" / "settings.json").read_text() == '{"live": true}\n'
    assert (appliance / "mealie" / "mealie.db").read_text() == "mealie-live\n"


def test_a_parent_dir_entry_is_accepted(server, tmp_path, calls):
    archive = snapshot(tmp_path / "s.tar.gz",
                       {"service/data/settings.json": "{}\n"},
                       dirs=("service", "service/data"))
    rc, result, out = run(server, archive, calls)
    assert rc == 0 and result["ok"] is True, out
    assert result["restored_dirs"] == ["service/data"]


def test_the_compose_file_decides_the_device_layout(appliance, tmp_path, calls):
    # A stray service/data on an appliance does not pull the restore there.
    (appliance / "service" / "data").mkdir(parents=True)
    archive = snapshot(tmp_path / "server.tar.gz", SERVER_SNAP)
    rc, result, out = run(appliance, archive, calls)
    assert rc == 0 and result["ok"] is True, out
    assert result["restored_dirs"] == ["data", "grocy", "mealie"]
    assert list((appliance / "service" / "data").iterdir()) == []


def _untouched_appliance(p: Path, calls):
    assert (p / "data" / "settings.json").read_text() == '{"live": true}\n'
    assert (p / "grocy" / "data" / "grocy.db").read_text() == "grocy-live\n"
    assert (p / "mealie" / "mealie.db").read_text() == "mealie-live\n"
    assert not list(p.glob("*.pre-restore-*"))
    assert not list(p.glob(".restore-unpack-*"))
    # The stack was never stopped.
    assert not calls[1].exists()


def test_mixed_layouts_are_refused_whole(appliance, tmp_path, calls):
    archive = snapshot(tmp_path / "mixed.tar.gz", {
        "data/settings.json": "{}\n",
        "service/data/settings.json": "{}\n",
    })
    rc, result, out = run(appliance, archive, calls)
    assert rc == 1 and result["ok"] is False
    assert "mixes" in result["error"]
    _untouched_appliance(appliance, calls)


def test_appliance_app_data_with_server_grocy_is_refused(appliance, tmp_path, calls):
    archive = snapshot(tmp_path / "mixed.tar.gz", {
        "data/settings.json": "{}\n",
        "grocy/config/data/grocy.db": "x\n",
    })
    rc, result, out = run(appliance, archive, calls)
    assert rc == 1 and result["ok"] is False
    assert "mixes" in result["error"]
    _untouched_appliance(appliance, calls)


def test_an_unrecognized_layout_is_refused(appliance, tmp_path, calls):
    archive = snapshot(tmp_path / "other.tar.gz", {"photos/cat.jpg": "meow\n"})
    rc, result, out = run(appliance, archive, calls)
    assert rc == 1 and result["ok"] is False
    assert "no Pantry Raider data dirs" in result["error"]
    _untouched_appliance(appliance, calls)


def test_a_server_snapshot_with_stray_grocy_files_is_refused(server, tmp_path, calls):
    archive = snapshot(tmp_path / "odd.tar.gz", {
        "service/data/settings.json": "{}\n",
        "grocy/notes.txt": "x\n",
    })
    rc, result, out = run(server, archive, calls)
    assert rc == 1 and result["ok"] is False
    assert (server / "service" / "data" / "settings.json").read_text() == '{"live": true}\n'
    assert not calls[1].exists()


def test_a_device_with_no_known_layout_is_refused(tmp_path, calls):
    p = tmp_path / "empty"
    p.mkdir()
    archive = snapshot(tmp_path / "usb.tar.gz", APPLIANCE_SNAP)
    rc, result, out = run(p, archive, calls)
    assert rc == 1 and result["ok"] is False
    assert "Could not tell" in result["error"]
    assert list(p.iterdir()) == []
    assert not calls[1].exists()


def test_server_restore_sh_refuses_a_device_snapshot(server, tmp_path):
    root = server
    (root / "scripts").mkdir()
    shutil.copy(SCRIPT.parents[1] / "restore.sh", root / "scripts" / "restore.sh")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker_calls = tmp_path / "docker-calls"
    (bindir / "docker").write_text(f'#!/bin/sh\necho "$@" >> "{docker_calls}"\nexit 0\n')
    (bindir / "docker").chmod(0o755)
    archive = snapshot(tmp_path / "usb.tar.gz", APPLIANCE_SNAP)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    proc = subprocess.run(["bash", str(root / "scripts" / "restore.sh"), str(archive)],
                          env=env, capture_output=True, text=True)
    assert proc.returncode == 1
    assert "foodassistant-restore" in proc.stderr
    assert not docker_calls.exists()
    assert (root / "service" / "data" / "settings.json").read_text() == '{"live": true}\n'
    assert not list((root / "service").glob("data.pre-restore-*"))


@pytest.mark.skipif(not shutil.which("flock"), reason="flock is required")
def test_restore_refuses_while_an_update_holds_the_lock(appliance, tmp_path, calls):
    """foodassistant-update holds this lock for its whole run; a restore that
    starts meanwhile stops before touching the stack or the data."""
    import fcntl

    lock = tmp_path / "update.lock"
    archive = snapshot(tmp_path / "usb.tar.gz", APPLIANCE_SNAP)
    stub, compose_log = calls
    env = {**os.environ, "INSTALL_DIR": str(appliance), "COMPOSE": str(stub),
           "UPDATE_LOCK_FILE": str(lock)}
    for k in ("REPO_DIR", "DATA_DIRS"):
        env.pop(k, None)
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        proc = subprocess.run(["bash", str(SCRIPT), str(archive)], env=env,
                              capture_output=True, text=True)
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert proc.returncode == 1 and result["ok"] is False
    assert "update is running" in result["error"]
    assert not compose_log.exists()
    assert (appliance / "data" / "settings.json").read_text() == '{"live": true}\n'
    assert not list(appliance.glob("*.pre-restore-*"))

    # Once the update has finished, the same restore goes ahead.
    proc = subprocess.run(["bash", str(SCRIPT), str(archive)], env=env,
                          capture_output=True, text=True)
    assert json.loads(proc.stdout.strip().splitlines()[-1])["ok"] is True, proc.stdout
