"""The Grocy config.php repair scripts (the Grocy 4.7 AUTH_CLASS move).

Two bash scripts, both run for real here (no Docker, no root):

- docker/grocy-init/10-repair-auth-class.sh runs inside the Grocy container
  at start. Its paths are env-overridable, so it is exercised against temp
  files on the host.
- scripts/image-build/foodassistant-grocy-repair runs on an appliance host
  and reaches into the container with docker exec. A stub docker on PATH
  answers `compose ps` / `ps` with a fake container id and runs every `exec`
  command locally, so the whole flow (read, decide, back up, rewrite, verify)
  runs against the same temp files.

No php on most test hosts, so the rewrite takes the literal sed fallback;
the php path runs only where php is installed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INIT_SCRIPT = REPO / "docker" / "grocy-init" / "10-repair-auth-class.sh"
HOST_SCRIPT = REPO / "scripts" / "image-build" / "foodassistant-grocy-repair"

pytestmark = pytest.mark.skipif(
    not shutil.which("bash"), reason="bash is required to exercise the scripts")

OLD = "Grocy\\Middleware\\DefaultAuthMiddleware"
NEW = "Grocy\\Middleware\\Auth\\DefaultAuthMiddleware"

OLD_CONFIG = (
    "<?php\n"
    "// Grocy config, edited by hand\n"
    "Setting('MODE', 'production');\n"
    "// Setting('AUTH_CLASS', 'Grocy\\Middleware\\ReverseProxyAuthMiddleware');\n"
    "Setting('AUTH_CLASS', 'Grocy\\Middleware\\DefaultAuthMiddleware');\n"
    "Setting('BASE_URL', '/');\n"
)
NEW_CONFIG = OLD_CONFIG.replace(OLD, NEW)


def _run(script, env_extra, cwd=None):
    env = dict(os.environ)
    env.update(env_extra)
    return subprocess.run(["bash", str(script)], env=env, cwd=cwd,
                          capture_output=True, text=True)


# --- The in-container init script ---------------------------------------------

@pytest.fixture
def rig(tmp_path):
    config = tmp_path / "config.php"
    config.write_text(OLD_CONFIG)
    new_class = tmp_path / "DefaultAuthMiddleware.php"
    new_class.write_text("<?php namespace Grocy\\Middleware\\Auth;\n")
    env = {"GROCY_CONFIG": str(config), "GROCY_NEW_CLASS_FILE": str(new_class),
           "GROCY_REPAIR_TOOL": "sed"}
    return {"config": config, "new_class": new_class, "env": env, "dir": tmp_path}


def _backups(rig):
    return sorted(rig["dir"].glob("config.php.bak-*"))


def test_scripts_are_executable_in_the_repo():
    # The LinuxServer image ignores a non-executable custom-init script, and
    # the update helper installs bin helpers by name; both must carry +x.
    assert os.access(INIT_SCRIPT, os.X_OK)
    assert os.access(HOST_SCRIPT, os.X_OK)


def test_init_dry_run_decides_but_writes_nothing(rig):
    r = _run(INIT_SCRIPT, {**rig["env"], "DRY_RUN": "1"})
    assert r.returncode == 0, r.stderr
    assert "DRY_RUN: would back up" in r.stdout
    assert rig["config"].read_text() == OLD_CONFIG
    assert _backups(rig) == []


def test_init_repairs_with_a_backup_and_keeps_the_inode(rig):
    inode = rig["config"].stat().st_ino
    r = _run(INIT_SCRIPT, rig["env"])
    assert r.returncode == 0, r.stderr
    assert "repaired: AUTH_CLASS now names " + NEW in r.stdout
    assert rig["config"].read_text() == NEW_CONFIG
    assert rig["config"].stat().st_ino == inode
    backups = _backups(rig)
    assert len(backups) == 1 and backups[0].read_text() == OLD_CONFIG


def test_init_is_idempotent(rig):
    assert _run(INIT_SCRIPT, rig["env"]).returncode == 0
    r = _run(INIT_SCRIPT, rig["env"])
    assert r.returncode == 0
    assert "already names" in r.stdout
    assert rig["config"].read_text() == NEW_CONFIG
    assert len(_backups(rig)) == 1


def test_init_skips_when_the_new_class_file_is_missing(rig):
    rig["new_class"].unlink()
    r = _run(INIT_SCRIPT, rig["env"])
    assert r.returncode == 0
    assert "older than 4.7" in r.stdout
    assert rig["config"].read_text() == OLD_CONFIG
    assert _backups(rig) == []


def test_init_ignores_a_commented_out_old_line(rig):
    rig["config"].write_text(
        "<?php\n// Setting('AUTH_CLASS', 'Grocy\\Middleware\\DefaultAuthMiddleware');\n")
    r = _run(INIT_SCRIPT, rig["env"])
    assert r.returncode == 0
    assert "sets no AUTH_CLASS" in r.stdout
    assert _backups(rig) == []


def test_init_leaves_a_custom_class_alone(rig):
    custom = "<?php\nSetting('AUTH_CLASS', 'Grocy\\Middleware\\ReverseProxyAuthMiddleware');\n"
    rig["config"].write_text(custom)
    r = _run(INIT_SCRIPT, rig["env"])
    assert r.returncode == 0
    assert "custom class" in r.stdout
    assert rig["config"].read_text() == custom


def test_init_exits_clean_without_a_config(rig):
    rig["config"].unlink()
    r = _run(INIT_SCRIPT, rig["env"])
    assert r.returncode == 0
    assert "nothing to repair" in r.stdout


@pytest.mark.skipif(not shutil.which("php"), reason="php is not installed here")
def test_init_php_path_rewrites_literally(rig):
    env = dict(rig["env"])
    env.pop("GROCY_REPAIR_TOOL")
    r = _run(INIT_SCRIPT, env)
    assert r.returncode == 0, r.stderr
    assert rig["config"].read_text() == NEW_CONFIG


# --- The appliance host helper (stub docker) ----------------------------------

STUB_DOCKER = r'''#!/usr/bin/env bash
# Stub docker for tests: logs every call, answers the container lookups with
# the fake id from CID (empty means none running), and runs `exec` commands
# locally after dropping the options and the container id.
printf '%s\n' "docker $*" >> "$DOCKER_LOG"
case "$1" in
  compose)
    if [ "$2" = "ps" ]; then printf '%s\n' "$CID"; fi
    exit 0 ;;
  ps)
    printf '%s\n' "$CID"; exit 0 ;;
  exec)
    shift
    while [ $# -gt 0 ]; do
      case "$1" in
        -i) shift ;;
        -e) shift 2 ;;
        *) break ;;
      esac
    done
    shift   # the container id
    exec "$@" ;;
esac
exit 1
'''


@pytest.fixture
def host_rig(tmp_path):
    config = tmp_path / "config.php"
    config.write_text(OLD_CONFIG)
    new_class = tmp_path / "DefaultAuthMiddleware.php"
    new_class.write_text("<?php\n")
    install = tmp_path / "install"
    install.mkdir()
    (install / "docker-compose.yml").write_text("services:\n  grocy:\n    image: x\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "docker"
    stub.write_text(STUB_DOCKER)
    stub.chmod(0o755)
    log = tmp_path / "docker.log"
    env = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "DOCKER_LOG": str(log), "CID": "abc123",
        "INSTALL_DIR": str(install),
        "GROCY_CONFIG": str(config), "GROCY_NEW_CLASS_FILE": str(new_class),
        "GROCY_REPAIR_TOOL": "sed",
    }
    return {"config": config, "new_class": new_class, "install": install,
            "log": log, "env": env, "dir": tmp_path}


def _summary(r):
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    return json.loads(lines[-1])


def _docker_calls(rig):
    return rig["log"].read_text().splitlines() if rig["log"].exists() else []


def test_host_dry_run_reports_would_repair_without_touching_anything(host_rig):
    r = _run(HOST_SCRIPT, {**host_rig["env"], "DRY_RUN": "1"})
    assert r.returncode == 0, r.stderr
    s = _summary(r)
    assert s["ok"] is True and s["action"] == "would-repair" and s["dry_run"] is True
    assert s["backup"].startswith(str(host_rig["config"]) + ".bak-")
    assert host_rig["config"].read_text() == OLD_CONFIG
    assert not any(" cp " in c for c in _docker_calls(host_rig))
    assert not any(" sh -s " in c for c in _docker_calls(host_rig))


def test_host_repairs_via_compose_container_with_backup_and_verify(host_rig):
    r = _run(HOST_SCRIPT, host_rig["env"])
    assert r.returncode == 0, r.stderr
    s = _summary(r)
    assert s["ok"] is True and s["action"] == "repaired" and s["dry_run"] is False
    assert NEW in s["reason"]
    assert host_rig["config"].read_text() == NEW_CONFIG
    backup = Path(s["backup"])
    assert backup.exists() and backup.read_text() == OLD_CONFIG
    calls = _docker_calls(host_rig)
    # Found through compose, read, gated on the new class file, backed up,
    # rewrote in the container, read back.
    assert calls[0].startswith("docker compose ps -q grocy")
    assert any("exec abc123 cat " in c for c in calls)
    assert any("exec abc123 test -f " in c for c in calls)
    assert any("exec abc123 cp -p " in c for c in calls)
    assert any("exec -i -e GROCY_REPAIR_TOOL=sed abc123 sh -s -- " in c for c in calls)
    assert calls[-1].startswith("docker exec abc123 cat ")


def test_host_is_idempotent(host_rig):
    assert _run(HOST_SCRIPT, host_rig["env"]).returncode == 0
    r = _run(HOST_SCRIPT, host_rig["env"])
    assert r.returncode == 0
    s = _summary(r)
    assert s["ok"] is True and s["action"] == "skipped"
    assert "already names" in s["reason"]
    assert len(list(host_rig["dir"].glob("config.php.bak-*"))) == 1


def test_host_skips_an_older_grocy_without_the_new_class(host_rig):
    host_rig["new_class"].unlink()
    r = _run(HOST_SCRIPT, host_rig["env"])
    assert r.returncode == 0
    s = _summary(r)
    assert s["action"] == "skipped" and "older than 4.7" in s["reason"]
    assert host_rig["config"].read_text() == OLD_CONFIG
    assert not any(" cp " in c for c in _docker_calls(host_rig))


def test_host_skips_when_no_container_is_running(host_rig):
    r = _run(HOST_SCRIPT, {**host_rig["env"], "CID": ""})
    assert r.returncode == 0
    s = _summary(r)
    assert s["ok"] is True and s["action"] == "skipped"
    assert "No running Grocy container" in s["reason"]
    assert not any(" exec " in c for c in _docker_calls(host_rig))


def test_host_falls_back_to_docker_ps_without_a_compose_project(host_rig):
    (host_rig["install"] / "docker-compose.yml").unlink()
    r = _run(HOST_SCRIPT, host_rig["env"])
    assert r.returncode == 0, r.stderr
    assert _summary(r)["action"] == "repaired"
    calls = _docker_calls(host_rig)
    assert not any(c.startswith("docker compose") for c in calls)
    assert calls[0].startswith("docker ps -q --filter name=^foodassistant-grocy$")


def test_host_skips_a_missing_config(host_rig):
    host_rig["config"].unlink()
    r = _run(HOST_SCRIPT, host_rig["env"])
    assert r.returncode == 0
    assert _summary(r)["action"] == "skipped"


def test_host_summary_is_valid_json_with_the_backslashes_intact(host_rig):
    r = _run(HOST_SCRIPT, {**host_rig["env"], "DRY_RUN": "1"})
    s = _summary(r)
    assert NEW in s["reason"]
    assert "\\\\" not in s["reason"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write a read-only file")
def test_host_failed_rewrite_restores_the_backup_and_reports_failure(host_rig):
    host_rig["config"].chmod(0o444)
    r = _run(HOST_SCRIPT, host_rig["env"])
    assert r.returncode == 1
    s = _summary(r)
    assert s["ok"] is False and s["action"] == "failed"
    assert host_rig["config"].read_text() == OLD_CONFIG


# --- The compose files mount the init folder ----------------------------------

@pytest.mark.parametrize("compose, mount", [
    (REPO / "docker-compose.yml", "./docker/grocy-init:/custom-cont-init.d:ro"),
    (REPO / "docker-compose.prod.yml", "./docker/grocy-init:/custom-cont-init.d:ro"),
    (REPO / "scripts" / "image-build" / "docker-compose.appliance.yml",
     "/docker/grocy-init:/custom-cont-init.d:ro"),
    (REPO / "unraid" / "docker-compose.yml",
     "/mnt/user/appdata/pantryraider-grocy-init:/custom-cont-init.d:ro"),
])
def test_every_managed_stack_mounts_the_self_heal_read_only(compose, mount):
    text = compose.read_text()
    assert mount in text
    # The pin stays: bumping it marches every install through the cliff.
    assert "lscr.io/linuxserver/grocy:4.6.0" in text
