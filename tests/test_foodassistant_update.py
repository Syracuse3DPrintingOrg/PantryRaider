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
    "foodassistant-ap-watchdog", "foodassistant-boot-splash",
]
NON_HELPERS = [
    "foodassistant-host-bridge.service", "foodassistant-firstboot.service",
    "foodassistant-firstrun.sh", "foodassistant-boot-splash.service",
    "foodassistant-ap-watchdog.service",
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
        # Keep the channel plumbing hermetic: never read or write the real
        # /etc/foodassistant files, even when the test host is a deployed Pi.
        "CHANNEL_FILE": str(tmp_path / "update-channel"),
        "PIN_STATE_FILE": str(tmp_path / "update-tag-restore"),
        "PINNED_VERSION_FILE": str(tmp_path / "pinned-version"),
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
    # A freshly provisioned unit already carries the app wait inline (and, like a
    # real fresh provision, the hidden-cursor Environment lines); both drop-ins
    # are then skipped. The cursor path is pinned under tmp_path so the run never
    # touches (and never creates the drop-in from) a host icons directory, which
    # is what made this leak on CI runners where that path is writable.
    unit = tmp_path / "foodassistant-kiosk.service"
    unit.write_text(
        "[Service]\n"
        "ExecStartPre=-/bin/sh -c 'command -v curl >/dev/null 2>&1 || exit 0'\n"
        "Environment=XCURSOR_THEME=foodassistant-hidden\n"
        "ExecStart=/usr/bin/cage -- /usr/bin/chromium http://localhost/ui/\n"
        "ExecStartPost=-/usr/local/bin/foodassistant-apply-rotation\n"
        "Restart=always\n")
    dropin_dir = tmp_path / "kiosk.service.d"
    rig["env"]["KIOSK_UNIT"] = str(unit)
    rig["env"]["KIOSK_DROPIN_DIR"] = str(dropin_dir)
    rig["env"]["CURSOR_THEME_DIR"] = str(tmp_path / "icons" / "foodassistant-hidden")
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


def _splash_rig(rig, tmp_path, with_kiosk=True):
    """Commit boot splash sources upstream and point the splash install paths
    into the sandbox; returns (splash unit dst, splash asset dir)."""
    ib = rig["work"] / "scripts" / "image-build"
    (ib / "foodassistant-boot-splash.service").write_text("[Unit]\n# splash unit v1\n")
    (ib / "boot-splash").mkdir()
    (ib / "boot-splash" / "splash.ppm.gz").write_bytes(b"\x1f\x8bFAKE")
    _git(rig["work"], "add", "-A")
    _git(rig["work"], "commit", "-m", "splash sources")
    _git(rig["work"], "push", "origin", "main")
    if with_kiosk:
        unit = tmp_path / "foodassistant-kiosk.service"
        unit.write_text(_kiosk_unit_text())
        rig["env"]["KIOSK_UNIT"] = str(unit)
    else:
        rig["env"]["KIOSK_UNIT"] = str(tmp_path / "no-kiosk.service")
    rig["env"]["KIOSK_DROPIN_DIR"] = str(tmp_path / "kiosk.d")
    rig["env"]["CURSOR_THEME_DIR"] = str(tmp_path / "icons" / "hidden")
    rig["env"]["CMDLINE_CANDIDATES"] = str(tmp_path / "cmdline.txt")
    splash_unit = tmp_path / "foodassistant-boot-splash.service"
    asset_dir = tmp_path / "boot-splash"
    rig["env"]["SPLASH_UNIT"] = str(splash_unit)
    rig["env"]["SPLASH_ASSET_DIR"] = str(asset_dir)
    return splash_unit, asset_dir


def test_boot_splash_unit_and_image_installed_for_kiosk_devices(rig, tmp_path):
    # Devices imaged before the framebuffer splash existed never got its unit
    # or image; the updater installs both on kiosk devices
    # (FoodAssistant-y8vj). The writer script itself arrives through the
    # generic helper sync.
    splash_unit, asset_dir = _splash_rig(rig, tmp_path)
    result, out = run_update(rig)
    assert splash_unit.read_text() == "[Unit]\n# splash unit v1\n"
    assert (asset_dir / "splash.ppm.gz").read_bytes() == b"\x1f\x8bFAKE"
    assert "Installed the boot splash" in out
    assert (rig["bin"] / "foodassistant-boot-splash").is_file()


def test_boot_splash_rerun_is_quiet_when_current(rig, tmp_path):
    _splash_rig(rig, tmp_path)
    run_update(rig)
    _, out = run_update(rig)
    assert "Installed the boot splash" not in out
    assert "Refreshed the boot splash image." not in out


def test_boot_splash_refreshes_a_changed_image(rig, tmp_path):
    splash_unit, asset_dir = _splash_rig(rig, tmp_path)
    run_update(rig)
    art = rig["work"] / "scripts" / "image-build" / "boot-splash" / "splash.ppm.gz"
    art.write_bytes(b"\x1f\x8bNEW")
    _git(rig["work"], "add", "-A")
    _git(rig["work"], "commit", "-m", "new art")
    _git(rig["work"], "push", "origin", "main")
    _, out = run_update(rig)
    assert (asset_dir / "splash.ppm.gz").read_bytes() == b"\x1f\x8bNEW"
    assert "Refreshed the boot splash image." in out
    # The unchanged unit is not reinstalled.
    assert "Installed the boot splash" not in out


def test_boot_splash_skipped_without_a_kiosk_unit(rig, tmp_path):
    # A headless box has no display to splash: nothing is installed.
    splash_unit, asset_dir = _splash_rig(rig, tmp_path, with_kiosk=False)
    run_update(rig)
    assert not splash_unit.exists()
    assert not (asset_dir / "splash.ppm.gz").exists()


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


def test_ap_watchdog_unit_retrofits_to_the_repo_copy(rig, tmp_path):
    # The watchdog became a continuous loop, so a device imaged with the old
    # Type=oneshot unit must get the repo unit file on its next update (and a
    # service restart, stubbed here) or it keeps the boot-only check forever.
    unit = tmp_path / "foodassistant-ap-watchdog.service"
    unit.write_text(
        "[Service]\nType=oneshot\nRemainAfterExit=yes\n"
        "ExecStart=/usr/local/sbin/foodassistant-ap-watchdog\n"
    )
    sbin = tmp_path / "sbin"
    sbin.mkdir()
    rig["env"]["AP_WATCHDOG_UNIT"] = str(unit)
    rig["env"]["SBIN_DIR"] = str(sbin)
    _, out = run_update(rig)
    repo_unit = (rig["device"] / "scripts" / "image-build"
                 / "foodassistant-ap-watchdog.service").read_text()
    assert unit.read_text() == repo_unit
    assert "Type=oneshot" not in unit.read_text()
    assert "continuous" in out


def test_ap_watchdog_unit_untouched_when_current(rig, tmp_path):
    unit = tmp_path / "foodassistant-ap-watchdog.service"
    sbin = tmp_path / "sbin"
    sbin.mkdir()
    rig["env"]["AP_WATCHDOG_UNIT"] = str(unit)
    rig["env"]["SBIN_DIR"] = str(sbin)
    # Seed the deployed unit with the repo copy so the first run is a no-op
    # on the unit; a second run must not rewrite it either.
    unit.write_text((rig["device"] / "scripts" / "image-build"
                     / "foodassistant-ap-watchdog.service").read_text())
    run_update(rig)
    before = unit.stat().st_mtime_ns
    _, out = run_update(rig)
    assert unit.stat().st_mtime_ns == before
    assert "continuous" not in out


def test_ap_watchdog_unit_not_created_on_devices_without_the_ap(rig, tmp_path):
    # A device that never got the AP fallback (no unit) must not grow one from
    # an update; provisioning owns first installs, updates only refresh.
    unit = tmp_path / "no-such-unit.service"
    sbin = tmp_path / "sbin"
    sbin.mkdir()
    rig["env"]["AP_WATCHDOG_UNIT"] = str(unit)
    rig["env"]["SBIN_DIR"] = str(sbin)
    run_update(rig)
    assert not unit.exists()


# -- update channel (FoodAssistant-wkwx) --------------------------------------

def _full_head(repo):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          check=True, capture_output=True, text=True).stdout.strip()


