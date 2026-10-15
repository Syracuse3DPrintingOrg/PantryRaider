"""Tests for the first-boot provisioner's config-parsing / decision logic.

These exercise firstboot.sh in DRY_RUN mode (no installs, no Docker, no system
writes) and assert it makes the right decisions from a given config.env. They
are pure-logic and need only bash: no network or Docker.

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
        # Hermetic display detection: point the DRM connector scan at an empty
        # tree so has_display never sees the machine running the tests, and
        # drop any inherited graphical-session variables.
        "DRM_SYS_ROOT": str(tmp_path / "drm"),
        # Hermetic I2C detection: point the accessory bus check at a path that
        # does not exist so has_i2c_device never probes the machine running
        # the tests. Tests force presence with FORCE_I2C=1.
        "I2C_DEV": str(tmp_path / "no-i2c"),
        # Hermetic release trust and helper paths: nothing outside tmp_path.
        "TRUST_FILE": str(tmp_path / "trust" / "release-signers"),
        "STATE_FILE": str(tmp_path / "update-state.json"),
        "CHANNEL_FILE": str(tmp_path / "update-channel"),
        "PIN_STATE_FILE": str(tmp_path / "update-tag-restore"),
        "HELPER_BIN_DIR": str(tmp_path / "bin"),
    }
    for k in ("UPDATE_CHANNEL", "FOODASSISTANT_TAG", "FA_BOOTSTRAP_SIGNERS",
              "AP_BOOT_COPY", "STEPS", "FORCE"):
        env.pop(k, None)
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["bash", str(FIRSTBOOT)],
        env=env,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_default_leaves_mealie_off(tmp_path):
    # FoodAssistant-6n4a: recipes, the meal plan, and the shopping list are
    # built into Pantry Raider, so a default install provisions no Mealie.
    rc, out = run_firstboot(tmp_path, "HOSTNAME=foodassistant\n")
    assert rc == 0, out
    assert "MEALIE=false" in out
    assert "with-mealie" not in out
    assert "with-ollama" not in out


def test_explicit_mealie_opt_in_still_wins(tmp_path):
    # People who already use Mealie can still install it alongside.
    rc, out = run_firstboot(tmp_path, "ENABLE_MEALIE=true\n")
    assert rc == 0, out
    assert "--profile with-mealie" in out


def test_seed_writes_local_backend_urls_pi_hosted(tmp_path):
    # The host-networked app reaches its backends at localhost:PORT, so the
    # seed pre-fills the backend URLs there (Pantry Raider). Mealie only rides
    # along when explicitly enabled.
    rc, out = run_firstboot(tmp_path, "ENABLE_MEALIE=true\n")
    assert rc == 0, out
    assert "grocy_base_url" in out and "localhost:9383" in out
    assert "mealie_base_url" in out and "localhost:9285" in out


def test_seed_skips_mealie_url_when_mealie_off(tmp_path):
    rc, out = run_firstboot(tmp_path, "ENABLE_MEALIE=false\n")
    assert rc == 0, out
    assert "grocy_base_url" in out and "localhost:9383" in out
    assert "mealie_base_url" not in out


def test_seed_skips_backend_urls_for_pi_remote(tmp_path):
    # A satellite pulls Grocy/Mealie config from its server, so they are not
    # seeded locally.
    rc, out = run_firstboot(tmp_path, "DEPLOYMENT_MODE=pi_remote\nREMOTE_SERVER_URL=http://srv:9284\n")
    assert rc == 0, out
    assert "grocy_base_url" not in out


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


def test_kiosk_enabled_without_display_warns_but_installs(tmp_path):
    # An explicit ENABLE_KIOSK=true wins over the display gate: warn, but
    # install anyway so the kiosk starts once a display is attached.
    rc, out = run_firstboot(
        tmp_path, "ENABLE_KIOSK=true\n", extra_env={"FORCE_DISPLAY": ""}
    )
    assert rc == 0, out
    assert "no display detected" in out
    assert "Installing Chromium kiosk" in out


def _fake_drm(tmp_path: Path, statuses: dict[str, str]) -> Path:
    """Build a fake /sys/class/drm tree with the given connector statuses."""
    root = tmp_path / "drm"
    for name, status in statuses.items():
        d = root / name
        d.mkdir(parents=True)
        (d / "status").write_text(status + "\n")
    return root


def test_kiosk_auto_connected_drm_connector_installs(tmp_path):
    # "auto" with a connected DRM connector (a display actually plugged in)
    # installs the kiosk with no flags at all.
    root = _fake_drm(tmp_path, {"card1-HDMI-A-1": "connected"})
    rc, out = run_firstboot(
        tmp_path, "ENABLE_KIOSK=auto\n", extra_env={"DRM_SYS_ROOT": str(root)}
    )
    assert rc == 0, out
    assert "Installing Chromium kiosk" in out


def test_kiosk_auto_disconnected_drm_connector_skips(tmp_path):
    # A KMS card whose connectors all read "disconnected" is a headless Pi:
    # the card node alone must not count as a display.
    root = _fake_drm(
        tmp_path, {"card1-HDMI-A-1": "disconnected", "card1-HDMI-A-2": "disconnected"}
    )
    rc, out = run_firstboot(
        tmp_path, "ENABLE_KIOSK=auto\n", extra_env={"DRM_SYS_ROOT": str(root)}
    )
    assert rc == 0, out
    assert "Kiosk not enabled" in out


def test_kiosk_auto_display_type_counts_as_display(tmp_path):
    # A wizard-selected panel type (DSI/SPI) means a display even before its
    # overlay is active, so "auto" installs the kiosk.
    rc, out = run_firstboot(tmp_path, "ENABLE_KIOSK=auto\nDISPLAY_TYPE=dsi_7inch\n")
    assert rc == 0, out
    assert "Installing Chromium kiosk" in out


def test_kiosk_force_display_zero_means_absent(tmp_path):
    # FORCE_DISPLAY=0 forces "no display" even with a connected connector.
    root = _fake_drm(tmp_path, {"card1-HDMI-A-1": "connected"})
    rc, out = run_firstboot(
        tmp_path,
        "ENABLE_KIOSK=auto\n",
        extra_env={"DRM_SYS_ROOT": str(root), "FORCE_DISPLAY": "0"},
    )
    assert rc == 0, out
    assert "Kiosk not enabled" in out


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


def test_hide_cursor_auto_hides_even_with_pointer(tmp_path):
    # HIDE_CURSOR=auto hides regardless of attached pointers: USB barcode
    # scanners enumerate a composite HID mouse, so pointer detection kept a
    # visible cursor on scanner-equipped kiosks. Mouse users opt out with
    # HIDE_CURSOR=false.
    rc, out = run_firstboot(
        tmp_path,
        "ENABLE_KIOSK=true\n",
        extra_env={"FORCE_DISPLAY": "1", "FORCE_POINTER": "1"},
    )
    assert rc == 0, out
    assert "Cursor will be hidden" in out


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


def test_hosted_mode_configures_multihome_sysctl(tmp_path):
    # FoodAssistant-0s5q: reachability on both Ethernet and Wi-Fi at once needs
    # the arp_ignore/arp_announce/rp_filter sysctl drop-in written and applied.
    rc, out = run_firstboot(tmp_path, "DEPLOYMENT_MODE=pi_hosted\n")
    assert rc == 0, out
    assert "DRY_RUN would apply /etc/sysctl.d/85-foodassistant-multihome.conf" in out


def test_remote_mode_also_configures_multihome_sysctl(tmp_path):
    # A satellite has the same dual-NIC reachability problem, so it gets the
    # same drop-in even though it skips the Docker stack.
    rc, out = run_firstboot(
        tmp_path,
        "DEPLOYMENT_MODE=pi_remote\nREMOTE_SERVER_URL=http://server.local:9284\n",
    )
    assert rc == 0, out
    assert "DRY_RUN would apply /etc/sysctl.d/85-foodassistant-multihome.conf" in out


def test_steps_multihome_targeted(tmp_path):
    # STEPS=multihome should run only the sysctl drop-in, bypassing the done
    # marker and skipping the Docker stack entirely.
    rc, out = run_firstboot(
        tmp_path, "HOSTNAME=foodassistant\n",
        extra_env={"STEPS": "multihome"},
    )
    assert rc == 0, out
    assert "Targeted step run" in out
    assert "DRY_RUN would apply /etc/sysctl.d/85-foodassistant-multihome.conf" in out
    assert "Deploying stack" not in out


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
    # Brand default: the box answers at pr.local (FoodAssistant-a8fn).
    assert "http://pr.local/" in out


def test_fa_hostname_env_is_honored(tmp_path):
    # An explicit installer/env choice arrives as FA_HOSTNAME (HOSTNAME is
    # shadowed by bash), and wins over the pr default.
    rc, out = run_firstboot(
        tmp_path, "ENABLE_MEALIE=false\n", extra_env={"FA_HOSTNAME": "kitchen"}
    )
    assert rc == 0, out
    assert "http://kitchen.local/" in out
    assert "pr.local" not in out


def test_config_hostname_beats_fa_hostname(tmp_path):
    # A HOSTNAME= line in the config file is the most explicit, so it wins.
    rc, out = run_firstboot(
        tmp_path, "HOSTNAME=pantry\n", extra_env={"FA_HOSTNAME": "kitchen"}
    )
    assert rc == 0, out
    assert "http://pantry.local/" in out


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


def test_usb_touch_matches_any_touchscreen(tmp_path):
    # A USB HID panel reports a vendor name, not "*Touchscreen*", so the rule
    # must match by the ID_INPUT_TOUCHSCREEN udev property or the matrix never
    # reaches the panel (FoodAssistant-ly82). At rotation 0 the matrix is
    # identity.
    rc, out = _touch_run(tmp_path, {"TOUCH_DRIVER": "usb"})
    assert rc == 0, out
    assert "matrix=1 0 0 0 1 0" in out
    assert "name=id-touchscreen" in out


def test_usb_touch_follows_rotation(tmp_path):
    # The Bandit case: a USB touch panel at 270 must get the 270 rotation matrix
    # AND match any touchscreen by property, so touch follows the rotated
    # display instead of staying panel-native (FoodAssistant-ly82).
    rc, out = _touch_run(tmp_path, {"TOUCH_DRIVER": "usb", "DISPLAY_ROTATION": "270"})
    assert rc == 0, out
    assert "matrix=0 1 0 -1 0 1" in out
    assert "name=id-touchscreen" in out


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


# Resistive HDMI / ADS7846 (FoodAssistant-mox4): selecting this display type
# forces the ads7846 driver even though SPI is off (auto-detect would miss it),
# so the overlay + SPI get written and a calibration rule is applied.

def test_ads7846_hdmi_forces_ads7846_driver(tmp_path):
    rc, out = _touch_run(tmp_path, {"DISPLAY_TYPE": "ads7846_hdmi"})
    assert rc == 0, out
    assert "forcing ADS7846 SPI touch" in out
    assert "Configuring touch driver: ads7846" in out
    # The calibration rule matches the ADS7846 controller name.
    assert "name=ADS7846*" in out


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


def test_env_file_persists_compose_profiles(tmp_path):
    # The generated .env records the enabled profiles as COMPOSE_PROFILES so
    # every later plain `docker compose` command from the install dir (manual,
    # host bridge, OTA helper) keeps operating on the same services.
    rc, out = run_firstboot(tmp_path, "ENABLE_MEALIE=true\n")
    assert rc == 0, out
    assert "COMPOSE_PROFILES=with-mealie" in out


def test_env_file_profiles_empty_when_mealie_off(tmp_path):
    rc, out = run_firstboot(tmp_path, "ENABLE_MEALIE=false\n")
    assert rc == 0, out
    assert "COMPOSE_PROFILES=)" in out  # empty value in the DRY_RUN write line
    assert "with-mealie" not in out


def test_env_file_profiles_include_ollama(tmp_path):
    rc, out = run_firstboot(tmp_path, "ENABLE_MEALIE=true\nENABLE_OLLAMA=true\n")
    assert rc == 0, out
    assert "COMPOSE_PROFILES=with-mealie,with-ollama" in out


# --- Quiet boot (kernel cmdline) --------------------------------------------

PI_CMDLINE = ("console=serial0,115200 console=tty1 root=PARTUUID=deadbeef-02 "
              "rootfstype=ext4 fsck.repair=yes rootwait")


def test_quiet_boot_appends_params_for_a_kiosk_device(tmp_path):
    # With a kiosk configured, provisioning ends by quieting the boot console:
    # the quiet params are appended to the kernel cmdline so they only take
    # effect from the NEXT boot on (the first boot keeps its console output).
    cmdline = tmp_path / "cmdline.txt"
    cmdline.write_text(PI_CMDLINE + "\n")
    rc, out = run_firstboot(
        tmp_path, "ENABLE_KIOSK=true\n",
        extra_env={"CMDLINE_CANDIDATES": str(cmdline)})
    assert rc == 0, out
    assert f"DRY_RUN would append to {cmdline}:" in out
    for p in ("quiet", "loglevel=3", "vt.global_cursor_default=0",
              "logo.nologo", "consoleblank=0", "rd.systemd.show_status=false",
              "systemd.show_status=false"):
        assert p in out
    # DRY_RUN never writes.
    assert cmdline.read_text() == PI_CMDLINE + "\n"


def test_quiet_boot_skipped_without_a_kiosk(tmp_path):
    # No display and ENABLE_KIOSK=auto means no kiosk, so the boot console
    # stays verbose (nothing scrolls over a screen that is not there).
    cmdline = tmp_path / "cmdline.txt"
    cmdline.write_text(PI_CMDLINE + "\n")
    rc, out = run_firstboot(
        tmp_path, "HOSTNAME=foodassistant\n",
        extra_env={"CMDLINE_CANDIDATES": str(cmdline)})
    assert rc == 0, out
    assert "Quiet boot skipped" in out
    assert "DRY_RUN would append to" not in out


def test_quiet_boot_already_quiet_cmdline_adds_nothing(tmp_path):
    cmdline = tmp_path / "cmdline.txt"
    cmdline.write_text(
        PI_CMDLINE + " quiet loglevel=3 vt.global_cursor_default=0"
        " logo.nologo consoleblank=0 rd.systemd.show_status=false"
        " systemd.show_status=false\n")
    rc, out = run_firstboot(
        tmp_path, "ENABLE_KIOSK=true\n",
        extra_env={"CMDLINE_CANDIDATES": str(cmdline)})
    assert rc == 0, out
    assert "Boot cmdline already quiet" in out
    assert "DRY_RUN would append to" not in out


def test_quiet_boot_missing_cmdline_is_a_noop(tmp_path):
    missing = tmp_path / "no-cmdline.txt"
    rc, out = run_firstboot(
        tmp_path, "ENABLE_KIOSK=true\n",
        extra_env={"CMDLINE_CANDIDATES": str(missing)})
    assert rc == 0, out
    assert "No boot cmdline file found" in out
    assert not missing.exists()


# --- SD-card resilience + memory tuning (FoodAssistant-p5ev / xkyy) ----------

def test_sd_resilience_runs_on_pi(tmp_path):
    # On a Pi the provisioner sets journald to volatile and adds noatime +
    # commit to the root mount, both aimed at fewer SD writes and a smaller
    # power-loss window. DRY_RUN only logs the intent (no writes).
    rc, out = run_firstboot(
        tmp_path, "HOSTNAME=foodassistant\n",
        extra_env={"FORCE_PI": "1", "FORCE_MEM_MB": "2000"})
    assert rc == 0, out
    assert "journald set to volatile" in out
    assert "noatime,commit=120" in out


def test_zram_runs_on_pi(tmp_path):
    # zram gives compressed swap in RAM instead of swapping to (and wearing) the
    # SD card; the on-card dphys-swapfile is disabled.
    rc, out = run_firstboot(
        tmp_path, "HOSTNAME=foodassistant\n",
        extra_env={"FORCE_PI": "1", "FORCE_MEM_MB": "2000"})
    assert rc == 0, out
    assert "dphys-swapfile" in out
    assert "zram-tools" in out


def test_sd_resilience_skipped_off_pi(tmp_path):
    # A non-Pi host (server mode on a mini PC) is not on an SD card, so the
    # tuning is skipped rather than applied to the wrong hardware.
    rc, out = run_firstboot(
        tmp_path, "HOSTNAME=foodassistant\n",
        extra_env={"FORCE_MEM_MB": "4000"})
    assert rc == 0, out
    assert "skipping SD-card resilience tuning" in out
    assert "skipping zram swap setup" in out


def test_install_docker_retries_and_falls_back_to_distro():
    """firstboot install_docker must survive a broken Docker CDN
    (FoodAssistant-4qz5): retry get.docker.com with cache revalidation, then fall
    back to the distro docker.io + compose-v2 packages, rather than aborting the
    whole first boot. Source-guard: DRY_RUN skips the real install."""
    text = FIRSTBOOT.read_text()
    start = text.index("install_docker()")
    body = text[start:start + 3500]
    # Retries get.docker.com with No-Cache revalidation (busts the stale CDN edge)
    # and clears the local lists between attempts.
    assert "for attempt in 1 2 3" in body
    assert 'Acquire::http::No-Cache "true"' in body
    assert "/var/lib/apt/lists/*" in body
    # Docker's repo does not publish by-hash, so it must NOT be force-enabled here.
    assert 'By-Hash "force"' not in body
    # Falls back to the distro packages, which never touch download.docker.com.
    assert "docker.io" in body
    assert "sources.list.d/docker.list" in body
    # No longer a single attempt that dies on the first failure.
    assert 'sh "$tmp" || die "Docker install failed"' not in body


def test_install_compose_v2_falls_back_to_github_binary():
    """Compose v2 must be installable without Docker's repo (FoodAssistant-4qz5):
    install_compose_v2 tries the distro/repo packages, then drops in the official
    static plugin binary from GitHub. Source-guard; DRY_RUN short-circuits it."""
    text = FIRSTBOOT.read_text()
    start = text.index("install_compose_v2()")
    body = text[start:start + 1600]
    assert "docker-compose-v2" in body            # distro package attempt
    assert "cli-plugins" in body                  # official plugin location
    assert "github.com/docker/compose/releases" in body   # CDN-independent binary
    assert "docker compose version" in body       # verifies it actually runs
    # install_docker's "compose missing" branch must use the helper, not the
    # unavailable docker-compose-plugin name alone.
    di = text.index("install_docker()")
    assert "install_compose_v2" in text[di:di + 3800]


def test_deploy_stack_uses_quiet_spinner_for_compose():
    """The noisy docker compose pull/build/up must run behind spin_run with
    Docker's per-layer progress quieted, so the console shows one clean line per
    phase instead of scrolling layers (FoodAssistant-tq2j). Source-guard."""
    text = FIRSTBOOT.read_text()
    # The spinner helper exists and captures detail to a log for failures.
    assert "spin_run()" in text
    assert "STEP_LOG" in text
    assert "tail -n 20" in text
    # It degrades when not a TTY and animates when it is.
    assert "[ ! -t 1 ]" in text
    # deploy_stack drives compose through it with the quiet flags.
    ds = text[text.index("deploy_stack()"):]
    ds = ds[:ds.index("\n}\n") + 3]
    assert "spin_run" in ds
    assert "pull --quiet" in ds
    assert "build --progress=quiet" in ds
    assert "up -d --quiet-pull" in ds


def test_kiosk_unit_waits_for_provisioning_and_the_app():
    """The kiosk must not paint until first-boot provisioning is finished and
    the app actually answers HTTP. On Dan's fresh install (2026-07-10) the
    setup screen appeared while the stack was still pulling and building,
    because the unit started as soon as it was installed and its app wait gave
    up after ~160s. Source-guard on the generated unit."""
    text = FIRSTBOOT.read_text()
    ck = text[text.index("configure_kiosk()"):]
    ck = ck[:ck.index("\n}\n") + 3]
    # The wait probes the app's cheap /health endpoint, derived from KIOSK_URL.
    assert "kiosk_wait_url" in ck
    assert "/health" in ck
    # It defers while the first-boot provisioner is still running, and skips
    # straight to the health probe on a normal boot (done marker present).
    assert "foodassistant-firstboot.service" in ck
    assert '"$DONE_MARKER"' in ck
    # Long enough for a first-boot pull/build (420 probes, 2s apart), with a
    # matching unit start budget. \\$\\$ so systemd hands the shell a literal $.
    assert "seq 1 420" in ck
    assert "TimeoutStartSec=900" in ck
    # Best-effort: an app that stays down never wedges the kiosk service.
    assert ck.count("exit 0") >= 2
    # Starting the kiosk from inside provisioning must not deadlock on the
    # unit's own wait-for-provisioning.
    assert "systemctl start --no-block foodassistant-kiosk.service" in ck


def test_deploy_stack_overlaps_pulls_and_waits_for_the_app():
    """First boot pulls the Grocy image in parallel with the app image and
    then waits only for the APP to answer /health (FoodAssistant-0m61). The
    old wait-for-Grocy is gone on purpose: while Grocy runs its slow first
    start, the app now shows the /ui/getting-ready progress page and hands
    off to the wizard when the inventory answers, so holding provisioning
    open here only delayed that page. Source-guard."""
    text = FIRSTBOOT.read_text()
    ds = text[text.index("deploy_stack()"):]
    ds = ds[:ds.index("\n}\n") + 3]
    # The Grocy image download overlaps the app image download.
    assert "pull --quiet grocy" in ds
    assert "grocy_pull_pid" in ds
    # The post-start wait targets the app's own health endpoint, briefly and
    # best-effort (never fails the install), and is skipped on a satellite.
    assert "localhost:9284/health" in ds
    assert "Waiting for the inventory service to be ready" not in ds
    assert 'pi_remote' in ds


def test_boot_splash_is_optin_and_failsafe():
    """The Plymouth boot splash (FoodAssistant-y8vj) is opt-in (default off) and
    never breaks the boot path: any install failure returns without touching
    cmdline/theme, so the worst case is the existing quiet boot. Source-guard."""
    text = FIRSTBOOT.read_text()
    assert 'ENABLE_BOOT_SPLASH="${ENABLE_BOOT_SPLASH:-false}"' in text
    body = text[text.index("configure_boot_splash()"):]
    body = body[:body.index("\n}\n") + 3]
    # Gated on the flag and on a display being present.
    assert 'is_true "$ENABLE_BOOT_SPLASH"' in body
    assert 'flag_enabled "$ENABLE_KIOSK"' in body
    # Every failure path leaves the plain quiet boot rather than dying.
    assert body.count("leaving the plain quiet boot") >= 2
    # A failed theme rebuild reverts rather than risking a black screen.
    assert "reverting to the default theme" in body
    # Only adds the splash param; quiet is handled separately.
    assert "$line splash" in body


def test_boot_splash_theme_assets_present():
    """The theme the provisioner installs must ship in the repo."""
    d = REPO / "scripts" / "image-build" / "plymouth-theme" / "pantryraider"
    assert (d / "pantryraider.plymouth").is_file()
    assert (d / "pantryraider.script").is_file()
    assert (d / "logo.png").is_file()


def test_fb_splash_installs_by_default_on_a_kiosk_device(tmp_path):
    # The framebuffer splash (FoodAssistant-y8vj) is ON by default wherever a
    # kiosk is: unlike the opt-in Plymouth theme it cannot break a boot (its
    # unit skips itself when anything is missing).
    rc, out = run_firstboot(tmp_path, "ENABLE_KIOSK=true\n")
    assert rc == 0, out
    assert "DRY_RUN would install the framebuffer boot splash" in out


def test_fb_splash_installs_on_a_satellite_kiosk(tmp_path):
    # pi_remote devices are kiosks by definition; they get the splash too.
    rc, out = run_firstboot(
        tmp_path,
        "DEPLOYMENT_MODE=pi_remote\nREMOTE_SERVER_URL=http://srv:9284\nENABLE_KIOSK=true\n",
    )
    assert rc == 0, out
    assert "DRY_RUN would install the framebuffer boot splash" in out


def test_fb_splash_skipped_without_a_display(tmp_path):
    # A headless box has no screen to splash.
    rc, out = run_firstboot(tmp_path, "HOSTNAME=pr\n")
    assert rc == 0, out
    assert "Framebuffer splash skipped (no kiosk/display" in out


def test_fb_splash_can_be_turned_off(tmp_path):
    rc, out = run_firstboot(tmp_path, "ENABLE_KIOSK=true\nENABLE_FB_SPLASH=false\n")
    assert rc == 0, out
    assert "Framebuffer splash not enabled" in out
    assert "DRY_RUN would install the framebuffer boot splash" not in out


def test_fb_splash_is_default_on_and_failsafe():
    """Source-guard: the framebuffer splash defaults on, gates on a display,
    and every failure path leaves the plain quiet boot rather than dying, so
    it can never produce a broken boot."""
    text = FIRSTBOOT.read_text()
    assert 'ENABLE_FB_SPLASH="${ENABLE_FB_SPLASH:-true}"' in text
    body = text[text.index("configure_fb_splash()"):]
    body = body[:body.index("\n}\n") + 3]
    assert 'is_true "$ENABLE_FB_SPLASH"' in body
    assert 'flag_enabled "$ENABLE_KIOSK"' in body
    assert body.count("leaving the plain quiet boot") >= 4
    # Registered for BOTH the hosted and the satellite provisioning paths.
    assert text.count('_step_requested "fb_splash"') == 2


def test_fb_splash_sources_ship_in_the_repo():
    """Everything configure_fb_splash installs must exist in the checkout."""
    ib = REPO / "scripts" / "image-build"
    assert (ib / "foodassistant-boot-splash").is_file()
    assert (ib / "foodassistant-boot-splash.service").is_file()
    assert (ib / "boot-splash" / "splash-art.gz").is_file()
    assert (ib / "boot-splash" / "getty-tty1.conf").is_file()


def test_fb_splash_turns_off_the_firmware_splash(tmp_path):
    # config.txt gets disable_splash=1 (in a marked [all] block) on a kiosk
    # device that does not set it already.
    cfg = tmp_path / "config.txt"
    cfg.write_text("dtoverlay=vc4-kms-v3d\n")
    rc, out = run_firstboot(tmp_path, "ENABLE_KIOSK=true\n",
                            extra_env={"CONFIG_TXT_CANDIDATES": str(cfg)})
    assert rc == 0, out
    assert f"DRY_RUN would add disable_splash=1 to {cfg}" in out
    cfg.write_text("disable_splash=0\n")
    rc, out = run_firstboot(tmp_path, "ENABLE_KIOSK=true\n",
                            extra_env={"CONFIG_TXT_CANDIDATES": str(cfg)})
    assert "already sets disable_splash" in out


def test_boot_screen_config_block_and_remount_helper():
    """Source-guard: the config.txt block is marked and starts with [all], and
    boot partition writes go through with_boot_rw (rw for the write, ro again
    when it was)."""
    text = FIRSTBOOT.read_text()
    body = text[text.index("_append_boot_screen_block()"):]
    body = body[:body.index("\n}\n")]
    assert "$BOOT_SCREEN_BEGIN\n[all]\ndisable_splash=1\n$BOOT_SCREEN_END" in body
    rw = text[text.index("with_boot_rw() {"):]
    rw = rw[:rw.index("\n}\n")]
    assert "mount -o remount,rw" in rw and "mount -o remount,ro" in rw
    assert 'with_boot_rw "$cmdline" atomic_write' in text
    assert 'with_boot_rw "$cfg" _append_boot_screen_block' in text


def test_fallback_ap_has_no_shared_passphrase_and_no_tkip():
    """The recovery hotspot passphrase is per device (FoodAssistant-fgh3):
    the old shared constant and the TKIP cipher must never come back, and the
    hostapd config (which now holds a secret) is written root-only from a
    passphrase established by ap_fallback_passphrase. Source-guard."""
    text = FIRSTBOOT.read_text()
    assert "wpa_passphrase=foodassist\n" not in text
    assert "wpa_pairwise=TKIP" not in text
    assert "rsn_pairwise=CCMP" in text
    body = text[text.index("configure_wifi_ap_fallback()"):]
    body = body[:body.index("\n}\n") + 3]
    assert "ap_fallback_passphrase" in body
    assert "chmod 600 /etc/hostapd/hostapd.conf" in body
    # No passphrase means no hotspot config at all, never an open one.
    assert "leaving the fallback AP unconfigured" in body


def _eval_ap_functions() -> str:
    """Bash snippet that loads the AP identity helpers from firstboot.sh so
    they can be exercised without running the provisioner. `warn` is stubbed
    so the helpers' own logging does not need the full provisioner sourced."""
    return (
        'warn() { printf "%s\\n" "$*" >&2; }; '
        'eval "$(awk \'/^gen_ap_passphrase\\(\\)/,/^}/\' "$FIRSTBOOT")"; '
        'eval "$(awk \'/^ap_fallback_passphrase\\(\\)/,/^}/\' "$FIRSTBOOT")"; '
        'eval "$(awk \'/^ap_ssid_ok\\(\\)/,/^}/\' "$FIRSTBOOT")"; '
        'eval "$(awk \'/^ap_fallback_ssid\\(\\)/,/^}/\' "$FIRSTBOOT")"; '
        'eval "$(awk \'/^ap_boot_copy_enabled\\(\\)/,/^}/\' "$FIRSTBOOT")"; '
        'eval "$(awk \'/^ap_boot_copy_text\\(\\)/,/^}/\' "$FIRSTBOOT")"; '
        'eval "$(awk \'/^ap_boot_copy_path\\(\\)/,/^}/\' "$FIRSTBOOT")"; '
    )


