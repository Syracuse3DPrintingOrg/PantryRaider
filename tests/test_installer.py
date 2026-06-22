"""Tests for the on-device installer (loader) decision logic.

These run install.sh in PLAN_ONLY mode, which resolves the deployment mode and
add-on flags from hardware detection + env overrides and prints a single stable
"PLAN ..." line without cloning the repo, using sudo, or provisioning. Pure
bash, no network/Docker.

Run: python -m pytest tests/test_installer.py -q
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INSTALL = REPO / "install.sh"


def plan(extra_env: dict | None = None) -> dict:
    """Run install.sh in PLAN_ONLY mode; return the parsed PLAN fields."""
    env = {
        **os.environ,
        "NONINTERACTIVE": "1",
        "PLAN_ONLY": "1",
        "NO_COLOR": "1",
    }
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["bash", str(INSTALL)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    line = next(
        (l for l in proc.stdout.splitlines() if l.startswith("PLAN ")), ""
    )
    assert line, "no PLAN line in output:\n" + proc.stdout + proc.stderr
    fields = {}
    for tok in line[len("PLAN "):].split():
        k, _, v = tok.partition("=")
        fields[k] = v
    return fields


def test_script_is_valid_bash():
    proc = subprocess.run(["bash", "-n", str(INSTALL)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_pi_defaults_to_pi_hosted():
    p = plan({"FORCE_PI": "1"})
    assert p["mode"] == "pi_hosted"


def test_non_pi_defaults_to_server():
    # No FORCE_PI: detection on a CI box returns not-a-Pi.
    p = plan()
    assert p["mode"] == "server"


def test_display_present_defaults_kiosk_on():
    p = plan({"FORCE_PI": "1", "FORCE_DISPLAY": "1"})
    assert p["kiosk"] == "true"


def test_no_display_defaults_kiosk_off():
    p = plan({"FORCE_PI": "1"})
    assert p["kiosk"] == "false"


def test_streamdeck_present_defaults_on():
    p = plan({"FORCE_PI": "1", "FORCE_STREAMDECK": "1"})
    assert p["streamdeck"] == "true"


def test_streamdeck_absent_defaults_off():
    p = plan({"FORCE_PI": "1"})
    assert p["streamdeck"] == "false"


def test_pi_remote_keeps_server_url():
    p = plan({
        "FORCE_PI": "1",
        "DEPLOYMENT_MODE": "pi_remote",
        "REMOTE_SERVER_URL": "http://192.168.1.50:9284",
    })
    assert p["mode"] == "pi_remote"
    assert p["remote"] == "http://192.168.1.50:9284"


def test_pi_remote_forces_mealie_ollama_off():
    # Even if the env asks for Mealie, a thin remote installs nothing heavy.
    p = plan({
        "FORCE_PI": "1",
        "DEPLOYMENT_MODE": "pi_remote",
        "REMOTE_SERVER_URL": "http://x:9284",
        "ENABLE_MEALIE": "true",
        "ENABLE_OLLAMA": "true",
    })
    assert p["mealie"] == "false"
    assert p["ollama"] == "false"


def test_mealie_opt_in_on_hosted():
    p = plan({"FORCE_PI": "1", "ENABLE_MEALIE": "true"})
    assert p["mealie"] == "true"


def test_rotation_passthrough():
    p = plan({"FORCE_PI": "1", "FORCE_DISPLAY": "1", "DISPLAY_ROTATION": "270"})
    assert p["rotation"] == "270"


def test_repo_dir_default_is_on_device():
    p = plan({"FORCE_PI": "1"})
    # Never the user's PC working copy; an on-device path.
    assert p["repo_dir"].startswith("/opt/")