def _tag_head(rig, name):
    """Tag the work repo's HEAD and push the tag to origin."""
    _git(rig["work"], "tag", name)
    _git(rig["work"], "push", "origin", name)


def _advance_main(rig, marker):
    """Push a new commit to origin main; returns its full hash."""
    (rig["work"] / "scripts" / "image-build" / "foodassistant-set-rotation").write_text(
        f"#!/usr/bin/env bash\n# {marker}\n")
    _git(rig["work"], "add", "-A")
    _git(rig["work"], "commit", "-m", marker)
    _git(rig["work"], "push", "origin", "main")
    return _full_head(rig["work"])


def test_stable_channel_checks_out_the_newest_release_tag(rig):
    _tag_head(rig, "v0.1.0")
    _advance_main(rig, "second release")
    _tag_head(rig, "v0.2.0")
    tagged = _full_head(rig["work"])
    tip = _advance_main(rig, "after the release")  # main tip is past the tag

    rig["env"]["UPDATE_CHANNEL"] = "stable"
    result, out = run_update(rig)
    assert "Checked out release v0.2.0" in out
    assert _full_head(rig["device"]) == tagged
    assert _full_head(rig["device"]) != tip


def test_stable_channel_is_read_from_the_channel_file(rig):
    # No UPDATE_CHANNEL env: the persisted file (written by the host bridge on
    # settings save) decides.
    _tag_head(rig, "v0.1.0")
    tagged = _full_head(rig["work"])
    _advance_main(rig, "past the release")
    Path(rig["env"]["CHANNEL_FILE"]).write_text("stable\n")

    result, out = run_update(rig)
    assert "Update channel: stable" in out
    assert _full_head(rig["device"]) == tagged


