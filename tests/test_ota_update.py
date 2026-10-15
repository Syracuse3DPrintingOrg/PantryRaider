"""Integration tests for the foodassistant-update OTA deploy helper (FoodAssistant-zgc1).

The helper is a root-run bash script with no .py extension. These tests run the
real script against a throwaway git checkout and deploy layout under tmp_path,
with systemctl and pip stubbed on PATH, and assert it deploys BOTH the web app
and the Stream Deck package, installs deps only when requirements change, and is
safe to re-run (idempotent) after a manual pull.

Every commit is signed by a throwaway release key the updater trusts, and the
deploy reads the staged export of the verified commit, never the checkout.

Run: python -m pytest tests/test_ota_update.py -q
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("bash", "git", "python3", "ssh-keygen")),
    reason="bash, git, python3 and ssh-keygen are required to exercise the updater",
)

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "scripts" / "image-build" / "foodassistant-update"


def _run(args, cwd=None, env=None):
    return subprocess.run(
        args, cwd=cwd, env=env, capture_output=True, text=True
    )


def _git(args, cwd, env):
    r = _run(["git", *args], cwd=cwd, env=env)
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r


@pytest.fixture(scope="session")
def release_key(tmp_path_factory):
    """A throwaway release key and the signers list that trusts it."""
    d = tmp_path_factory.mktemp("ota-key")
    key = d / "key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "ota",
                    "-f", str(key)], check=True, capture_output=True)
    kind, blob = (d / "key.pub").read_text().split()[:2]
    signers = d / "signers"
    signers.write_text(
        f'pantryraider-release namespaces="git,pantryraider-release" {kind} {blob}\n')
    return {"key": key, "signers": signers}


@pytest.fixture
def git_env():
    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.com",
    )
    return env


def _make_fake_bin(bin_dir: Path, log_path: Path) -> None:
    """A fake systemctl + pip that log their args and always succeed."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in ("systemctl",):
        p = bin_dir / name
        p.write_text(
            "#!/bin/sh\n"
            f'printf "%s %s\\n" "{name}" "$*" >> "{log_path}"\n'
            "exit 0\n"
        )
        p.chmod(0o755)
    # The package retrofits answer like a host without the package and
    # without root, so a run never depends on this machine's packages or
    # reaches its real package manager.
    for name in ("apt-get", "dpkg-query"):
        p = bin_dir / name
        p.write_text("#!/bin/sh\nexit 1\n")
        p.chmod(0o755)


