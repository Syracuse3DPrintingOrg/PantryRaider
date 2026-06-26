"""Integration tests for the foodassistant-update OTA deploy helper (FoodAssistant-zgc1).

The helper is a root-run bash script with no .py extension. These tests run the
real script against a throwaway git checkout and deploy layout under tmp_path,
with systemctl and pip stubbed on PATH, and assert it deploys BOTH the web app
and the Stream Deck package, installs deps only when requirements change, and is
safe to re-run (idempotent) after a manual pull.

Run: python -m pytest tests/test_ota_update.py -q
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

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


def _scaffold(tmp_path: Path, git_env: dict) -> dict:
    """Build a bare origin + working checkout + empty deploy targets."""
    origin = tmp_path / "origin.git"
    _run(["git", "init", "--bare", "-b", "main", str(origin)])

    work = tmp_path / "src"
    _run(["git", "init", "-b", "main", str(work)])
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

    env = dict(git_env)
    env.update(
        REPO_DIR=str(work),
        APP_DIR=str(deploy / "service"),
        VENV_DIR=str(deploy / "venv"),
        SERVICE="foodassistant-remote.service",
        SD_DST=str(deploy / "foodassistant_streamdeck"),
        SD_SERVICE="foodassistant-streamdeck.service",
        PATH=f"{fake_bin}:{env['PATH']}",
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


def test_first_run_deploys_app_and_streamdeck(tmp_path, git_env):
    ctx = _scaffold(tmp_path, git_env)
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


def test_rerun_is_idempotent_and_skips_pip(tmp_path, git_env):
    ctx = _scaffold(tmp_path, git_env)
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


def test_manual_pull_then_helper_still_deploys_deck_change(tmp_path, git_env):
    """A deck-only change pulled by hand still reaches the device on the next run."""
    ctx = _scaffold(tmp_path, git_env)
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
