"""The Grocy config.php repair scripts (the Grocy 4.7 AUTH_CLASS move).

Grocy 4.7 moved the Default, ReverseProxy, and Ldap auth middlewares into a
Grocy\\Middleware\\Auth namespace. Both scripts make the active AUTH_CLASS line
match the Grocy that is installed, in either direction: forward on a 4.7 tree
(middleware/Auth/ exists), back on a 4.6 tree after a rollback (no Auth/
folder, the old class file present). A fake middleware folder stands in for
the one inside the container.

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
NAMES = ("Default", "ReverseProxy", "Ldap")


def old_class(name):
    return "Grocy\\Middleware\\" + name + "AuthMiddleware"


def new_class(name):
    return "Grocy\\Middleware\\Auth\\" + name + "AuthMiddleware"


OLD_CONFIG = (
    "<?php\n"
    "// Grocy config, edited by hand\n"
    "Setting('MODE', 'production');\n"
    "// Setting('AUTH_CLASS', 'Grocy\\Middleware\\ReverseProxyAuthMiddleware');\n"
    "Setting('AUTH_CLASS', 'Grocy\\Middleware\\DefaultAuthMiddleware');\n"
    "Setting('BASE_URL', '/');\n"
)
NEW_CONFIG = OLD_CONFIG.replace(OLD, NEW)


def config_naming(cls):
    """A config.php whose active AUTH_CLASS line names cls, with the same
    commented-out line and neighbours as OLD_CONFIG."""
    return OLD_CONFIG.replace("'" + OLD + "'", "'" + cls + "'")


def grocy_47_tree(mw):
    """Grocy 4.7's middleware folder: the auth classes live under Auth/."""
    if mw.exists():
        shutil.rmtree(mw)
    (mw / "Auth").mkdir(parents=True)
    for name in NAMES:
        (mw / "Auth" / f"{name}AuthMiddleware.php").write_text(
            "<?php namespace Grocy\\Middleware\\Auth;\n")
    (mw / "BaseMiddleware.php").write_text("<?php\n")


def grocy_46_tree(mw):
    """Grocy 4.6's middleware folder: no Auth/, the classes sit at the top."""
    if mw.exists():
        shutil.rmtree(mw)
    mw.mkdir(parents=True)
    for name in NAMES:
        (mw / f"{name}AuthMiddleware.php").write_text(
            "<?php namespace Grocy\\Middleware;\n")
    (mw / "BaseMiddleware.php").write_text("<?php\n")


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
    mw = tmp_path / "middleware"
    grocy_47_tree(mw)
    env = {"GROCY_CONFIG": str(config), "GROCY_MIDDLEWARE_DIR": str(mw),
           "GROCY_REPAIR_TOOL": "sed"}
    return {"config": config, "mw": mw, "env": env, "dir": tmp_path}


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


def test_init_skips_the_old_class_on_an_older_grocy(rig):
    grocy_46_tree(rig["mw"])
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


@pytest.mark.parametrize("cls", [
    "MyHouse\\Grocy\\SsoAuthMiddleware",
    # Starts like a built-in class but is longer, so it is someone's own.
    "Grocy\\Middleware\\DefaultAuthMiddlewareCustom",
])
@pytest.mark.parametrize("tree", [grocy_46_tree, grocy_47_tree])
def test_init_leaves_a_custom_class_alone(rig, cls, tree):
    tree(rig["mw"])
    custom = config_naming(cls)
    rig["config"].write_text(custom)
    r = _run(INIT_SCRIPT, rig["env"])
    assert r.returncode == 0
    assert "custom class" in r.stdout
    assert rig["config"].read_text() == custom
    assert _backups(rig) == []


@pytest.mark.parametrize("name", NAMES)
def test_init_moves_each_builtin_class_forward_on_grocy_47(rig, name):
    rig["config"].write_text(config_naming(old_class(name)))
    r = _run(INIT_SCRIPT, rig["env"])
    assert r.returncode == 0, r.stderr
    assert "repaired: AUTH_CLASS now names " + new_class(name) in r.stdout
    assert rig["config"].read_text() == config_naming(new_class(name))
    backups = _backups(rig)
    assert len(backups) == 1
    assert backups[0].read_text() == config_naming(old_class(name))


