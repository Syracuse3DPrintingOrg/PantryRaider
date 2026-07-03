"""foodassistant-update: helper refresh and force-push recovery.

Runs the real updater script against throwaway local git repos (no network,
no Docker, no root): BIN_DIR points at a temp dir, deploy targets point at
paths that do not exist (so the deploy branches skip), and stub systemctl /
systemd-run / docker binaries on PATH keep the script from touching the host.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "image-build" / "foodassistant-update"

pytestmark = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("git")),
    reason="bash and git are required to exercise the updater script",
)

GIT_ID = ["-c", "user.email=test@test", "-c", "user.name=test"]


def _git(cwd, *args):
    return subprocess.run(["git", *GIT_ID, *args], cwd=cwd, check=True,
                          capture_output=True, text=True)


def _short_head(repo):
    return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo,
                          check=True, capture_output=True, text=True).stdout.strip()


HELPERS = [
    "foodassistant-update", "foodassistant-restore", "foodassistant-host-bridge",
    "foodassistant-display-power", "foodassistant-set-rotation",
    "foodassistant-apply-rotation", "foodassistant-accel-rotation",
    "foodassistant-ap-watchdog",
]
NON_HELPERS = [
    "foodassistant-host-bridge.service", "foodassistant-firstboot.service",
    "foodassistant-firstrun.sh",
]


@pytest.fixture()
def rig(tmp_path):
    """origin (bare) + device clone with helper sources committed, plus a stub
    PATH and env that keeps the script inert outside the temp tree."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   check=True, capture_output=True)

    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(origin), str(work)],
                   check=True, capture_output=True)
    ib = work / "scripts" / "image-build"
    ib.mkdir(parents=True)
    for name in HELPERS + NON_HELPERS:
        (ib / name).write_text(f"#!/usr/bin/env bash\n# {name} v1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "v1")
    _git(work, "push", "origin", "main")

    device = tmp_path / "device"
    subprocess.run(["git", "clone", str(origin), str(device)],
                   check=True, capture_output=True)

    stub_bin = tmp_path / "stubs"
    stub_bin.mkdir()
    for stub in ("systemctl", "systemd-run", "docker"):
        s = stub_bin / stub
        s.write_text("#!/usr/bin/env bash\nexit 1\n")
        s.chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    env = {
        **os.environ,
        "PATH": f"{stub_bin}:{os.environ['PATH']}",
        "REPO_DIR": str(device),
        "BIN_DIR": str(bin_dir),
        "APP_DIR": str(tmp_path / "no-app"),
        "VENV_DIR": str(tmp_path / "no-venv"),
        "SD_DST": str(tmp_path / "no-deck" / "pkg"),
        "INSTALL_DIR": str(tmp_path / "no-install"),
    }
    return {"origin": origin, "work": work, "device": device,
            "bin": bin_dir, "env": env}


def run_update(rig):
    r = subprocess.run(["bash", str(SCRIPT)], env=rig["env"],
                       capture_output=True, text=True)
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    assert lines, r.stderr
    return json.loads(lines[-1]), r.stdout


def test_all_bin_helpers_are_installed_and_unit_files_are_not(rig):
    result, out = run_update(rig)
    for name in HELPERS:
        installed = rig["bin"] / name
        assert installed.is_file(), f"{name} was not installed"
        assert os.access(installed, os.X_OK)
    for name in NON_HELPERS:
        assert not (rig["bin"] / name).exists(), f"{name} should not be installed"


def test_newly_added_helper_reaches_an_already_imaged_device(rig):
    # First update installs the v1 set (the "imaged" state).
    run_update(rig)
    # A helper that did not exist when the device was imaged appears upstream.
    new_helper = rig["work"] / "scripts" / "image-build" / "foodassistant-new-tool"
    new_helper.write_text("#!/usr/bin/env bash\n# new tool v1\n")
    _git(rig["work"], "add", "-A")
    _git(rig["work"], "commit", "-m", "add new tool")
    _git(rig["work"], "push", "origin", "main")

    result, out = run_update(rig)
    assert (rig["bin"] / "foodassistant-new-tool").is_file()
    assert result["remote_recovered"] is False


def test_fast_forward_pull_updates_and_reports_no_recovery(rig):
    (rig["work"] / "scripts" / "image-build" / "foodassistant-set-rotation").write_text(
        "#!/usr/bin/env bash\n# set-rotation v2\n")
    _git(rig["work"], "add", "-A")
    _git(rig["work"], "commit", "-m", "v2")
    _git(rig["work"], "push", "origin", "main")
    new_head = _short_head(rig["work"])

    result, out = run_update(rig)
    assert result["after"] == new_head
    assert result["remote_recovered"] is False
    assert "# set-rotation v2" in (rig["bin"] / "foodassistant-set-rotation").read_text()


def test_force_pushed_remote_is_recovered_by_hard_reset(rig):
    # The device is on v1. Upstream history is rewritten (amend + force push),
    # so a plain --ff-only pull can never succeed again.
    (rig["work"] / "scripts" / "image-build" / "foodassistant-set-rotation").write_text(
        "#!/usr/bin/env bash\n# set-rotation rewritten\n")
    _git(rig["work"], "add", "-A")
    _git(rig["work"], "commit", "--amend", "-m", "v1 rewritten")
    _git(rig["work"], "push", "--force", "origin", "main")
    rewritten_head = _short_head(rig["work"])

    result, out = run_update(rig)
    assert result["remote_recovered"] is True
    assert result["after"] == rewritten_head
    assert "Recovered: reset the checkout to origin/main" in out
    assert "# set-rotation rewritten" in (rig["bin"] / "foodassistant-set-rotation").read_text()


def test_diverged_local_checkout_is_recovered(rig):
    # A stray local commit on the device plus a new upstream commit diverges
    # the histories, which breaks --ff-only forever; the local commit should be
    # discarded in favour of origin, because the checkout is purely an update
    # source. (A local commit alone does NOT fail --ff-only: the pull reports
    # "already up to date" until upstream moves.)
    marker = rig["device"] / "scripts" / "image-build" / "foodassistant-set-rotation"
    marker.write_text("#!/usr/bin/env bash\n# local hack\n")
    _git(rig["device"], "add", "-A")
    _git(rig["device"], "commit", "-m", "local hack")
    (rig["work"] / "scripts" / "image-build" / "foodassistant-display-power").write_text(
        "#!/usr/bin/env bash\n# display-power v2\n")
    _git(rig["work"], "add", "-A")
    _git(rig["work"], "commit", "-m", "upstream v2")
    _git(rig["work"], "push", "origin", "main")
    origin_head = _short_head(rig["work"])

    result, out = run_update(rig)
    assert result["remote_recovered"] is True
    assert result["after"] == origin_head
    assert "# local hack" not in marker.read_text()


def test_unreachable_remote_keeps_the_current_checkout(rig):
    before = _short_head(rig["device"])
    subprocess.run(["git", "remote", "set-url", "origin",
                    str(rig["origin"].parent / "gone.git")],
                   cwd=rig["device"], check=True, capture_output=True)

    result, out = run_update(rig)
    assert result["remote_recovered"] is False
    assert result["after"] == before
    assert "continuing with the current checkout" in out


def test_stale_kiosk_unit_gains_the_rotation_execstartpost(rig, tmp_path):
    # A device imaged before the kiosk unit had the rotation ExecStartPost
    # never re-applied rotation on boot; the updater patches the line in
    # (FoodAssistant-prqg). The unit is device-generated, so it is patched in
    # place rather than reinstalled.
    unit = tmp_path / "foodassistant-kiosk.service"
    unit.write_text(
        "[Unit]\nDescription=kiosk\n\n[Service]\nExecStart=/usr/bin/cage\n"
        "Restart=always\nRestartSec=5\n\n[Install]\nWantedBy=multi-user.target\n")
    rig["env"]["KIOSK_UNIT"] = str(unit)
    run_update(rig)
    lines = unit.read_text().splitlines()
    idx = lines.index("ExecStartPost=-/usr/local/bin/foodassistant-apply-rotation")
    assert lines[idx + 1] == "Restart=always"


def test_current_kiosk_unit_is_left_alone(rig, tmp_path):
    unit = tmp_path / "foodassistant-kiosk.service"
    content = ("[Service]\nExecStart=/usr/bin/cage\n"
               "ExecStartPost=-/usr/local/bin/foodassistant-apply-rotation\n"
               "Restart=always\n")
    unit.write_text(content)
    rig["env"]["KIOSK_UNIT"] = str(unit)
    run_update(rig)
    assert unit.read_text() == content


def test_missing_kiosk_unit_is_not_created(rig, tmp_path):
    unit = tmp_path / "no-kiosk.service"
    rig["env"]["KIOSK_UNIT"] = str(unit)
    run_update(rig)
    assert not unit.exists()


def _kiosk_unit_text():
    return (
        "[Unit]\nDescription=kiosk\n\n[Service]\n"
        "ExecStart=/usr/bin/cage -- /usr/bin/chromium --kiosk \\\n"
        "  --disable-restore-session-state http://localhost/ui/?kiosk=1\n"
        "ExecStartPost=-/usr/local/bin/foodassistant-apply-rotation\n"
        "Restart=always\nRestartSec=5\n\n[Install]\nWantedBy=multi-user.target\n")


def test_kiosk_boot_dropin_is_installed_for_deployed_units(rig, tmp_path):
    # Deployed units predate the boot hardening (seatd ordering, no
    # start-limit give-up, app wait); the updater ships it as a drop-in
    # (FoodAssistant-9ext / FoodAssistant-kyl2).
    unit = tmp_path / "foodassistant-kiosk.service"
    unit.write_text(_kiosk_unit_text())
    dropin_dir = tmp_path / "kiosk.service.d"
    rig["env"]["KIOSK_UNIT"] = str(unit)
    rig["env"]["KIOSK_DROPIN_DIR"] = str(dropin_dir)
    run_update(rig)
    conf = (dropin_dir / "10-foodassistant-boot.conf").read_text()
    assert "After=seatd.service" in conf
    assert "StartLimitIntervalSec=0" in conf
    assert "TimeoutStartSec=240" in conf
    # The app wait probes the unit's own kiosk URL, with $$ so systemd passes
    # a literal $ through to the shell.
    assert '"http://localhost/ui/?kiosk=1"' in conf
    assert "$$(seq 1 40)" in conf


def test_kiosk_boot_dropin_without_url_omits_the_app_wait(rig, tmp_path):
    unit = tmp_path / "foodassistant-kiosk.service"
    unit.write_text("[Service]\nExecStart=/usr/bin/cage\nRestart=always\n")
    dropin_dir = tmp_path / "kiosk.service.d"
    rig["env"]["KIOSK_UNIT"] = str(unit)
    rig["env"]["KIOSK_DROPIN_DIR"] = str(dropin_dir)
    run_update(rig)
    conf = (dropin_dir / "10-foodassistant-boot.conf").read_text()
    assert "After=seatd.service" in conf
    assert "ExecStartPre" not in conf


def test_kiosk_boot_dropin_not_rewritten_or_duplicated(rig, tmp_path):
    unit = tmp_path / "foodassistant-kiosk.service"
    unit.write_text(_kiosk_unit_text())
    dropin_dir = tmp_path / "kiosk.service.d"
    dropin_dir.mkdir()
    marker = "# operator-tuned\n"
    (dropin_dir / "10-foodassistant-boot.conf").write_text(marker)
    rig["env"]["KIOSK_UNIT"] = str(unit)
    rig["env"]["KIOSK_DROPIN_DIR"] = str(dropin_dir)
    run_update(rig)
    # An existing drop-in is the operator's (or a previous run's); keep it.
    assert (dropin_dir / "10-foodassistant-boot.conf").read_text() == marker


def test_kiosk_boot_dropin_skipped_when_unit_has_the_app_wait(rig, tmp_path):
    # A freshly provisioned unit already carries the app wait inline; the
    # drop-in would run it twice, so it is skipped.
    unit = tmp_path / "foodassistant-kiosk.service"
    unit.write_text(
        "[Service]\n"
        "ExecStartPre=-/bin/sh -c 'command -v curl >/dev/null 2>&1 || exit 0'\n"
        "ExecStart=/usr/bin/cage -- /usr/bin/chromium http://localhost/ui/\n"
        "ExecStartPost=-/usr/local/bin/foodassistant-apply-rotation\n"
        "Restart=always\n")
    dropin_dir = tmp_path / "kiosk.service.d"
    rig["env"]["KIOSK_UNIT"] = str(unit)
    rig["env"]["KIOSK_DROPIN_DIR"] = str(dropin_dir)
    run_update(rig)
    assert not dropin_dir.exists()


def test_cursor_dropin_and_theme_installed(rig, tmp_path):
    # Devices imaged before the hidden-cursor provisioning (or whose scanner
    # masqueraded as a mouse at provision time) gain the transparent theme and
    # an Environment drop-in on update.
    unit = tmp_path / "foodassistant-kiosk.service"
    unit.write_text("[Service]\nExecStart=/usr/bin/cage\nRestart=always\n")
    theme = tmp_path / "icons" / "foodassistant-hidden"
    rig["env"]["KIOSK_UNIT"] = str(unit)
    rig["env"]["KIOSK_DROPIN_DIR"] = str(tmp_path / "kiosk.d")
    rig["env"]["CURSOR_THEME_DIR"] = str(theme)
    run_update(rig)
    cur = theme / "cursors" / "left_ptr"
    assert cur.is_file() and cur.read_bytes().startswith(b"Xcur")
    assert (theme / "cursors" / "default").exists()
    dropin = tmp_path / "kiosk.d" / "20-foodassistant-cursor.conf"
    body = dropin.read_text()
    assert "XCURSOR_THEME=foodassistant-hidden" in body
    assert f"XCURSOR_PATH={theme.parent}" in body


def test_cursor_dropin_respects_hide_cursor_false(rig, tmp_path):
    unit = tmp_path / "foodassistant-kiosk.service"
    unit.write_text("[Service]\nExecStart=/usr/bin/cage\nRestart=always\n")
    # The updater reads HIDE_CURSOR from fixed config.env paths that do not
    # exist in the test sandbox, so emulate the opt-out by pre-marking the
    # unit as already themed (grep guard) and assert nothing is written when
    # XCURSOR_THEME is already present.
    unit.write_text("[Service]\nEnvironment=XCURSOR_THEME=x\nExecStart=/usr/bin/cage\n")
    rig["env"]["KIOSK_UNIT"] = str(unit)
    rig["env"]["KIOSK_DROPIN_DIR"] = str(tmp_path / "kiosk.d")
    rig["env"]["CURSOR_THEME_DIR"] = str(tmp_path / "icons" / "foodassistant-hidden")
    run_update(rig)
    assert not (tmp_path / "kiosk.d" / "20-foodassistant-cursor.conf").exists()


PI_CMDLINE = ("console=serial0,115200 console=tty1 root=PARTUUID=deadbeef-02 "
              "rootfstype=ext4 fsck.repair=yes rootwait")
QUIET_PARAMS = ["quiet", "loglevel=3", "vt.global_cursor_default=0",
                "logo.nologo", "consoleblank=0", "rd.systemd.show_status=false",
                "systemd.show_status=false"]


def _quiet_rig(rig, tmp_path, cmdline_text=PI_CMDLINE + "\n"):
    unit = tmp_path / "foodassistant-kiosk.service"
    unit.write_text(_kiosk_unit_text())
    cmdline = tmp_path / "cmdline.txt"
    if cmdline_text is not None:
        cmdline.write_text(cmdline_text)
    rig["env"]["KIOSK_UNIT"] = str(unit)
    rig["env"]["KIOSK_DROPIN_DIR"] = str(tmp_path / "kiosk.d")
    rig["env"]["CURSOR_THEME_DIR"] = str(tmp_path / "icons" / "hidden")
    rig["env"]["CMDLINE_CANDIDATES"] = str(cmdline)
    return cmdline


def test_quiet_boot_params_added_once_to_kiosk_cmdline(rig, tmp_path):
    # A deployed kiosk device gets the quiet-boot kernel params appended to its
    # single-line cmdline, keeping console=tty1 intact (FoodAssistant-go5e).
    cmdline = _quiet_rig(rig, tmp_path)
    result, out = run_update(rig)
    text = cmdline.read_text()
    assert text.endswith("\n") and text.count("\n") == 1  # still one line
    params = text.strip().split()
    assert params[:2] == ["console=serial0,115200", "console=tty1"]
    for p in QUIET_PARAMS:
        assert params.count(p) == 1, p
    assert "takes effect after the next reboot" in out


def test_quiet_boot_rerun_is_idempotent(rig, tmp_path):
    cmdline = _quiet_rig(rig, tmp_path)
    run_update(rig)
    first = cmdline.read_text()
    _, out = run_update(rig)
    assert cmdline.read_text() == first
    assert "Quieted the boot console" not in out


def test_quiet_boot_missing_cmdline_is_a_noop(rig, tmp_path):
    cmdline = _quiet_rig(rig, tmp_path, cmdline_text=None)
    result, out = run_update(rig)
    assert not cmdline.exists()
    assert "Quieted the boot console" not in out
    assert "WARN: could not update" not in out


def test_quiet_boot_keeps_an_operator_set_loglevel(rig, tmp_path):
    # A key the operator already pinned (loglevel=7 for debugging) is never
    # overridden or duplicated; the other params are still added.
    cmdline = _quiet_rig(rig, tmp_path, cmdline_text=PI_CMDLINE + " loglevel=7\n")
    run_update(rig)
    params = cmdline.read_text().strip().split()
    assert "loglevel=7" in params and "loglevel=3" not in params
    assert "quiet" in params and "systemd.show_status=false" in params


def test_quiet_boot_skipped_without_a_kiosk_unit(rig, tmp_path):
    # Headless boxes keep a verbose console: no kiosk unit, no cmdline edit.
    cmdline = tmp_path / "cmdline.txt"
    cmdline.write_text(PI_CMDLINE + "\n")
    rig["env"]["CMDLINE_CANDIDATES"] = str(cmdline)
    run_update(rig)
    assert cmdline.read_text() == PI_CMDLINE + "\n"


def test_self_update_reexecs_the_new_version(rig):
    # The updater replaces itself, then must re-exec the NEW version so steps
    # added in it apply on the same press (previously one press behind). The
    # device starts with an OLD updater installed; upstream ships a new one
    # that writes a marker when run.
    old = rig["bin"] / "foodassistant-update"
    old.write_text("#!/usr/bin/env bash\n# stale installed updater\n")
    new_src = rig["work"] / "scripts" / "image-build" / "foodassistant-update"
    new_src.write_text(
        "#!/usr/bin/env bash\n"
        "echo \"REEXEC_MARKER guard=${FA_UPDATE_REEXEC:-unset}\"\n"
        "echo '{\"ok\": true}'\n")
    _git(rig["work"], "add", "-A")
    _git(rig["work"], "commit", "-m", "new updater")
    _git(rig["work"], "push", "origin", "main")

    # Run the REAL updater script (as the bridge would); it syncs helpers,
    # sees itself changed, and must exec the new installed copy.
    result, out = run_update(rig)
    assert "REEXEC_MARKER guard=1" in out
    assert result == {"ok": True}


def test_reexec_guard_prevents_loops(rig, tmp_path):
    # With the guard env set (already re-exec'd once), a self-refresh must NOT
    # exec again; the run carries on and emits the normal result JSON.
    rig["env"]["FA_UPDATE_REEXEC"] = "1"
    result, out = run_update(rig)
    assert "remote_recovered" in result  # the real script's JSON, not a re-exec


def test_ap_watchdog_sbin_copy_refreshes_when_unit_exists(rig, tmp_path):
    # The AP fallback watchdog unit (written at firstboot) executes the copy
    # in /usr/local/sbin, which the generic bin sync never touches; the
    # dedicated block must refresh it from the repo file (FoodAssistant-fuat).
    unit = tmp_path / "foodassistant-ap-watchdog.service"
    unit.write_text("[Service]\nExecStart=/usr/local/sbin/foodassistant-ap-watchdog\n")
    sbin = tmp_path / "sbin"
    sbin.mkdir()
    stale = sbin / "foodassistant-ap-watchdog"
    stale.write_text("#!/usr/bin/env bash\n# stale imaged watchdog\n")
    rig["env"]["AP_WATCHDOG_UNIT"] = str(unit)
    rig["env"]["SBIN_DIR"] = str(sbin)
    run_update(rig)
    body = stale.read_text()
    assert "stale imaged watchdog" not in body
    assert "# foodassistant-ap-watchdog v1" in body
    assert os.access(stale, os.X_OK)


def test_ap_watchdog_sbin_copy_skipped_without_unit(rig, tmp_path):
    # No watchdog unit means the device never got the AP fallback (or is not a
    # Pi); the sbin copy must not appear. The bin copy still syncs like any
    # other helper.
    sbin = tmp_path / "sbin"
    sbin.mkdir()
    rig["env"]["AP_WATCHDOG_UNIT"] = str(tmp_path / "no-such-unit.service")
    rig["env"]["SBIN_DIR"] = str(sbin)
    run_update(rig)
    assert not (sbin / "foodassistant-ap-watchdog").exists()
    assert (rig["bin"] / "foodassistant-ap-watchdog").is_file()


def test_ap_watchdog_sbin_copy_untouched_when_current(rig, tmp_path):
    unit = tmp_path / "foodassistant-ap-watchdog.service"
    unit.write_text("[Service]\nExecStart=/usr/local/sbin/foodassistant-ap-watchdog\n")
    sbin = tmp_path / "sbin"
    sbin.mkdir()
    rig["env"]["AP_WATCHDOG_UNIT"] = str(unit)
    rig["env"]["SBIN_DIR"] = str(sbin)
    run_update(rig)
    installed = sbin / "foodassistant-ap-watchdog"
    before = installed.stat().st_mtime_ns
    _, out = run_update(rig)
    assert installed.stat().st_mtime_ns == before


def test_cec_pointer_ignore_rule_installed(rig, tmp_path):
    # The vc4 HDMI CEC devices masquerade as pointers and make the compositor
    # draw a cursor on mouse-less kiosks; the updater ships the libinput
    # ignore rule to deployed devices.
    rules = tmp_path / "rules.d" / "71-foodassistant-cec-pointer.rules"
    rig["env"]["CEC_RULES_FILE"] = str(rules)
    run_update(rig)
    body = rules.read_text()
    assert 'ATTRS{name}=="vc4-hdmi*"' in body
    assert 'ENV{LIBINPUT_IGNORE_DEVICE}="1"' in body
    # Idempotent: a second run leaves the file untouched.
    before = rules.stat().st_mtime
    run_update(rig)
    assert rules.stat().st_mtime == before
