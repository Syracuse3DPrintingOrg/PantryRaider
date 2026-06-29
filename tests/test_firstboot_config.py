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


def test_kiosk_sets_up_seatd(tmp_path):
    # seatd brokers DRM/VT to the kiosk user on a headless-provisioned Pi
    # (FoodAssistant-hmr3); the provisioning path must enable it and add the
    # kiosk user to the _seatd group.
    rc, out = run_firstboot(
        tmp_path, "ENABLE_KIOSK=true\n", extra_env={"FORCE_DISPLAY": "1"}
    )
    assert rc == 0, out
    assert "seatd" in out
    assert "_seatd" in out


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


# Touchscreen calibration (FoodAssistant-8ji): the ADS7846 SPI panel needs a
# measured affine, not identity, or taps land off. Priority: explicit
# TOUCH_CALIBRATION_MATRIX > ads7846 measured default (unrotated) > rotation.

ADS7846_MEASURED = "0 1.3753 -0.1688 1.2635 0 -0.1166"


def _touch_run(tmp_path, env):
    return run_firstboot(tmp_path, "HOSTNAME=foodassistant\n",
                         extra_env={"STEPS": "touch", **env})


def test_ads7846_uses_measured_default_matrix(tmp_path):
    rc, out = _touch_run(tmp_path, {"TOUCH_DRIVER": "ads7846"})
    assert rc == 0, out
    assert f"matrix={ADS7846_MEASURED}" in out
    assert "name=ADS7846*" in out
    assert "99-foodassistant-touch.rules" in out
    assert "LIBINPUT_CALIBRATION_MATRIX" in out


def test_explicit_matrix_overrides_ads7846_default(tmp_path):
    rc, out = _touch_run(tmp_path, {
        "TOUCH_DRIVER": "ads7846",
        "TOUCH_CALIBRATION_MATRIX": "1 0 0 0 1 0",
    })
    assert rc == 0, out
    assert "matrix=1 0 0 0 1 0" in out
    assert ADS7846_MEASURED not in out


def test_ads7846_rotated_falls_back_to_rotation_matrix(tmp_path):
    # A rotated ADS7846 panel cannot use the unrotated measured affine; it falls
    # back to the rotation matrix (operator should set a composed matrix).
    rc, out = _touch_run(tmp_path, {"TOUCH_DRIVER": "ads7846",
                                    "DISPLAY_ROTATION": "90"})
    assert rc == 0, out
    assert "matrix=0 -1 1 1 0 0" in out
    assert ADS7846_MEASURED not in out


def test_usb_touch_keeps_identity_default(tmp_path):
    rc, out = _touch_run(tmp_path, {"TOUCH_DRIVER": "usb"})
    assert rc == 0, out
    assert "matrix=1 0 0 0 1 0" in out
    assert "name=*Touchscreen*" in out


def test_touch_none_skips_configuration(tmp_path):
    rc, out = _touch_run(tmp_path, {"TOUCH_DRIVER": "none"})
    assert rc == 0, out
    assert "99-foodassistant-touch.rules" not in out
    assert "LIBINPUT_CALIBRATION_MATRIX" not in out


def test_touch_installs_tools_and_calibrate_helper(tmp_path):
    rc, out = _touch_run(tmp_path, {"TOUCH_DRIVER": "ads7846"})
    assert rc == 0, out
    assert "libinput-tools evtest" in out
    assert "foodassistant-touch-calibrate" in out


# Waveshare HDMI touchscreen (FoodAssistant-qs5): DISPLAY_TYPE=waveshare_hdmi
# installs the panel's touch overlay + a libinput udev rule so the controller is
# registered. Gated on the display type so it never runs for other hardware.

def test_waveshare_display_writes_overlay_and_udev(tmp_path):
    rc, out = _touch_run(tmp_path, {"DISPLAY_TYPE": "waveshare_hdmi"})
    assert rc == 0, out
    assert "Display type is waveshare_hdmi" in out
    assert "98-foodassistant-waveshare-touch.rules" in out
    # The calibration rule still gets a Waveshare name match.
    assert "name=*WaveShare*" in out


def test_waveshare_overlay_name_overridable(tmp_path):
    # A panel that needs a different overlay can set WAVESHARE_TOUCH_OVERLAY.
    rc, out = _touch_run(tmp_path, {
        "DISPLAY_TYPE": "waveshare_hdmi",
        "WAVESHARE_TOUCH_OVERLAY": "waveshare-7inch-touch",
    })
    assert rc == 0, out
    # No Pi boot config.txt in the test env, so the overlay is not appended; the
    # Waveshare touch path still ran (udev rule written).
    assert "98-foodassistant-waveshare-touch.rules" in out


def test_generic_display_skips_waveshare_path(tmp_path):
    # The default (generic) display must not touch the Waveshare overlay/udev.
    rc, out = _touch_run(tmp_path, {"TOUCH_DRIVER": "usb",
                                    "DISPLAY_TYPE": "generic"})
    assert rc == 0, out
    assert "Display type is waveshare_hdmi" not in out
    assert "98-foodassistant-waveshare-touch.rules" not in out