def _scaffold(tmp_path: Path, git_env: dict, key: dict) -> dict:
    """Build a bare origin + working checkout + empty deploy targets."""
    origin = tmp_path / "origin.git"
    _run(["git", "init", "--bare", "-b", "main", str(origin)])

    work = tmp_path / "src"
    _run(["git", "init", "-b", "main", str(work)])
    for k, v in (("gpg.format", "ssh"), ("user.signingkey", str(key["key"])),
                 ("commit.gpgsign", "true")):
        _git(["config", k, v], work, git_env)
    (work / "service" / "app").mkdir(parents=True)
    (work / "service" / "requirements.txt").write_text("fastapi==1.0\n")
    (work / "service" / "app" / "config.py").write_text('APP_VERSION = "0.6.4"\n')
    sd = work / "streamdeck" / "foodassistant_streamdeck"
    sd.mkdir(parents=True)
    (sd / "__main__.py").write_text("print('deck')\n")
    (sd / "actions.py").write_text('COOK_ICON = "fire"\n')
    _git(["add", "-A"], work, git_env)
    _git(["commit", "-m", "init"], work, git_env)
    _git(["remote", "add", "origin", str(origin)], work, git_env)
    _git(["push", "-u", "origin", "main"], work, git_env)

    # Deploy targets: an existing (empty) app dir and a venv with a fake pip,
    # plus the parent dir the Stream Deck copy lands in.
    deploy = tmp_path / "opt"
    (deploy / "service").mkdir(parents=True)
    venv_bin = deploy / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    pip_log = tmp_path / "pip.log"
    pip = venv_bin / "pip"
    pip.write_text(
        "#!/bin/sh\n"
        f'printf "pip %s\\n" "$*" >> "{pip_log}"\n'
        "exit 0\n"
    )
    pip.chmod(0o755)

    fake_bin = tmp_path / "fakebin"
    svc_log = tmp_path / "systemctl.log"
    _make_fake_bin(fake_bin, svc_log)

    trust = tmp_path / "trust" / "release-signers"
    trust.parent.mkdir()
    trust.write_text(key["signers"].read_text())

    env = dict(git_env)
    env.update(
        REPO_DIR=str(work),
        APP_DIR=str(deploy / "service"),
        VENV_DIR=str(deploy / "venv"),
        SERVICE="foodassistant-remote.service",
        SD_DST=str(deploy / "foodassistant_streamdeck"),
        SD_SERVICE="foodassistant-streamdeck.service",
        GD_DST=str(deploy / "no-gadgets"),
        PATH=f"{fake_bin}:{env['PATH']}",
        BIN_DIR=str(tmp_path / "helpers"),
        # Release trust and the update plumbing, all under tmp_path; these
        # tests follow the signed tip of main.
        UPDATE_CHANNEL="main",
        CHANNEL_FILE=str(tmp_path / "update-channel"),
        PIN_STATE_FILE=str(tmp_path / "update-tag-restore"),
        PINNED_VERSION_FILE=str(tmp_path / "pinned-version"),
        TRUST_FILE=str(trust),
        FA_BOOTSTRAP_SIGNERS=str(key["signers"]),
        STATE_FILE=str(tmp_path / "update-state.json"),
        STAGE_ROOT=str(tmp_path / "releases"),
        FA_STAGE_MIN_MB="1",
        RELEASE_BASE_URL=(tmp_path / "published").as_uri(),
        FA_RUNNING_VERSION="0.6.4",
        SETTINGS_JSON=str(tmp_path / "settings.json"),
        # The run lock under tmp_path; the wait for the restarted app to
        # answer is covered in test_foodassistant_update.py.
        UPDATE_LOCK_FILE=str(tmp_path / "update.lock"),
        FA_HEALTH_TIMEOUT="0",
    )
    return {
        "work": work,
        "deploy": deploy,
        "env": env,
        "svc_log": svc_log,
        "pip_log": pip_log,
    }


def _last_json(stdout: str) -> dict:
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    return json.loads(lines[-1])


def test_first_run_deploys_app_and_streamdeck(tmp_path, git_env, release_key):
    ctx = _scaffold(tmp_path, git_env, release_key)
    r = _run([str(HELPER)], env=ctx["env"])
    assert r.returncode == 0, r.stdout + r.stderr
    res = _last_json(r.stdout)

    assert res["ok"] is True
    assert res["service_synced"] is True
    assert res["streamdeck_synced"] is True
    assert res["deps_installed"] is True  # deployed reqs were absent
    assert res["restarted"] is True

    # The app copy and the Stream Deck package both landed in their deploy dirs,
    # and the deck package is NOT nested one level too deep.
    assert (ctx["deploy"] / "service" / "app" / "config.py").exists()
    assert (ctx["deploy"] / "foodassistant_streamdeck" / "__main__.py").exists()
    assert not (
        ctx["deploy"] / "foodassistant_streamdeck" / "foodassistant_streamdeck"
    ).exists()

    # Both units were restarted.
    svc_log = ctx["svc_log"].read_text()
    assert "restart foodassistant-remote.service" in svc_log
    assert "restart foodassistant-streamdeck.service" in svc_log


def test_rerun_is_idempotent_and_skips_pip(tmp_path, git_env, release_key):
    ctx = _scaffold(tmp_path, git_env, release_key)
    first = _run([str(HELPER)], env=ctx["env"])
    assert first.returncode == 0, first.stdout + first.stderr

    # A second run with nothing new (mirrors re-running after a manual pull):
    # still redeploys both copies, but the deployed requirements now match the
    # source so pip is skipped.
    second = _run([str(HELPER)], env=ctx["env"])
    assert second.returncode == 0, second.stdout + second.stderr
    res = _last_json(second.stdout)
    assert res["ok"] is True
    assert res["service_synced"] is True
    assert res["streamdeck_synced"] is True
    assert res["deps_installed"] is False  # unchanged requirements, no pip