def test_main_channel_ignores_release_tags(rig):
    # Regression: with no channel configured, tags change nothing and the
    # device follows the branch tip exactly as before.
    _tag_head(rig, "v0.1.0")
    tip = _advance_main(rig, "newer than any tag")
    result, out = run_update(rig)
    assert "Update channel: main" in out
    assert _full_head(rig["device"]) == tip


def test_stable_moves_when_a_new_release_appears(rig):
    # Tags never move; a new release is a NEW tag, and the next run finds it.
    _tag_head(rig, "v0.1.0")
    rig["env"]["UPDATE_CHANNEL"] = "stable"
    run_update(rig)
    first = _full_head(rig["device"])

    _advance_main(rig, "next release")
    _tag_head(rig, "v0.2.0")
    result, out = run_update(rig)
    assert "Checked out release v0.2.0" in out
    assert _full_head(rig["device"]) != first
    assert _full_head(rig["device"]) == _full_head(rig["work"])


def test_stable_prefers_the_highest_version_not_the_newest_tag(rig):
    # Version sort, not tag-creation order: a later-created lower tag (a
    # backport or a re-tag) must not win over the numerically newest release.
    _advance_main(rig, "big release")
    _tag_head(rig, "v0.10.0")
    high = _full_head(rig["work"])
    _advance_main(rig, "backport")
    _tag_head(rig, "v0.9.1")  # created later, but numerically older

    rig["env"]["UPDATE_CHANNEL"] = "stable"
    result, out = run_update(rig)
    assert "Checked out release v0.10.0" in out
    assert _full_head(rig["device"]) == high


def test_stable_without_releases_keeps_the_current_checkout(rig):
    # Stable with no release tags must never jump to the main tip; the device
    # waits where it is until a release is published.
    before = _full_head(rig["device"])
    _advance_main(rig, "unreleased work")
    rig["env"]["UPDATE_CHANNEL"] = "stable"
    result, out = run_update(rig)
    assert "No release tags found" in out
    assert _full_head(rig["device"]) == before