# MIPI DSI 7-inch panel (FoodAssistant-4yey): DISPLAY_TYPE=dsi_7inch writes the
# vc4-kms-dsi-7inch overlay so the panel comes up on Bookworm full KMS, and
# applies a Goodix-name touch calibration matrix.

def test_dsi_7inch_display_configures_panel(tmp_path):
    rc, out = _touch_run(tmp_path, {"DISPLAY_TYPE": "dsi_7inch"})
    assert rc == 0, out
    assert "Display type is dsi_7inch" in out
    # The calibration rule matches the DSI panel's Goodix touch controller.
    assert "name=*Goodix*" in out


def test_generic_display_skips_dsi_path(tmp_path):
    rc, out = _touch_run(tmp_path, {"TOUCH_DRIVER": "usb",
                                    "DISPLAY_TYPE": "generic"})
    assert rc == 0, out
    assert "Display type is dsi_7inch" not in out


def test_waveshare_display_type_read_from_settings_json(tmp_path):
    # display_type written by the web wizard to settings.json is honoured.
    sf = tmp_path / "settings.json"
    sf.write_text('{"display_type": "waveshare_hdmi"}')
    rc, out = run_firstboot(
        tmp_path, "HOSTNAME=foodassistant\n",
        extra_env={"STEPS": "touch", "SETTINGS_JSON": str(sf)},
    )
    assert rc == 0, out
    assert "Display type is waveshare_hdmi" in out


def _load_calibrate_helper():
    """Extract the embedded foodassistant-touch-calibrate Python and import it."""
    import importlib.util
    import re
    src = FIRSTBOOT.read_text()
    m = re.search(r"cat > \"\$dst\" <<'PYEOF'\n(.*?)\nPYEOF", src, re.S)
    assert m, "calibrate helper heredoc not found in firstboot.sh"
    code = m.group(1)
    path = REPO / "tests" / "_touch_calibrate_extracted.py"
    path.write_text(code)
    spec = importlib.util.spec_from_file_location("touch_calibrate", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    path.unlink()
    return mod


def test_calibrate_helper_compiles_and_recovers_known_affine():
    tc = _load_calibrate_helper()
    # screen_x = 1.2*nx - 0.1*ny + 0.05 ; screen_y = 1.3*ny - 0.08
    def known(nx, ny):
        return (1.2 * nx - 0.1 * ny + 0.05, 1.3 * ny - 0.08)
    samples = [(nx, ny, *known(nx, ny))
               for nx, ny in [(0.1, 0.1), (0.9, 0.1), (0.1, 0.9), (0.9, 0.9)]]
    a, b, c, d, e, f = tc.solve_affine(samples)
    got = tuple(round(v, 4) for v in (a, b, c, d, e, f))
    assert got == (1.2, -0.1, 0.05, 0.0, 1.3, -0.08), got


def test_calibrate_helper_rejects_degenerate_corners():
    import pytest
    tc = _load_calibrate_helper()
    # All four taps at the same point: the design matrix is singular.
    samples = [(0.5, 0.5, 0.0, 0.0)] * 4
    with pytest.raises(ValueError):
        tc.solve_affine(samples)


def test_read_minmax_ignores_pressure_axis():
    """ABS_PRESSURE's Max=255 must not overwrite the ABS_Y Max after parsing."""
    tc = _load_calibrate_helper()
    # Simulate the evtest startup banner for ADS7846 with ABS_PRESSURE following.
    # The bug: code stays "Y" when ABS_PRESSURE appears, so pressure Max=255
    # overwrites the real ABS_Y Max=4095.
    banner = [
        "Input device name: \"ADS7846 Touchscreen\"\n",
        "  Event type 3 (EV_ABS)\n",
        "    Event code 0 (ABS_X)\n",
        "      Value    0\n",
        "      Min      0\n",
        "      Max      4095\n",
        "    Event code 1 (ABS_Y)\n",
        "      Value    0\n",
        "      Min      0\n",
        "      Max      4095\n",
        "    Event code 24 (ABS_PRESSURE)\n",
        "      Value    0\n",
        "      Min      0\n",
        "      Max      255\n",
        "Testing ... (interrupt to abort)\n",
    ]
    import subprocess as sp

    # Patch subprocess.Popen to feed our banner lines
    class _FakeProc:
        def __init__(self):
            self.stdout = iter(banner)
        def terminate(self):
            pass

    orig = sp.Popen
    sp.Popen = lambda *a, **kw: _FakeProc()
    try:
        ranges = tc.read_minmax("/dev/input/event0")
    finally:
        sp.Popen = orig

    assert ranges["X"]["Max"] == 4095, ranges
    assert ranges["Y"]["Max"] == 4095, "ABS_PRESSURE Max=255 contaminated ABS_Y range"
