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


def test_default_enables_mealie(tmp_path):
    # A pi_hosted appliance is a full kitchen hub, so a default (non-remote)
    # install now ships Mealie on and pulls its image during provisioning.
    rc, out = run_firstboot(tmp_path, "HOSTNAME=foodassistant\n")
    assert rc == 0, out
    assert "--profile with-mealie" in out
    assert "with-ollama" not in out


def test_mealie_disabled_runs_grocy_only(tmp_path):
    # Explicit opt-out drops back to Grocy only (<none> optional profiles).
    rc, out = run_firstboot(tmp_path, "ENABLE_MEALIE=false\n")
    assert rc == 0, out
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
    assert "http://mypantry.local/" in out
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


def test_hide_cursor_auto_no_pointer_hides(tmp_path):
    # HIDE_CURSOR=auto with no pointer device attached -> hide the cursor.
    rc, out = run_firstboot(
        tmp_path,
        "ENABLE_KIOSK=true\n",
        extra_env={"FORCE_DISPLAY": "1", "FORCE_POINTER": ""},
    )
    assert rc == 0, out
    assert "HIDE_CURSOR=auto" in out
    assert "Cursor will be hidden" in out


def test_hide_cursor_auto_with_pointer_shows(tmp_path):
    # HIDE_CURSOR=auto with a pointer device present -> keep the cursor visible.
    rc, out = run_firstboot(
        tmp_path,
        "ENABLE_KIOSK=true\n",
        extra_env={"FORCE_DISPLAY": "1", "FORCE_POINTER": "1"},
    )
    assert rc == 0, out
    assert "Cursor will be shown" in out
    assert "Cursor will be hidden" not in out


def test_hide_cursor_false_never_hides(tmp_path):
    # HIDE_CURSOR=false keeps the cursor even with no pointer device.
    rc, out = run_firstboot(
        tmp_path,
        "ENABLE_KIOSK=true\nHIDE_CURSOR=false\n",
        extra_env={"FORCE_DISPLAY": "1", "FORCE_POINTER": ""},
    )
    assert rc == 0, out
    assert "HIDE_CURSOR=false" in out
    assert "Cursor will be shown" in out


def test_hide_cursor_true_hides_even_with_pointer(tmp_path):
    # HIDE_CURSOR=true forces hiding even when a mouse is attached.
    rc, out = run_firstboot(
        tmp_path,
        "ENABLE_KIOSK=true\nHIDE_CURSOR=true\n",
        extra_env={"FORCE_DISPLAY": "1", "FORCE_POINTER": "1"},
    )
    assert rc == 0, out
    assert "HIDE_CURSOR=true" in out
    assert "Cursor will be hidden" in out


def test_remote_mode_skips_docker_and_stack(tmp_path):
    rc, out = run_firstboot(
        tmp_path,
        "DEPLOYMENT_MODE=pi_remote\nREMOTE_SERVER_URL=http://192.168.1.50:9284\n",
    )
    assert rc == 0, out
    assert "Satellite mode: skipping Docker" in out
    # The heavy steps must not run in remote mode.
    assert "Deploying stack" not in out
    assert "192.168.1.50:9284" in out


def test_remote_mode_kiosk_points_at_local_app(tmp_path):
    # A satellite runs the full app locally on port 80 and pulls its backend
    # config from the main server, so the kiosk shows the LOCAL UI. The app's
    # setup-redirect handles the unconfigured case.
    rc, out = run_firstboot(
        tmp_path,
        "DEPLOYMENT_MODE=pi_remote\nREMOTE_SERVER_URL=http://server.local:9284\n",
        extra_env={"FORCE_DISPLAY": "1"},
    )
    assert rc == 0, out
    assert "Installing Chromium kiosk" in out
    # Kiosk URL is the local app on port 80, not the remote server.
    assert "localhost/ui/?kiosk=1" in out
    assert "server.local:9284/ui" not in out


def test_remote_mode_without_url_instructs_web_ui(tmp_path):
    # No REMOTE_SERVER_URL: provisioner should succeed and tell the user to
    # open the web UI to configure the URL (no longer a fatal warning).
    rc, out = run_firstboot(tmp_path, "DEPLOYMENT_MODE=pi_remote\n")
    assert rc == 0, out
    assert "configure via web UI" in out or "browser" in out.lower()


def test_remote_mode_streamdeck_drives_local_app(tmp_path):
    rc, out = run_firstboot(
        tmp_path,
        "DEPLOYMENT_MODE=pi_remote\nREMOTE_SERVER_URL=http://server.local:9284\n",
        extra_env={"FORCE_STREAMDECK": "1"},
    )
    assert rc == 0, out
    # The satellite's deck drives its own local app on port 80, not the server.
    assert "base http://localhost:80" in out
    # The deck base must not be the remote server.
    assert "base http://server.local:9284" not in out


def test_hosted_mode_still_deploys_stack(tmp_path):
    rc, out = run_firstboot(tmp_path, "DEPLOYMENT_MODE=pi_hosted\n")
    assert rc == 0, out
    assert "Deploying stack" in out
    assert "Pi Remote mode" not in out


def test_hosted_mode_configures_port80_redirect(tmp_path):
    # pi_hosted should set up the iptables 80 -> 9284 redirect so the UI is
    # reachable on port 80.
    rc, out = run_firstboot(tmp_path, "DEPLOYMENT_MODE=pi_hosted\n")
    assert rc == 0, out
    assert "PREROUTING 80->9284" in out
    assert "9284" in out