def test_switching_back_to_main_reattaches_the_branch(rig):
    # Stable leaves the checkout detached on a tag; a later main-channel run
    # must reattach to the branch and follow the tip again. The tag is placed
    # past the device's current commit so the stable run really detaches.
    _advance_main(rig, "the release")
    _tag_head(rig, "v0.1.0")
    tagged = _full_head(rig["work"])
    tip = _advance_main(rig, "newer main work")
    rig["env"]["UPDATE_CHANNEL"] = "stable"
    run_update(rig)
    assert _full_head(rig["device"]) == tagged  # parked on the tag, detached

    rig["env"]["UPDATE_CHANNEL"] = "main"
    result, out = run_update(rig)
    assert "Reattached the checkout to main" in out
    assert _full_head(rig["device"]) == tip
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                            cwd=rig["device"], check=True,
                            capture_output=True, text=True).stdout.strip()
    assert branch == "main"


def _hosted_rig(rig, tmp_path, env_tag="latest"):
    """Give the rig a Pi Hosted layout: a compose project dir with a .env."""
    install = tmp_path / "install"
    install.mkdir()
    (install / "docker-compose.yml").write_text(
        "services:\n  service:\n    image: ghcr.io/syracuse3dprintingorg/"
        "pantryraider:${FOODASSISTANT_TAG:-latest}\n")
    (install / ".env").write_text(f"TZ=UTC\nFOODASSISTANT_TAG={env_tag}\n")
    rig["env"]["INSTALL_DIR"] = str(install)
    return install


def test_stable_pins_the_hosted_image_tag(rig, tmp_path):
    # On a Pi Hosted box the stable channel pins FOODASSISTANT_TAG to the
    # release version (the publish workflow tags images per release), and the
    # replaced value is remembered so main can restore it. The docker stub
    # fails the pull afterwards; the pin itself must already be on disk.
    _tag_head(rig, "v0.2.0")
    install = _hosted_rig(rig, tmp_path)
    rig["env"]["UPDATE_CHANNEL"] = "stable"
    result, out = run_update(rig)
    env_text = (install / ".env").read_text()
    assert "FOODASSISTANT_TAG=0.2.0" in env_text
    assert "Pinned the app image to 0.2.0" in out
    assert Path(rig["env"]["PIN_STATE_FILE"]).read_text().strip() == "latest"


def test_main_restores_the_hosted_image_tag(rig, tmp_path):
    _tag_head(rig, "v0.2.0")
    install = _hosted_rig(rig, tmp_path)
    rig["env"]["UPDATE_CHANNEL"] = "stable"
    run_update(rig)
    assert "FOODASSISTANT_TAG=0.2.0" in (install / ".env").read_text()

    rig["env"]["UPDATE_CHANNEL"] = "main"
    result, out = run_update(rig)
    assert "FOODASSISTANT_TAG=latest" in (install / ".env").read_text()
    assert not Path(rig["env"]["PIN_STATE_FILE"]).exists()


def _add_streamdeck_package(rig):
    """Put a Stream Deck package in the checkout so the update can sync it."""
    pkg = rig["work"] / "streamdeck" / "foodassistant_streamdeck"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__main__.py").write_text("# deck entrypoint\n")
    (pkg / "actions.py").write_text("# post_deck_confirmation lives here\n")
    _git(rig["work"], "add", "-A")
    _git(rig["work"], "commit", "-m", "add streamdeck package")
    _git(rig["work"], "push", "origin", "main")


def _succeeding_docker(tmp_path):
    """A docker stub that succeeds and reports a stable image id, so the Pi
    Hosted path runs to completion ('image unchanged') and reaches the deck
    sync instead of bailing out on a failed pull like the default stub."""
    d = tmp_path / "docker-ok"
    d.mkdir()
    stub = d / "docker"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do case "$a" in images) echo sha256:same; exit 0;; esac; done\n'
        "exit 0\n"
    )
    stub.chmod(0o755)
    return d


