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


def test_interactive_does_not_require_remote_url():
    # The interactive flow must never block waiting for a server URL: a shipped
    # pi_remote device is configured from the web wizard, not an SSH prompt.
    src = INSTALL.read_text()
    assert 'while [ -z "$REMOTE_SERVER_URL" ]' not in src
    assert "A server URL is required" not in src


def test_pi_remote_without_url_does_not_block():
    # A pre-provisioned image ships in pi_remote mode with no server URL; the
    # installer must succeed and leave the URL empty for the web wizard to set.
    # (No interactive prompt, no failure -- the regression we are guarding.)
    p = plan({
        "FORCE_PI": "1",
        "DEPLOYMENT_MODE": "pi_remote",
    })
    assert p["mode"] == "pi_remote"
    assert p["remote"] == ""


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


def test_mealie_defaults_off_everywhere():
    # FoodAssistant-6n4a: recipes, the meal plan, and the shopping list are
    # built into Pantry Raider, so no install mode provisions Mealie unless
    # explicitly asked to.
    p = plan({"FORCE_PI": "1"})
    assert p["mealie"] == "false"
    p = plan({})
    assert p["mode"] == "server"
    assert p["mealie"] == "false"


def test_explicit_mealie_opt_in_still_wins():
    # People who already use Mealie can still install it alongside.
    p = plan({"FORCE_PI": "1", "ENABLE_MEALIE": "true"})
    assert p["mealie"] == "true"


def test_mealie_explicit_false_respected():
    p = plan({"FORCE_PI": "1", "ENABLE_MEALIE": "false"})
    assert p["mealie"] == "false"


def test_three_pi_modes_offered():
    """The Pi mode prompt offers three clear modes (FoodAssistant-9mu5):
    Pi Host Kiosk, Pi Host Standalone, and Pi Remote. Source-guard, since the
    prompt itself is interactive (TTY) and PLAN_ONLY skips it."""
    text = INSTALL.read_text()
    assert "Pi Host Kiosk" in text
    assert "Pi Host Standalone" in text
    assert "Pi Remote" in text
    # The three prompt keys map to the right stack shape.
    assert "kiosk)" in text and "standalone)" in text and "remote)" in text
    # Standalone is headless: kiosk and Stream Deck are forced off.
    seg = text[text.index("standalone)"):text.index("remote)")]
    assert "ENABLE_KIOSK=false" in seg
    assert "ENABLE_STREAMDECK=false" in seg


def test_bare_install_lands_on_the_branded_hostname():
    """A plain install answers at pr.local (FoodAssistant-a8fn).

    The trap this pins: bash auto-populates $HOSTNAME with the machine's own
    name, so reading HOSTNAME here meant the brand default could never fire and
    every curl-pipe-bash install shipped as raspberrypi.local instead.
    """
    p = plan({"FORCE_PI": "1"})
    assert p["hostname"] == "pr"


def test_hostname_choice_ignores_the_shells_own_hostname():
    p = plan({"FORCE_PI": "1", "HOSTNAME": "raspberrypi"})
    assert p["hostname"] == "pr"


def test_explicit_hostname_choice_wins():
    p = plan({"FORCE_PI": "1", "PR_HOSTNAME": "kitchen"})
    assert p["hostname"] == "kitchen"
    # The provisioner's own variable name is accepted too, so a scripted
    # install that already sets it keeps working.
    p = plan({"FORCE_PI": "1", "FA_HOSTNAME": "pantry"})
    assert p["hostname"] == "pantry"