@pytest.mark.parametrize("name", NAMES)
def test_init_moves_each_builtin_class_back_after_a_rollback_to_46(rig, name):
    # 4.7 already rewrote the line, then the image went back to 4.6.0: the
    # Auth\ class does not exist there and every request fails.
    grocy_46_tree(rig["mw"])
    rig["config"].write_text(config_naming(new_class(name)))
    inode = rig["config"].stat().st_ino
    r = _run(INIT_SCRIPT, rig["env"])
    assert r.returncode == 0, r.stderr
    assert "repaired: AUTH_CLASS now names " + old_class(name) + " again" in r.stdout
    assert rig["config"].read_text() == config_naming(old_class(name))
    assert rig["config"].stat().st_ino == inode
    backups = _backups(rig)
    assert len(backups) == 1
    assert backups[0].read_text() == config_naming(new_class(name))


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("tree, start", [
    (grocy_47_tree, old_class),
    (grocy_46_tree, new_class),
])
def test_init_reruns_are_idempotent_in_both_directions(rig, name, tree, start):
    tree(rig["mw"])
    rig["config"].write_text(config_naming(start(name)))
    assert _run(INIT_SCRIPT, rig["env"]).returncode == 0
    fixed = rig["config"].read_text()
    for _ in range(2):
        r = _run(INIT_SCRIPT, rig["env"])
        assert r.returncode == 0
        assert "repaired" not in r.stdout
        assert rig["config"].read_text() == fixed
    assert len(_backups(rig)) == 1


# Grocy's own config.php lists the classes in a comment right above the
# setting; only the Setting line may change.
DOC_COMMENT = ('// Either "Grocy\\Middleware\\DefaultAuthMiddleware", '
               '"Grocy\\Middleware\\ReverseProxyAuthMiddleware"\n'
               '// or any class that implements Grocy\\Middleware\\AuthMiddleware\n')


@pytest.mark.parametrize("tree, start, end", [
    (grocy_47_tree, OLD, NEW),
    (grocy_46_tree, NEW, OLD),
])
def test_init_changes_only_the_setting_line(rig, tree, start, end):
    tree(rig["mw"])
    body = "<?php\n" + DOC_COMMENT.replace(OLD, start) + \
        "Setting('AUTH_CLASS', '" + start + "');\n"
    rig["config"].write_text(body)
    r = _run(INIT_SCRIPT, rig["env"])
    assert r.returncode == 0, r.stderr
    assert rig["config"].read_text() == "<?php\n" + DOC_COMMENT.replace(OLD, start) + \
        "Setting('AUTH_CLASS', '" + end + "');\n"


def test_init_keeps_the_new_class_when_the_old_file_is_missing_too(rig):
    # No Auth/ folder but no old class file either: not a layout this script
    # knows, so it changes nothing.
    grocy_46_tree(rig["mw"])
    (rig["mw"] / "DefaultAuthMiddleware.php").unlink()
    rig["config"].write_text(NEW_CONFIG)
    r = _run(INIT_SCRIPT, rig["env"])
    assert r.returncode == 0
    assert "already names" in r.stdout
    assert rig["config"].read_text() == NEW_CONFIG
    assert _backups(rig) == []


def test_init_reverse_dry_run_writes_nothing(rig):
    grocy_46_tree(rig["mw"])
    rig["config"].write_text(NEW_CONFIG)
    r = _run(INIT_SCRIPT, {**rig["env"], "DRY_RUN": "1"})
    assert r.returncode == 0
    assert "DRY_RUN: would back up" in r.stdout and "to " + OLD in r.stdout
    assert rig["config"].read_text() == NEW_CONFIG
    assert _backups(rig) == []


@pytest.mark.skipif(not shutil.which("busybox"), reason="busybox is not installed here")
@pytest.mark.parametrize("tree, start, end", [
    (grocy_47_tree, OLD, NEW),
    (grocy_46_tree, NEW, OLD),
])
def test_init_sed_path_works_with_busybox_sed(rig, tmp_path, tree, start, end):
    # The Grocy image is Alpine, so its sed is busybox.
    bb = tmp_path / "bb"
    bb.mkdir()
    (bb / "sed").symlink_to(shutil.which("busybox"))
    tree(rig["mw"])
    rig["config"].write_text(config_naming(start))
    env = {**rig["env"], "PATH": f"{bb}:{os.environ.get('PATH', '')}"}
    r = _run(INIT_SCRIPT, env)
    assert r.returncode == 0, r.stderr
    assert rig["config"].read_text() == config_naming(end)


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