def test_hosted_mode_port80_persistence_invoked(tmp_path):
    # The redirect must survive reboot: a systemd unit re-applies it on boot and
    # iptables-persistent is used as a secondary save. Both must be referenced.
    rc, out = run_firstboot(tmp_path, "DEPLOYMENT_MODE=pi_hosted\n")
    assert rc == 0, out
    assert "foodassistant-port80.service" in out
    assert "re-apply the redirect on every boot" in out
    assert "iptables-persistent" in out


def test_hosted_mode_configures_mdns_avahi(tmp_path):
    # pi_hosted should install/enable avahi so <hostname>.local resolves.
    rc, out = run_firstboot(tmp_path, "DEPLOYMENT_MODE=pi_hosted\n")
    assert rc == 0, out
    assert "avahi-daemon" in out
    assert "enable --now avahi-daemon" in out
    assert ".local should resolve" in out


def test_hosted_mode_mdns_notes_windows_bonjour(tmp_path):
    # A note about Windows needing Bonjour helps users who cannot resolve .local.
    rc, out = run_firstboot(tmp_path, "DEPLOYMENT_MODE=pi_hosted\n")
    assert rc == 0, out
    assert "Bonjour" in out


def test_remote_mode_does_not_redirect_port80(tmp_path):
    # The satellite binds uvicorn directly on port 80; it must NOT run the
    # iptables redirect step (no PREROUTING 80->9284 hijack).
    rc, out = run_firstboot(
        tmp_path,
        "DEPLOYMENT_MODE=pi_remote\nREMOTE_SERVER_URL=http://server.local:9284\n",
    )
    assert rc == 0, out
    assert "PREROUTING 80->9284" not in out
    assert "foodassistant-port80.service" not in out


def test_default_mode_runs_port80_step(tmp_path):
    # A plain (non-remote) deployment with no explicit mode still runs the
    # port80 redirect step (same hosted path).
    rc, out = run_firstboot(tmp_path, "HOSTNAME=foodassistant\n")
    assert rc == 0, out
    assert "PREROUTING 80->9284" in out
    assert "foodassistant-port80.service" in out


def test_mode_read_from_settings_json(tmp_path):
    # A settings.json (written by the web wizard) overrides config.env mode.
    sf = tmp_path / "settings.json"
    sf.write_text('{"deployment_mode": "pi_remote", "remote_server_url": "http://from-json:9284"}')
    rc, out = run_firstboot(
        tmp_path, "HOSTNAME=foodassistant\n",
        extra_env={"SETTINGS_JSON": str(sf)},
    )
    assert rc == 0, out
    assert "Satellite mode: skipping Docker" in out
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


def test_steps_streamdeck_bypasses_done_marker(tmp_path):
    # STEPS=streamdeck should bypass the done marker and run only streamdeck.
    marker = tmp_path / "firstboot.done"
    marker.write_text("2026-01-01T00:00:00Z\n")
    rc, out = run_firstboot(
        tmp_path, "HOSTNAME=foodassistant\n",
        extra_env={"DONE_MARKER": str(marker), "STEPS": "streamdeck", "FORCE_STREAMDECK": "1"},
    )
    assert rc == 0, out
    assert "Already provisioned" not in out
    assert "Targeted step run" in out
    assert "Installing Stream Deck controller" in out
    # mark_done must NOT fire for a targeted run.
    assert "DRY_RUN would touch" not in out


def test_steps_only_runs_named_steps(tmp_path):
    # STEPS=kiosk should skip docker and stack entirely.
    rc, out = run_firstboot(
        tmp_path, "HOSTNAME=foodassistant\n",
        extra_env={"STEPS": "kiosk", "FORCE_DISPLAY": ""},
    )
    assert rc == 0, out
    assert "Deploying stack" not in out
    assert "DRY_RUN would download and run" not in out
    # kiosk step ran (no display, so it logged the skip)
    assert "Kiosk not enabled" in out


def test_steps_empty_runs_all_and_marks_done(tmp_path):
    # Default (STEPS unset) runs all steps and writes the done marker.
    rc, out = run_firstboot(tmp_path, "HOSTNAME=foodassistant\n")
    assert rc == 0, out
    assert "Targeted step run" not in out
    assert "DRY_RUN would touch" in out


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
    assert "http://foodassistant.local/" in out


def test_display_rotation_zero_normal(tmp_path):
    # DISPLAY_ROTATION=0 maps to the compositor "normal" transform.
    rc, out = run_firstboot(tmp_path, "DISPLAY_ROTATION=0\n",
                            extra_env={"STEPS": "rotation"})
    assert rc == 0, out
    assert "WLR_OUTPUT_TRANSFORM=normal" in out


def test_display_rotation_180_dry_run(tmp_path):
    # DISPLAY_ROTATION=180 sets the compositor transform in the kiosk env file.
    rc, out = run_firstboot(tmp_path, "DISPLAY_ROTATION=180\n",
                            extra_env={"STEPS": "rotation"})
    assert rc == 0, out
    assert "WLR_OUTPUT_TRANSFORM=180" in out


def test_display_rotation_invalid_warns(tmp_path):
    # An invalid value should warn and skip without error.
    rc, out = run_firstboot(tmp_path, "DISPLAY_ROTATION=45\n",
                            extra_env={"STEPS": "rotation"})
    assert rc == 0, out
    assert "not valid" in out


def test_display_rotation_step_targeted(tmp_path):
    # STEPS=rotation should only run the rotation step.
    rc, out = run_firstboot(tmp_path, "DISPLAY_ROTATION=90\nHOSTNAME=foodassistant\n",
                            extra_env={"STEPS": "rotation"})
    assert rc == 0, out
    assert "Deploying stack" not in out
    assert "Targeted step run" in out