def test_generated_ap_passphrase_is_unique_and_wpa2_sized(tmp_path):
    out = subprocess.run(
        ["bash", "-c", _eval_ap_functions() + "gen_ap_passphrase; gen_ap_passphrase"],
        env={**os.environ, "FIRSTBOOT": str(FIRSTBOOT)},
        capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr
    first, second = out.stdout.split()
    assert first != second, "passphrase must be random per call"
    for p in (first, second):
        assert 8 <= len(p) <= 63  # WPA2 bounds
        assert all(c.islower() or c.isdigit() or c == "-" for c in p)


def test_ap_passphrase_persists_and_honors_owner_override(tmp_path):
    env = {
        **os.environ,
        "FIRSTBOOT": str(FIRSTBOOT),
        "AP_PASSPHRASE_FILE": str(tmp_path / "ap-passphrase"),
        "AP_BOOTDIR_CANDIDATES": str(tmp_path / "boot"),
    }
    (tmp_path / "boot").mkdir()
    twice = _eval_ap_functions() + "ap_fallback_passphrase && ap_fallback_passphrase"
    once = _eval_ap_functions() + "ap_fallback_passphrase"
    out = subprocess.run(["bash", "-c", twice], env=env,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    first, second = out.stdout.split()
    assert first == second, "a re-run must reuse the stored passphrase"
    stored = tmp_path / "ap-passphrase"
    assert stored.read_text().strip() == first
    assert (stored.stat().st_mode & 0o777) == 0o600
    # The boot-partition mirror lets a headless owner read the password by
    # putting the SD card in any computer.
    mirror = (tmp_path / "boot" / "pantry-raider-hotspot.txt").read_text()
    assert first in mirror and "FoodAssistant" in mirror
    # A valid owner-chosen config.env passphrase wins.
    out = subprocess.run(
        ["bash", "-c", once],
        env={**env, "AP_PASSPHRASE": "owner-picked-99"},
        capture_output=True, text=True)
    assert out.stdout.strip() == "owner-picked-99"
    # A too-short owner passphrase must NOT strand a headless device without a
    # recovery hotspot: it falls back to a valid generated one, never "short".
    fresh = str(tmp_path / "fresh-ap-passphrase")
    out = subprocess.run(
        ["bash", "-c", once],
        env={**env, "AP_PASSPHRASE": "short", "AP_PASSPHRASE_FILE": fresh},
        capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    fallback = out.stdout.strip()
    assert fallback != "short"
    assert 8 <= len(fallback) <= 63


def test_ap_ssid_precedence_env_then_stored_then_default(tmp_path):
    """The hotspot SSID resolves like the passphrase does: an owner-chosen
    AP_SSID from config.env, then a name the user set from the app (stored at
    AP_SSID_FILE), then the default FoodAssistant. Whatever wins is persisted
    so it survives a re-provision."""
    ssid_file = tmp_path / "ap-ssid"
    env = {
        **os.environ,
        "FIRSTBOOT": str(FIRSTBOOT),
        "AP_SSID_FILE": str(ssid_file),
    }
    once = _eval_ap_functions() + "ap_fallback_ssid"
    # No seed, no stored name: the default.
    out = subprocess.run(["bash", "-c", once], env=env,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "FoodAssistant"
    assert ssid_file.read_text().strip() == "FoodAssistant"
    # A stored name (written by the app through the bridge) is reused.
    ssid_file.write_text("Kitchen Pi\n")
    out = subprocess.run(["bash", "-c", once], env=env,
                         capture_output=True, text=True)
    assert out.stdout.strip() == "Kitchen Pi"
    # An owner-chosen config.env seed wins over the stored name.
    out = subprocess.run(["bash", "-c", once],
                         env={**env, "AP_SSID": "Pantry Basecamp"},
                         capture_output=True, text=True)
    assert out.stdout.strip() == "Pantry Basecamp"
    assert ssid_file.read_text().strip() == "Pantry Basecamp"


def test_ap_ssid_invalid_values_fall_back(tmp_path):
    """An unusable AP_SSID (too long, or carrying control characters that
    could smuggle hostapd config lines) warns and falls through to the stored
    or default name; it must never leave the hotspot unnamed."""
    ssid_file = tmp_path / "ap-ssid"
    env = {
        **os.environ,
        "FIRSTBOOT": str(FIRSTBOOT),
        "AP_SSID_FILE": str(ssid_file),
    }
    once = _eval_ap_functions() + "ap_fallback_ssid"
    out = subprocess.run(["bash", "-c", once],
                         env={**env, "AP_SSID": "x" * 33},
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "FoodAssistant"
    # A stored name that grew control characters is also refused.
    ssid_file.write_bytes(b"bad\tname\n")
    out = subprocess.run(["bash", "-c", once], env=env,
                         capture_output=True, text=True)
    assert out.stdout.strip() == "FoodAssistant"


def test_ap_boot_mirror_carries_the_custom_ssid(tmp_path):
    """The boot-partition recovery note shows the name the hotspot actually
    broadcasts, so an owner who renamed it is told the right network to join."""
    env = {
        **os.environ,
        "FIRSTBOOT": str(FIRSTBOOT),
        "AP_PASSPHRASE_FILE": str(tmp_path / "ap-passphrase"),
        "AP_BOOTDIR_CANDIDATES": str(tmp_path / "boot"),
    }
    (tmp_path / "boot").mkdir()
    out = subprocess.run(
        ["bash", "-c", _eval_ap_functions() + 'ap_fallback_passphrase "Kitchen Pi"'],
        env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    mirror = (tmp_path / "boot" / "pantry-raider-hotspot.txt").read_text()
    assert "Network name: Kitchen Pi" in mirror
    assert out.stdout.strip() in mirror


def test_fallback_ap_uses_the_resolved_ssid():
    """Source-guard: the hostapd config is written from the resolved SSID, not
    a hardcoded name, and the AP step resolves it via ap_fallback_ssid."""
    text = FIRSTBOOT.read_text()
    body = text[text.index("configure_wifi_ap_fallback()"):]
    body = body[:body.index("\n}\n") + 3]
    assert "ap_fallback_ssid" in body
    assert "ssid=$ap_ssid" in body
    assert "ssid=FoodAssistant" not in body


# --- I2C bus + gadgets auto-detect (FoodAssistant-dxr22) ---------------------


def test_i2c_step_runs_unconditionally_pi_hosted(tmp_path):
    # The accessories bus is turned on regardless of ENABLE_GADGETS: gating it
    # on the reader opt-in is how a fresh flash shipped with a dead NeoKey.
    rc, out = run_firstboot(tmp_path, "ENABLE_GADGETS=false\n")
    assert rc == 0, out
    assert "DRY_RUN would enable the I2C bus" in out
    assert "Sensor reader not enabled" in out


def test_i2c_step_runs_unconditionally_pi_remote(tmp_path):
    # The satellite registry carries the step too: a Bandit is exactly the
    # device a NeoKey plugs into.
    rc, out = run_firstboot(
        tmp_path,
        "DEPLOYMENT_MODE=pi_remote\nREMOTE_SERVER_URL=http://srv:9284\n"
        "ENABLE_GADGETS=false\n",
    )
    assert rc == 0, out
    assert "DRY_RUN would enable the I2C bus" in out


def test_gadgets_auto_installs_when_an_accessory_answers(tmp_path):
    # Default "auto" resolves from the bus: a board answering means the reader
    # installs with no flag set, mirroring the Stream Deck. REPO_DIR points at
    # this checkout so the step finds foodassistant-gadgets-setup.
    rc, out = run_firstboot(tmp_path, "", extra_env={"FORCE_I2C": "1",
                                                     "REPO_DIR": str(REPO)})
    assert rc == 0, out
    assert "DRY_RUN would run foodassistant-gadgets-setup" in out


def test_gadgets_auto_skips_when_nothing_answers(tmp_path):
    # Nothing plugged in resolves auto to off; Settings then offers the
    # one-click setup, so nothing is lost.
    rc, out = run_firstboot(tmp_path, "", extra_env={"FORCE_I2C": "0"})
    assert rc == 0, out
    assert "Sensor reader not enabled (ENABLE_GADGETS=auto" in out
    assert "DRY_RUN would run foodassistant-gadgets-setup" not in out


def test_gadgets_true_wins_over_an_empty_bus(tmp_path):
    # An explicit opt-in installs regardless of detection (Bluetooth-only
    # rigs have nothing on the bus to find).
    rc, out = run_firstboot(tmp_path, "ENABLE_GADGETS=true\n",
                            extra_env={"FORCE_I2C": "0",
                                       "REPO_DIR": str(REPO)})
    assert rc == 0, out
    assert "DRY_RUN would run foodassistant-gadgets-setup" in out


def test_gadgets_false_wins_over_a_detected_accessory(tmp_path):
    # An explicit opt-out is honored even with a board plugged in.
    rc, out = run_firstboot(tmp_path, "ENABLE_GADGETS=false\n",
                            extra_env={"FORCE_I2C": "1"})
    assert rc == 0, out
    assert "Sensor reader not enabled" in out
    assert "DRY_RUN would run foodassistant-gadgets-setup" not in out


def test_i2c_registered_in_both_registries():
    """Source-guard: the i2c step sits in BOTH the hosted and the satellite
    step registries, before the gadgets step so auto-detect sees a live bus."""
    text = FIRSTBOOT.read_text()
    calls = [line for line in text.splitlines()
             if '_step_requested "i2c"' in line]
    assert len(calls) == 2, calls
    hosted = text[text.index("_step_requested \"docker\""):]
    assert hosted.index('_step_requested "i2c"') \
        < hosted.index('_step_requested "gadgets"')


def test_satellite_env_never_turns_the_password_off():
    """A second screen demands a password like every other install.

    The provisioner used to pin AUTH_REQUIRED=false into the satellite env
    file, and an env var beats anything the setup wizard saves later, so those
    devices stayed open to anyone on the network. Source-guard: the env file is
    written from a heredoc, so its contents are not observable in DRY_RUN.
    """
    text = FIRSTBOOT.read_text()
    assert "AUTH_REQUIRED=false" not in text
    body = text[text.index("deploy_remote_service()"):]
    assert "DEPLOYMENT_MODE=pi_remote\n" in body
    assert "AUTH_REQUIRED" not in body[:body.index("chmod 600 \"$env_file\"")]


def test_satellite_container_defaults_to_requiring_a_password():
    compose = (REPO / "scripts" / "image-build"
               / "docker-compose.remote.yml").read_text()
    assert "AUTH_REQUIRED=${AUTH_REQUIRED:-true}" in compose


def test_shared_venv_and_deck_package_stay_owned_by_root():
    """Root runs python out of the shared venv (the host bridge asks the deck
    controller for its layout), so neither the venv nor the package may be
    writable by the desktop account. Source-guard on both install paths."""
    text = FIRSTBOOT.read_text()
    deck = text[text.index("configure_streamdeck()"):]
    deck = deck[:deck.index("\n}\n")]
    assert 'chown -R root:root "$sd_dst" "$venv_dir"' in deck
    assert 'chown -R "$sd_user" "$sd_dst" "$venv_dir"' not in deck
    remote = text[text.index("deploy_remote_service()"):]
    remote = remote[:remote.index("\n}\n")]
    # The satellite hands over its data dir, never the whole install tree.
    assert 'chown -R "$sd_user":"$sd_user" "$INSTALL_DIR/data"' in remote
    assert 'chown -R "$sd_user":"$sd_user" "$INSTALL_DIR" ' not in remote
    assert 'chown -R root:root "$venv_dir"' in remote


# --- 64-bit gate -------------------------------------------------------------
# The published container image has no 32-bit manifest, so the full stack needs
# a 64-bit OS. The check warns and continues rather than dying: the device is
# already flashed by the time first boot runs, and aborting would also skip the
# kiosk, Stream Deck, and network steps that work fine on 32-bit hardware.


def test_32bit_os_warns_that_the_stack_needs_64bit(tmp_path):
    rc, out = run_firstboot(tmp_path, "DEPLOYMENT_MODE=pi_hosted\n",
                            extra_env={"FORCE_ARCH": "armv7l"})
    assert rc == 0, out
    assert "32-bit OS detected (armv7l)" in out
    assert "Raspberry Pi OS Lite (64-bit)" in out


def test_32bit_userland_on_a_64bit_kernel_is_still_caught(tmp_path):
    # uname -m says aarch64 while the userland is 32-bit; getconf is the tell.
    rc, out = run_firstboot(tmp_path, "DEPLOYMENT_MODE=pi_hosted\n",
                            extra_env={"FORCE_ARCH": "aarch64",
                                       "FORCE_LONG_BIT": "32"})
    assert rc == 0, out
    assert "32-bit OS detected (aarch64)" in out


def test_32bit_satellite_is_told_it_still_works(tmp_path):
    # A Pi Zero 2 W thin client pulls no image, so the wording must not tell
    # the owner to reflash a device that is doing its job.
    rc, out = run_firstboot(tmp_path, "DEPLOYMENT_MODE=pi_remote\n",
                            extra_env={"FORCE_ARCH": "armv7l"})
    assert rc == 0, out
    assert "Satellite mode works here" in out
    assert "The stack will not start" not in out


def test_64bit_os_says_nothing_about_architecture(tmp_path):
    rc, out = run_firstboot(tmp_path, "DEPLOYMENT_MODE=pi_hosted\n",
                            extra_env={"FORCE_ARCH": "aarch64",
                                       "FORCE_LONG_BIT": "64"})
    assert rc == 0, out
    assert "32-bit OS detected" not in out


# --- Compose plugin checksum -------------------------------------------------


def _verify_compose(tmp_path, binary: bytes, sums: str | None,
                    arch: str = "aarch64"):
    """Call verify_compose_binary in isolation and return (rc, output, path)."""
    binpath = tmp_path / "docker-compose"
    binpath.write_bytes(binary)
    env = {**os.environ, "FIRSTBOOT_SOURCE_ONLY": "1", "DRY_RUN": "1"}
    if sums is not None:
        sumfile = tmp_path / "checksums.txt"
        sumfile.write_text(sums)
        env["COMPOSE_SUMS_FILE"] = str(sumfile)
    else:
        # An empty file stands in for a checksums file that could not be
        # fetched, so the test never reaches the network.
        sumfile = tmp_path / "empty.txt"
        sumfile.write_text("")
        env["COMPOSE_SUMS_FILE"] = str(sumfile)
    proc = subprocess.run(
        ["bash", "-c",
         f'source "{FIRSTBOOT}"; verify_compose_binary v2.29.7 {arch} "{binpath}"'],
        env=env, capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr, binpath


def test_compose_binary_with_a_matching_checksum_is_kept(tmp_path):
    import hashlib
    body = b"#!/bin/true\n"
    digest = hashlib.sha256(body).hexdigest()
    rc, out, binpath = _verify_compose(
        tmp_path, body, f"{digest}  docker-compose-linux-aarch64\n")
    assert rc == 0, out
    assert binpath.exists()
    assert "Verified the Docker Compose" in out


def test_compose_binary_with_a_bad_checksum_is_deleted(tmp_path):
    rc, out, binpath = _verify_compose(
        tmp_path, b"tampered\n", f"{'0' * 64}  docker-compose-linux-aarch64\n")
    assert rc != 0
    assert not binpath.exists()
    assert "checksum mismatch" in out


def test_compose_checksum_reads_the_line_for_this_arch(tmp_path):
    import hashlib
    body = b"#!/bin/true\n"
    digest = hashlib.sha256(body).hexdigest()
    sums = (f"{'1' * 64}  docker-compose-linux-x86_64\n"
            f"{digest} *docker-compose-linux-aarch64\n")
    rc, out, binpath = _verify_compose(tmp_path, body, sums)
    assert rc == 0, out
    assert binpath.exists()


def test_unverifiable_compose_binary_is_kept_with_a_warning(tmp_path):
    # No checksums file (offline, or the release moved it): this path is the
    # last resort after both apt routes missed, so dropping the only compose
    # binary would leave the stack with none at all.
    rc, out, binpath = _verify_compose(tmp_path, b"#!/bin/true\n", None)
    assert rc == 0, out
    assert binpath.exists()
    assert "Could not verify" in out


def test_a_missing_compose_download_fails_verification(tmp_path):
    rc, out, _ = _verify_compose(tmp_path, b"", "")
    assert rc != 0


# --- Stream Deck dependency pins ---------------------------------------------


def test_streamdeck_deps_come_from_the_repo_requirements_file(tmp_path):
    rc, out = run_firstboot(
        tmp_path,
        "DEPLOYMENT_MODE=pi_hosted\n",
        extra_env={"FORCE_STREAMDECK": "1",
                   "REPO_DIR": str(REPO),
                   "ASSET_DIR": str(tmp_path / "no-assets")},
    )
    assert rc == 0, out
    assert f"pip install --quiet -r {REPO}/streamdeck/requirements.txt" in out
    # The inline list is the fallback only; it must not run alongside the file.
    assert "streamdeck>=0.9.8,<1" not in out


def test_streamdeck_inline_pins_match_the_requirements_file():
    """firstboot can run from a boot payload with no repo checked out, so it
    keeps an inline copy of the pins. The two must not drift."""
    inline = FIRSTBOOT.read_text()
    inline = inline[inline.index("installing the inline pin list"):]
    inline = inline[:inline.index("\n  fi\n")]
    for line in (REPO / "streamdeck" / "requirements.txt").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        assert f'"{line}"' in inline, f"{line} missing from the inline fallback"


def test_accelerometer_smbus2_comes_from_apt_not_pip():
    """pip is not part of Raspberry Pi OS Lite on trixie, so the accelerometer
    helper's smbus2 comes from the distro package (bookworm and trixie both
    carry it, inside the range gadgets/requirements.txt pins)."""
    text = FIRSTBOOT.read_text()
    body = text[text.index("install_accel_rotation()"):]
    body = body[:body.index("\n}\n") + 3]
    assert "apt_install python3-smbus2" in body
    assert "pip3 install" not in body
    assert "--break-system-packages" not in body


# --- Recovery hotspot services (PIHOST-V1, PIHOST-V2) ------------------------


def _function_body(name: str) -> str:
    text = FIRSTBOOT.read_text()
    body = text[text.index(f"{name}() {{"):]
    return body[:body.index("\n}\n") + 3]


def test_hostapd_is_unmasked_and_both_services_kept_off_at_boot():
    """Debian's hostapd postinst masks hostapd.service when it is installed
    before its config exists, which is the order this function uses, and a
    masked unit refuses the watchdog's 'systemctl start hostapd'. The unmask
    and the boot-time disable must come after the install, and neither unit
    may be masked (the watchdog and the bridge start them on demand)."""
    body = _function_body("configure_wifi_ap_fallback")
    install = body.index("apt-get install -y -q hostapd dnsmasq")
    unmask = body.index("systemctl unmask hostapd.service")
    disable = body.index("systemctl disable hostapd.service dnsmasq.service")
    stop = body.index("systemctl stop dnsmasq.service")
    assert install < unmask < disable < stop
    assert "systemctl mask" not in body
    assert "enable hostapd" not in body and "enable dnsmasq" not in body


def test_hotspot_dnsmasq_binds_only_the_hotspot_interface():
    body = _function_body("configure_wifi_ap_fallback")
    conf = body[body.index("foodassistant-ap.conf <<'EOF'"):]
    conf = conf[:conf.index("\nEOF\n")]
    for line in ("interface=wlan0", "bind-interfaces", "except-interface=lo",
                 "address=/#/192.168.99.1"):
        assert f"\n{line}\n" in conf + "\n", line


def test_hosted_pi_dry_run_mentions_the_unmask(tmp_path):
    rc, out = run_firstboot(tmp_path, "DEPLOYMENT_MODE=pi_hosted\n",
                            extra_env={"FORCE_PI": "1"})
    assert rc == 0, out
    assert "install hostapd dnsmasq" in out
    assert "unmask hostapd" in out
    assert "disable hostapd and dnsmasq at boot" in out


def test_hotspot_skipped_off_a_pi(tmp_path):
    rc, out = run_firstboot(tmp_path, "DEPLOYMENT_MODE=pi_hosted\n")
    assert rc == 0, out
    assert "skipping the Wi-Fi recovery hotspot" in out
    assert "install hostapd dnsmasq" not in out


# --- Server mode stays off the machine's own network setup (PIHOST-3/V4) ------


def test_server_mode_leaves_hotspot_port80_and_hostname_alone(tmp_path):
    # FORCE_PI too: server mode alone must keep the appliance steps away.
    rc, out = run_firstboot(tmp_path, "DEPLOYMENT_MODE=server\n",
                            extra_env={"FORCE_PI": "1"})
    assert rc == 0, out
    assert "install hostapd dnsmasq" not in out
    assert "PREROUTING" not in out
    assert "foodassistant-port80.service" not in out
    assert "Setting hostname" not in out
    assert "keeping this machine's hostname" in out
    assert "the app is served on port 9284" in out
    # The closing lines point at the port the app actually answers on.
    assert ".local:9284/setup" in out
    assert ".local/setup" not in out


def test_server_mode_renames_when_asked_by_the_installer(tmp_path):
    rc, out = run_firstboot(tmp_path, "DEPLOYMENT_MODE=server\n",
                            extra_env={"FA_HOSTNAME": "kitchen"})
    assert rc == 0, out
    assert "Setting hostname" in out and "-> 'kitchen'" in out
    assert "http://kitchen.local:9284/setup" in out


def test_server_mode_renames_when_the_config_names_a_host(tmp_path):
    rc, out = run_firstboot(tmp_path, "DEPLOYMENT_MODE=server\nHOSTNAME=pantry\n")
    assert rc == 0, out
    assert "-> 'pantry'" in out


def test_server_targeted_run_names_the_machine_not_the_brand(tmp_path):
    # A STEPS run that skips the hostname step still reports the host the
    # server answers at, never the brand default.
    rc, out = run_firstboot(tmp_path, "DEPLOYMENT_MODE=server\n",
                            extra_env={"STEPS": "timezone"})
    assert rc == 0, out
    assert "http://pr.local" not in out
    assert "Setting hostname" not in out


def test_pi_hosted_still_takes_the_brand_hostname(tmp_path):
    rc, out = run_firstboot(tmp_path, "DEPLOYMENT_MODE=pi_hosted\n")
    assert rc == 0, out
    assert "-> 'pr'" in out or "Hostname already 'pr'" in out


# --- Port 80 redirect scoped to this device (PIHOST-V3) ----------------------


def _port80_unit_text(script: Path = FIRSTBOOT) -> str:
    env = {**os.environ, "FIRSTBOOT_SOURCE_ONLY": "1", "DRY_RUN": "1"}
    proc = subprocess.run(
        ["bash", "-c", f'source "{script}"; port80_unit_text'],
        env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_port80_unit_redirects_only_traffic_for_this_device():
    unit = _port80_unit_text()
    execs = [l for l in unit.splitlines() if l.startswith("ExecStart=")]
    pre = [l for l in execs if "PREROUTING" in l]
    assert len(pre) == 1
    assert "-m addrtype --dst-type LOCAL" in pre[0]
    # It clears the old unscoped rule a deployed device may still carry, and
    # the -C check and -A add both carry the scope.
    assert ("iptables -t nat -D PREROUTING -p tcp --dport 80 -j REDIRECT "
            "--to-port 9284") in pre[0]
    assert pre[0].count("--dst-type LOCAL") == 2
    out = [l for l in execs if "OUTPUT" in l]
    assert len(out) == 1 and "-o lo" in out[0]


def test_every_prerouting_add_in_firstboot_is_scoped():
    import re
    text = FIRSTBOOT.read_text()
    adds = re.findall(r"-t nat -[AC] PREROUTING[^'\n;|]*", text)
    assert adds, "no PREROUTING rules found"
    for rule in adds:
        assert "-m addrtype --dst-type LOCAL" in rule, rule


def test_port80_live_path_deletes_the_old_rule_first():
    body = _function_body("configure_port80")
    delete = body.index("-D PREROUTING -p tcp --dport 80 -j REDIRECT")
    add = body.index("-A PREROUTING")
    assert delete < add
    assert 'port80_unit_text > "$oneshot"' in body


def test_hosted_dry_run_describes_the_scoped_redirect(tmp_path):
    rc, out = run_firstboot(tmp_path, "DEPLOYMENT_MODE=pi_hosted\n")
    assert rc == 0, out
    assert "--dst-type LOCAL" in out


# --- Provisioning polish -----------------------------------------------------


def test_seed_points_ollama_at_localhost_when_enabled(tmp_path):
    # The app is host-networked; http://ollama:11434 does not resolve there.
    rc, out = run_firstboot(tmp_path, "ENABLE_OLLAMA=true\n")
    assert rc == 0, out
    assert '"ollama_base_url": "http://localhost:11434"' in out


def test_seed_leaves_ollama_alone_when_off(tmp_path):
    rc, out = run_firstboot(tmp_path, "ENABLE_OLLAMA=false\n")
    assert rc == 0, out
    assert "ollama_base_url" not in out


def _write_env_file(tmp_path: Path, existing: str | None) -> str:
    env_path = tmp_path / "stack.env"
    if existing is not None:
        env_path.write_text(existing)
    env = {**os.environ, "FIRSTBOOT_SOURCE_ONLY": "1", "DRY_RUN": "0"}
    script = (
        f'source "{FIRSTBOOT}"; '
        'TZ=UTC FOODASSISTANT_TAG=latest ENABLE_MEALIE=false ENABLE_OLLAMA=false '
        'REPO_DIR=/opt/foodassistant-src; '
        f'write_env_file "{env_path}"'
    )
    proc = subprocess.run(["bash", "-c", script], env=env,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return env_path.read_text()


def test_env_file_records_repo_dir(tmp_path):
    text = _write_env_file(tmp_path, None)
    assert "\nREPO_DIR=/opt/foodassistant-src\n" in text
    assert "FOODASSISTANT_TAG=latest" in text


def test_existing_env_file_gains_only_a_missing_repo_dir(tmp_path):
    text = _write_env_file(tmp_path, "TZ=Europe/Paris\nMY_SECRET=keep\n")
    assert text == ("TZ=Europe/Paris\nMY_SECRET=keep\n"
                    "REPO_DIR=/opt/foodassistant-src\n")


def test_repo_dir_never_joins_an_unterminated_last_line(tmp_path):
    # A hand-edited .env without a final newline keeps its last setting intact.
    text = _write_env_file(tmp_path, "TZ=UTC\nCOMPOSE_PROFILES=with-grocy")
    assert text == ("TZ=UTC\nCOMPOSE_PROFILES=with-grocy\n"
                    "REPO_DIR=/opt/foodassistant-src\n")


def test_existing_env_file_with_repo_dir_is_untouched(tmp_path):
    original = "TZ=UTC\nREPO_DIR=/somewhere/else\n"
    assert _write_env_file(tmp_path, original) == original


def test_compose_v2_comes_from_debian_trixies_docker_compose(tmp_path):
    """Debian trixie ships Compose v2 as plain 'docker-compose'; neither of
    the other package names exists there. With it on offer, no GitHub
    download happens."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    marker = tmp_path / "compose-installed"
    log = tmp_path / "calls.log"
    (stubs / "docker").write_text(
        "#!/usr/bin/env bash\n"
        f'[ "$1" = compose ] && {{ [ -f "{marker}" ]; exit $?; }}\n'
        "exit 0\n")
    (stubs / "apt-get").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "apt-get $*" >> "{log}"\n'
        f'[ "${{@: -1}}" = docker-compose ] && touch "{marker}" && exit 0\n'
        "exit 100\n")
    for tool in ("curl", "wget"):
        (stubs / tool).write_text(
            f'#!/usr/bin/env bash\necho "{tool} $*" >> "{log}"\nexit 1\n')
    for s in stubs.iterdir():
        s.chmod(0o755)
    env = {**os.environ, "FIRSTBOOT_SOURCE_ONLY": "1", "DRY_RUN": "0",
           "PATH": f"{stubs}:{os.environ['PATH']}"}
    proc = subprocess.run(
        ["bash", "-c", f'source "{FIRSTBOOT}"; install_compose_v2'],
        env=env, capture_output=True, text=True)
    calls = log.read_text()
    assert proc.returncode == 0, proc.stdout + proc.stderr + calls
    assert "install -y docker-compose\n" in calls
    assert "curl" not in calls and "wget" not in calls


def test_distro_docker_fallback_tries_debians_compose_package():
    body = _function_body("install_docker")
    v2 = body.index("apt-get install -y docker.io docker-compose-v2")
    deb = body.index("apt-get install -y docker.io docker-compose;")
    bare = body.index("apt-get install -y docker.io;")
    assert v2 < deb < bare


# --- R2: release trust, verified checkout, fresh sign-in state ----------------
# These run the real provisioner functions against throwaway git repositories
# and keys made under tmp_path (nothing reads this repository's history or
# tags), with every system path pointed into tmp_path.

import importlib.machinery  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import stat  # noqa: E402

import pytest  # noqa: E402

UPDATER = REPO / "scripts" / "image-build" / "foodassistant-update"
BRIDGE = REPO / "scripts" / "image-build" / "foodassistant-host-bridge"
RELEASE_SIGNERS = REPO / "scripts" / "release-signers"

needs_git_tools = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("bash", "git", "python3", "ssh-keygen")),
    reason="bash, git, python3 and ssh-keygen are needed for the signed rig",
)

_GIT_ID = ["-c", "user.email=test@test", "-c", "user.name=test",
           "-c", "core.hooksPath=/dev/null", "-c", "init.defaultBranch=main"]


def _clean_env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    for k in ("UPDATE_CHANNEL", "FOODASSISTANT_TAG", "FA_BOOTSTRAP_SIGNERS",
              "REPO_DIR", "STEPS", "FORCE", "AP_BOOT_COPY", "AP_PASSPHRASE"):
        env.pop(k, None)
    env.update(extra)
    return env


def _git(cwd, *args):
    return subprocess.run(["git", *_GIT_ID, *args], cwd=cwd, check=True,
                          capture_output=True, text=True)


def _heredoc(path: Path, opener: str, closer: str) -> str:
    """The text between the line ending in OPENER and the CLOSER line."""
    lines = path.read_text().splitlines(keepends=True)
    start = next(i for i, l in enumerate(lines) if l.rstrip().endswith(opener))
    end = next(i for i in range(start + 1, len(lines)) if lines[i].rstrip("\n") == closer)
    return "".join(lines[start + 1:end])


def _key_lines(text: str) -> list[str]:
    return [l.strip() for l in text.splitlines()
            if l.strip() and not l.strip().startswith("#")]


@pytest.fixture(scope="module")
def release_key(tmp_path_factory):
    if not shutil.which("ssh-keygen"):
        pytest.skip("ssh-keygen is needed for the signed rig")
    d = tmp_path_factory.mktemp("fb-release-key")
    key = d / "key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test",
                    "-f", str(key)], check=True, capture_output=True)
    kind, blob = (d / "key.pub").read_text().split()[:2]
    signers = d / "signers"
    signers.write_text(
        f'pantryraider-release namespaces="git,pantryraider-release" {kind} {blob}\n')
    return {"key": key, "signers": signers}


def _origin(tmp_path: Path, release_key, *, signed=True, version="0.9.1",
            tag=True) -> Path:
    """A bare origin holding one commit (service/app/config.py at VERSION) and,
    with TAG, an annotated tag vVERSION; signed with the test key when SIGNED."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", *_GIT_ID, "init", "-q", "--bare", str(origin)],
                   check=True, capture_output=True)
    work = tmp_path / "work"
    subprocess.run(["git", *_GIT_ID, "init", "-q", str(work)], check=True,
                   capture_output=True)
    if signed:
        _git(work, "config", "gpg.format", "ssh")
        _git(work, "config", "user.signingkey", str(release_key["key"]))
        _git(work, "config", "commit.gpgsign", "true")
        _git(work, "config", "tag.gpgsign", "true")
    else:
        _git(work, "config", "commit.gpgsign", "false")
        _git(work, "config", "tag.gpgsign", "false")
    cfg = work / "service" / "app" / "config.py"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(f'APP_VERSION = "{version}"\n')
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "release")
    if tag:
        _git(work, "tag", "-a", "-m", f"v{version}", f"v{version}")
    _git(work, "push", "-q", str(origin), "main", "--tags")
    return origin


def _verified_checkout(tmp_path, release_key, origin, **extra):
    """Run ensure_verified_checkout for real (DRY_RUN=0) against ORIGIN."""
    trust = tmp_path / "trust" / "release-signers"
    trust.parent.mkdir(exist_ok=True)
    trust.write_text(release_key["signers"].read_text())
    env = _clean_env(
        FIRSTBOOT_SOURCE_ONLY="1", DRY_RUN="0",
        REPO_DIR=str(tmp_path / "src"), REPO_URL=str(origin),
        TRUST_FILE=str(trust),
        STATE_FILE=str(tmp_path / "state" / "update-state.json"),
        CHANNEL_FILE=str(tmp_path / "etc" / "update-channel"),
        PIN_STATE_FILE=str(tmp_path / "etc" / "update-tag-restore"),
    )
    env.update(extra)
    script = (f'source "{FIRSTBOOT}"; '
              'FOODASSISTANT_TAG_EXPLICIT=0; FOODASSISTANT_TAG=latest; '
              '[ -n "${PIN_TAG:-}" ] && { FOODASSISTANT_TAG="$PIN_TAG"; FOODASSISTANT_TAG_EXPLICIT=1; }; '
              'case "${CHAN:-}" in main) UPDATE_CHANNEL=main ;; *) UPDATE_CHANNEL=stable ;; esac; '
              'ensure_verified_checkout; echo "TAG=$FOODASSISTANT_TAG"')
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True,
                          text=True)


def test_release_key_is_installed_before_the_checkout_and_dry_run_runs_no_git(tmp_path):
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    calls = tmp_path / "git-calls"
    (stubs / "git").write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\nexit 1\n')
    (stubs / "git").chmod(0o755)
    rc, out = run_firstboot(tmp_path, "HOSTNAME=pr\n",
                            {"PATH": f"{stubs}:{os.environ['PATH']}"})
    assert rc == 0, out
    seed = out.index("DRY_RUN would seed")
    key = out.index("DRY_RUN would install the release key at")
    checkout = out.index(
        "DRY_RUN would verify and check out the newest signed release (channel stable)")
    assert seed < key < checkout < out.index("Deploying stack")
    assert not calls.exists(), calls.read_text()


def test_dry_run_names_the_main_channel(tmp_path):
    rc, out = run_firstboot(tmp_path, "UPDATE_CHANNEL=main\n")
    assert rc == 0, out
    assert "(channel main)" in out
    assert '"update_channel": "main"' in out


def test_an_unset_channel_is_not_saved_as_a_choice(tmp_path):
    rc, out = run_firstboot(tmp_path, "HOSTNAME=pr\n")
    assert rc == 0, out
    assert "update_channel" not in out
    rc, out = run_firstboot(tmp_path, "UPDATE_CHANNEL=stable\n")
    assert '"update_channel": "stable"' in out


@needs_git_tools
def test_signed_release_is_checked_out_and_recorded(tmp_path, release_key):
    origin = _origin(tmp_path, release_key)
    r = _verified_checkout(tmp_path, release_key, origin)
    assert r.returncode == 0, r.stdout + r.stderr
    src = tmp_path / "src"
    head = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    tagged = subprocess.run(["git", "-C", str(origin), "rev-parse", "v0.9.1^{commit}"],
                            capture_output=True, text=True).stdout.strip()
    assert head == tagged
    state = json.loads((tmp_path / "state" / "update-state.json").read_text())
    assert state["highest_version"] == "0.9.1"
    assert state["sha"] == tagged
    assert state["channel"] == "stable"
    assert state["trust_fp"].startswith("SHA256:")
    assert state["bootstrapped_at"].endswith("Z")
    # The first image follows the verified release, and moving to main later
    # restores a main image.
    assert "TAG=0.9.1" in r.stdout
    assert (tmp_path / "etc" / "update-tag-restore").read_text() == "main\n"
    assert (tmp_path / "etc" / "update-channel").read_text() == "stable\n"


@needs_git_tools
def test_an_explicit_image_tag_wins_over_the_verified_version(tmp_path, release_key):
    origin = _origin(tmp_path, release_key)
    r = _verified_checkout(tmp_path, release_key, origin, PIN_TAG="v1.2.3")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "TAG=v1.2.3" in r.stdout
    assert not (tmp_path / "etc" / "update-tag-restore").exists()


@needs_git_tools
def test_main_channel_checks_out_the_signed_tip(tmp_path, release_key):
    origin = _origin(tmp_path, release_key, tag=False)
    r = _verified_checkout(tmp_path, release_key, origin, CHAN="main")
    assert r.returncode == 0, r.stdout + r.stderr
    branch = subprocess.run(["git", "-C", str(tmp_path / "src"), "rev-parse",
                             "--abbrev-ref", "HEAD"], capture_output=True, text=True)
    assert branch.stdout.strip() == "main"
    assert "TAG=main" in r.stdout
    assert (tmp_path / "etc" / "update-channel").read_text() == "main\n"
    assert not (tmp_path / "etc" / "update-tag-restore").exists()


@needs_git_tools
def test_an_unsigned_origin_stops_provisioning(tmp_path, release_key):
    origin = _origin(tmp_path, release_key, signed=False)
    r = _verified_checkout(tmp_path, release_key, origin)
    assert r.returncode != 0
    assert ("Setup could not verify the Pantry Raider download and will try "
            "again shortly.") in r.stdout + r.stderr
    head = subprocess.run(["git", "-C", str(tmp_path / "src"), "rev-parse", "-q",
                           "--verify", "HEAD"], capture_output=True, text=True)
    assert head.returncode != 0, "nothing may be checked out"
    assert not (tmp_path / "state" / "update-state.json").exists()
    # The main channel refuses an unsigned tip the same way.
    shutil.rmtree(tmp_path / "src")
    r = _verified_checkout(tmp_path, release_key, origin, CHAN="main")
    assert r.returncode != 0
    assert "Setup could not verify" in r.stdout + r.stderr


@needs_git_tools
def test_a_tag_signed_by_another_key_is_skipped(tmp_path, release_key):
    origin = _origin(tmp_path, release_key)
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f",
                    str(other / "k")], check=True, capture_output=True)
    kind, blob = (other / "k.pub").read_text().split()[:2]
    wrong = {"signers": other / "signers"}
    wrong["signers"].write_text(
        f'pantryraider-release namespaces="git,pantryraider-release" {kind} {blob}\n')
    r = _verified_checkout(tmp_path, wrong, origin)
    assert r.returncode != 0
    assert "v0.9.1 did not verify" in r.stdout + r.stderr


@needs_git_tools
def test_a_steps_run_never_clones_or_moves_the_checkout(tmp_path, release_key):
    origin = _origin(tmp_path, release_key)
    src = tmp_path / "src"
    subprocess.run(["git", "clone", "-q", str(origin), str(src)], check=True,
                   capture_output=True)
    ib = src / "scripts" / "image-build"
    ib.mkdir(parents=True)
    (ib / "foodassistant-agent-key").write_text("#!/bin/sh\n")
    before = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"],
                            capture_output=True, text=True).stdout
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    calls = tmp_path / "git-calls"
    (stubs / "git").write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\nexit 1\n')
    (stubs / "git").chmod(0o755)
    asset = tmp_path / "asset"
    asset.mkdir()
    env = _clean_env(
        DRY_RUN="0", STEPS="hostbridge", REPO_DIR=str(src),
        REPO_URL=str(tmp_path / "nowhere.git"), ASSET_DIR=str(asset),
        CONFIG_CANDIDATES=str(tmp_path / "none.env"),
        LOG_FILE=str(tmp_path / "fb.log"), STEP_LOG=str(tmp_path / "step.log"),
        DONE_MARKER=str(tmp_path / "done"), INSTALL_DIR=str(tmp_path / "install"),
        HELPER_BIN_DIR=str(tmp_path / "bin"),
        TRUST_FILE=str(tmp_path / "trust"), STATE_FILE=str(tmp_path / "state.json"),
        PATH=f"{stubs}:{os.environ['PATH']}",
    )
    r = subprocess.run(["bash", str(FIRSTBOOT)], env=env, capture_output=True,
                       text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not calls.exists(), calls.read_text()
    after = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"],
                           capture_output=True, text=True).stdout
    assert before == after
    assert not (tmp_path / "trust").exists()
    assert not (tmp_path / "install" / "data" / "auth-state.json").exists()
    # The hostbridge step still installs the helpers from the checkout.
    assert (tmp_path / "bin" / "foodassistant-agent-key").exists()


def test_full_run_trust_steps_sit_behind_the_steps_guard():
    body = _function_body("main")
    guard = body.index('if [ -z "$STEPS" ]; then\n    use_root_owned_checkout\n    seed_auth_state')
    assert body.index("check_arch_64bit") < guard
    assert body.index("apt-get update") < guard
    assert body.count("ensure_verified_checkout") == 1
    order = [body.index(n) for n in
             ("use_root_owned_checkout", "seed_auth_state", "install_release_trust",
              "ensure_verified_checkout", "configure_hostname")]
    assert order == sorted(order)
    assert "ensure_repo" not in FIRSTBOOT.read_text()


def _source_only(script: str, **env) -> subprocess.CompletedProcess:
    full = _clean_env(FIRSTBOOT_SOURCE_ONLY="1", DRY_RUN="0",
                      CHANNEL_FILE="/nonexistent/pantry-raider-test/update-channel")
    full.update(env)
    return subprocess.run(["bash", "-c", f'source "{FIRSTBOOT}"; {script}'],
                          env=full, capture_output=True, text=True)


def test_repo_dir_prefers_an_existing_checkout(tmp_path):
    a, b = tmp_path / "opt-src", tmp_path / "home-src"
    (b / ".git").mkdir(parents=True)
    cands = f"{a} {b}"
    r = _source_only('echo "DIR=$REPO_DIR"', REPO_DIR_CANDIDATES=cands)
    assert f"DIR={b}" in r.stdout
    (a / ".git").mkdir(parents=True)
    r = _source_only('echo "DIR=$REPO_DIR"', REPO_DIR_CANDIDATES=cands)
    assert f"DIR={a}" in r.stdout
    r = _source_only('echo "DIR=$REPO_DIR"', REPO_DIR_CANDIDATES=cands,
                     REPO_DIR="/custom/src")
    assert "DIR=/custom/src" in r.stdout


def test_a_full_run_never_uses_the_old_home_checkout(tmp_path):
    """FORCE=1 on an older device: only the /home clone exists. A full run
    sets up the root-owned checkout instead; an explicit REPO_DIR wins."""
    a, b = tmp_path / "opt-src", tmp_path / "home-src"
    (b / ".git").mkdir(parents=True)
    cands = f"{a} {b}"
    r = _source_only('use_root_owned_checkout; echo "DIR=$REPO_DIR"',
                     REPO_DIR_CANDIDATES=cands)
    assert f"DIR={a}" in r.stdout, r.stdout + r.stderr
    r = _source_only('use_root_owned_checkout; echo "DIR=$REPO_DIR"',
                     REPO_DIR_CANDIDATES=cands, REPO_DIR=str(b))
    assert f"DIR={b}" in r.stdout
    body = FIRSTBOOT.read_text()
    main = body[body.index("\nmain() {"):]
    assert main.index("use_root_owned_checkout") < main.index("ensure_verified_checkout")


def test_repo_dir_defaults_to_opt_for_a_fresh_device(tmp_path):
    r = _source_only('echo "DIR=$REPO_DIR"',
                     REPO_DIR_CANDIDATES=f"{tmp_path / 'x'} {tmp_path / 'y'}")
    assert "DIR=/opt/foodassistant-src" in r.stdout


SEED_KEYS = {"v", "origin", "first_start", "loopback_trust", "kiosk_gen"}


def test_seed_auth_state_writes_the_fresh_seed_at_0600(tmp_path):
    r = _source_only("seed_auth_state", INSTALL_DIR=str(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    path = tmp_path / "data" / "auth-state.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    doc = json.loads(path.read_text())
    assert set(doc) == SEED_KEYS
    assert doc["v"] == 1 and doc["origin"] == "fresh" and doc["kiosk_gen"] == 1
    assert doc["loopback_trust"] == {
        "state": "retired", "since": doc["first_start"], "by": "firstboot",
        "retrust_until": None}
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", doc["first_start"])


def test_seed_auth_state_leaves_an_existing_install_alone(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    existing = b'{"v": 1, "origin": "upgraded", "custom": [1, 2]}\n'
    (data / "auth-state.json").write_bytes(existing)
    r = _source_only("seed_auth_state", INSTALL_DIR=str(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert (data / "auth-state.json").read_bytes() == existing
    # An install with settings but no state file is not seeded either.
    other = tmp_path / "other"
    (other / "data").mkdir(parents=True)
    (other / "data" / "settings.json").write_text("{}")
    _source_only("seed_auth_state", INSTALL_DIR=str(other))
    assert not (other / "data" / "auth-state.json").exists()


def _kiosk_exec(tmp_path, with_launcher: bool) -> str:
    bin_dir = tmp_path / ("bin-l" if with_launcher else "bin-n")
    bin_dir.mkdir()
    if with_launcher:
        launcher = bin_dir / "foodassistant-kiosk-launch"
        launcher.write_text("#!/bin/sh\n")
        launcher.chmod(0o755)
    r = _source_only('kiosk_exec_start /usr/bin/cage /usr/bin/chromium '
                     '"--load-extension=/x" "http://localhost:9284/ui/?kiosk=1"',
                     HELPER_BIN_DIR=str(bin_dir))
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_kiosk_exec_start_uses_the_launcher_when_installed(tmp_path):
    out = _kiosk_exec(tmp_path, True)
    first = out.splitlines()[0]
    after = first.split(" -- ", 1)[1]
    assert after.startswith(str(tmp_path / "bin-l" / "foodassistant-kiosk-launch") + " /usr/bin/chromium --kiosk")
    assert "--remote-debugging-port" not in out
    assert out.rstrip("\n").split()[-1] == "http://localhost:9284/ui/?kiosk=1"
    assert "--load-extension=/x" in out


def test_kiosk_exec_start_keeps_the_debug_port_without_the_launcher(tmp_path):
    out = _kiosk_exec(tmp_path, False)
    assert out.startswith("/usr/bin/cage -- /usr/bin/chromium --kiosk")
    assert "--remote-debugging-port=9222 --disable-restore-session-state" in out
    assert out.rstrip("\n").split()[-1] == "http://localhost:9284/ui/?kiosk=1"
    # Continuation lines end in a single backslash, as the unit needs.
    assert all(l.endswith(" \\") for l in out.splitlines()[:-1])


@pytest.mark.parametrize("fn,unit", [
    ("configure_kiosk", "foodassistant-kiosk.service"),
    ("configure_streamdeck", "foodassistant-streamdeck.service"),
])
def test_local_credentials_land_before_the_first_start(fn, unit):
    body = _function_body(fn)
    write = body.index(f"cat > /etc/systemd/system/{unit} <<EOF")
    reload_ = body.index("systemctl daemon-reload", write)
    harden = body.index("run_host_harden", reload_)
    enable = body.index(f"systemctl enable {unit}")
    start = body.index("systemctl start", enable)
    assert write < reload_ < harden < enable < start
    assert "local-credentials" in _function_body("run_host_harden")


def test_kiosk_unit_takes_its_execstart_from_kiosk_exec_start():
    body = _function_body("configure_kiosk")
    assert "ExecStart=$kiosk_exec\n" in body
    assert "--remote-debugging-port=9222" not in body


def _helper_tree(tmp_path: Path, update_gen: int | None) -> Path:
    src = tmp_path / "src"
    ib = src / "scripts" / "image-build"
    ib.mkdir(parents=True)
    for name in ("foodassistant-host-harden", "foodassistant-kiosk-launch",
                 "foodassistant-set-rotation", "foodassistant-host-bridge",
                 "foodassistant-host-bridge.service", "foodassistant-firstrun.sh"):
        (ib / name).write_text(f"#!/bin/sh\n# {name}\n")
    upd = "#!/bin/sh\n"
    if update_gen is not None:
        upd += f"UPDATER_TRUST_GENERATION={update_gen}\n"
    (ib / "foodassistant-update").write_text(upd)
    return src


def test_install_all_helpers_installs_every_helper_but_the_bridge(tmp_path):
    src = _helper_tree(tmp_path, 1)
    bin_dir = tmp_path / "bin"
    r = _source_only("install_all_helpers", REPO_DIR=str(src),
                     HELPER_BIN_DIR=str(bin_dir))
    assert r.returncode == 0, r.stdout + r.stderr
    got = sorted(p.name for p in bin_dir.iterdir())
    assert got == ["foodassistant-host-harden", "foodassistant-kiosk-launch",
                   "foodassistant-set-rotation", "foodassistant-update"]
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o755 for p in bin_dir.iterdir())


def test_install_all_helpers_keeps_a_newer_generation_updater(tmp_path):
    src = _helper_tree(tmp_path, None)  # an updater that predates signing
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    installed = "#!/bin/sh\nUPDATER_TRUST_GENERATION=1\n# installed\n"
    (bin_dir / "foodassistant-update").write_text(installed)
    r = _source_only("install_all_helpers", REPO_DIR=str(src),
                     HELPER_BIN_DIR=str(bin_dir))
    assert r.returncode == 0, r.stdout + r.stderr
    assert (bin_dir / "foodassistant-update").read_text() == installed
    assert "Kept the installed updater" in r.stdout
    # An updater of the same or a higher generation replaces it.
    newer = src / "scripts" / "image-build" / "foodassistant-update"
    newer.write_text("#!/bin/sh\nUPDATER_TRUST_GENERATION=2\n")
    _source_only("install_all_helpers", REPO_DIR=str(src), HELPER_BIN_DIR=str(bin_dir))
    assert (bin_dir / "foodassistant-update").read_text() == newer.read_text()


def test_the_rotation_and_update_installers_share_install_all_helpers():
    assert "install_all_helpers" in _function_body("install_rotation_helper")
    assert "install_all_helpers" in _function_body("install_update_helper")
    assert "install_all_helpers" in _function_body("install_host_bridge")


def test_satellite_runs_the_hostbridge_step_before_the_screen():
    body = _function_body("main")
    remote = body[body.index("if is_remote_mode; then"):]
    assert remote.index('"hostbridge"') < remote.index('"kiosk"') < remote.index('"streamdeck"')


def test_remote_unit_ignores_forwarding_headers():
    body = _function_body("deploy_remote_service")
    line = next(l for l in body.splitlines() if l.startswith("ExecStart=$exec_uvicorn"))
    assert line.count("--no-proxy-headers") == 1


def _load_bridge():
    spec = importlib.util.spec_from_loader(
        "fb_test_bridge", importlib.machinery.SourceFileLoader("fb_test_bridge", str(BRIDGE)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ap_env(tmp_path, **extra):
    boot = tmp_path / "boot"
    boot.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "FIRSTBOOT": str(FIRSTBOOT),
        "AP_PASSPHRASE_FILE": str(tmp_path / "ap-passphrase"),
        "AP_BOOTDIR_CANDIDATES": str(boot),
        "AP_BOOT_COPY_FILE": str(tmp_path / "ap-boot-copy"),
    }
    for k in ("AP_BOOT_COPY", "AP_PASSPHRASE", "CONFIG_FOUND"):
        env.pop(k, None)
    env.update(extra)
    return env, boot / "pantry-raider-hotspot.txt"


def _ap_run(env, call='ap_fallback_passphrase "Kitchen Pi"'):
    return subprocess.run(["bash", "-c", _eval_ap_functions() + call], env=env,
                          capture_output=True, text=True)


def test_hotspot_card_copy_matches_the_bridge_text(tmp_path):
    env, card = _ap_env(tmp_path)
    r = _ap_run(env)
    assert r.returncode == 0, r.stderr
    passphrase = r.stdout.strip()
    assert card.read_text() == _load_bridge()._render_hotspot_mirror("Kitchen Pi", passphrase)
    assert len(card.read_text().splitlines()) == 4


def test_hotspot_card_copy_off_writes_the_flag_and_removes_a_copy(tmp_path):
    env, card = _ap_env(tmp_path, AP_BOOT_COPY="false")
    card.write_text("old copy\n")
    r = _ap_run(env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip()  # the hotspot still gets its password
    assert not card.exists()
    assert (tmp_path / "ap-boot-copy").read_text().strip() == "0"


def test_hotspot_card_copy_honours_the_owner_switch(tmp_path):
    env, card = _ap_env(tmp_path)
    (tmp_path / "ap-boot-copy").write_text("0\n")
    card.write_text("old copy\n")
    r = _ap_run(env)
    assert r.returncode == 0, r.stderr
    assert not card.exists()
    # Any other flag value keeps the copy.
    (tmp_path / "ap-boot-copy").write_text("1\n")
    _ap_run(env)
    assert card.exists()


def test_config_passphrase_is_commented_out_once_saved(tmp_path):
    cfg = tmp_path / "foodassistant.config.env"
    cfg.write_text("HOSTNAME=pr\nAP_PASSPHRASE=owner-picked-99\nTZ=UTC\n")
    env, _ = _ap_env(tmp_path, AP_PASSPHRASE="owner-picked-99", CONFIG_FOUND=str(cfg))
    r = _ap_run(env)
    assert r.stdout.strip() == "owner-picked-99"
    assert cfg.read_text() == "HOSTNAME=pr\n# AP_PASSPHRASE= (saved on the device)\nTZ=UTC\n"
    assert (tmp_path / "ap-passphrase").read_text().strip() == "owner-picked-99"


def test_config_passphrase_line_stays_when_it_was_not_used(tmp_path):
    cfg = tmp_path / "foodassistant.config.env"
    cfg.write_text("AP_PASSPHRASE=short\n")
    env, _ = _ap_env(tmp_path, AP_PASSPHRASE="short", CONFIG_FOUND=str(cfg))
    _ap_run(env)
    assert cfg.read_text() == "AP_PASSPHRASE=short\n"


def test_load_config_records_the_file_it_read(tmp_path):
    cfg = tmp_path / "c.env"
    cfg.write_text("HOSTNAME=pr\n")
    r = _source_only('load_config >/dev/null 2>&1; echo "FOUND=$CONFIG_FOUND"',
                     CONFIG_CANDIDATES=str(cfg), INSTALL_DIR=str(tmp_path))
    assert f"FOUND={cfg}" in r.stdout


def test_hotspot_closing_line_names_the_copy_only_when_written():
    body = _function_body("configure_wifi_ap_fallback")
    assert "ap_boot_copy_path" in body
    assert "pantry-raider-hotspot.txt on the SD card" not in body


def test_server_closing_log_says_it_keeps_itself_up_to_date(tmp_path):
    rc, out = run_firstboot(tmp_path, "DEPLOYMENT_MODE=server\n")
    assert rc == 0, out
    assert ("This server keeps itself up to date with tested releases. You can "
            "change that in Settings, Backups & Updates.") in out


def test_a_device_keeps_the_channel_it_already_follows(tmp_path):
    (tmp_path / "update-channel").write_text("main\n")
    rc, out = run_firstboot(tmp_path, "HOSTNAME=pr\n")
    assert rc == 0, out
    assert "(channel main)" in out
    # Read from the device, not asked for: not saved as a new choice.
    assert "update_channel" not in out
    # An explicit value still wins over the file.
    rc, out = run_firstboot(tmp_path, "UPDATE_CHANNEL=stable\n")
    assert "(channel stable)" in out


def test_update_state_floor_never_goes_down(tmp_path):
    state = tmp_path / "update-state.json"
    state.write_text(json.dumps({"highest_version": "0.30.0", "sha": "old"}) + "\n")
    r = _source_only('UPDATE_CHANNEL=stable; write_update_state 0.21.0 abc123',
                     STATE_FILE=str(state), TRUST_FILE=str(tmp_path / "none"))
    assert r.returncode == 0, r.stdout + r.stderr
    doc = json.loads(state.read_text())
    assert doc["highest_version"] == "0.30.0"
    assert doc["sha"] == "abc123"
    # A newer release raises it.
    r = _source_only('UPDATE_CHANNEL=stable; write_update_state 0.31.2 def456',
                     STATE_FILE=str(state), TRUST_FILE=str(tmp_path / "none"))
    assert json.loads(state.read_text())["highest_version"] == "0.31.2"


def test_a_replaced_bridge_that_was_running_is_restarted():
    body = _function_body("install_host_bridge")
    active = body.index("systemctl is-active --quiet foodassistant-host-bridge.service")
    changed = body.index('cmp -s "$bridge_src" /usr/local/bin/foodassistant-host-bridge')
    install = body.index('install -m 755 "$bridge_src"')
    assert active < install and changed < install
    restart = body[body.index('if [ "$bridge_was_active" = "1" ] && [ "$bridge_changed" = "1" ]'):]
    assert "--on-active=5s" in restart
    assert "systemctl restart foodassistant-host-bridge.service" in restart


def test_embedded_signers_match_the_release_signers_file():
    embedded = _heredoc(FIRSTBOOT, "<<'SIGNERS' || true", "SIGNERS")
    assert _key_lines(embedded) == _key_lines(RELEASE_SIGNERS.read_text())
    assert _key_lines(embedded)


def test_embedded_verifier_matches_the_updaters_byte_for_byte():
    ours = _heredoc(FIRSTBOOT, "<<'PYVERIFY' || true", "PYVERIFY")
    theirs = _heredoc(UPDATER, "<<'PYVERIFY' || true", "PYVERIFY")
    assert ours == theirs
    assert "def verify_tag" in ours


def test_new_provisioning_copy_has_no_em_dash():
    for path in (FIRSTBOOT, REPO / "image" / "config.env", REPO / "install.sh"):
        assert "\u2014" not in path.read_text(), path


# -- REPO_DIR moves off the home checkout (FoodAssistant-e5z9q) ---------------

_HOME_REPO = "/" + "home/foodassistant/FoodAssistant"


def _sound_root_src(tmp_path, monkeypatch, hook=True, git=True, config=True):
    src = tmp_path / "root-src"
    src.mkdir()
    if hook:
        (src / "docker" / "grocy-init").mkdir(parents=True)
    if git:
        (src / ".git").write_text("gitdir: /elsewhere\n")
    if config:
        (src / "service" / "app").mkdir(parents=True)
        (src / "service" / "app" / "config.py").write_text("APP_VERSION = '0.0.0'\n")
    src.chmod(0o755)
    monkeypatch.setenv("ROOT_SRC_DIR", str(src))
    monkeypatch.setenv("ROOT_SRC_UID", str(os.getuid()))
    return src


def test_existing_env_file_moves_repo_dir_off_home(tmp_path, monkeypatch):
    src = _sound_root_src(tmp_path, monkeypatch)
    text = _write_env_file(tmp_path, f"TZ=UTC\nREPO_DIR={_HOME_REPO}\nMY_SECRET=keep\n")
    assert text == f"TZ=UTC\nREPO_DIR={src}\nMY_SECRET=keep\n"


def test_home_repo_dir_stays_without_the_root_source(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOT_SRC_DIR", str(tmp_path / "absent"))
    original = f"TZ=UTC\nREPO_DIR={_HOME_REPO}\n"
    assert _write_env_file(tmp_path, original) == original


def test_home_repo_dir_stays_when_the_root_source_lacks_the_hook(tmp_path, monkeypatch):
    _sound_root_src(tmp_path, monkeypatch, hook=False)
    original = f"TZ=UTC\nREPO_DIR={_HOME_REPO}\n"
    assert _write_env_file(tmp_path, original) == original


@pytest.mark.parametrize("missing", ["git", "config", "both"])
def test_home_repo_dir_stays_when_the_root_source_is_not_a_checkout(
        tmp_path, monkeypatch, missing):
    # "both" is the empty folder Docker makes for a missing bind-mount source.
    _sound_root_src(tmp_path, monkeypatch, git=missing == "config",
                    config=missing == "git")
    original = f"TZ=UTC\nREPO_DIR={_HOME_REPO}\n"
    assert _write_env_file(tmp_path, original) == original


def test_home_repo_dir_stays_when_the_root_source_is_not_roots(tmp_path, monkeypatch):
    if os.getuid() == 0:
        pytest.skip("running as root")
    _sound_root_src(tmp_path, monkeypatch)
    monkeypatch.setenv("ROOT_SRC_UID", "0")
    original = f"TZ=UTC\nREPO_DIR={_HOME_REPO}\n"
    assert _write_env_file(tmp_path, original) == original