def test_hosted_path_syncs_the_streamdeck_package(rig, tmp_path):
    # A Pi Hosted (Docker) device runs the deck as a host systemd unit the app
    # container never touches. The Docker update path used to return before the
    # deck sync, so deck code changes never reached hosted devices
    # (FoodAssistant-aus1). It must now sync the deck on the way out.
    _add_streamdeck_package(rig)
    _hosted_rig(rig, tmp_path)
    sd_parent = tmp_path / "deck-hosted"
    sd_parent.mkdir()
    rig["env"]["SD_DST"] = str(sd_parent / "foodassistant_streamdeck")
    rig["env"]["PATH"] = f"{_succeeding_docker(tmp_path)}:{rig['env']['PATH']}"
    result, out = run_update(rig)
    assert result["streamdeck_synced"] is True, out
    synced = Path(rig["env"]["SD_DST"]) / "actions.py"
    assert synced.is_file() and "post_deck_confirmation" in synced.read_text()
    assert "Syncing Stream Deck package" in out


def test_venv_path_syncs_the_streamdeck_package(rig, tmp_path):
    # The non-Docker (Pi Remote / venv) path shares the same sync_streamdeck
    # helper, so it must keep syncing the deck after the refactor.
    _add_streamdeck_package(rig)
    sd_parent = tmp_path / "deck-venv"
    sd_parent.mkdir()
    rig["env"]["SD_DST"] = str(sd_parent / "foodassistant_streamdeck")
    result, out = run_update(rig)
    assert result["streamdeck_synced"] is True, out
    assert (Path(rig["env"]["SD_DST"]) / "actions.py").is_file()


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


# -- I2C retrofit for plug-in accessories (FoodAssistant-9nybq) ---------------

I2C_GRID_WITH_NEOKEY = (
    "     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f\n"
    "00:                         -- -- -- -- -- -- -- --\n"
    "10: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --\n"
    "20: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --\n"
    "30: 30 -- -- -- -- -- -- -- -- -- -- -- -- -- -- --\n"
    "40: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --\n"
)
I2C_GRID_EMPTY = I2C_GRID_WITH_NEOKEY.replace("30: 30", "30: --")


def _stub(rig, tmp_path, name, body):
    """A stub on the rig's PATH that records its argv to STUB_LOG."""
    s = tmp_path / "stubs" / name
    s.write_text("#!/usr/bin/env bash\n"
                 f"printf '{name} %s\\n' \"$*\" >> \"$STUB_LOG\"\n" + body)
    s.chmod(0o755)
    return s


def _i2c_rig(rig, tmp_path, *, config_text="dtoverlay=vc4-kms-v3d\n",
             tools_present=False, grid=None, bus_node=False,
             settings=None, with_setup_helper=True):
    """A Pi-shaped sandbox for the I2C block: a boot config (None = not a
    Pi), a modules-load dir, stubbed root tools, an i2cdetect stand-in, and
    the gadgets setup helper committed upstream so the helper sync installs
    it. Returns the boot config path."""
    cfg = tmp_path / "config.txt"
    if config_text is not None:
        cfg.write_text(config_text)
    modules = tmp_path / "modules-load.d"
    modules.mkdir()
    log = tmp_path / "stub.log"
    log.write_text("")
    rig["env"].update({
        "STUB_LOG": str(log),
        "I2C_CONFIG_CANDIDATES": str(cfg),
        "I2C_MODULES_DIR": str(modules),
        "I2C_DEV": str(tmp_path / ("i2c-1" if bus_node else "no-i2c-1")),
        "GD_USER": "pr-test-user",
        "GD_DST": str(tmp_path / "no-gadgets"),
        "SETTINGS_JSON": str(tmp_path / "settings.json"),
    })
    if bus_node:
        (tmp_path / "i2c-1").write_text("")
    if settings is not None:
        (tmp_path / "settings.json").write_text(json.dumps(settings))
    for name in ("modprobe", "apt-get", "usermod"):
        _stub(rig, tmp_path, name, "exit 0\n")
    _stub(rig, tmp_path, "getent", "exit 0\n")
    if tools_present:
        _stub(rig, tmp_path, "fake-i2cdetect",
              f"cat <<'GRID'\n{grid or I2C_GRID_EMPTY}GRID\n")
        rig["env"]["I2C_TOOL"] = "fake-i2cdetect"
    else:
        rig["env"]["I2C_TOOL"] = "fake-i2cdetect-missing"
    if with_setup_helper:
        helper = rig["work"] / "scripts" / "image-build" / "foodassistant-gadgets-setup"
        helper.write_text("#!/usr/bin/env bash\n"
                          "printf 'gadgets-setup INSTALL_DIR=%s REPO_DIR=%s\\n' "
                          "\"$INSTALL_DIR\" \"$REPO_DIR\" >> \"$STUB_LOG\"\n")
        _git(rig["work"], "add", "-A")
        _git(rig["work"], "commit", "-m", "gadgets setup helper")
        _git(rig["work"], "push", "origin", "main")
    return cfg


