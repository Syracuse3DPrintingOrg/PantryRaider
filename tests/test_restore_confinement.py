"""Tar confinement in the appliance restore helper (FoodAssistant-ifmt).

foodassistant-restore runs as root on a device and unpacks a snapshot into the
compose project dir. These tests run the real script in a sandbox (COMPOSE
stubbed to `true`, INSTALL_DIR pointed at a temp tree, no root and no Docker
needed) and prove that a snapshot is confined to the whitelisted data dirs:
absolute members, ".." traversal, members outside the data dirs, links that
point outside them, and special files are all rejected before the stack is
touched, while a legitimate snapshot still round-trips.

Run: python -m pytest tests/test_restore_confinement.py -q
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


def make_archive(path: Path, members: list[tuple]) -> Path:
    """Write a .tar.gz with explicit members.

    Each member is (name, kind, payload):
      kind "file"    payload is the file body (str)
      kind "dir"     payload ignored
      kind "sym"     payload is the symlink target
      kind "hard"    payload is the hardlink target (archive-relative)
      kind "fifo"    payload ignored
    """
    with tarfile.open(path, "w:gz") as tf:
        for name, kind, payload in members:
            info = tarfile.TarInfo(name)
            info.mode = 0o644
            if kind == "file":
                data = payload.encode()
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tf.addfile(info)
            elif kind == "sym":
                info.type = tarfile.SYMTYPE
                info.linkname = payload
                tf.addfile(info)
            elif kind == "hard":
                info.type = tarfile.LNKTYPE
                info.linkname = payload
                tf.addfile(info)
            elif kind == "fifo":
                info.type = tarfile.FIFOTYPE
                tf.addfile(info)
            else:  # pragma: no cover - test bug
                raise ValueError(kind)
    return path


@pytest.fixture()
def rig(tmp_path):
    """A fake compose project dir with all three live data dirs and a compose
    file, so a partial-snapshot restore can be checked against the dirs the
    archive does NOT carry."""
    proj = tmp_path / "proj"
    (proj / "service" / "data").mkdir(parents=True)
    (proj / "service" / "data" / "settings.json").write_text('{"live": true}\n')
    (proj / "grocy" / "config").mkdir(parents=True)
    (proj / "grocy" / "config" / "db.sqlite").write_text("grocy-live\n")
    (proj / "mealie" / "data").mkdir(parents=True)
    (proj / "mealie" / "data" / "mealie.db").write_text("mealie-live\n")
    (proj / "docker-compose.appliance.yml").write_text("services: {}\n")
    return proj


def run_restore(proj: Path, source: str):
    env = {
        **os.environ,
        "INSTALL_DIR": str(proj),
        "COMPOSE": "true",  # `true stop` / `true start` always succeed
    }
    env.pop("REPO_DIR", None)
    env.pop("DATA_DIRS", None)
    proc = subprocess.run(["bash", str(SCRIPT), source], env=env,
                          capture_output=True, text=True)
    last = proc.stdout.strip().splitlines()[-1]
    return proc.returncode, json.loads(last), proc.stdout


def test_benign_snapshot_round_trips(rig, tmp_path):
    archive = make_archive(tmp_path / "ok.tar.gz", [
        ("service/data", "dir", None),
        ("service/data/settings.json", "file", '{"restored": true}\n'),
    ])
    rc, result, _ = run_restore(rig, str(archive))
    assert rc == 0 and result["ok"] is True
    assert result["restored_dirs"] == ["service/data"]
    restored = (rig / "service" / "data" / "settings.json").read_text()
    assert "restored" in restored
    # Nothing deleted: the previous data moved to a .pre-restore dir.
    kept = list((rig / "service").glob("data.pre-restore-*"))
    assert kept and (kept[0] / "settings.json").read_text() == '{"live": true}\n'
    # No staging leftovers.
    assert not list(rig.glob(".restore-unpack-*"))


def test_partial_snapshot_leaves_other_data_dirs_in_place(rig, tmp_path):
    # A snapshot that carries only service/data must NOT strand the live
    # grocy/config or mealie/data (FoodAssistant-ifmt hardening review).
    archive = make_archive(tmp_path / "partial.tar.gz", [
        ("service/data", "dir", None),
        ("service/data/settings.json", "file", '{"restored": true}\n'),
    ])
    rc, result, _ = run_restore(rig, str(archive))
    assert rc == 0 and result["ok"] is True
    assert result["restored_dirs"] == ["service/data"]
    # The untouched dirs keep their live data, never moved aside.
    assert (rig / "grocy" / "config" / "db.sqlite").read_text() == "grocy-live\n"
    assert (rig / "mealie" / "data" / "mealie.db").read_text() == "mealie-live\n"
    assert not list((rig / "grocy").glob("config.pre-restore-*"))
    assert not list((rig / "mealie").glob("data.pre-restore-*"))


def test_full_three_dir_snapshot_round_trips(rig, tmp_path):
    archive = make_archive(tmp_path / "full.tar.gz", [
        ("service/data", "dir", None),
        ("service/data/settings.json", "file", "app\n"),
        ("grocy/config", "dir", None),
        ("grocy/config/db.sqlite", "file", "grocy-new\n"),
        ("mealie/data", "dir", None),
        ("mealie/data/mealie.db", "file", "mealie-new\n"),
    ])
    rc, result, _ = run_restore(rig, str(archive))
    assert rc == 0 and result["ok"] is True
    assert sorted(result["restored_dirs"]) == ["grocy/config", "mealie/data", "service/data"]
    assert (rig / "grocy" / "config" / "db.sqlite").read_text() == "grocy-new\n"
    assert (rig / "mealie" / "data" / "mealie.db").read_text() == "mealie-new\n"
    # Each replaced dir kept its previous copy aside; none deleted.
    assert list((rig / "grocy").glob("config.pre-restore-*"))
    assert list((rig / "mealie").glob("data.pre-restore-*"))


def test_member_outside_data_dirs_is_rejected(rig, tmp_path):
    # The classic attack: a snapshot that also carries a replacement compose
    # file, which a root extract into the project dir would install.
    archive = make_archive(tmp_path / "evil.tar.gz", [
        ("service/data/settings.json", "file", "{}\n"),
        ("docker-compose.appliance.yml", "file", "services: {owned: true}\n"),
    ])
    rc, result, _ = run_restore(rig, str(archive))
    assert rc == 1 and result["ok"] is False
    assert "outside" in result["error"]
    assert (rig / "docker-compose.appliance.yml").read_text() == "services: {}\n"
    assert (rig / "service" / "data" / "settings.json").read_text() == '{"live": true}\n'


def test_absolute_member_is_rejected(rig, tmp_path):
    archive = make_archive(tmp_path / "abs.tar.gz", [
        ("service/data/x", "file", "x\n"),
        ("/etc/evil", "file", "evil\n"),
    ])
    rc, result, _ = run_restore(rig, str(archive))
    assert rc == 1 and result["ok"] is False


def test_dotdot_member_is_rejected(rig, tmp_path):
    archive = make_archive(tmp_path / "dotdot.tar.gz", [
        ("service/data/../../escape", "file", "evil\n"),
    ])
    rc, result, _ = run_restore(rig, str(archive))
    assert rc == 1 and result["ok"] is False
    assert not (rig.parent / "escape").exists()


def test_symlink_to_absolute_target_is_rejected(rig, tmp_path):
    archive = make_archive(tmp_path / "symabs.tar.gz", [
        ("service/data/link", "sym", "/etc"),
    ])
    rc, result, _ = run_restore(rig, str(archive))
    assert rc == 1 and result["ok"] is False
    assert "symlink" in result["error"]


def test_symlink_with_dotdot_target_is_rejected(rig, tmp_path):
    archive = make_archive(tmp_path / "symdd.tar.gz", [
        ("service/data/link", "sym", "../../../../etc"),
    ])
    rc, result, _ = run_restore(rig, str(archive))
    assert rc == 1 and result["ok"] is False


def test_in_tree_relative_symlink_is_allowed(rig, tmp_path):
    archive = make_archive(tmp_path / "symok.tar.gz", [
        ("service/data/real.txt", "file", "hello\n"),
        ("service/data/alias", "sym", "real.txt"),
    ])
    rc, result, _ = run_restore(rig, str(archive))
    assert rc == 0 and result["ok"] is True
    assert (rig / "service" / "data" / "alias").is_symlink()
    assert (rig / "service" / "data" / "alias").read_text() == "hello\n"


def test_hardlink_outside_data_dirs_is_rejected(rig, tmp_path):
    archive = make_archive(tmp_path / "hard.tar.gz", [
        ("service/data/x", "file", "x\n"),
        ("service/data/link", "hard", "docker-compose.appliance.yml"),
    ])
    rc, result, _ = run_restore(rig, str(archive))
    assert rc == 1 and result["ok"] is False
    assert "hardlink" in result["error"]


def test_fifo_member_is_rejected(rig, tmp_path):
    archive = make_archive(tmp_path / "fifo.tar.gz", [
        ("service/data/pipe", "fifo", None),
    ])
    rc, result, _ = run_restore(rig, str(archive))
    assert rc == 1 and result["ok"] is False


def test_archive_without_expected_dirs_is_rejected(rig, tmp_path):
    archive = make_archive(tmp_path / "other.tar.gz", [
        ("random/notes.txt", "file", "hi\n"),
    ])
    rc, result, _ = run_restore(rig, str(archive))
    assert rc == 1 and result["ok"] is False


def test_not_a_tarball_is_rejected(rig, tmp_path):
    bogus = tmp_path / "bogus.tar.gz"
    bogus.write_text("this is not a tarball")
    rc, result, _ = run_restore(rig, str(bogus))
    assert rc == 1 and result["ok"] is False
    assert "readable" in result["error"]
