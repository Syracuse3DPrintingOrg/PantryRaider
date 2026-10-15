"""Tests for the on-device installer (loader) decision logic.

These run install.sh in PLAN_ONLY mode, which resolves the deployment mode and
add-on flags from hardware detection + env overrides and prints a single stable
"PLAN ..." line without cloning the repo, using sudo, or provisioning. Pure
bash, no network/Docker.

Run: python -m pytest tests/test_installer.py -q
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INSTALL = REPO / "install.sh"
UPDATER = REPO / "scripts" / "image-build" / "foodassistant-update"
RELEASE_SIGNERS = REPO / "scripts" / "release-signers"
OBJECTS = Path(__file__).resolve().parent / "data" / "release_objects"
# A done-marker path that never exists, so a machine that happens to run
# Pantry Raider never turns these runs into the helper-only install.
NO_MARKER = "/nonexistent/pantry-raider-test/firstboot.done"
# Inherited settings that would change what the installer decides.
_INHERITED = ("UPDATE_CHANNEL", "FORCE", "DONE_MARKER", "TRUST_FILE",
              "FA_BOOTSTRAP_SIGNERS", "REPO_URL", "REPO_DIR", "CHANNEL_FILE")
# A saved-channel path that never exists, for the same reason.
NO_CHANNEL_FILE = "/nonexistent/pantry-raider-test/update-channel"


def plan(extra_env: dict | None = None) -> dict:
    """Run install.sh in PLAN_ONLY mode; return the parsed PLAN fields."""
    env = {
        **os.environ,
        "NONINTERACTIVE": "1",
        "PLAN_ONLY": "1",
        "NO_COLOR": "1",
    }
    for k in _INHERITED:
        env.pop(k, None)
    env["DONE_MARKER"] = NO_MARKER
    env["CHANNEL_FILE"] = NO_CHANNEL_FILE
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
              "FORCE_DISPLAY", "FORCE_ARCH", "FORCE_LONG_BIT", *_INHERITED):
        env.pop(k, None)
    # The installer runs git fetch and checkout on REPO_DIR. An inherited
    # GIT_DIR (a git hook running the suite) would aim those at the real repo.
    for k in [k for k in env if k.startswith("GIT_")]:
        env.pop(k)
    env["DONE_MARKER"] = NO_MARKER
    env["CHANNEL_FILE"] = NO_CHANNEL_FILE
    env.update(extra_env)
    # stderr folded into stdout so the order of warnings and plan is kept. A
    # new session has no terminal, so a prompt fails instead of waiting.
    return subprocess.run(["bash", str(INSTALL)], env=env, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          stdin=subprocess.DEVNULL, start_new_session=True)


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


needs_git_tools = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("bash", "git", "python3", "ssh-keygen")),
    reason="bash, git, python3 and ssh-keygen are needed for the signed origin",
)

_GIT_ID = ["-c", "user.email=test@test", "-c", "user.name=test",
           "-c", "core.hooksPath=/dev/null", "-c", "init.defaultBranch=main"]


def _git(cwd, *args):
    return subprocess.run(["git", *_GIT_ID, *args], cwd=cwd, check=True,
                          capture_output=True, text=True)


@pytest.fixture(scope="module")
def release_key(tmp_path_factory):
    if not shutil.which("ssh-keygen"):
        pytest.skip("ssh-keygen is needed for the signed origin")
    d = tmp_path_factory.mktemp("installer-release-key")
    key = d / "key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test",
                    "-f", str(key)], check=True, capture_output=True)
    kind, blob = (d / "key.pub").read_text().split()[:2]
    signers = d / "signers"
    signers.write_text(
        f'pantryraider-release namespaces="git,pantryraider-release" {kind} {blob}\n')
    return {"key": key, "signers": signers}


def _signed_origin(tmp_path: Path, release_key, signed: bool = True):
    """A local origin whose release v0.9.1 holds a provisioner that only
    records what it was handed, plus a sudo stand-in, so the real fetch and
    run_provisioner paths run without root or network. With SIGNED the commit
    and the annotated tag are signed by a throwaway release key the installer
    trusts through FA_BOOTSTRAP_SIGNERS."""
    record = tmp_path / "firstboot-env.txt"
    origin = tmp_path / "origin.git"
    subprocess.run(["git", *_GIT_ID, "init", "-q", "--bare", str(origin)],
                   check=True, capture_output=True)
    work = tmp_path / "work"
    subprocess.run(["git", *_GIT_ID, "init", "-q", str(work)], check=True,
                   capture_output=True)
    sign = "true" if signed else "false"
    if signed:
        _git(work, "config", "gpg.format", "ssh")
        _git(work, "config", "user.signingkey", str(release_key["key"]))
    _git(work, "config", "commit.gpgsign", sign)
    _git(work, "config", "tag.gpgsign", sign)
    cfg = work / "service" / "app" / "config.py"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('APP_VERSION = "0.9.1"\n')
    fb = work / "scripts" / "image-build" / "firstboot.sh"
    fb.parent.mkdir(parents=True)
    fb.write_text(
        "#!/usr/bin/env bash\n"
        "echo CALL >> \"$RECORD\"\n"
        "env | grep -E '^(FA_HOSTNAME|DEPLOYMENT_MODE|ENABLE_KIOSK|STEPS|UPDATE_CHANNEL|REPO_DIR)=' >> \"$RECORD\"\n"
    )
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "release")
    _git(work, "tag", "-a", "-m", "v0.9.1", "v0.9.1")
    _git(work, "push", "-q", str(origin), "main", "--tags")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    sudo = stubs / "sudo"
    sudo.write_text('#!/bin/sh\nexec "$@"\n')
    sudo.chmod(0o755)
    env = {
        "REPO_DIR": str(tmp_path / "src"),
        "REPO_URL": str(origin),
        "TRUST_FILE": str(tmp_path / "trust" / "release-signers"),
        "FA_BOOTSTRAP_SIGNERS": str(release_key["signers"]),
        "RECORD": str(record),
        "PATH": f"{stubs}:{os.environ['PATH']}",
    }
    return env, record, origin


def _calls(record: Path) -> list[dict]:
    """One dict of handed-over variables per provisioner run."""
    runs = []
    for line in record.read_text().splitlines():
        if line == "CALL":
            runs.append({})
        else:
            k, _, v = line.partition("=")
            runs[-1][k] = v
    return runs


@needs_git_tools
def test_server_install_does_not_hand_the_provisioner_the_brand_hostname(tmp_path, release_key):
    env, record, _ = _signed_origin(tmp_path, release_key)
    r = _run_install(env)
    assert r.returncode == 0, r.stdout
    [handed] = _calls(record)
    assert handed["DEPLOYMENT_MODE"] == "server"
    assert "FA_HOSTNAME" not in handed


@needs_git_tools
def test_explicit_hostname_reaches_the_provisioner(tmp_path, release_key):
    env, record, _ = _signed_origin(tmp_path, release_key)
    r = _run_install({**env, "PR_HOSTNAME": "kitchen"})
    assert r.returncode == 0, r.stdout
    [handed] = _calls(record)
    assert handed["FA_HOSTNAME"] == "kitchen"


@needs_git_tools
def test_install_checks_out_the_signed_release_and_installs_the_key(tmp_path, release_key):
    env, record, origin = _signed_origin(tmp_path, release_key)
    r = _run_install(env)
    assert r.returncode == 0, r.stdout
    head = subprocess.run(["git", "-C", env["REPO_DIR"], "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    tagged = subprocess.run(["git", "-C", str(origin), "rev-parse", "v0.9.1^{commit}"],
                            capture_output=True, text=True).stdout.strip()
    assert head == tagged
    assert Path(env["TRUST_FILE"]).read_text() == release_key["signers"].read_text()
    [handed] = _calls(record)
    # No channel was asked for, so none is handed over as a choice.
    assert handed["UPDATE_CHANNEL"] == ""
    assert handed["REPO_DIR"] == env["REPO_DIR"]


@needs_git_tools
def test_an_unsigned_origin_stops_before_the_provisioner_runs(tmp_path, release_key):
    env, record, _ = _signed_origin(tmp_path, release_key, signed=False)
    r = _run_install(env)
    assert r.returncode != 0
    assert ("The Pantry Raider download could not be checked against the release "
            "key, so nothing was installed.") in r.stdout
    assert not record.exists()
    assert not Path(env["TRUST_FILE"]).exists()


@needs_git_tools
def test_an_unsigned_main_tip_stops_the_main_channel_too(tmp_path, release_key):
    env, record, _ = _signed_origin(tmp_path, release_key, signed=False)
    r = _run_install({**env, "UPDATE_CHANNEL": "main"})
    assert r.returncode != 0
    assert "could not be checked against the release key" in r.stdout
    assert not record.exists()


@needs_git_tools
def test_an_already_set_up_device_gets_only_the_helpers(tmp_path, release_key):
    env, record, _ = _signed_origin(tmp_path, release_key)
    marker = tmp_path / "firstboot.done"
    marker.write_text("2026-01-01T00:00:00Z\n")
    # Not NONINTERACTIVE and no terminal: any setup question would fail.
    r = _run_install({**env, "DONE_MARKER": str(marker), "NONINTERACTIVE": "0"})
    assert r.returncode == 0, r.stdout
    assert ("This device already runs Pantry Raider. The installer adds the "
            "update helper and leaves everything else as it is.") in r.stdout
    assert ("Done. Update from Settings, Backups & Updates (Update now), or run "
            "sudo foodassistant-update.") in r.stdout
    assert "Install plan" not in r.stdout
    [handed] = _calls(record)
    assert handed["STEPS"] == "hostbridge"
    assert "DEPLOYMENT_MODE" not in handed
    assert Path(env["TRUST_FILE"]).exists()


def test_helper_only_plan_with_a_done_marker(tmp_path):
    marker = tmp_path / "firstboot.done"
    marker.write_text("x\n")
    r = _run_install({"PLAN_ONLY": "1", "DONE_MARKER": str(marker),
                      "REPO_DIR": "/opt/foodassistant-src"})
    assert r.returncode == 0, r.stdout
    assert "PLAN helper-only repo_dir=/opt/foodassistant-src channel=stable" in r.stdout
    assert "PLAN mode=" not in r.stdout
    # FORCE=1 runs the full install even on a set-up device.
    r = _run_install({"PLAN_ONLY": "1", "DONE_MARKER": str(marker), "FORCE": "1"})
    assert "PLAN mode=" in r.stdout


def test_full_plan_without_a_done_marker(tmp_path):
    r = _run_install({"PLAN_ONLY": "1", "DONE_MARKER": str(tmp_path / "absent")})
    assert r.returncode == 0, r.stdout
    assert "PLAN mode=server" in r.stdout
    assert "helper-only" not in r.stdout


def test_plan_carries_the_channel():
    assert plan()["channel"] == "stable"
    assert plan({"UPDATE_CHANNEL": "main"})["channel"] == "main"
    assert plan({"UPDATE_CHANNEL": "beta"})["channel"] == "stable"


def test_a_device_keeps_the_channel_it_already_follows(tmp_path):
    saved = tmp_path / "update-channel"
    saved.write_text("main\n")
    assert plan({"CHANNEL_FILE": str(saved)})["channel"] == "main"
    # An explicit choice wins over the saved one.
    assert plan({"CHANNEL_FILE": str(saved), "UPDATE_CHANNEL": "stable"})["channel"] == "stable"
    marker = tmp_path / "firstboot.done"
    marker.write_text("x\n")
    r = _run_install({"PLAN_ONLY": "1", "DONE_MARKER": str(marker),
                      "CHANNEL_FILE": str(saved), "REPO_DIR": "/opt/foodassistant-src"})
    assert "PLAN helper-only repo_dir=/opt/foodassistant-src channel=main" in r.stdout


def test_tools_are_installed_in_one_apt_line_with_openssh_client():
    text = INSTALL.read_text()
    lines = [l for l in text.splitlines() if "apt-get install -y" in l]
    assert any("openssh-client" in l and "git" in l and "python3" in l for l in lines)
    body = text[text.index("fetch_repo() {"):]
    body = body[:body.index("\n}\n")]
    assert body.index("ensure_tools") < body.index("git -C")
    assert "reset --hard" not in text


def test_the_provisioner_is_handed_the_channel():
    text = INSTALL.read_text()
    body = text[text.index("run_provisioner() {"):]
    body = body[:body.index("\n}\n")]
    assert 'UPDATE_CHANNEL="$UPDATE_CHANNEL"' in body


def _heredoc(path: Path, opener: str, closer: str) -> str:
    lines = path.read_text().splitlines(keepends=True)
    start = next(i for i, l in enumerate(lines) if l.rstrip().endswith(opener))
    end = next(i for i in range(start + 1, len(lines)) if lines[i].rstrip("\n") == closer)
    return "".join(lines[start + 1:end])


def _key_lines(text: str) -> list[str]:
    return [l.strip() for l in text.splitlines()
            if l.strip() and not l.strip().startswith("#")]


def test_embedded_signers_and_verifier_match_the_updater():
    signers = _heredoc(INSTALL, "<<'SIGNERS' || true", "SIGNERS")
    assert _key_lines(signers) == _key_lines(RELEASE_SIGNERS.read_text())
    assert _key_lines(signers) == _key_lines(
        _heredoc(UPDATER, "<<'SIGNERS' || true", "SIGNERS"))
    assert _heredoc(INSTALL, "<<'PYVERIFY' || true", "PYVERIFY") == \
        _heredoc(UPDATER, "<<'PYVERIFY' || true", "PYVERIFY")


def _load_release_objects(repo: Path, ref_name: str) -> None:
    index = json.loads((OBJECTS / "v0.20.0.objects.json").read_text())
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    for row in index["objects"]:
        out = subprocess.run(
            ["git", "-C", str(repo), "hash-object", "-w", "--literally", "-t",
             row["type"], str(OBJECTS / row["file"])],
            check=True, capture_output=True, text=True).stdout.strip()
        assert out == row["id"], row["file"]
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/tags/" + ref_name,
                    index["objects"][0]["id"]], check=True, capture_output=True)


def _pyverify(*args) -> dict:
    code = _heredoc(INSTALL, "<<'PYVERIFY' || true", "PYVERIFY")
    r = subprocess.run(["python3", "-c", code, *map(str, args)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


@needs_git_tools
def test_installers_verifier_accepts_the_published_v0_20_0(tmp_path):
    repo = tmp_path / "objects"
    _load_release_objects(repo, "v0.20.0")
    out = _pyverify("verify-tag", repo, "v0.20.0", RELEASE_SIGNERS)
    assert out["ok"] is True, out
    assert out["version"] == "0.20.0"


@needs_git_tools
def test_installers_verifier_refuses_the_tag_under_another_name(tmp_path):
    repo = tmp_path / "moved"
    _load_release_objects(repo, "v0.20.1")
    out = _pyverify("verify-tag", repo, "v0.20.1", RELEASE_SIGNERS)
    assert out["ok"] is False
    assert out["reason"] == "name_mismatch"


def test_new_installer_copy_has_no_em_dash():
    assert "\u2014" not in INSTALL.read_text()


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