def _stub_log(rig):
    return Path(rig["env"]["STUB_LOG"]).read_text()


def test_i2c_retrofit_turns_the_bus_on_and_flags_a_reboot(rig, tmp_path):
    # A device imaged before the bus was part of provisioning has no dtparam,
    # no module, no i2c-tools, and a service user outside the i2c group; one
    # update fixes all four and says a reboot is needed for the bus itself.
    cfg = _i2c_rig(rig, tmp_path)
    result, out = run_update(rig)
    assert cfg.read_text().count("dtparam=i2c_arm=on") == 1
    assert result["reboot_needed"] is True
    assert "REBOOT NEEDED" in out
    modules_conf = tmp_path / "modules-load.d" / "foodassistant-i2c.conf"
    assert modules_conf.read_text() == "i2c-dev\n"
    log = _stub_log(rig)
    assert "modprobe i2c-dev" in log
    assert "apt-get install -y --no-install-recommends i2c-tools" in log
    assert "usermod -aG i2c pr-test-user" in log
    assert "Added pr-test-user to the i2c group" in out


def test_i2c_retrofit_rerun_is_idempotent(rig, tmp_path):
    cfg = _i2c_rig(rig, tmp_path)
    run_update(rig)
    first = cfg.read_text()
    result, out = run_update(rig)
    assert cfg.read_text() == first
    assert result["reboot_needed"] is False
    assert "REBOOT NEEDED" not in out
    assert "Set the i2c-dev module" not in out


def test_i2c_retrofit_prefers_raspi_config_when_present(rig, tmp_path):
    # raspi-config writes the parameter itself; the append fallback then has
    # nothing to do, so the line lands exactly once.
    cfg = _i2c_rig(rig, tmp_path)
    _stub(rig, tmp_path, "raspi-config",
          f"printf 'dtparam=i2c_arm=on\\n' >> '{cfg}'\nexit 0\n")
    result, out = run_update(rig)
    assert "raspi-config nonint do_i2c 0" in _stub_log(rig)
    assert cfg.read_text().count("dtparam=i2c_arm=on") == 1
    assert result["reboot_needed"] is True


def test_i2c_retrofit_falls_back_to_the_append_when_raspi_config_refuses(rig, tmp_path):
    cfg = _i2c_rig(rig, tmp_path)
    _stub(rig, tmp_path, "raspi-config", "exit 1\n")
    result, out = run_update(rig)
    assert "raspi-config nonint do_i2c 0" in _stub_log(rig)
    assert cfg.read_text().count("dtparam=i2c_arm=on") == 1
    assert result["reboot_needed"] is True


def test_i2c_bus_already_on_needs_no_reboot_and_no_packages(rig, tmp_path):
    cfg = _i2c_rig(rig, tmp_path, config_text="dtparam=i2c_arm=on\n",
                   tools_present=True)
    result, out = run_update(rig)
    assert cfg.read_text() == "dtparam=i2c_arm=on\n"
    assert result["reboot_needed"] is False
    assert "REBOOT NEEDED" not in out
    # (The stubbed apt-get also sees the older package retrofits; only the
    # I2C tools matter here.)
    assert "i2c-tools" not in _stub_log(rig)
    # The module and group steps still re-assert (they are what a half-done
    # manual enable most often misses).
    assert (tmp_path / "modules-load.d" / "foodassistant-i2c.conf").is_file()
    assert "usermod -aG i2c pr-test-user" in _stub_log(rig)