@pytest.mark.skipif(not shutil.which("php"), reason="php is not installed here")
def test_init_php_path_rewrites_back_literally(rig):
    grocy_46_tree(rig["mw"])
    rig["config"].write_text(NEW_CONFIG)
    env = dict(rig["env"])
    env.pop("GROCY_REPAIR_TOOL")
    r = _run(INIT_SCRIPT, env)
    assert r.returncode == 0, r.stderr
    assert rig["config"].read_text() == OLD_CONFIG


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
    mw = tmp_path / "middleware"
    grocy_47_tree(mw)
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
        "GROCY_CONFIG": str(config), "GROCY_MIDDLEWARE_DIR": str(mw),
        "GROCY_REPAIR_TOOL": "sed",
    }
    return {"config": config, "mw": mw, "install": install,
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
    grocy_46_tree(host_rig["mw"])
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


def _host_backups(rig):
    return sorted(rig["dir"].glob("config.php.bak-*"))


@pytest.mark.parametrize("name", NAMES)
def test_host_moves_each_builtin_class_forward_on_grocy_47(host_rig, name):
    host_rig["config"].write_text(config_naming(old_class(name)))
    r = _run(HOST_SCRIPT, host_rig["env"])
    assert r.returncode == 0, r.stderr
    s = _summary(r)
    assert s["ok"] is True and s["action"] == "repaired"
    assert new_class(name) in s["reason"]
    assert host_rig["config"].read_text() == config_naming(new_class(name))
    assert Path(s["backup"]).read_text() == config_naming(old_class(name))


@pytest.mark.parametrize("name", NAMES)
def test_host_moves_each_builtin_class_back_after_a_rollback_to_46(host_rig, name):
    grocy_46_tree(host_rig["mw"])
    host_rig["config"].write_text(config_naming(new_class(name)))
    r = _run(HOST_SCRIPT, host_rig["env"])
    assert r.returncode == 0, r.stderr
    s = _summary(r)
    assert s["ok"] is True and s["action"] == "repaired"
    assert "back to " + old_class(name) in s["reason"]
    assert host_rig["config"].read_text() == config_naming(old_class(name))
    backup = Path(s["backup"])
    assert backup.read_text() == config_naming(new_class(name))
    calls = _docker_calls(host_rig)
    assert any("exec abc123 test -d " in c for c in calls)
    assert any("exec abc123 cp -p " in c for c in calls)


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("tree, start", [
    (grocy_47_tree, old_class),
    (grocy_46_tree, new_class),
])
def test_host_reruns_are_idempotent_in_both_directions(host_rig, name, tree, start):
    tree(host_rig["mw"])
    host_rig["config"].write_text(config_naming(start(name)))
    assert _summary(_run(HOST_SCRIPT, host_rig["env"]))["action"] == "repaired"
    fixed = host_rig["config"].read_text()
    for _ in range(2):
        r = _run(HOST_SCRIPT, host_rig["env"])
        assert r.returncode == 0
        assert _summary(r)["action"] == "skipped"
        assert host_rig["config"].read_text() == fixed
    assert len(_host_backups(host_rig)) == 1


@pytest.mark.parametrize("cls", [
    "MyHouse\\Grocy\\SsoAuthMiddleware",
    "Grocy\\Middleware\\Auth\\LdapAuthMiddlewareCustom",
])
@pytest.mark.parametrize("tree", [grocy_46_tree, grocy_47_tree])
def test_host_leaves_a_custom_class_alone(host_rig, cls, tree):
    tree(host_rig["mw"])
    custom = config_naming(cls)
    host_rig["config"].write_text(custom)
    r = _run(HOST_SCRIPT, host_rig["env"])
    assert r.returncode == 0
    s = _summary(r)
    assert s["action"] == "skipped" and "custom class" in s["reason"]
    assert host_rig["config"].read_text() == custom
    assert _host_backups(host_rig) == []
    assert not any(" cp " in c for c in _docker_calls(host_rig))


def test_host_changes_only_the_setting_line(host_rig):
    grocy_46_tree(host_rig["mw"])
    head = "<?php\n" + DOC_COMMENT.replace(OLD, NEW)
    host_rig["config"].write_text(head + "  Setting('AUTH_CLASS', '" + NEW + "');\n")
    r = _run(HOST_SCRIPT, host_rig["env"])
    assert r.returncode == 0, r.stderr
    assert _summary(r)["action"] == "repaired"
    assert host_rig["config"].read_text() == \
        head + "  Setting('AUTH_CLASS', '" + OLD + "');\n"


def test_host_leaves_the_new_class_alone_on_grocy_47(host_rig):
    host_rig["config"].write_text(NEW_CONFIG)
    r = _run(HOST_SCRIPT, host_rig["env"])
    assert r.returncode == 0
    s = _summary(r)
    assert s["action"] == "skipped" and "already names" in s["reason"]
    assert host_rig["config"].read_text() == NEW_CONFIG


@pytest.mark.skipif(not shutil.which("busybox"), reason="busybox is not installed here")
def test_host_sed_path_works_with_busybox_sed(host_rig):
    bb = host_rig["dir"] / "bb"
    bb.mkdir()
    (bb / "sed").symlink_to(shutil.which("busybox"))
    grocy_46_tree(host_rig["mw"])
    host_rig["config"].write_text(NEW_CONFIG)
    env = {**host_rig["env"], "PATH": f"{bb}:{host_rig['env']['PATH']}"}
    r = _run(HOST_SCRIPT, env)
    assert r.returncode == 0, r.stderr
    assert _summary(r)["action"] == "repaired"
    assert host_rig["config"].read_text() == OLD_CONFIG


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
