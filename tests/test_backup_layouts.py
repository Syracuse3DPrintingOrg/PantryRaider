"""scripts/backup.sh and scripts/restore.sh follow the layout in use
(FoodAssistant-ph52m).

docker-compose.prod.yml, install.sh servers and Pi appliances mount ./data,
./grocy and ./mealie; a checkout's docker-compose.yml mounts service/data,
grocy/config and mealie/data. backup.sh used to look only for the second, so
on a Compose install it found nothing. These tests run the scripts in a
sandbox with docker stubbed out.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BACKUP = REPO / "scripts" / "backup.sh"
RESTORE = REPO / "scripts" / "restore.sh"

pytestmark = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("tar")),
    reason="bash and tar are required to exercise the scripts",
)


def _checkout(root: Path) -> Path:
    """A bare checkout holding only the two scripts (no data of its own)."""
    (root / "scripts").mkdir(parents=True)
    shutil.copy(BACKUP, root / "scripts" / "backup.sh")
    shutil.copy(RESTORE, root / "scripts" / "restore.sh")
    return root


def _stack(root: Path, compose: str = "docker-compose.prod.yml",
           as_name: str = "docker-compose.yml") -> Path:
    """A Compose stack folder laid out the way docker-compose.prod.yml mounts it."""
    root.mkdir(parents=True)
    shutil.copy(REPO / compose, root / as_name)
    (root / "data").mkdir()
    (root / "data" / "settings.json").write_text('{"live": true}\n')
    (root / "data" / "bridge-token").write_text("mine\n")
    (root / "grocy").mkdir()
    (root / "grocy" / "grocy.db").write_text("inventory")
    (root / "mealie").mkdir()
    (root / "mealie" / "mealie.db").write_text("recipes")
    return root


def _docker_stub(tmp_path: Path) -> tuple[dict, Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "docker.log"
    (bindir / "docker").write_text(f'#!/bin/sh\necho "$*" >> "{log}"\nexit 0\n')
    os.chmod(bindir / "docker", 0o755)
    return {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}, log


def _run(script: Path, *args, cwd: Path, env=None, **extra):
    env = dict(env or os.environ)
    env.pop("INSTALL_DIR", None)
    env.update(extra)
    return subprocess.run(["bash", str(script), *map(str, args)], cwd=cwd, env=env,
                          capture_output=True, text=True)


def _names(archive: Path) -> set[str]:
    with tarfile.open(archive) as tf:
        return {m.name for m in tf.getmembers()}


def test_backup_finds_the_prod_compose_layout(tmp_path):
    src = _checkout(tmp_path / "src")
    stack = _stack(tmp_path / "stack")
    dest = tmp_path / "out"
    proc = _run(src / "scripts" / "backup.sh", dest, cwd=tmp_path, INSTALL_DIR=str(stack))
    assert proc.returncode == 0, proc.stderr
    [archive] = list(dest.glob("foodassistant-*.tar.gz"))
    names = _names(archive)
    assert {"data/settings.json", "grocy/grocy.db", "mealie/mealie.db"} <= names
    assert "data/bridge-token" not in names
    assert not any(n.startswith("service/") for n in names)
    assert "docker-compose.yml" not in names


def test_backup_finds_the_stack_from_its_own_folder(tmp_path):
    """Run from the folder with the compose file, as the README says."""
    src = _checkout(tmp_path / "src")
    stack = _stack(tmp_path / "stack")
    proc = _run(src / "scripts" / "backup.sh", tmp_path / "out", cwd=stack)
    assert proc.returncode == 0, proc.stderr
    [archive] = list((tmp_path / "out").glob("foodassistant-*.tar.gz"))
    assert "data/settings.json" in _names(archive)


def test_backup_finds_a_checkout_running_the_prod_file(tmp_path):
    root = _stack(tmp_path / "clone", as_name="docker-compose.prod.yml")
    _checkout_scripts = root / "scripts"
    _checkout_scripts.mkdir()
    shutil.copy(BACKUP, _checkout_scripts / "backup.sh")
    shutil.copy(REPO / "docker-compose.yml", root / "docker-compose.yml")
    proc = _run(root / "scripts" / "backup.sh", tmp_path / "out", cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    [archive] = list((tmp_path / "out").glob("foodassistant-*.tar.gz"))
    assert "data/settings.json" in _names(archive)


def test_backup_keeps_the_checkout_layout(tmp_path):
    root = _checkout(tmp_path / "clone")
    shutil.copy(REPO / "docker-compose.yml", root / "docker-compose.yml")
    (root / "service" / "data").mkdir(parents=True)
    (root / "service" / "data" / "settings.json").write_text("{}")
    (root / "grocy" / "config").mkdir(parents=True)
    (root / "grocy" / "config" / "grocy.db").write_text("inventory")
    proc = _run(root / "scripts" / "backup.sh", tmp_path / "out", cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    [archive] = list((tmp_path / "out").glob("foodassistant-*.tar.gz"))
    assert {"service/data/settings.json", "grocy/config/grocy.db"} <= _names(archive)


@pytest.mark.parametrize("shape", ["nothing", "empty-folders"])
def test_backup_fails_loudly_without_data(tmp_path, shape):
    src = _checkout(tmp_path / "src")
    stack = tmp_path / "stack"
    stack.mkdir()
    if shape == "empty-folders":
        shutil.copy(REPO / "docker-compose.prod.yml", stack / "docker-compose.yml")
        for d in ("data", "grocy", "mealie"):
            (stack / d).mkdir()
    dest = tmp_path / "out"
    proc = _run(src / "scripts" / "backup.sh", dest, cwd=tmp_path, INSTALL_DIR=str(stack))
    assert proc.returncode != 0
    assert "found no Pantry Raider data" in proc.stderr
    assert not list(dest.glob("*.tar.gz*"))


def test_backup_refuses_to_guess_between_two_layouts(tmp_path):
    root = _stack(tmp_path / "clone", as_name="docker-compose.prod.yml")
    shutil.copy(REPO / "docker-compose.yml", root / "docker-compose.yml")
    (root / "service" / "data").mkdir(parents=True)
    (root / "service" / "data" / "settings.json").write_text("{}")
    (root / "scripts").mkdir()
    shutil.copy(BACKUP, root / "scripts" / "backup.sh")
    proc = _run(root / "scripts" / "backup.sh", tmp_path / "out", cwd=tmp_path)
    assert proc.returncode != 0
    assert "both service/data and data" in proc.stderr
    assert not list((tmp_path / "out").glob("*.tar.gz*"))


def test_a_prod_backup_restores_into_the_prod_layout(tmp_path):
    src = _checkout(tmp_path / "src")
    stack = _stack(tmp_path / "stack")
    out = tmp_path / "out"
    assert _run(src / "scripts" / "backup.sh", out, cwd=tmp_path,
                INSTALL_DIR=str(stack)).returncode == 0
    [archive] = list(out.glob("foodassistant-*.tar.gz"))
    (stack / "data" / "settings.json").write_text('{"changed": true}\n')
    (stack / "grocy" / "grocy.db").write_text("later")
    env, log = _docker_stub(tmp_path)
    proc = _run(src / "scripts" / "restore.sh", archive, cwd=tmp_path, env=env,
                INSTALL_DIR=str(stack))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (stack / "data" / "settings.json").read_text() == '{"live": true}\n'
    assert (stack / "grocy" / "grocy.db").read_text() == "inventory"
    assert (stack / "data" / "bridge-token").read_text() == "mine\n"
    assert (stack / "data" / "restore-note-pending").exists()
    assert list(stack.glob("data.pre-restore-*"))
    calls = log.read_text().splitlines()
    assert calls[0].startswith("compose -f docker-compose.yml ") and calls[0].endswith(" stop")
    assert calls[-1].endswith(" start")


def test_restore_refuses_a_backup_from_the_other_layout(tmp_path):
    src = _checkout(tmp_path / "src")
    stack = _stack(tmp_path / "stack")
    archive = tmp_path / "server.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        body = b"{}"
        info = tarfile.TarInfo("service/data/settings.json")
        info.size = len(body)
        tf.addfile(info, io.BytesIO(body))
    env, log = _docker_stub(tmp_path)
    proc = _run(src / "scripts" / "restore.sh", archive, cwd=tmp_path, env=env,
                INSTALL_DIR=str(stack))
    assert proc.returncode != 0
    assert "foodassistant-restore" in proc.stderr
    assert not log.exists()
    assert (stack / "data" / "settings.json").read_text() == '{"live": true}\n'


def test_restore_unpacks_only_the_data_folders(tmp_path):
    src = _checkout(tmp_path / "src")
    stack = _stack(tmp_path / "stack")
    compose = (stack / "docker-compose.yml").read_text()
    archive = tmp_path / "snap.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        for name, body in (("data/settings.json", b"{}"),
                           ("docker-compose.yml", b"services: {}\n"),
                           (".env", b"AUTH_PASSWORD=x\n")):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tf.addfile(info, io.BytesIO(body))
    env, _log = _docker_stub(tmp_path)
    proc = _run(src / "scripts" / "restore.sh", archive, cwd=tmp_path, env=env,
                INSTALL_DIR=str(stack))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (stack / "docker-compose.yml").read_text() == compose
    assert not (stack / ".env").exists()
    # Grocy was not in the backup, so it is left where it was.
    assert (stack / "grocy" / "grocy.db").read_text() == "inventory"