def test_i2c_retrofit_skipped_off_a_pi(rig, tmp_path):
    # No Pi boot config means a plain server (or a test box): nothing is
    # touched and no package or group change is attempted.
    _i2c_rig(rig, tmp_path, config_text=None)
    result, out = run_update(rig)
    assert not (tmp_path / "config.txt").exists()
    assert not (tmp_path / "modules-load.d" / "foodassistant-i2c.conf").exists()
    log = _stub_log(rig)
    assert "i2c-tools" not in log and "usermod" not in log and "modprobe" not in log
    assert result["reboot_needed"] is False


def test_gadgets_setup_runs_when_a_neokey_answers_on_the_bus(rig, tmp_path):
    # ENABLE_GADGETS unset resolves to auto; a pad answering at 0x30 with no
    # reader installed triggers the same helper the Set up for me button runs.
    _i2c_rig(rig, tmp_path, config_text="dtparam=i2c_arm=on\n",
             tools_present=True, grid=I2C_GRID_WITH_NEOKEY, bus_node=True)
    result, out = run_update(rig)
    log = _stub_log(rig)
    assert "fake-i2cdetect -y 1" in log
    assert f"gadgets-setup INSTALL_DIR={rig['env']['INSTALL_DIR']} REPO_DIR={rig['env']['REPO_DIR']}" in log
    assert "The accessory reader is set up" in out


def test_gadgets_setup_runs_when_a_pad_is_registered_in_settings(rig, tmp_path):
    # The bus was just turned on (no node until the reboot), but the app's
    # registry already names a pad: install the reader now so it works on
    # the very next boot instead of after a second update.
    _i2c_rig(rig, tmp_path, settings={"stemma_devices": [
        {"id": "i2c:1:0x30", "kind": "neokey", "name": "Pad"}]})
    result, out = run_update(rig)
    assert "gadgets-setup INSTALL_DIR=" in _stub_log(rig)
    assert result["reboot_needed"] is True


def test_gadgets_setup_not_run_without_an_accessory(rig, tmp_path):
    # An empty bus and an empty registry: a normal update never grows the
    # reader onto a device uninvited.
    _i2c_rig(rig, tmp_path, config_text="dtparam=i2c_arm=on\n",
             tools_present=True, grid=I2C_GRID_EMPTY, bus_node=True,
             settings={"stemma_devices": []})
    run_update(rig)
    assert "gadgets-setup" not in _stub_log(rig)


def test_gadgets_setup_not_run_when_opted_out(rig, tmp_path):
    _i2c_rig(rig, tmp_path, config_text="dtparam=i2c_arm=on\n",
             tools_present=True, grid=I2C_GRID_WITH_NEOKEY, bus_node=True)
    rig["env"]["ENABLE_GADGETS"] = "false"
    run_update(rig)
    assert "gadgets-setup" not in _stub_log(rig)


def test_gadgets_setup_not_run_when_the_reader_is_already_installed(rig, tmp_path):
    _i2c_rig(rig, tmp_path, config_text="dtparam=i2c_arm=on\n",
             tools_present=True, grid=I2C_GRID_WITH_NEOKEY, bus_node=True)
    (tmp_path / "no-gadgets").mkdir()   # GD_DST exists: sync_gadgets owns it
    run_update(rig)
    assert "gadgets-setup" not in _stub_log(rig)


def test_gadgets_setup_missing_helper_points_at_settings(rig, tmp_path):
    _i2c_rig(rig, tmp_path, config_text="dtparam=i2c_arm=on\n",
             tools_present=True, grid=I2C_GRID_WITH_NEOKEY, bus_node=True,
             with_setup_helper=False)
    result, out = run_update(rig)
    assert "foodassistant-gadgets-setup is not installed; use Set up for me" in out
    assert "gadgets-setup" not in _stub_log(rig)


# --- Version pin (hold a device on one release) ------------------------------

def _pin(rig, text):
    Path(rig["env"]["PINNED_VERSION_FILE"]).write_text(text)