def _run_install(extra_env: dict) -> subprocess.CompletedProcess:
    env = {**os.environ, "NONINTERACTIVE": "1", "NO_COLOR": "1"}
    for k in ("PR_HOSTNAME", "FA_HOSTNAME", "ENABLE_KIOSK", "FORCE_PI",
              "FORCE_DISPLAY", "FORCE_ARCH", "FORCE_LONG_BIT"):
        env.pop(k, None)
    # The installer runs git fetch and reset --hard on REPO_DIR. An inherited
    # GIT_DIR (a git hook running the suite) would aim those at the real repo.
    for k in [k for k in env if k.startswith("GIT_")]:
        env.pop(k)
    env.update(extra_env)
    # stderr folded into stdout so the order of warnings and plan is kept.
    return subprocess.run(["bash", str(INSTALL)], env=env, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def test_server_kiosk_defaults_off_even_with_a_display():
    # Most mini PCs have a graphics chip, so a display "exists" on a headless
    # server; the kiosk would take over its console. A server only gets the
    # kiosk when asked for it.
    p = plan({"FORCE_DISPLAY": "1"})
    assert p["mode"] == "server"
    assert p["kiosk"] == "false"


def test_server_explicit_kiosk_still_wins():
    p = plan({"FORCE_DISPLAY": "1", "ENABLE_KIOSK": "true"})
    assert p["kiosk"] == "true"


def test_server_keeps_its_own_hostname():
    p = plan({})
    assert p["mode"] == "server"
    assert p["hostname"] == "keep"


def test_server_hostname_changes_only_when_asked():
    p = plan({"PR_HOSTNAME": "kitchen"})
    assert p["mode"] == "server"
    assert p["hostname"] == "kitchen"


def _fake_checkout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A checkout whose provisioner only records what it was handed, plus a
    sudo stand-in, so the real run_provisioner path runs without root."""
    repo = tmp_path / "src"
    fb = repo / "scripts" / "image-build" / "firstboot.sh"
    fb.parent.mkdir(parents=True)
    record = tmp_path / "firstboot-env.txt"
    fb.write_text(
        "#!/usr/bin/env bash\n"
        f"env | grep -E '^(FA_HOSTNAME|DEPLOYMENT_MODE|ENABLE_KIOSK)=' > '{record}'\n"
    )
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    sudo = stubs / "sudo"
    sudo.write_text('#!/bin/sh\nexec "$@"\n')
    sudo.chmod(0o755)
    return repo, stubs, record


def test_server_install_does_not_hand_the_provisioner_the_brand_hostname(tmp_path):
    repo, stubs, record = _fake_checkout(tmp_path)
    r = _run_install({"REPO_DIR": str(repo),
                      "PATH": f"{stubs}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stdout
    handed = record.read_text()
    assert "DEPLOYMENT_MODE=server" in handed
    assert "FA_HOSTNAME" not in handed


def test_explicit_hostname_reaches_the_provisioner(tmp_path):
    repo, stubs, record = _fake_checkout(tmp_path)
    r = _run_install({"REPO_DIR": str(repo), "PR_HOSTNAME": "kitchen",
                      "PATH": f"{stubs}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stdout
    assert "FA_HOSTNAME=kitchen" in record.read_text()


def test_32bit_pi_hosted_is_warned_before_anything_installs():
    r = _run_install({"PLAN_ONLY": "1", "FORCE_PI": "1", "FORCE_LONG_BIT": "32"})
    assert r.returncode == 0, r.stdout
    out = r.stdout
    assert "32-bit OS detected" in out
    assert "64-bit only" in out
    assert "Pi Remote (satellite) mode works on 32-bit" in out
    # The warning comes before the plan, and the install carries on.
    assert out.index("32-bit OS detected") < out.index("Install plan")
    assert "PLAN mode=pi_hosted" in r.stdout


def test_32bit_server_is_not_sent_to_raspberry_pi_os():
    r = _run_install({"PLAN_ONLY": "1", "FORCE_ARCH": "i686",
                      "FORCE_LONG_BIT": "32"})
    assert r.returncode == 0, r.stdout
    assert "32-bit OS detected (i686)" in r.stdout
    assert "64-bit version of Debian or Ubuntu" in r.stdout
    assert "Raspberry Pi OS" not in r.stdout
    assert "Pi Remote" not in r.stdout


def test_32bit_arch_alone_triggers_the_warning():
    r = _run_install({"PLAN_ONLY": "1", "FORCE_PI": "1", "FORCE_ARCH": "armv7l",
                      "FORCE_LONG_BIT": "64"})
    assert "32-bit OS detected (armv7l)" in r.stdout


def test_32bit_satellite_is_not_warned():
    r = _run_install({"PLAN_ONLY": "1", "FORCE_PI": "1", "FORCE_LONG_BIT": "32",
                      "DEPLOYMENT_MODE": "pi_remote"})
    assert r.returncode == 0, r.stdout
    assert "32-bit OS detected" not in r.stdout


def test_64bit_pi_is_not_warned():
    r = _run_install({"PLAN_ONLY": "1", "FORCE_PI": "1", "FORCE_ARCH": "aarch64",
                      "FORCE_LONG_BIT": "64"})
    assert "32-bit OS detected" not in r.stdout


def test_print_setup_recreates_the_stack_with_repo_dir():
    # The appliance compose file mounts the Grocy start-up hook from REPO_DIR;
    # a compose call without it falls back to a path installer devices lack.
    text = INSTALL.read_text()
    body = text[text.index("run_print_setup() {"):]
    body = body[:body.index("\n}\n")]
    assert 'env REPO_DIR="$REPO_DIR" docker compose up -d' in body
