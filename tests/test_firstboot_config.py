"""Tests for the first-boot provisioner's config-parsing / decision logic.

These exercise firstboot.sh in DRY_RUN mode (no installs, no Docker, no system
writes) and assert it makes the right decisions from a given config.env. They
are pure-logic and need only bash — no network or Docker.

Run: python -m pytest tests/test_firstboot_config.py -q
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIRSTBOOT = REPO / "scripts" / "image-build" / "firstboot.sh"
COMPOSE = REPO / "scripts" / "image-build" / "docker-compose.appliance.yml"


def run_firstboot(tmp_path: Path, config: str, extra_env: dict | None = None):
    """Run firstboot.sh in DRY_RUN with a given config file; return (rc, output)."""
    cfg = tmp_path / "config.env"
    cfg.write_text(textwrap.dedent(config))
    env = {
        **os.environ,
        "DRY_RUN": "1",
        "CONFIG_CANDIDATES": str(cfg),
        "COMPOSE_SRC": str(COMPOSE),
        "DONE_MARKER": str(tmp_path / "firstboot.done"),
    }
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["bash", str(FIRSTBOOT)],
        env=env,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_defaults_only_grocy(tmp_path):
    rc, out = run_firstboot(tmp_path, "HOSTNAME=foodassistant\n")
    assert rc == 0, out
    # No optional profiles requested -> compose runs with <none>.
    assert "Compose profiles: <none>" in out
    assert "with-mealie" not in out
    assert "with-ollama" not in out


def test_mealie_enabled_adds_profile(tmp_path):
    rc, out = run_firstboot(tmp_path, "ENABLE_MEALIE=true\n")
    assert rc == 0, out
    assert "--profile with-mealie" in out
    assert "with-ollama" not in out


def test_both_optional_backends(tmp_path):
    rc, out = run_firstboot(tmp_path, "ENABLE_MEALIE=yes\nENABLE_OLLAMA=1\n")
    assert rc == 0, out
    assert "--profile with-mealie" in out
    assert "--profile with-ollama" in out


def test_custom_hostname_in_output(tmp_path):
    rc, out = run_firstboot(tmp_path, "HOSTNAME=mypantry\n")
    assert rc == 0, out
    assert "http://mypantry.local:9284/" in out
    assert "set hostname" in out.lower()


def test_kiosk_disabled_skips(tmp_path):
    rc, out = run_firstboot(tmp_path, "ENABLE_KIOSK=false\n")
    assert rc == 0, out
    assert "Kiosk not enabled" in out


def test_kiosk_auto_no_display_skips(tmp_path):
    # Default "auto": no display -> kiosk is skipped quietly.
    rc, out = run_firstboot(
        tmp_path, "ENABLE_KIOSK=auto\n", extra_env={"FORCE_DISPLAY": ""}
    )
    assert rc == 0, out
    assert "Kiosk not enabled" in out


def test_kiosk_auto_with_display_installs(tmp_path):
    # Default "auto": a display present -> kiosk installs with no flag set.
    rc, out = run_firstboot(
        tmp_path, "ENABLE_KIOSK=auto\n", extra_env={"FORCE_DISPLAY": "1"}
    )
    assert rc == 0, out
    assert "Installing Chromium kiosk" in out


def test_kiosk_enabled_without_display_warns(tmp_path):
    # No display forced -> kiosk requested but skipped with a warning.
    rc, out = run_firstboot(
        tmp_path, "ENABLE_KIOSK=true\n", extra_env={"FORCE_DISPLAY": ""}
    )
    assert rc == 0, out
    assert "no display detected" in out


def test_kiosk_enabled_with_display(tmp_path):
    rc, out = run_firstboot(
        tmp_path, "ENABLE_KIOSK=true\n", extra_env={"FORCE_DISPLAY": "1"}
    )
    assert rc == 0, out
    assert "Installing Chromium kiosk" in out


def test_remote_mode_skips_docker_and_stack(tmp_path):
    rc, out = run_firstboot(
        tmp_path,
        "DEPLOYMENT_MODE=pi_remote\nREMOTE_SERVER_URL=http://192.168.1.50:9284\n",
    )
    assert rc == 0, out
    assert "Pi Remote mode: skipping Docker" in out
    # The heavy steps must not run in remote mode.
    assert "Deploying stack" not in out
    assert "192.168.1.50:9284" in out


def test_remote_mode_kiosk_points_at_remote(tmp_path):
    rc, out = run_firstboot(
        tmp_path,
        "DEPLOYMENT_MODE=pi_remote\nREMOTE_SERVER_URL=http://server.local:9284\n",
        extra_env={"FORCE_DISPLAY": "1"},
    )
    assert rc == 0, out
    assert "Installing Chromium kiosk" in out
    # Kiosk URL is the remote server, not localhost.
    assert "server.local:9284/ui/?kiosk=1" in out
    assert "localhost:9284/ui" not in out


def test_remote_mode_without_url_warns(tmp_path):
    rc, out = run_firstboot(tmp_path, "DEPLOYMENT_MODE=pi_remote\n")
    assert rc == 0, out
    assert "REMOTE_SERVER_URL is empty" in out


def test_remote_mode_streamdeck_gets_base_env(tmp_path):
    rc, out = run_firstboot(
        tmp_path,
        "DEPLOYMENT_MODE=pi_remote\nREMOTE_SERVER_URL=http://server.local:9284\n",
        extra_env={"FORCE_STREAMDECK": "1"},
    )
    assert rc == 0, out
    # The DRY_RUN streamdeck step announces the remote base it will inject.
    assert "base http://server.local:9284" in out


def test_hosted_mode_still_deploys_stack(tmp_path):
    rc, out = run_firstboot(tmp_path, "DEPLOYMENT_MODE=pi_hosted\n")
    assert rc == 0, out
    assert "Deploying stack" in out
    assert "Pi Remote mode" not in out


def test_mode_read_from_settings_json(tmp_path):
    # A settings.json (written by the web wizard) overrides config.env mode.
    sf = tmp_path / "settings.json"
    sf.write_text('{"deployment_mode": "pi_remote", "remote_server_url": "http://from-json:9284"}')
    rc, out = run_firstboot(
        tmp_path, "HOSTNAME=foodassistant\n",
        extra_env={"SETTINGS_JSON": str(sf)},
    )
    assert rc == 0, out
    assert "Pi Remote mode: skipping Docker" in out
    assert "from-json:9284" in out


def test_tag_pinning_propagates(tmp_path):
    rc, out = run_firstboot(tmp_path, "FOODASSISTANT_TAG=v1.2.3\n")
    assert rc == 0, out
    assert "TAG=v1.2.3" in out


def test_done_marker_skips_rerun(tmp_path):
    marker = tmp_path / "firstboot.done"
    marker.write_text("2026-01-01T00:00:00Z\n")
    rc, out = run_firstboot(
        tmp_path, "HOSTNAME=foodassistant\n",
        extra_env={"DONE_MARKER": str(marker)},
    )
    assert rc == 0, out
    assert "Already provisioned" in out


def test_missing_config_uses_defaults(tmp_path):
    # Point at a nonexistent config path.
    env = {
        **os.environ,
        "DRY_RUN": "1",
        "CONFIG_CANDIDATES": str(tmp_path / "nope.env"),
        "COMPOSE_SRC": str(COMPOSE),
        "DONE_MARKER": str(tmp_path / "firstboot.done"),
    }
    proc = subprocess.run(
        ["bash", str(FIRSTBOOT)], env=env, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout + proc.stderr
    assert "No config file found" in out
    assert "http://foodassistant.local:9284/" in out
