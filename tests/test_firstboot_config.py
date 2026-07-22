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
        # Hermetic display detection: point the DRM connector scan at an empty
        # tree so has_display never sees the machine running the tests, and
        # drop any inherited graphical-session variables.
        "DRM_SYS_ROOT": str(tmp_path / "drm"),
    }
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
    """Bash snippet that loads the two AP passphrase helpers from firstboot.sh
    so they can be exercised without running the provisioner. `warn` is stubbed
    so the helpers' own logging does not need the full provisioner sourced."""
    return (
        'warn() { printf "%s\\n" "$*" >&2; }; '
        'eval "$(awk \'/^gen_ap_passphrase\\(\\)/,/^}/\' "$FIRSTBOOT")"; '
        'eval "$(awk \'/^ap_fallback_passphrase\\(\\)/,/^}/\' "$FIRSTBOOT")"; '
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