def test_pinned_version_holds_the_device_on_that_release(rig):
    # Two releases exist; the pin names the older one, so the newer release
    # must not be installed over it.
    _advance_main(rig, "first release")
    _tag_head(rig, "v0.1.0")
    pinned = _full_head(rig["work"])
    _advance_main(rig, "second release")
    _tag_head(rig, "v0.2.0")

    rig["env"]["UPDATE_CHANNEL"] = "stable"
    _pin(rig, "0.1.0\n")
    result, out = run_update(rig)
    assert "Staying on the pinned release v0.1.0" in out
    assert "Checked out release v0.1.0" in out
    assert _full_head(rig["device"]) == pinned


def test_pinned_version_accepts_the_v_prefix(rig):
    _advance_main(rig, "first release")
    _tag_head(rig, "v0.1.0")
    pinned = _full_head(rig["work"])
    _advance_main(rig, "second release")
    _tag_head(rig, "v0.2.0")

    rig["env"]["UPDATE_CHANNEL"] = "stable"
    _pin(rig, "v0.1.0\n")
    result, out = run_update(rig)
    assert _full_head(rig["device"]) == pinned


def test_pin_is_reported_on_every_run(rig):
    # A device that looks stuck on an old version has to explain itself in the
    # update log, whichever channel it is on.
    _tag_head(rig, "v0.1.0")
    rig["env"]["UPDATE_CHANNEL"] = "stable"
    _pin(rig, "0.1.0\n")
    result, out = run_update(rig)
    assert "holds v0.1.0" in out


def test_unknown_pinned_version_follows_the_newest_release(rig):
    # A typo, or a version that was never published, must not strand the
    # device: say so and carry on with the newest release.
    _tag_head(rig, "v0.1.0")
    _advance_main(rig, "second release")
    _tag_head(rig, "v0.2.0")
    newest = _full_head(rig["work"])

    rig["env"]["UPDATE_CHANNEL"] = "stable"
    _pin(rig, "9.9.9\n")
    result, out = run_update(rig)
    assert "no release tag matches the pinned version v9.9.9" in out
    assert _full_head(rig["device"]) == newest


def test_no_pin_file_still_follows_the_newest_release(rig):
    _tag_head(rig, "v0.1.0")
    _advance_main(rig, "second release")
    _tag_head(rig, "v0.2.0")
    newest = _full_head(rig["work"])

    rig["env"]["UPDATE_CHANNEL"] = "stable"
    result, out = run_update(rig)
    assert "pinned" not in out.lower()
    assert _full_head(rig["device"]) == newest


def test_empty_pin_file_changes_nothing(rig):
    _tag_head(rig, "v0.1.0")
    _advance_main(rig, "second release")
    _tag_head(rig, "v0.2.0")
    newest = _full_head(rig["work"])

    rig["env"]["UPDATE_CHANNEL"] = "stable"
    _pin(rig, "\n")
    result, out = run_update(rig)
    assert _full_head(rig["device"]) == newest


def test_pin_on_the_main_channel_is_reported_and_not_applied(rig):
    _tag_head(rig, "v0.1.0")
    tip = _advance_main(rig, "newer than any tag")
    _pin(rig, "0.1.0\n")
    result, out = run_update(rig)
    assert "The pin applies on the stable channel" in out
    assert _full_head(rig["device"]) == tip


# --- Code tree ownership -----------------------------------------------------

@pytest.mark.skipif(os.geteuid() == 0,
                    reason="needs a non-root owner to have anything to retrofit")
def test_a_user_owned_venv_is_handed_back_to_root(rig, tmp_path):
    """Root runs python out of this venv (the host bridge asks the deck
    controller for its layout, and the pip step runs as root), so a venv the
    desktop account can rewrite is a way to run code as root. A device flashed
    before that was tightened is retrofitted on its next update."""
    venv = tmp_path / "user-venv"
    (venv / "bin").mkdir(parents=True)
    rig["env"]["VENV_DIR"] = str(venv)

    result, out = run_update(rig)
    assert "Securing" in out
    assert "the code tree belongs to root" in out


def test_an_already_root_owned_tree_is_left_alone(rig, tmp_path):
    # Idempotent: the common case must not chown anything on every update.
    missing = tmp_path / "not-installed"
    rig["env"]["VENV_DIR"] = str(missing)
    result, out = run_update(rig)
    assert "the code tree belongs to root" not in out