def test_manual_pull_then_helper_still_deploys_deck_change(tmp_path, git_env, release_key):
    """A deck-only change pulled by hand still reaches the device on the next run."""
    ctx = _scaffold(tmp_path, git_env, release_key)
    # Initial deploy.
    _run([str(HELPER)], env=ctx["env"])

    # New deck commit pushed to origin, then pulled MANUALLY (defeating the old
    # before==after short-circuit).
    sd_file = ctx["work"] / "streamdeck" / "foodassistant_streamdeck" / "actions.py"
    sd_file.write_text('COOK_ICON = "flame"\n')
    _git(["add", "-A"], ctx["work"], git_env)
    _git(["commit", "-m", "deck change"], ctx["work"], git_env)
    _git(["push", "origin", "main"], ctx["work"], git_env)
    # Simulate the operator pulling by hand before running the helper.
    _git(["pull", "--ff-only"], ctx["work"], git_env)

    r = _run([str(HELPER)], env=ctx["env"])
    assert r.returncode == 0, r.stdout + r.stderr
    deployed = (
        ctx["deploy"] / "foodassistant_streamdeck" / "actions.py"
    ).read_text()
    assert "flame" in deployed  # the manual-pull change was deployed


def _make_fake_docker(bin_dir: Path, log_path: Path) -> None:
    """A fake `docker` that logs its args and succeeds. `compose images -q`
    prints a constant id so the helper's before/after comparison is stable."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    p = bin_dir / "docker"
    p.write_text(
        "#!/bin/sh\n"
        f'printf "docker %s\\n" "$*" >> "{log_path}"\n'
        'if [ "$1 $2" = "compose images" ]; then echo sha256:constid; fi\n'
        "exit 0\n"
    )
    p.chmod(0o755)


def test_pi_hosted_updates_via_docker(tmp_path, git_env, release_key):
    """On a Pi Hosted box (compose project at INSTALL_DIR + docker present) the
    helper pulls and recreates the service container instead of the venv deploy
    (FoodAssistant-tu0i)."""
    ctx = _scaffold(tmp_path, git_env, release_key)

    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / "docker-compose.yml").write_text("services: {}\n")

    docker_bin = tmp_path / "dockerbin"
    docker_log = tmp_path / "docker.log"
    _make_fake_docker(docker_bin, docker_log)

    env = dict(ctx["env"])
    env["INSTALL_DIR"] = str(install_dir)
    env["PATH"] = f"{docker_bin}:{env['PATH']}"

    r = _run([str(HELPER)], env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    res = _last_json(r.stdout)
    assert res["ok"] is True

    log = docker_log.read_text()
    assert "compose pull service" in log
    assert "compose up -d --no-deps service" in log

    # The Docker path exits before the venv deploy, so the venv app dir is not
    # synced from the checkout.
    assert not (ctx["deploy"] / "service" / "app" / "config.py").exists()
    assert res["service_synced"] is False


def test_pi_hosted_update_runs_the_grocy_config_repair(tmp_path, git_env, release_key):
    """After the Docker update the helper checks the running Grocy for the
    4.7 AUTH_CLASS config.php problem by running foodassistant-grocy-repair
    from BIN_DIR (best effort, never fatal), and logs the helper's verdict."""
    ctx = _scaffold(tmp_path, git_env, release_key)

    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / "docker-compose.yml").write_text("services: {}\n")

    docker_bin = tmp_path / "dockerbin"
    docker_log = tmp_path / "docker.log"
    _make_fake_docker(docker_bin, docker_log)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    repair_log = tmp_path / "repair.log"
    stub = bin_dir / "foodassistant-grocy-repair"
    stub.write_text(
        "#!/bin/sh\n"
        f'printf "INSTALL_DIR=%s\\n" "$INSTALL_DIR" >> "{repair_log}"\n'
        'echo "[grocy-repair] AUTH_CLASS already names the new class; nothing to repair."\n'
        'echo \'{"ok":true,"action":"skipped","reason":"nothing to repair","backup":"","dry_run":false}\'\n'
        "exit 0\n"
    )
    stub.chmod(0o755)

    env = dict(ctx["env"])
    env["INSTALL_DIR"] = str(install_dir)
    env["BIN_DIR"] = str(bin_dir)
    env["PATH"] = f"{docker_bin}:{env['PATH']}"

    r = _run([str(HELPER)], env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _last_json(r.stdout)["ok"] is True
    # The helper ran once, against the compose project, after the compose up.
    assert repair_log.read_text() == f"INSTALL_DIR={install_dir}\n"
    assert "Grocy config check: [grocy-repair] AUTH_CLASS already names" in r.stdout
    assert "compose up -d --no-deps service" in docker_log.read_text()


def test_pi_hosted_update_survives_a_failing_grocy_repair(tmp_path, git_env, release_key):
    ctx = _scaffold(tmp_path, git_env, release_key)
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / "docker-compose.yml").write_text("services: {}\n")
    docker_bin = tmp_path / "dockerbin"
    _make_fake_docker(docker_bin, tmp_path / "docker.log")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "foodassistant-grocy-repair"
    stub.write_text('#!/bin/sh\necho "[grocy-repair] Could not write the backup."\nexit 1\n')
    stub.chmod(0o755)
    env = dict(ctx["env"])
    env["INSTALL_DIR"] = str(install_dir)
    env["BIN_DIR"] = str(bin_dir)
    env["PATH"] = f"{docker_bin}:{env['PATH']}"

    r = _run([str(HELPER)], env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _last_json(r.stdout)["ok"] is True
    assert "WARN: Grocy config check did not complete: [grocy-repair] Could not write the backup." in r.stdout


def test_venv_path_deploys_app_deck_and_gadgets_from_the_export(tmp_path, git_env, release_key):
    """Every host install reads the staged export of the verified commit: a
    file dropped into the checkout never reaches the device, and the reader
    package (installed earlier, so it is refreshed) comes from the export too."""
    ctx = _scaffold(tmp_path, git_env, release_key)
    gd = ctx["work"] / "gadgets" / "foodassistant_gadgets"
    gd.mkdir(parents=True)
    (gd / "__main__.py").write_text("print('reader')\n")
    _git(["add", "-A"], ctx["work"], git_env)
    _git(["commit", "-m", "reader"], ctx["work"], git_env)
    _git(["push", "origin", "main"], ctx["work"], git_env)
    (ctx["deploy"] / "no-gadgets").mkdir()
    # Untracked files in the checkout: not part of any signed release.
    (ctx["work"] / "service" / "app" / "stray.py").write_text("# local\n")
    (ctx["work"] / "streamdeck" / "foodassistant_streamdeck" / "stray.py").write_text("# local\n")
    (gd / "stray.py").write_text("# local\n")

    r = _run([str(HELPER)], env=ctx["env"])
    assert r.returncode == 0, r.stdout + r.stderr
    res = _last_json(r.stdout)
    assert res["ok"] is True and res["verified"] is True
    deploy = ctx["deploy"]
    assert (deploy / "service" / "app" / "config.py").exists()
    assert (deploy / "foodassistant_streamdeck" / "__main__.py").exists()
    assert (deploy / "no-gadgets" / "__main__.py").exists()
    assert not (deploy / "service" / "app" / "stray.py").exists()
    assert not (deploy / "foodassistant_streamdeck" / "stray.py").exists()
    assert not (deploy / "no-gadgets" / "stray.py").exists()
    assert "restart foodassistant-gadgets.service" in ctx["svc_log"].read_text()
    state = json.loads((tmp_path / "update-state.json").read_text())
    assert state["highest_version"] == "0.6.4"
    assert state["channel"] == "main"


def test_an_unsigned_commit_is_not_deployed(tmp_path, git_env, release_key):
    ctx = _scaffold(tmp_path, git_env, release_key)
    _run([str(HELPER)], env=ctx["env"])
    sd_file = ctx["work"] / "streamdeck" / "foodassistant_streamdeck" / "actions.py"
    sd_file.write_text('COOK_ICON = "unsigned"\n')
    _git(["add", "-A"], ctx["work"], git_env)
    _git(["-c", "commit.gpgsign=false", "commit", "-m", "unsigned"], ctx["work"], git_env)
    _git(["push", "origin", "main"], ctx["work"], git_env)

    r = _run([str(HELPER)], env=ctx["env"])
    res = _last_json(r.stdout)
    assert res["ok"] is False
    assert "not signed by the Pantry Raider release key" in res["error"]
    deployed = (ctx["deploy"] / "foodassistant_streamdeck" / "actions.py").read_text()
    assert "unsigned" not in deployed
