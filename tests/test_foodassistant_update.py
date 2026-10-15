"""foodassistant-update: release verification, helper refresh, recovery.

Runs the real updater script against throwaway local git repos (no network,
no Docker, no root): BIN_DIR points at a temp dir, deploy targets point at
paths that do not exist (so the deploy branches skip), and stub systemctl /
systemd-run / docker binaries on PATH keep the script from touching the host.

The rig is signed: a session fixture makes a throwaway ed25519 release key,
the work repo signs every commit and tag with it, and the updater trusts it
through TRUST_FILE (installed) or FA_BOOTSTRAP_SIGNERS (first run). Nothing
here reads this repository's own history or tags.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "image-build" / "foodassistant-update"
OLD_UPDATER = Path(__file__).resolve().parent / "data" / "foodassistant-update-v0.20.0"

pytestmark = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("bash", "git", "python3", "ssh-keygen")),
    reason="bash, git, python3 and ssh-keygen are required to exercise the updater script",
)

GIT_ID = ["-c", "user.email=test@test", "-c", "user.name=test"]
START_VERSION = "0.0.1"


def _git(cwd, *args):
    return subprocess.run(["git", *GIT_ID, *args], cwd=cwd, check=True,
                          capture_output=True, text=True)


def _short_head(repo):
    return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo,
                          check=True, capture_output=True, text=True).stdout.strip()


def _keypair(dirpath, name):
    key = Path(dirpath) / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name,
                    "-f", str(key)], check=True, capture_output=True)
    kind, blob = Path(str(key) + ".pub").read_text().split()[:2]
    line = f'pantryraider-release namespaces="git,pantryraider-release" {kind} {blob}\n'
    signers = Path(dirpath) / f"{name}.signers"
    signers.write_text(line)
    return {"key": key, "line": line, "signers": signers}


@pytest.fixture(scope="session")
def signing(tmp_path_factory):
    """Two throwaway release keys: k1 signs the rig, k2 is for rotation."""
    d = tmp_path_factory.mktemp("release-keys")
    return {"k1": _keypair(d, "key1"), "k2": _keypair(d, "key2")}


def _use_key(repo, key):
    """Sign every commit and tag made in REPO with KEY (None: stop signing)."""
    if key is None:
        _git(repo, "config", "commit.gpgsign", "false")
        _git(repo, "config", "tag.gpgsign", "false")
        return
    _git(repo, "config", "gpg.format", "ssh")
    _git(repo, "config", "user.signingkey", str(key["key"]))
    _git(repo, "config", "commit.gpgsign", "true")
    _git(repo, "config", "tag.gpgsign", "true")


def _set_version(work, version):
    cfg = Path(work) / "service" / "app" / "config.py"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(f'APP_NAME = "Pantry Raider"\nAPP_VERSION = "{version}"\n')


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


def _helper_stub(name):
    body = f"#!/usr/bin/env bash\n# {name} v1\n"
    if name == "foodassistant-update":
        # An updater stub that can check signatures (generation 1), so the
        # generation rule lets the helper sync install it.
        body += "UPDATER_TRUST_GENERATION=1\n"
    return body


_RIG_REPOS = ("origin.git", "work", "device")


@pytest.fixture(scope="session")
def rig_template(tmp_path_factory, signing):
    """The rig's three repos, built once per session: origin (bare), the
    signed work clone with v1 pushed, and the device clone. Each test gets its
    own copy (see rig), so building them, which costs a signed commit and
    a handful of git processes, happens once instead of per test."""
    base = tmp_path_factory.mktemp("rig-template")
    origin = base / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   check=True, capture_output=True)

    work = base / "work"
    subprocess.run(["git", "clone", str(origin), str(work)],
                   check=True, capture_output=True)
    _use_key(work, signing["k1"])
    ib = work / "scripts" / "image-build"
    ib.mkdir(parents=True)
    for name in HELPERS + NON_HELPERS:
        (ib / name).write_text(_helper_stub(name))
    _set_version(work, START_VERSION)
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "v1")
    _git(work, "push", "origin", "main")

    device = base / "device"
    subprocess.run(["git", "clone", str(origin), str(device)],
                   check=True, capture_output=True)
    return base


def _copy_rig_repos(template: Path, dest: Path) -> None:
    """Copy the template repos under DEST and point both clones' origin
    remote at the copied bare repo (a clone records its origin's absolute
    path, which would otherwise still name the shared template)."""
    for name in _RIG_REPOS:
        shutil.copytree(template / name, dest / name, symlinks=True)
    old_url, new_url = str(template / "origin.git"), str(dest / "origin.git")
    for clone in ("work", "device"):
        cfg = dest / clone / ".git" / "config"
        text = cfg.read_text()
        assert old_url in text, cfg
        cfg.write_text(text.replace(old_url, new_url))


@pytest.fixture()
def rig(tmp_path, signing, rig_template):
    """origin (bare) + device clone with helper sources committed, plus a stub
    PATH and env that keeps the script inert outside the temp tree. Every
    commit and tag in the work repo is signed by the trusted test key."""
    _copy_rig_repos(rig_template, tmp_path)
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    device = tmp_path / "device"

    stub_bin = tmp_path / "stubs"
    stub_bin.mkdir()
    for stub in ("systemctl", "systemd-run", "docker"):
        s = stub_bin / stub
        s.write_text("#!/usr/bin/env bash\nexit 1\n")
        s.chmod(0o755)
    # The package retrofits run apt-get and dpkg-query. Stubbed, they answer
    # like a host without the package and without root, every time: the test
    # never depends on what this machine has installed and never reaches the
    # real package manager (which alone cost about a tenth of each run). A
    # test that inspects these calls replaces the stubs with logging ones.
    for stub in ("apt-get", "dpkg-query"):
        s = stub_bin / stub
        s.write_text("#!/usr/bin/env bash\nexit 1\n")
        s.chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    trust = tmp_path / "trust" / "release-signers"
    trust.parent.mkdir()
    trust.write_text(signing["k1"]["line"])
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
        # Release trust, all under tmp_path. The existing tests follow the
        # main channel (the stable default is set explicitly where tested).
        "TRUST_FILE": str(trust),
        "STATE_FILE": str(tmp_path / "state" / "update-state.json"),
        "STAGE_ROOT": str(tmp_path / "releases"),
        "FA_STAGE_MIN_MB": "1",
        "RELEASE_BASE_URL": (tmp_path / "published").as_uri(),
        "FA_BOOTSTRAP_SIGNERS": str(signing["k1"]["signers"]),
        "FA_RUNNING_VERSION": START_VERSION,
        "UPDATE_CHANNEL": "main",
        "SETTINGS_JSON": str(tmp_path / "no-settings.json"),
        # The run lock lives under tmp_path, and the wait for the new version
        # to answer is off unless a test turns it on (see _health_rig).
        "UPDATE_LOCK_FILE": str(tmp_path / "update.lock"),
        "FA_HEALTH_TIMEOUT": "0",
        # The boot screen's getty@tty1 drop-in and config.txt stay in the
        # sandbox too.
        "GETTY_DROPIN": str(tmp_path / "getty@tty1.service.d" / "10-foodassistant-boot-splash.conf"),
        "CONFIG_TXT_CANDIDATES": str(tmp_path / "no-config.txt"),
    }
    return {"origin": origin, "work": work, "device": device,
            "bin": bin_dir, "env": env, "tmp": tmp_path, "signing": signing,
            "trust": trust, "state": Path(env["STATE_FILE"]),
            "src": Path(env["STAGE_ROOT"]) / "current"}


def run_update(rig, *args, script=None):
    r = subprocess.run(["bash", str(script or SCRIPT), *args], env=rig["env"],
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
    # The app wait probes /health on the kiosk URL's origin (a page fetch
    # would count as a keyless local caller), with $$ so systemd passes a
    # literal $ through to the shell.
    assert '"http://localhost/health"' in conf
    assert "/ui/?kiosk=1" not in conf
    assert "$$(seq 1 40)" in conf


@pytest.mark.parametrize("url,want", [
    ("http://192.168.1.20:9284/ui/?kiosk=1", '"http://192.168.1.20:9284/health"'),
    ("http://[::1]:9284/ui/", '"http://[::1]:9284/health"'),
    ("http://evil';touch$IFS/tmp/x;'/ui/", None),
])
def test_kiosk_boot_dropin_waits_only_on_a_plain_origin(rig, tmp_path, url, want):
    # The origin is written inside a quoted shell command, so anything but a
    # plain scheme, host and port leaves the wait out.
    unit = tmp_path / "foodassistant-kiosk.service"
    unit.write_text("[Service]\nExecStart=/usr/bin/cage -- /usr/bin/chromium --kiosk "
                    f"{url}\nRestart=always\n")
    dropin_dir = tmp_path / "kiosk.service.d"
    rig["env"]["KIOSK_UNIT"] = str(unit)
    rig["env"]["KIOSK_DROPIN_DIR"] = str(dropin_dir)
    run_update(rig)
    conf = (dropin_dir / "10-foodassistant-boot.conf").read_text()
    assert "After=seatd.service" in conf
    if want is None:
        assert "ExecStartPre" not in conf
        assert "evil" not in conf
    else:
        assert want in conf


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
    # real fresh provision, the hidden-cursor Environment lines and the VT
    # switch); every drop-in is then skipped. The cursor path is pinned under tmp_path so the run never
    # touches (and never creates the drop-in from) a host icons directory, which
    # is what made this leak on CI runners where that path is writable.
    unit = tmp_path / "foodassistant-kiosk.service"
    unit.write_text(
        "[Service]\n"
        "ExecStartPre=-/bin/sh -c 'command -v curl >/dev/null 2>&1 || exit 0'\n"
        "Environment=XCURSOR_THEME=foodassistant-hidden\n"
        "ExecStartPre=-+/usr/bin/timeout 10 /usr/bin/chvt 7\n"
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


def _splash_rig(rig, tmp_path, with_kiosk=True, config_txt="dtparam=audio=on\n"):
    """Commit boot screen sources upstream and point the install paths into
    the sandbox; returns (splash unit dst, artwork dir)."""
    ib = rig["work"] / "scripts" / "image-build"
    (ib / "foodassistant-boot-splash.service").write_text("[Unit]\n# splash unit v1\n")
    (ib / "boot-splash").mkdir()
    (ib / "boot-splash" / "splash-art.gz").write_bytes(b"\x1f\x8bFAKE")
    (ib / "boot-splash" / "getty-tty1.conf").write_text(
        "[Unit]\nAfter=foodassistant-boot-splash.service\n")
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
    cfg = tmp_path / "config.txt"
    if config_txt is not None:
        cfg.write_text(config_txt)
    rig["env"]["CONFIG_TXT_CANDIDATES"] = str(cfg)
    splash_unit = tmp_path / "foodassistant-boot-splash.service"
    asset_dir = tmp_path / "boot-splash"
    rig["env"]["SPLASH_UNIT"] = str(splash_unit)
    rig["env"]["SPLASH_ASSET_DIR"] = str(asset_dir)
    return splash_unit, asset_dir


def test_boot_splash_unit_and_image_installed_for_kiosk_devices(rig, tmp_path):
    # Devices imaged before the boot screen existed (or with the earlier still
    # image) get the unit, the artwork and the getty@tty1 ordering on a kiosk
    # device (FoodAssistant-y8vj). The screen script itself arrives through
    # the generic helper sync.
    splash_unit, asset_dir = _splash_rig(rig, tmp_path)
    asset_dir.mkdir()
    (asset_dir / "splash.ppm.gz").write_bytes(b"old still image")
    result, out = run_update(rig)
    assert splash_unit.read_text() == "[Unit]\n# splash unit v1\n"
    assert (asset_dir / "splash-art.gz").read_bytes() == b"\x1f\x8bFAKE"
    assert not (asset_dir / "splash.ppm.gz").exists()
    getty = Path(rig["env"]["GETTY_DROPIN"])
    assert "After=foodassistant-boot-splash.service" in getty.read_text()
    assert "Installed the boot splash" in out
    assert "Ordered the tty1 console after the boot screen." in out
    assert (rig["bin"] / "foodassistant-boot-splash").is_file()


def test_boot_splash_rerun_is_quiet_when_current(rig, tmp_path):
    _splash_rig(rig, tmp_path)
    run_update(rig)
    _, out = run_update(rig)
    assert "Installed the boot splash" not in out
    assert "Refreshed the boot screen artwork." not in out
    assert "Ordered the tty1 console" not in out
    assert "firmware colour splash" not in out


def test_boot_splash_refreshes_a_changed_image(rig, tmp_path):
    splash_unit, asset_dir = _splash_rig(rig, tmp_path)
    run_update(rig)
    art = rig["work"] / "scripts" / "image-build" / "boot-splash" / "splash-art.gz"
    art.write_bytes(b"\x1f\x8bNEW")
    _git(rig["work"], "add", "-A")
    _git(rig["work"], "commit", "-m", "new art")
    _git(rig["work"], "push", "origin", "main")
    _, out = run_update(rig)
    assert (asset_dir / "splash-art.gz").read_bytes() == b"\x1f\x8bNEW"
    assert "Refreshed the boot screen artwork." in out
    # The unchanged unit is not reinstalled.
    assert "Installed the boot splash" not in out


def test_boot_splash_skipped_without_a_kiosk_unit(rig, tmp_path):
    # A headless box has no display to splash: nothing is installed.
    splash_unit, asset_dir = _splash_rig(rig, tmp_path, with_kiosk=False)
    run_update(rig)
    assert not splash_unit.exists()
    assert not (asset_dir / "splash-art.gz").exists()
    assert not Path(rig["env"]["GETTY_DROPIN"]).exists()
    assert (tmp_path / "config.txt").read_text() == "dtparam=audio=on\n"


def test_boot_screen_turns_off_the_firmware_splash_once(rig, tmp_path):
    _splash_rig(rig, tmp_path)
    cfg = tmp_path / "config.txt"
    _, out = run_update(rig)
    text = cfg.read_text()
    assert text.startswith("dtparam=audio=on\n")
    assert text.endswith("# BEGIN Pantry Raider boot screen\n[all]\ndisable_splash=1\n"
                         "# END Pantry Raider boot screen\n")
    assert "Turned off the firmware colour splash" in out
    run_update(rig)
    assert cfg.read_text() == text


def test_boot_screen_keeps_an_owner_set_disable_splash(rig, tmp_path):
    _splash_rig(rig, tmp_path, config_txt="[pi4]\ndisable_splash=0\n")
    run_update(rig)
    assert (tmp_path / "config.txt").read_text() == "[pi4]\ndisable_splash=0\n"


def _tty1_kiosk_unit():
    return ("[Unit]\nDescription=kiosk\n"
            "After=foodassistant.target systemd-user-sessions.service getty@tty1.service "
            "network-online.target seatd.service\n"
            "Wants=network-online.target seatd.service\nConflicts=getty@tty1.service\n\n"
            "[Service]\nTTYPath=/dev/tty1\nUtmpIdentifier=tty1\n"
            "ExecStartPre=-/bin/sh -c 'command -v curl >/dev/null 2>&1 || exit 0; "
            "curl -sf \"http://localhost/health\" && exit 0; exit 0'\n"
            "ExecStart=/usr/bin/cage -- /usr/bin/chromium --kiosk --noerrdialogs "
            "http://localhost/ui/?kiosk=1\n"
            "ExecStartPost=-/usr/local/bin/foodassistant-apply-rotation\n"
            "Restart=always\n\n[Install]\nWantedBy=multi-user.target\n")


def test_kiosk_moves_to_its_own_vt(rig, tmp_path):
    # A kiosk unit written before the boot screen ran cage on tty1 with
    # Conflicts=getty@tty1; the updater drops the conflict and adds the VT
    # switch as a drop-in that runs after any older app wait.
    unit = tmp_path / "foodassistant-kiosk.service"
    unit.write_text(_tty1_kiosk_unit())
    dropins = tmp_path / "foodassistant-kiosk.service.d"  # the device layout
    rig["env"]["KIOSK_UNIT"] = str(unit)
    rig["env"]["KIOSK_DROPIN_DIR"] = str(dropins)
    _, out = run_update(rig)
    text = unit.read_text()
    assert "getty@tty1" not in text
    assert ("After=foodassistant.target systemd-user-sessions.service "
            "network-online.target seatd.service\n") in text
    vt = (dropins / "30-foodassistant-vt.conf").read_text()
    lines = vt.splitlines()
    assert "[Service]" in lines and "TTYPath=/dev/tty7" in lines
    assert "UtmpIdentifier=tty7" in lines
    assert "ExecStartPre=-+/usr/bin/timeout 10 /usr/bin/chvt 7" in lines
    assert "kiosk runs on its own VT from its next start" in out
    # The boot screen recognises it.
    splash = _load_splash()
    assert splash.kiosk_switches_vt(splash.kiosk_text(str(unit)))
    # Idempotent, and no restart of its own.
    before = text, vt
    _, out = run_update(rig)
    assert (unit.read_text(), (dropins / "30-foodassistant-vt.conf").read_text()) == before
    assert "own VT" not in out and "conflicts with the tty1" not in out


def test_kiosk_with_its_own_chvt_gets_no_vt_dropin(rig, tmp_path):
    unit = tmp_path / "foodassistant-kiosk.service"
    content = ("[Service]\nTTYPath=/dev/tty7\n"
               "ExecStartPre=-+/usr/bin/timeout 10 /usr/bin/chvt 7\n"
               "ExecStart=/usr/bin/cage\n"
               "ExecStartPost=-/usr/local/bin/foodassistant-apply-rotation\n"
               "Restart=always\n")
    unit.write_text(content)
    rig["env"]["KIOSK_UNIT"] = str(unit)
    rig["env"]["KIOSK_DROPIN_DIR"] = str(tmp_path / "kiosk.d")
    run_update(rig)
    assert unit.read_text() == content
    assert not (tmp_path / "kiosk.d" / "30-foodassistant-vt.conf").exists()


def _load_splash():
    import importlib.machinery
    import importlib.util
    path = ROOT / "scripts" / "image-build" / "foodassistant-boot-splash"
    loader = importlib.machinery.SourceFileLoader("fa_boot_splash_u", str(path))
    spec = importlib.util.spec_from_loader("fa_boot_splash_u", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


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
        "UPDATER_TRUST_GENERATION=1\n"
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
    """Make NAME a signed release: the tree's APP_VERSION is set to match it
    (a commit is added and pushed when it differs), then HEAD gets a signed
    annotated tag, pushed to origin."""
    work = rig["work"]
    cfg = work / "service" / "app" / "config.py"
    if f'APP_VERSION = "{name[1:]}"' not in cfg.read_text():
        _set_version(work, name[1:])
        _git(work, "add", "-A")
        _git(work, "commit", "-m", f"release {name}")
        _git(work, "push", "origin", "main")
    _git(work, "tag", "-s", "-m", name, name)
    _git(work, "push", "origin", name)


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
    rig["env"].pop("UPDATE_CHANNEL")

    result, out = run_update(rig)
    assert "Update channel: stable" in out
    assert _full_head(rig["device"]) == tagged


def test_main_channel_ignores_release_tags(rig):
    # On the main channel (the rig's UPDATE_CHANNEL), tags change nothing and
    # the device follows the signed branch tip.
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
    assert result["ok"] is False
    assert result["hold_reason"].startswith("No signed release is available yet")


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
    rig["env"]["PATH"] = f"{_succeeding_docker(tmp_path)}:{rig['env']['PATH']}"
    result, out = run_update(rig)
    env_text = (install / ".env").read_text()
    assert "FOODASSISTANT_TAG=0.2.0" in env_text
    assert "Pinned the app image to 0.2.0" in out
    assert Path(rig["env"]["PIN_STATE_FILE"]).read_text().strip() == "latest"


def test_main_restores_the_hosted_image_tag(rig, tmp_path):
    _tag_head(rig, "v0.2.0")
    install = _hosted_rig(rig, tmp_path)
    rig["env"]["UPDATE_CHANNEL"] = "stable"
    rig["env"]["PATH"] = f"{_succeeding_docker(tmp_path)}:{rig['env']['PATH']}"
    run_update(rig)
    assert "FOODASSISTANT_TAG=0.2.0" in (install / ".env").read_text()

    rig["env"]["UPDATE_CHANNEL"] = "main"
    result, out = run_update(rig)
    # The saved 'latest' comes back as 'main': :latest moves only on signed
    # releases now, :main is what follows the main channel.
    assert "FOODASSISTANT_TAG=main" in (install / ".env").read_text()
    assert not Path(rig["env"]["PIN_STATE_FILE"]).exists()


def test_hosted_update_migrates_an_old_compose_file(rig, tmp_path):
    # A device imaged in July keeps the compose file it was imaged with; the
    # update brings it to the current defaults with the shipped helper
    # (FoodAssistant-dlakt), keeping a backup, and says so in its log.
    helper = ROOT / "scripts" / "image-build" / "foodassistant-compose-migrate"
    dest = rig["work"] / "scripts" / "image-build" / "foodassistant-compose-migrate"
    shutil.copy(helper, dest)
    _git(rig["work"], "add", "-A")
    _git(rig["work"], "commit", "-m", "add compose migrate helper")
    _git(rig["work"], "push", "origin", "main")
    install = _hosted_rig(rig, tmp_path)
    old = ROOT / "tests" / "data" / "compose-migrate" / "appliance-2026-07-16.yml"
    shutil.copy(old, install / "docker-compose.yml")
    rig["env"]["PATH"] = f"{_succeeding_docker(tmp_path)}:{rig['env']['PATH']}"

    result, out = run_update(rig)
    text = (install / "docker-compose.yml").read_text()
    assert '"--port", "9284", "--workers", "1", "--no-proxy-headers"]' in text
    assert '"${BESZEL_BIND:-127.0.0.1}:8090:8090"' in text
    assert '"${GROCY_BIND:-0.0.0.0}:9383:80"' in text
    assert [p.read_text() for p in install.glob("docker-compose.yml.bak-*")] == [old.read_text()]
    assert "Updated docker-compose.yml to the current defaults" in out
    assert (rig["bin"] / "foodassistant-compose-migrate").is_file()

    # The next update finds nothing left to change.
    result, out = run_update(rig)
    assert "Updated docker-compose.yml" not in out
    assert len(list(install.glob("docker-compose.yml.bak-*"))) == 1


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
    # The helper reads the verified export, not the checkout.
    assert f"gadgets-setup INSTALL_DIR={rig['env']['INSTALL_DIR']} REPO_DIR={rig['src']}" in log
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


# --- Which account joins the i2c group (FoodAssistant-92bcf) ------------------
# The reader does not always run as "foodassistant": on a device imaged from
# Raspberry Pi Imager it runs as whatever account the owner created. Adding a
# name that does not exist warns and leaves the bus unusable, so the answer
# comes from the installed unit.


def _i2c_units(rig, tmp_path, **units):
    """Point the unit lookup at a sandbox and write the given User= units."""
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir(exist_ok=True)
    rig["env"]["UNIT_DIR"] = str(unit_dir)
    rig["env"].pop("GD_USER", None)
    for name, user in units.items():
        (unit_dir / name).write_text(
            f"[Service]\nUser={user}\nExecStart=/bin/true\n")
    return unit_dir


def test_i2c_group_uses_the_user_from_the_gadgets_unit(rig, tmp_path):
    _i2c_rig(rig, tmp_path)
    _i2c_units(rig, tmp_path, **{"foodassistant-gadgets.service": "autopi"})
    result, out = run_update(rig)
    assert "usermod -aG i2c autopi" in _stub_log(rig)
    assert "Added autopi to the i2c group" in out
    assert "foodassistant to the i2c group" not in out


def test_i2c_group_skips_a_root_unit_and_takes_the_next_one(rig, tmp_path):
    # Root is in every group already and is not an account to add, so a
    # root-run reader must not stop the search.
    _i2c_rig(rig, tmp_path)
    _i2c_units(rig, tmp_path, **{
        "foodassistant-gadgets.service": "root",
        "foodassistant-streamdeck.service": "deckowner",
    })
    result, out = run_update(rig)
    assert "usermod -aG i2c deckowner" in _stub_log(rig)


def test_i2c_group_falls_back_when_no_unit_declares_a_user(rig, tmp_path):
    # No units and no uid 1000 (the rig's getent stub answers nothing): the
    # literal name is the last resort, not the first guess.
    _i2c_rig(rig, tmp_path)
    _i2c_units(rig, tmp_path)
    result, out = run_update(rig)
    assert "usermod -aG i2c foodassistant" in _stub_log(rig)


def test_i2c_group_still_honours_an_explicit_override(rig, tmp_path):
    _i2c_rig(rig, tmp_path)
    _i2c_units(rig, tmp_path, **{"foodassistant-gadgets.service": "autopi"})
    rig["env"]["GD_USER"] = "chosen"
    result, out = run_update(rig)
    assert "usermod -aG i2c chosen" in _stub_log(rig)


# -- Recovery hotspot services (PIHOST-V1, PIHOST-V2) --------------------------

AP_DNSMASQ_OLD = (
    "interface=wlan0\n"
    "dhcp-range=192.168.99.2,192.168.99.20,12h\n"
    "dhcp-option=3,192.168.99.1\n"
    "dhcp-option=6,192.168.99.1\n"
    "address=/#/192.168.99.1\n"
)


def _systemctl_state_stub(rig, tmp_path, *, enabled=None, active=()):
    """A systemctl stand-in that answers is-enabled / is-active from the given
    state, records every call to STUB_LOG, and fails anything else like the
    rig's default stub (so unrelated units stay untouched)."""
    log = tmp_path / "stub.log"
    log.write_text("")
    rig["env"]["STUB_LOG"] = str(log)
    enabled = enabled or {}
    cases = "".join(f'    {u}) echo {s}; exit 0 ;;\n' for u, s in enabled.items())
    active_units = " ".join(active)
    s = tmp_path / "stubs" / "systemctl"
    s.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'systemctl %s\\n' \"$*\" >> \"$STUB_LOG\"\n"
        'unit="${@: -1}"\n'
        'case "$1" in\n'
        "  is-enabled)\n"
        '    case "$unit" in\n'
        f"{cases}"
        "    esac\n"
        "    exit 1 ;;\n"
        "  is-active)\n"
        f'    for a in {active_units}; do [ "$a" = "$unit" ] && exit 0; done\n'
        "    exit 3 ;;\n"
        "  unmask|disable|stop|try-restart) exit 0 ;;\n"
        "esac\n"
        "exit 1\n")
    s.chmod(0o755)
    return log


def _ap_rig(rig, tmp_path, *, dnsmasq_text=AP_DNSMASQ_OLD, hostapd=True, flag=False):
    conf = tmp_path / "foodassistant-ap.conf"
    if dnsmasq_text is not None:
        conf.write_text(dnsmasq_text)
    hostapd_conf = tmp_path / "hostapd.conf"
    if hostapd:
        hostapd_conf.write_text("interface=wlan0\nssid=FoodAssistant\n")
    ap_flag = tmp_path / "ap-active"
    if flag:
        ap_flag.write_text("")
    rig["env"].update({
        "AP_DNSMASQ_CONF": str(conf),
        "HOSTAPD_CONF": str(hostapd_conf),
        "AP_FLAG": str(ap_flag),
    })
    return conf


def test_masked_hostapd_is_unmasked_and_services_are_off_at_boot(rig, tmp_path):
    # A device provisioned before the fix: hostapd masked by its postinst,
    # dnsmasq enabled and running with the wildcard bind.
    conf = _ap_rig(rig, tmp_path)
    log = _systemctl_state_stub(
        rig, tmp_path,
        enabled={"hostapd.service": "masked", "dnsmasq.service": "enabled"},
        active=("dnsmasq.service",))
    result, out = run_update(rig)
    calls = log.read_text()
    assert "systemctl unmask hostapd.service" in calls
    assert "systemctl disable dnsmasq.service" in calls
    assert "systemctl stop dnsmasq.service" in calls
    # Masked is not enabled, so there is nothing to disable for hostapd.
    assert "systemctl disable hostapd.service" not in calls
    assert "systemctl mask" not in calls
    assert "Unmasked hostapd" in out
    assert "Turned off dnsmasq.service at boot" in out
    assert "Stopped dnsmasq" in out
    text = conf.read_text()
    assert text.startswith(AP_DNSMASQ_OLD)
    assert "\nbind-interfaces\nexcept-interface=lo\n" in text


def test_enabled_hostapd_is_turned_off_at_boot(rig, tmp_path):
    _ap_rig(rig, tmp_path)
    log = _systemctl_state_stub(
        rig, tmp_path,
        enabled={"hostapd.service": "enabled", "dnsmasq.service": "disabled"})
    run_update(rig)
    calls = log.read_text()
    assert "systemctl disable hostapd.service" in calls
    assert "systemctl unmask" not in calls
    assert "systemctl disable dnsmasq.service" not in calls
    assert "systemctl stop dnsmasq.service" not in calls


def test_hotspot_services_left_running_while_the_hotspot_is_up(rig, tmp_path):
    _ap_rig(rig, tmp_path, flag=True)
    log = _systemctl_state_stub(
        rig, tmp_path,
        enabled={"hostapd.service": "masked", "dnsmasq.service": "enabled"},
        active=("dnsmasq.service",))
    run_update(rig)
    calls = log.read_text()
    # The unmask is always safe; stopping dnsmasq would cut the hotspot off.
    assert "systemctl unmask hostapd.service" in calls
    assert "systemctl disable" not in calls
    assert "systemctl stop dnsmasq.service" not in calls
    # The new bind applies to the running hotspot straight away.
    assert "systemctl try-restart dnsmasq.service" in calls


def test_hotspot_retrofit_is_idempotent(rig, tmp_path):
    conf = _ap_rig(rig, tmp_path)
    _systemctl_state_stub(
        rig, tmp_path,
        enabled={"hostapd.service": "disabled", "dnsmasq.service": "disabled"})
    run_update(rig)
    run_update(rig)
    assert conf.read_text().count("bind-interfaces") == 1


def test_hotspot_retrofit_skipped_without_the_pantry_raider_hotspot(rig, tmp_path):
    # A machine with no Pantry Raider hotspot config may run its own dnsmasq
    # or hostapd; neither is touched.
    _ap_rig(rig, tmp_path, dnsmasq_text=None)
    log = _systemctl_state_stub(
        rig, tmp_path,
        enabled={"hostapd.service": "masked", "dnsmasq.service": "enabled"},
        active=("dnsmasq.service",))
    run_update(rig)
    calls = log.read_text()
    assert "hostapd" not in calls
    assert "dnsmasq" not in calls


# -- Port 80 redirect scoped to this device (PIHOST-V3) ------------------------

FIRSTBOOT = SCRIPT.parent / "firstboot.sh"

OLD_PORT80_UNIT = """[Unit]
Description=FoodAssistant port 80 -> 9284 redirect (pi_hosted)
After=network-pre.target
Wants=network-pre.target
Before=network.target

[Service]
Type=oneshot
RemainAfterExit=yes
# Idempotent: only add each rule if it is not already present.
ExecStart=/bin/sh -c 'iptables -t nat -C PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 9284 2>/dev/null || iptables -t nat -A PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 9284'
ExecStart=/bin/sh -c 'iptables -t nat -D OUTPUT -p tcp --dport 80 -j REDIRECT --to-port 9284 2>/dev/null; iptables -t nat -C OUTPUT -o lo -p tcp --dport 80 -j REDIRECT --to-port 9284 2>/dev/null || iptables -t nat -A OUTPUT -o lo -p tcp --dport 80 -j REDIRECT --to-port 9284'

[Install]
WantedBy=multi-user.target
"""


def _firstboot_port80_unit():
    env = {**os.environ, "FIRSTBOOT_SOURCE_ONLY": "1", "DRY_RUN": "1"}
    r = subprocess.run(["bash", "-c", f'source "{FIRSTBOOT}"; port80_unit_text'],
                       env=env, capture_output=True, text=True, check=True)
    return r.stdout


def test_updater_port80_unit_matches_firstboot():
    """The retrofit must write exactly what a fresh provision writes."""
    r = subprocess.run(
        ["bash", "-c",
         f"eval \"$(awk '/^port80_unit_text\\(\\)/,/^}}/' '{SCRIPT}')\"; port80_unit_text"],
        capture_output=True, text=True, check=True)
    assert r.stdout == _firstboot_port80_unit()
    assert "--dst-type LOCAL" in r.stdout


def _port80_rig(rig, tmp_path, unit_text=OLD_PORT80_UNIT):
    unit = tmp_path / "foodassistant-port80.service"
    unit.write_text(unit_text)
    rig["env"]["PORT80_UNIT"] = str(unit)
    log = tmp_path / "stub.log"
    log.write_text("")
    rig["env"]["STUB_LOG"] = str(log)
    for name, rc in (("systemctl", 0), ("iptables", 1)):
        s = tmp_path / "stubs" / name
        s.write_text("#!/usr/bin/env bash\n"
                     f"printf '{name} %s\\n' \"$*\" >> \"$STUB_LOG\"\n"
                     f"exit {rc}\n")
        s.chmod(0o755)
    return unit, log


def test_unscoped_port80_unit_is_rewritten_and_reapplied(rig, tmp_path):
    unit, log = _port80_rig(rig, tmp_path)
    result, out = run_update(rig)
    assert unit.read_text() == _firstboot_port80_unit()
    calls = log.read_text()
    assert ("iptables -t nat -D PREROUTING -p tcp --dport 80 -j REDIRECT "
            "--to-port 9284") in calls
    assert "systemctl daemon-reload" in calls
    assert "systemctl restart foodassistant-port80.service" in calls
    assert "Port 80 now redirects only traffic addressed to this device" in out


def test_scoped_port80_unit_is_left_alone(rig, tmp_path):
    unit, log = _port80_rig(rig, tmp_path, unit_text=_firstboot_port80_unit())
    before = unit.stat().st_mtime_ns
    result, out = run_update(rig)
    assert unit.stat().st_mtime_ns == before
    assert "foodassistant-port80.service" not in log.read_text()
    assert "Port 80 now redirects" not in out


# -- Docker path: saved REPO_DIR and accurate wording (GROCY-9, so5s9) ---------


def test_hosted_update_records_repo_dir_once(rig, tmp_path):
    install = _hosted_rig(rig, tmp_path)
    rig["env"]["PATH"] = f"{_succeeding_docker(tmp_path)}:{rig['env']['PATH']}"
    run_update(rig)
    text = (install / ".env").read_text()
    assert f"\nREPO_DIR={rig['device']}\n" in text
    # The main channel follows :main ('latest' moves only on signed releases).
    assert text.startswith("TZ=UTC\nFOODASSISTANT_TAG=main\n")
    run_update(rig)
    assert (install / ".env").read_text().count("REPO_DIR=") == 1


def test_hosted_update_keeps_an_existing_repo_dir(rig, tmp_path):
    install = _hosted_rig(rig, tmp_path)
    (install / ".env").write_text("TZ=UTC\nREPO_DIR=/custom/src\n")
    rig["env"]["PATH"] = f"{_succeeding_docker(tmp_path)}:{rig['env']['PATH']}"
    run_update(rig)
    text = (install / ".env").read_text()
    assert "REPO_DIR=/custom/src" in text
    assert text.count("REPO_DIR=") == 1


def test_docker_update_log_names_what_it_did(rig, tmp_path):
    # Any Docker install goes through this path (a server too), so the log
    # says what happened to the image, not "Pi Hosted".
    _hosted_rig(rig, tmp_path)
    rig["env"]["PATH"] = f"{_succeeding_docker(tmp_path)}:{rig['env']['PATH']}"
    result, out = run_update(rig)
    assert "Pi Hosted" not in out
    assert "Docker deployment detected at" in out
    assert "Service image already up to date (same, streamdeck=" in out


def test_docker_update_log_shows_old_and_new_image(rig, tmp_path):
    _hosted_rig(rig, tmp_path)
    d = tmp_path / "docker-new"
    d.mkdir()
    count = tmp_path / "images-calls"
    stub = d / "docker"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do case "$a" in images)\n'
        f'  n=$(cat "{count}" 2>/dev/null || echo 0); echo $((n + 1)) > "{count}"\n'
        '  if [ "$n" = 0 ]; then echo sha256:aaaaaaaaaaaaaaaaaaaa; else echo sha256:bbbbbbbbbbbbbbbbbbbb; fi\n'
        "  exit 0;; esac; done\n"
        "exit 0\n")
    stub.chmod(0o755)
    rig["env"]["PATH"] = f"{d}:{rig['env']['PATH']}"
    result, out = run_update(rig)
    assert "Service image refreshed (aaaaaaaaaaaa -> bbbbbbbbbbbb" in out
    assert result["service_synced"] is True


# -- Release trust (R2): signed targets, freshness, staging, records -----------

def _deploy(rig):
    """Give the rig a venv-layout app dir so a run deploys for real (ok true,
    service synced, STATE_FILE written)."""
    app = rig["tmp"] / "app"
    app.mkdir(exist_ok=True)
    rig["env"]["APP_DIR"] = str(app)
    return app


def _state(rig):
    return json.loads(rig["state"].read_text())


def _write_state(rig, highest):
    rig["state"].parent.mkdir(parents=True, exist_ok=True)
    rig["state"].write_text(json.dumps({"highest_version": highest}))


def _unsigned_commit(rig, marker, path="scripts/image-build/foodassistant-set-rotation",
                     body=None):
    f = rig["work"] / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body if body is not None else f"#!/usr/bin/env bash\n# {marker}\n")
    _git(rig["work"], "add", "-A")
    _git(rig["work"], "-c", "commit.gpgsign=false", "commit", "-m", marker)
    _git(rig["work"], "push", "origin", "main")
    return _full_head(rig["work"])


def _tree_files(root):
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*"))


def test_unsigned_newest_tag_is_skipped(rig):
    _tag_head(rig, "v0.1.0")
    good = _full_head(rig["work"])
    _set_version(rig["work"], "0.2.0")
    _git(rig["work"], "add", "-A")
    _git(rig["work"], "commit", "-m", "next")
    _git(rig["work"], "-c", "tag.gpgsign=false", "tag", "-a", "-m", "v0.2.0", "v0.2.0")
    _git(rig["work"], "push", "origin", "main", "v0.2.0")
    rig["env"]["UPDATE_CHANNEL"] = "stable"
    result, out = run_update(rig)
    assert "v0.2.0 is not signed by the Pantry Raider release key; skipped." in out
    assert _full_head(rig["device"]) == good
    assert result["target"] == "0.1.0"
    assert result["verified"] is True


def test_a_tag_ref_moved_onto_an_older_signed_tag_is_skipped(rig):
    _tag_head(rig, "v0.1.0")
    good = _full_head(rig["work"])
    tag_obj = _git(rig["work"], "rev-parse", "refs/tags/v0.1.0").stdout.strip()
    _git(rig["work"], "push", "origin", f"{tag_obj}:refs/tags/v0.3.0")
    rig["env"]["UPDATE_CHANNEL"] = "stable"
    result, out = run_update(rig)
    assert "v0.3.0 names another tag; skipped." in out
    assert _full_head(rig["device"]) == good
    assert result["target"] == "0.1.0"


def test_a_tag_whose_tree_has_another_version_is_skipped(rig):
    _tag_head(rig, "v0.1.0")
    good = _full_head(rig["work"])
    _advance_main(rig, "not a release")
    _git(rig["work"], "tag", "-s", "-m", "v0.3.0", "v0.3.0")  # tree still says 0.1.0
    _git(rig["work"], "push", "origin", "v0.3.0")
    rig["env"]["UPDATE_CHANNEL"] = "stable"
    result, out = run_update(rig)
    assert "v0.3.0 does not match its version; skipped." in out
    assert _full_head(rig["device"]) == good


def test_a_release_below_the_highest_installed_holds(rig):
    _tag_head(rig, "v0.2.0")
    _write_state(rig, "0.3.0")
    before = _full_head(rig["device"])
    rig["env"]["UPDATE_CHANNEL"] = "stable"
    result, out = run_update(rig)
    assert result["ok"] is True
    assert result["error"] == ""
    assert result["hold_reason"] == (
        "This device is on 0.3.0; the newest signed release is 0.2.0. "
        "It moves with the next release.")
    assert _full_head(rig["device"]) == before
    assert list(rig["bin"].iterdir()) == []
    assert not Path(rig["env"]["STAGE_ROOT"]).exists()
    assert _state(rig) == {"highest_version": "0.3.0"}


def test_the_floor_is_the_running_app_not_the_checkout(rig):
    # No state file: the running version is the floor, even when the
    # checkout already sits on something newer.
    _tag_head(rig, "v0.2.0")
    rig["env"]["FA_RUNNING_VERSION"] = "0.4.0"
    rig["env"]["UPDATE_CHANNEL"] = "stable"
    result, out = run_update(rig)
    assert result["ok"] is True
    assert "This device is on 0.4.0" in result["hold_reason"]


def test_a_pinned_release_below_the_highest_deploys(rig):
    _deploy(rig)
    _tag_head(rig, "v0.2.0")
    pinned = _full_head(rig["work"])
    _write_state(rig, "0.3.0")
    _pin(rig, "0.2.0\n")
    rig["env"]["UPDATE_CHANNEL"] = "stable"
    result, out = run_update(rig)
    assert result["ok"] is True, out
    assert result["hold_reason"] == ""
    assert "Moving down to the pinned release v0.2.0" in out
    assert _full_head(rig["device"]) == pinned
    state = _state(rig)
    assert state["highest_version"] == "0.3.0"
    assert state["sha"] == pinned
    assert state["channel"] == "stable"


def test_an_unsigned_main_tip_is_refused(rig):
    before = _full_head(rig["device"])
    _unsigned_commit(rig, "unsigned work")
    result, out = run_update(rig)
    # Shown in Settings as the reason this device waits; the channel is
    # called Every change there, so the text does not name it.
    msg = ("The newest change is not signed by the Pantry Raider release key; "
           "this device stays on 0.0.1.")
    assert result["ok"] is False
    assert result["error"] == msg
    assert result["hold_reason"] == msg
    assert result["verified"] is False
    assert _full_head(rig["device"]) == before
    assert list(rig["bin"].iterdir()) == []


def test_a_signed_tip_deploys_from_the_export_not_the_checkout(rig):
    app = _deploy(rig)
    tip = _advance_main(rig, "signed work")
    stray = rig["device"] / "scripts" / "image-build" / "foodassistant-stray"
    stray.write_text("#!/usr/bin/env bash\n# dropped into the checkout\n")
    (rig["device"] / "service" / "app" / "stray.py").write_text("# not released\n")
    result, out = run_update(rig)
    assert result["ok"] is True, out
    assert result["verified"] is True
    assert _full_head(rig["device"]) == tip
    assert not (rig["bin"] / "foodassistant-stray").exists()
    assert not (app / "app" / "stray.py").exists()
    assert (app / "app" / "config.py").is_file()
    assert "# signed work" in (rig["bin"] / "foodassistant-set-rotation").read_text()
    current = rig["src"].resolve()
    assert current.name == f"0.0.1-{tip[:7]}"
    # The export is the committed tree only.
    assert "scripts/image-build/foodassistant-stray" not in _tree_files(current)


def test_old_exports_are_pruned_to_current_and_previous(rig):
    _deploy(rig)
    names = []
    for i in range(3):
        tip = _advance_main(rig, f"change {i}")
        run_update(rig)
        names.append(f"0.0.1-{tip[:7]}")
    left = sorted(p.name for p in Path(rig["env"]["STAGE_ROOT"]).iterdir())
    assert left == sorted(["current", names[1], names[2]])


def test_the_updater_re_execs_only_after_verification(rig):
    old = rig["bin"] / "foodassistant-update"
    old.write_text("#!/usr/bin/env bash\n# installed updater\n")
    _unsigned_commit(
        rig, "unsigned updater", path="scripts/image-build/foodassistant-update",
        body="#!/usr/bin/env bash\nUPDATER_TRUST_GENERATION=1\necho REEXEC_MARKER\n")
    result, out = run_update(rig)
    assert "REEXEC_MARKER" not in out
    assert result["ok"] is False
    assert old.read_text() == "#!/usr/bin/env bash\n# installed updater\n"


def test_a_missing_channel_file_means_stable(rig):
    _tag_head(rig, "v0.1.0")
    tagged = _full_head(rig["work"])
    _advance_main(rig, "past the release")
    rig["env"].pop("UPDATE_CHANNEL")
    result, out = run_update(rig)
    assert "Update channel: stable" in out
    assert result["channel"] == "stable"
    assert _full_head(rig["device"]) == tagged


@pytest.mark.parametrize("text", ["", "\n", "beta\n"])
def test_an_empty_or_unknown_channel_file_means_stable(rig, text):
    Path(rig["env"]["CHANNEL_FILE"]).write_text(text)
    rig["env"].pop("UPDATE_CHANNEL")
    result, out = run_update(rig)
    assert "Update channel: stable" in out


def test_the_result_keeps_every_old_key_and_adds_the_trust_fields(rig):
    result, out = run_update(rig)
    old_keys = {"ok", "error", "before", "after", "restarted", "service_synced",
                "streamdeck_synced", "deps_installed", "remote_recovered",
                "reboot_needed"}
    new_keys = {"verified", "target", "channel", "trust_fingerprint", "hold_reason"}
    assert set(result) == old_keys | new_keys
    assert result["verified"] is True
    assert result["target"] == "0.0.1"
    assert result["channel"] == "main"
    assert result["trust_fingerprint"].startswith("SHA256:")


def test_a_git_without_ssh_signature_support_still_verifies(rig, tmp_path):
    # Bullseye's git cannot verify SSH signatures; the updater never asks it to.
    shim_dir = tmp_path / "old-git"
    shim_dir.mkdir()
    real_git = shutil.which("git")
    shim = shim_dir / "git"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do case "$a" in verify-tag|verify-commit) '
        'echo "unsupported" >&2; exit 1;; esac; done\n'
        f'exec "{real_git}" "$@"\n')
    shim.chmod(0o755)
    _deploy(rig)
    _tag_head(rig, "v0.1.0")
    rig["env"]["PATH"] = f"{shim_dir}:{rig['env']['PATH']}"
    rig["env"]["UPDATE_CHANNEL"] = "stable"
    result, out = run_update(rig)
    assert result["ok"] is True, out
    assert result["verified"] is True
    assert result["target"] == "0.1.0"


def test_allow_unsigned_once_installs_an_unsigned_tip(rig):
    _deploy(rig)
    tip = _unsigned_commit(rig, "unsigned fix")
    result, out = run_update(rig, "--allow-unsigned-once")
    assert result["ok"] is True, out
    assert result["verified"] is False
    assert "UNVERIFIED: installing" in out
    assert _full_head(rig["device"]) == tip


def test_allow_unsigned_once_never_installs_a_list_from_the_tree(rig, signing):
    rig["trust"].unlink()
    _unsigned_commit(rig, "unsigned keys", path="scripts/release-signers",
                     body=signing["k2"]["line"])
    result, out = run_update(rig, "--allow-unsigned-once")
    assert rig["trust"].read_text() == signing["k1"]["line"]


def test_an_unknown_option_is_refused(rig):
    before = _full_head(rig["device"])
    result, out = run_update(rig, "--force")
    assert result["ok"] is False
    assert result["error"] == (
        "Unknown option --force. The only option is --allow-unsigned-once.")
    assert _full_head(rig["device"]) == before
    assert list(rig["bin"].iterdir()) == []


def test_not_enough_space_to_stage_holds(rig):
    rig["env"]["FA_STAGE_MIN_MB"] = "999999999"
    result, out = run_update(rig)
    assert result["ok"] is False
    assert result["hold_reason"] == (
        "There is not enough free space on this device to stage the update "
        "(about 200 MB is needed).")
    assert list(rig["bin"].iterdir()) == []


# -- Bootstrap and rotation -------------------------------------------------------

def test_bootstrap_installs_the_anchor_after_a_signed_target(rig, signing):
    _deploy(rig)
    rig["trust"].unlink()
    result, out = run_update(rig)
    assert result["ok"] is True, out
    assert rig["trust"].read_text() == signing["k1"]["line"]
    assert oct(rig["trust"].stat().st_mode & 0o777) == "0o644"
    fp = result["trust_fingerprint"]
    assert f"Update signing is set up on this device (release key {fp})." in out
    state = _state(rig)
    assert state["bootstrapped_at"]
    assert state["trust_fp"] == fp
    assert state["highest_version"] == "0.0.1"
    # The next run keeps the bootstrap time.
    result, out = run_update(rig)
    assert _state(rig)["bootstrapped_at"] == state["bootstrapped_at"]
    assert "Update signing is set up" not in out


def test_bootstrap_holds_until_a_signed_release_exists(rig):
    rig["trust"].unlink()
    _set_version(rig["work"], "0.1.0")
    _git(rig["work"], "add", "-A")
    _git(rig["work"], "commit", "-m", "unsigned release")
    _git(rig["work"], "-c", "tag.gpgsign=false", "tag", "-a", "-m", "v0.1.0", "v0.1.0")
    _git(rig["work"], "push", "origin", "main", "v0.1.0")
    rig["env"]["UPDATE_CHANNEL"] = "stable"
    result, out = run_update(rig)
    assert result["ok"] is False
    assert result["hold_reason"] == (
        "Update signing could not be set up from this download; the device "
        "stays on 0.0.1 and will try again.")
    assert not rig["trust"].exists()
    assert list(rig["bin"].iterdir()) == []

    _deploy(rig)
    _tag_head(rig, "v0.2.0")
    result, out = run_update(rig)
    assert result["ok"] is True, out
    assert result["target"] == "0.2.0"
    assert rig["trust"].is_file()
    assert _state(rig)["bootstrapped_at"]


def test_rotation_trusts_a_backup_key_from_a_verified_release(rig, signing):
    _deploy(rig)
    rig["env"]["UPDATE_CHANNEL"] = "stable"
    signers = rig["work"] / "scripts" / "release-signers"
    signers.write_text("# Pantry Raider release keys\n" + signing["k1"]["line"]
                       + signing["k2"]["line"])
    _git(rig["work"], "add", "-A")
    _git(rig["work"], "commit", "-m", "add the backup key")
    _tag_head(rig, "v0.1.0")          # signed by key 1
    result, out = run_update(rig)
    assert result["ok"] is True, out
    assert rig["trust"].read_text() == signers.read_text()
    assert "," in result["trust_fingerprint"]
    assert "Installed the release-key list from release 0.1.0" in out

    _use_key(rig["work"], signing["k2"])
    _tag_head(rig, "v0.2.0")          # signed by key 2
    result, out = run_update(rig)
    assert result["ok"] is True, out
    assert result["target"] == "0.2.0"
    assert result["verified"] is True


def test_a_release_signed_by_an_untrusted_key_is_refused(rig, signing):
    before = _full_head(rig["device"])
    _use_key(rig["work"], signing["k2"])
    _advance_main(rig, "signed by a key the device does not trust")
    result, out = run_update(rig)
    assert result["ok"] is False
    assert "not signed by the Pantry Raider release key" in result["error"]
    assert _full_head(rig["device"]) == before


def test_a_pinned_move_down_keeps_the_newer_key_list(rig, signing):
    # The device already trusts a backup key a later release added; pinning it
    # back to a release whose list predates that key keeps the installed list.
    _deploy(rig)
    both = signing["k1"]["line"] + signing["k2"]["line"]
    rig["trust"].write_text(both)
    (rig["work"] / "scripts" / "release-signers").write_text(signing["k1"]["line"])
    _git(rig["work"], "add", "-A")
    _git(rig["work"], "commit", "-m", "the older key list")
    _tag_head(rig, "v0.2.0")
    _write_state(rig, "0.3.0")
    _pin(rig, "0.2.0\n")
    rig["env"]["UPDATE_CHANNEL"] = "stable"
    result, out = run_update(rig)
    assert result["ok"] is True, out
    assert result["target"] == "0.2.0"
    assert rig["trust"].read_text() == both
    assert ("Kept the installed release-key list: release 0.2.0 is older than one "
            "this device already ran.") in out


@pytest.mark.parametrize("tree_signers", [None, "# keys move here in a later release\n\n"])
def test_a_release_without_a_key_list_keeps_the_installed_one(rig, tree_signers):
    _deploy(rig)
    if tree_signers is not None:
        (rig["work"] / "scripts" / "release-signers").write_text(tree_signers)
        _git(rig["work"], "add", "-A")
        _git(rig["work"], "commit", "-m", "comment-only key list")
    _tag_head(rig, "v0.1.0")
    _advance_main(rig, "newer")
    _tag_head(rig, "v0.2.0")
    _pin(rig, "0.1.0\n")
    rig["env"]["UPDATE_CHANNEL"] = "stable"
    trust_before = rig["trust"].read_bytes()
    result, out = run_update(rig)
    assert result["ok"] is True, out
    assert result["target"] == "0.1.0"
    assert rig["trust"].read_bytes() == trust_before
    assert "This release carries no release-key list; keeping the installed one." in out


# -- The updater's own generation -------------------------------------------------

def test_an_older_updater_is_never_put_back(rig):
    # The no-tag-yet case: the newest signed release is the running one and
    # carries the pre-0.21 updater, which cannot check signatures.
    _deploy(rig)
    current = rig["bin"] / "foodassistant-update"
    current.write_bytes(SCRIPT.read_bytes())
    current.chmod(0o755)
    shutil.copyfile(OLD_UPDATER, rig["work"] / "scripts" / "image-build" / "foodassistant-update")
    _git(rig["work"], "add", "-A")
    _git(rig["work"], "commit", "-m", "the 0.20.2 tree")
    _tag_head(rig, "v0.20.2")
    rig["env"]["FA_RUNNING_VERSION"] = "0.20.2"
    rig["env"]["UPDATE_CHANNEL"] = "stable"
    result, out = run_update(rig)
    assert result["ok"] is True, out
    assert result["target"] == "0.20.2"
    assert ("Kept the current updater: the release being installed carries one "
            "that cannot check signatures.") in out
    assert current.read_bytes() == SCRIPT.read_bytes()
    assert "Updater refreshed itself" not in out


def test_the_old_updater_hands_over_to_this_one(rig, signing):
    # Compatibility hop: a device on the 0.20 updater pulls the 0.21 tree
    # unverified, installs this updater and re-execs it; this one sets up
    # update signing, verifies the tip and deploys it from the export.
    app = _deploy(rig)
    ib = rig["work"] / "scripts" / "image-build"
    shutil.copyfile(OLD_UPDATER, ib / "foodassistant-update")
    _git(rig["work"], "add", "-A")
    _git(rig["work"], "commit", "-m", "A: the 0.20 tree")
    _git(rig["work"], "push", "origin", "main")
    subprocess.run(["git", "pull", "-q", "--ff-only"], cwd=rig["device"],
                   check=True, capture_output=True)
    old = rig["bin"] / "foodassistant-update"
    shutil.copyfile(OLD_UPDATER, old)
    old.chmod(0o755)

    shutil.copyfile(SCRIPT, ib / "foodassistant-update")
    _set_version(rig["work"], "0.21.0")
    _git(rig["work"], "add", "-A")
    _git(rig["work"], "commit", "-m", "B: 0.21.0")
    _git(rig["work"], "push", "origin", "main")
    tip = _full_head(rig["work"])
    rig["trust"].unlink()

    result, out = run_update(rig, script=old)
    assert "Updater refreshed itself" in out
    assert result["ok"] is True, out
    assert result["verified"] is True
    assert result["target"] == "0.21.0"
    assert old.read_bytes() == SCRIPT.read_bytes()
    assert rig["trust"].read_text() == signing["k1"]["line"]
    state = _state(rig)
    assert state["highest_version"] == "0.21.0"
    assert state["sha"] == tip
    assert state["bootstrapped_at"]
    assert 'APP_VERSION = "0.21.0"' in (app / "app" / "config.py").read_text()


def test_embedded_signers_match_the_release_key_list():
    text = SCRIPT.read_text()
    m = re.search(r"<<'SIGNERS' \|\| true\n(.*?)\nSIGNERS\n", text, re.S)
    assert m

    def keys(s):
        return [l.strip() for l in s.splitlines()
                if l.strip() and not l.strip().startswith("#")]
    assert keys(m.group(1)) == keys((ROOT / "scripts" / "release-signers").read_text())
    assert keys(m.group(1))


def test_the_updater_declares_its_trust_generation():
    lines = SCRIPT.read_text().splitlines()
    assert "UPDATER_TRUST_GENERATION=1" in lines
    assert "MANIFEST_REQUIRED_AFTER='0.21.0'" in lines


def test_the_updater_has_no_em_dash():
    assert "\u2014" not in SCRIPT.read_text()


def _shell_function(name):
    """The text of one top-level bash function in the updater."""
    text = SCRIPT.read_text()
    start = text.index(f"\n{name}() {{\n") + 1
    end = text.index("\n}\n", start) + 3
    return text[start:end]


VCMP_PAIRS = [
    ("0.2.0", "0.1.0"), ("0.1.0", "0.2.0"), ("1.2.3", "1.2.3"),
    ("v1.2.3", "1.2.3"), (" 0.10.0\n", "0.9.9"), ("0.9.10", "0.9.9"),
    ("0.0.010", "0.0.9"), ("00.1.0", "0.1.0"), ("12345678901234567890.0.0", "9.0.0"),
    ("1.2", "1.2.0"), ("1.2.3.4", "1.2.3"), ("", "1.0.0"), ("x.y.z", "1.0.0"),
    ("1.2.3-rc1", "1.2.3"), ("vv1.2.3", "1.2.3"), ("1.0.0", "-1.0.0"),
]


def test_the_shell_version_compare_matches_the_verifier():
    """_vcmp runs in plain bash; it must answer exactly as the verifier's
    vcmp subcommand it replaced (-1, 0 or 1, nothing when unparseable)."""
    text = SCRIPT.read_text()
    pyverify = text.split("<<'PYVERIFY' || true\n", 1)[1].split("\nPYVERIFY\n", 1)[0]
    funcs = _shell_function("_vsplit") + _shell_function("_vcmp")
    for a, b in VCMP_PAIRS:
        r = subprocess.run(["python3", "-c", pyverify, "vcmp", a, b],
                           capture_output=True, text=True)
        d = json.loads(r.stdout)
        want = str(d["result"]) if d["ok"] else ""
        sh = subprocess.run(["bash", "-c", funcs + '\n_vcmp "$1" "$2"', "vcmp", a, b],
                            capture_output=True, text=True)
        assert sh.stdout.strip() == want, (a, b, sh.stdout, sh.stderr)


def test_the_updater_starts_few_interpreters(rig, tmp_path):
    """Every check used to start python3 several times (once for the check,
    once per field read). A plain deploy now needs only a handful."""
    _deploy(rig)
    _advance_main(rig, "count")
    shim = tmp_path / "py-count"
    shim.mkdir()
    log = tmp_path / "python3.log"
    real = shutil.which("python3")
    (shim / "python3").write_text(
        f'#!/usr/bin/env bash\necho run >> "{log}"\nexec "{real}" "$@"\n')
    (shim / "python3").chmod(0o755)
    rig["env"]["PATH"] = f"{shim}:{rig['env']['PATH']}"
    result, out = run_update(rig)
    assert result["ok"] is True, out
    assert result["verified"] is True
    assert len(log.read_text().splitlines()) <= 6


# -- Docker installs: the signed release record and the image pin ------------

def _docker_shim(tmp_path, pull_ok=True):
    """A docker stand-in that records every call (and the REPO_DIR it saw)."""
    d = tmp_path / "docker-shim"
    d.mkdir(exist_ok=True)
    log = tmp_path / "docker.log"
    log.write_text("")
    stub = d / "docker"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "docker %s REPO_DIR=%s\\n" "$*" "${{REPO_DIR:-}}" >> "{log}"\n'
        + ('case "$*" in *"compose pull"*) echo "manifest unknown" >&2; exit 1;; esac\n'
           if not pull_ok else "")
        + 'for a in "$@"; do case "$a" in images) echo sha256:same; exit 0;; esac; done\n'
        "exit 0\n")
    stub.chmod(0o755)
    return d, log


def _publish_record(rig, version, commit, key, **over):
    doc = {
        "format": 1, "version": version, "tag": "v" + version, "commit": commit,
        "image": {"name": "ghcr.io/syracuse3dprintingorg/pantryraider",
                  "index_digest": "sha256:" + "c" * 64},
        "assets": {},
    }
    doc.update(over)
    d = rig["tmp"] / "published" / ("v" + version)
    d.mkdir(parents=True, exist_ok=True)
    rec = d / "release.json"
    rec.write_text(json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n")
    subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(key["key"]),
                    "-n", "pantryraider-release", str(rec)],
                   check=True, capture_output=True)
    return doc["image"]["index_digest"]


def _stable_hosted(rig, tmp_path, env_tag="latest", pull_ok=True):
    install = _hosted_rig(rig, tmp_path, env_tag=env_tag)
    shim, log = _docker_shim(tmp_path, pull_ok=pull_ok)
    rig["env"]["PATH"] = f"{shim}:{rig['env']['PATH']}"
    rig["env"]["UPDATE_CHANNEL"] = "stable"
    return install, log


def test_a_signed_record_pins_the_image_digest(rig, tmp_path, signing):
    _tag_head(rig, "v0.2.0")
    digest = _publish_record(rig, "0.2.0", _full_head(rig["work"]), signing["k1"])
    install, log = _stable_hosted(rig, tmp_path)
    result, out = run_update(rig)
    assert result["ok"] is True, out
    assert f"FOODASSISTANT_TAG=0.2.0@{digest}\n" in (install / ".env").read_text()
    assert _state(rig)["highest_version"] == "0.2.0"


def test_a_record_signed_by_another_key_is_not_used(rig, tmp_path, signing):
    _tag_head(rig, "v0.21.1")
    _publish_record(rig, "0.21.1", _full_head(rig["work"]), signing["k2"])
    install, log = _stable_hosted(rig, tmp_path)
    result, out = run_update(rig)
    assert result["ok"] is False
    assert result["hold_reason"].startswith("The signed record for release 0.21.1")


def test_a_record_for_another_commit_is_not_used(rig, tmp_path, signing):
    _tag_head(rig, "v0.2.0")
    _publish_record(rig, "0.2.0", "d" * 40, signing["k1"])
    install, log = _stable_hosted(rig, tmp_path)
    result, out = run_update(rig)
    assert result["ok"] is True, out
    assert "FOODASSISTANT_TAG=0.2.0\n" in (install / ".env").read_text()
    assert "image not digest-pinned (record commit)" in out


def test_0_21_0_without_a_record_pulls_by_version(rig, tmp_path):
    _tag_head(rig, "v0.21.0")
    install, log = _stable_hosted(rig, tmp_path)
    result, out = run_update(rig)
    assert result["ok"] is True, out
    assert "FOODASSISTANT_TAG=0.21.0\n" in (install / ".env").read_text()
    assert "image not digest-pinned" in out


def test_a_later_release_without_a_record_holds(rig, tmp_path):
    _tag_head(rig, "v0.21.1")
    install, log = _stable_hosted(rig, tmp_path)
    env_before = (install / ".env").read_text()
    head_before = _full_head(rig["device"])
    result, out = run_update(rig)
    msg = ("The signed record for release 0.21.1 is not published yet; this "
           "device stays on 0.0.1 and will try again.")
    assert result["ok"] is False
    assert result["error"] == msg and result["hold_reason"] == msg
    assert (install / ".env").read_text() == env_before
    assert "compose" not in log.read_text()
    assert list(rig["bin"].iterdir()) == []
    # The hold leaves the checkout and the current export where they were, and
    # the result reports no move.
    assert _full_head(rig["device"]) == head_before
    assert result["after"] == result["before"]
    assert not os.path.lexists(rig["src"])


@pytest.mark.parametrize("with_record", [True, False])
@pytest.mark.parametrize("env_tag", ["latest", None])
def test_a_failed_stable_pull_puts_the_tag_back_and_never_builds(
        rig, tmp_path, signing, with_record, env_tag):
    version = "0.2.0" if with_record else "0.21.0"
    _tag_head(rig, "v" + version)
    if with_record:
        _publish_record(rig, version, _full_head(rig["work"]), signing["k1"])
    install, log = _stable_hosted(rig, tmp_path, pull_ok=False)
    if env_tag is None:
        (install / ".env").write_text("TZ=UTC\n")
    result, out = run_update(rig)
    msg = (f"The app image for {version} is not available yet; this device "
           "stays on 0.0.1 and will try again.")
    assert result["ok"] is False
    assert result["error"] == msg and result["hold_reason"] == msg
    env_lines = (install / ".env").read_text().splitlines()
    tag_lines = [l for l in env_lines if l.startswith("FOODASSISTANT_TAG=")]
    assert tag_lines == ([] if env_tag is None else ["FOODASSISTANT_TAG=latest"])
    assert f"REPO_DIR={rig['device']}" in env_lines
    assert not Path(rig["env"]["PIN_STATE_FILE"]).exists()
    calls = log.read_text()
    assert "compose pull service" in calls
    assert "compose build" not in calls
    assert "compose up" not in calls
    assert not rig["state"].exists()


def test_a_failed_main_pull_builds_from_the_export(rig, tmp_path):
    _hosted_rig(rig, tmp_path)
    shim, log = _docker_shim(tmp_path, pull_ok=False)
    rig["env"]["PATH"] = f"{shim}:{rig['env']['PATH']}"
    result, out = run_update(rig)
    assert result["ok"] is True, out
    calls = log.read_text()
    assert f"docker compose build service REPO_DIR={rig['src']}" in calls
    assert f"docker compose up -d --no-deps service REPO_DIR={rig['device']}" in calls


def test_main_keeps_an_operator_image_tag(rig, tmp_path):
    install = _hosted_rig(rig, tmp_path, env_tag="0.19.4")
    rig["env"]["PATH"] = f"{_succeeding_docker(tmp_path)}:{rig['env']['PATH']}"
    run_update(rig)
    assert "FOODASSISTANT_TAG=0.19.4\n" in (install / ".env").read_text()


# -- Server installs, local credentials, kiosk last ----------------------------

def _apt_stubs(rig, tmp_path):
    log = tmp_path / "stub.log"
    log.write_text("")
    rig["env"]["STUB_LOG"] = str(log)
    _stub(rig, tmp_path, "apt-get", "exit 1\n")
    _stub(rig, tmp_path, "dpkg-query", "exit 1\n")
    return log


def test_a_server_install_skips_the_appliance_packages(rig, tmp_path):
    log = _apt_stubs(rig, tmp_path)
    Path(rig["env"]["SETTINGS_JSON"]).write_text(
        json.dumps({"deployment_mode": "server", "app_name": "x"}))
    result, out = run_update(rig)
    assert ("Server install: skipping the appliance-only packages "
            "(wireguard-tools, libheif-examples).") in out
    calls = log.read_text()
    assert "wireguard-tools" not in calls
    assert "libheif-examples" not in calls


@pytest.mark.parametrize("settings", [None, {"deployment_mode": "pi_hosted"}])
def test_an_appliance_keeps_the_package_retrofits(rig, tmp_path, settings):
    log = _apt_stubs(rig, tmp_path)
    if settings is not None:
        Path(rig["env"]["SETTINGS_JSON"]).write_text(json.dumps(settings))
    result, out = run_update(rig)
    assert "Server install" not in out
    calls = log.read_text()
    assert "wireguard-tools" in calls
    assert "libheif-examples" in calls


HOST_HARDEN_STUB = (
    "#!/usr/bin/env bash\n"
    'printf "host-harden %s\\n" "$*" >> "$STUB_LOG"\n'
    "echo 'local credentials checked'\n"
    'echo \'{"ok": true, "changed": ["kiosk-dropin"]}\'\n'
    "exit 3\n")


def _restart_order_rig(rig, tmp_path, harden=True):
    log = tmp_path / "stub.log"
    log.write_text("")
    rig["env"]["STUB_LOG"] = str(log)
    s = tmp_path / "stubs" / "systemctl"
    s.write_text("#!/usr/bin/env bash\n"
                 'printf "systemctl %s\\n" "$*" >> "$STUB_LOG"\n'
                 "exit 0\n")
    s.chmod(0o755)
    _add_streamdeck_package(rig)
    if harden:
        (rig["work"] / "scripts" / "image-build" / "foodassistant-host-harden").write_text(
            HOST_HARDEN_STUB)
        _git(rig["work"], "add", "-A")
        _git(rig["work"], "commit", "-m", "host-harden")
        _git(rig["work"], "push", "origin", "main")
    sd_parent = tmp_path / "deck"
    sd_parent.mkdir()
    rig["env"]["SD_DST"] = str(sd_parent / "foodassistant_streamdeck")
    return log


def _order(log, *needles):
    lines = log.read_text().splitlines()
    idx = []
    for n in needles:
        hits = [i for i, l in enumerate(lines) if n in l]
        assert hits, (n, lines)
        idx.append(hits[-1])
    return idx, lines


@pytest.mark.parametrize("layout", ["venv", "docker"])
def test_host_harden_runs_before_the_deck_and_the_kiosk_restarts_last(rig, tmp_path, layout):
    log = _restart_order_rig(rig, tmp_path)
    if layout == "venv":
        _deploy(rig)
    else:
        _hosted_rig(rig, tmp_path)
        rig["env"]["PATH"] = f"{_succeeding_docker(tmp_path)}:{rig['env']['PATH']}"
    result, out = run_update(rig)
    assert result["ok"] is True, out
    assert 'Local credentials: {"ok": true, "changed": ["kiosk-dropin"]}' in out
    (h, deck, kiosk), lines = _order(
        log, "host-harden local-credentials",
        "systemctl restart foodassistant-streamdeck.service",
        "systemctl restart foodassistant-kiosk.service")
    assert h < deck < kiosk
    restarts = [l for l in lines if l.startswith("systemctl restart foodassistant-")
                and "host-bridge" not in l]
    assert restarts[-1] == "systemctl restart foodassistant-kiosk.service"


def test_a_changed_kiosk_launch_restarts_the_kiosk_without_new_app_code(rig, tmp_path):
    # No app dir: nothing to sync for the web app, but host-harden changed the
    # kiosk's launch settings, so the kiosk restarts anyway.
    log = _restart_order_rig(rig, tmp_path)
    result, out = run_update(rig)
    assert result["service_synced"] is False
    assert "systemctl restart foodassistant-kiosk.service" in log.read_text()


def test_releases_without_host_harden_skip_it(rig, tmp_path):
    log = _restart_order_rig(rig, tmp_path, harden=False)
    harden = rig["bin"] / "foodassistant-host-harden"
    harden.write_text(HOST_HARDEN_STUB)
    harden.chmod(0o755)
    result, out = run_update(rig)
    assert "host-harden" not in log.read_text()
    assert "Local credentials" not in out


# -- REPO_DIR moves off the home checkout (FoodAssistant-e5z9q) ---------------
#
# An old device's .env can say REPO_DIR=/home/..., so a plain 'docker compose
# up' mounts the Grocy start-up hook from the desktop user's checkout. The
# updater points it at the root-owned source, but only when that is there.

_HOME_REPO = "/" + "home/foodassistant/FoodAssistant"


def _root_src(tmp_path, hook=True, mode=0o755, git=True, config=True):
    src = tmp_path / "root-src"
    src.mkdir()
    if hook:
        (src / "docker" / "grocy-init").mkdir(parents=True)
    if git:
        (src / ".git").mkdir()
    if config:
        (src / "service" / "app").mkdir(parents=True)
        (src / "service" / "app" / "config.py").write_text("APP_VERSION = '0.0.0'\n")
    src.chmod(mode)
    return src


def test_hosted_update_moves_repo_dir_off_home(rig, tmp_path):
    install = _hosted_rig(rig, tmp_path)
    (install / ".env").write_text(f"TZ=UTC\nREPO_DIR={_HOME_REPO}\nMY_SECRET=keep\n")
    src = _root_src(tmp_path)
    rig["env"]["ROOT_SRC_DIR"] = str(src)
    rig["env"]["ROOT_SRC_UID"] = str(os.getuid())
    rig["env"]["PATH"] = f"{_succeeding_docker(tmp_path)}:{rig['env']['PATH']}"
    result, out = run_update(rig)
    text = (install / ".env").read_text()
    assert f"\nREPO_DIR={src}\n" in text
    assert _HOME_REPO not in text
    assert "MY_SECRET=keep" in text and text.startswith("TZ=UTC\n")
    assert text.count("REPO_DIR=") == 1
    assert f"now points at {src}" in out


def test_quoted_home_repo_dir_moves_too(rig, tmp_path):
    install = _hosted_rig(rig, tmp_path)
    (install / ".env").write_text(f'TZ=UTC\nREPO_DIR="{_HOME_REPO}"\n')
    src = _root_src(tmp_path)
    rig["env"]["ROOT_SRC_DIR"] = str(src)
    rig["env"]["ROOT_SRC_UID"] = str(os.getuid())
    rig["env"]["PATH"] = f"{_succeeding_docker(tmp_path)}:{rig['env']['PATH']}"
    run_update(rig)
    assert f"\nREPO_DIR={src}\n" in (install / ".env").read_text()


@pytest.mark.parametrize("case", ["missing", "not-root", "writable", "no-hook",
                                  "no-git", "no-config", "docker-made"])
def test_home_repo_dir_stays_without_a_sound_root_source(rig, tmp_path, case):
    install = _hosted_rig(rig, tmp_path)
    (install / ".env").write_text(f"TZ=UTC\nREPO_DIR={_HOME_REPO}\n")
    if case == "missing":
        rig["env"]["ROOT_SRC_DIR"] = str(tmp_path / "absent")
        rig["env"]["ROOT_SRC_UID"] = str(os.getuid())
    else:
        # docker-made: the empty hook folder Docker creates for a missing
        # bind-mount source, with nothing else of a checkout around it.
        src = _root_src(tmp_path, hook=(case != "no-hook"),
                        mode=0o777 if case == "writable" else 0o755,
                        git=case not in ("no-git", "docker-made"),
                        config=case not in ("no-config", "docker-made"))
        rig["env"]["ROOT_SRC_DIR"] = str(src)
        rig["env"]["ROOT_SRC_UID"] = "0" if case == "not-root" else str(os.getuid())
        if case == "not-root" and os.getuid() == 0:
            pytest.skip("running as root")
    rig["env"]["PATH"] = f"{_succeeding_docker(tmp_path)}:{rig['env']['PATH']}"
    result, out = run_update(rig)
    assert f"\nREPO_DIR={_HOME_REPO}\n" in (install / ".env").read_text()
    assert "so it stays" in out


def test_repo_dir_outside_home_is_never_moved(rig, tmp_path):
    install = _hosted_rig(rig, tmp_path)
    (install / ".env").write_text("TZ=UTC\nREPO_DIR=/custom/src\n")
    src = _root_src(tmp_path)
    rig["env"]["ROOT_SRC_DIR"] = str(src)
    rig["env"]["ROOT_SRC_UID"] = str(os.getuid())
    rig["env"]["PATH"] = f"{_succeeding_docker(tmp_path)}:{rig['env']['PATH']}"
    run_update(rig)
    assert "\nREPO_DIR=/custom/src\n" in (install / ".env").read_text()


# -- One run at a time ------------------------------------------------------------

def test_a_second_run_steps_aside_while_one_is_running(rig):
    import fcntl
    _deploy(rig)
    _advance_main(rig, "while locked")
    lock = open(rig["env"]["UPDATE_LOCK_FILE"], "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result, out = run_update(rig)
    finally:
        lock.close()
    assert result["ok"] is True
    assert result["error"] == ""
    assert result["hold_reason"] == (
        "An update is already running on this device. It finishes on its own; "
        "check back in a few minutes.")
    # Nothing was staged, pruned or installed.
    assert not Path(rig["env"]["STAGE_ROOT"]).exists()
    assert list(rig["bin"].iterdir()) == []
    # Once the first run lets go, the next one goes through.
    result, out = run_update(rig)
    assert result["ok"] is True, out
    assert result["hold_reason"] == ""


def test_the_run_lock_is_released_when_a_run_ends(rig):
    import fcntl
    _deploy(rig)
    result, out = run_update(rig)
    assert result["ok"] is True, out
    with open(rig["env"]["UPDATE_LOCK_FILE"], "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if still held


def test_a_re_exec_keeps_the_lock(rig):
    """The refreshed updater re-execs with the lock descriptor still open; it
    must carry on under that lock instead of finding itself busy."""
    _deploy(rig)
    (rig["bin"] / "foodassistant-update").write_text("#!/usr/bin/env bash\n# old\n")
    (rig["work"] / "scripts" / "image-build" / "foodassistant-update").write_text(
        SCRIPT.read_text())
    _git(rig["work"], "add", "-A")
    _git(rig["work"], "commit", "-m", "ship the real updater")
    _git(rig["work"], "push", "origin", "main")
    result, out = run_update(rig)
    assert "re-running the new version" in out
    assert result["ok"] is True, out
    assert "already running" not in out


# -- Bootstrap that ends in a freshness hold ---------------------------------------

def test_a_bootstrap_that_holds_still_records_when_signing_was_set_up(rig, signing):
    rig["trust"].unlink()
    _tag_head(rig, "v0.2.0")
    _write_state(rig, "0.3.0")
    rig["env"]["UPDATE_CHANNEL"] = "stable"
    result, out = run_update(rig)
    assert result["ok"] is True
    assert "This device is on 0.3.0" in result["hold_reason"]
    assert rig["trust"].read_text() == signing["k1"]["line"]
    state = _state(rig)
    assert state["bootstrapped_at"]
    assert state["trust_fp"] == result["trust_fingerprint"]
    assert state["highest_version"] == "0.3.0"
    # A later run that installs keeps the time signing was set up.
    first = state["bootstrapped_at"]
    _deploy(rig)
    _tag_head(rig, "v0.4.0")
    result, out = run_update(rig)
    assert result["ok"] is True, out
    assert _state(rig)["bootstrapped_at"] == first


# -- The new version must answer; otherwise the old one goes back -----------------

def _health_rig(rig, tmp_path, broken=()):
    """A venv device whose app 'starts' when systemctl restarts it: the stub
    reads the version deployed in APP_DIR and writes the /health answer for it
    to a file the updater polls, unless that version is listed as broken."""
    app = _deploy(rig)
    health = tmp_path / "health.json"
    log = tmp_path / "calls.log"
    log.write_text("")
    stubs = tmp_path / "health-stubs"
    stubs.mkdir()
    (stubs / "systemctl").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "systemctl $*" >> "{log}"\n'
        'case "$1" in\n'
        '  list-unit-files) [ "$2" = "foodassistant-remote.service" ]; exit $? ;;\n'
        '  restart)\n'
        '    [ "$2" = "foodassistant-remote.service" ] || exit 1\n'
        f'    v="$(grep -oE "[0-9]+\\.[0-9]+\\.[0-9]+" "{app}/app/config.py" | head -n1)"\n'
        '    case " $FA_TEST_BROKEN " in *" $v "*) rm -f "$FA_TEST_HEALTH"; exit 0 ;; esac\n'
        '    printf \'{"app": "foodassistant", "version": "%s"}\' "${FA_TEST_ANSWER:-$v}" > "$FA_TEST_HEALTH"\n'
        '    exit 0 ;;\n'
        'esac\n'
        "exit 1\n")
    (stubs / "systemd-run").write_text(
        f'#!/usr/bin/env bash\necho "systemd-run $*" >> "{log}"\nexit 0\n')
    for s in stubs.iterdir():
        s.chmod(0o755)
    rig["env"].update({
        "PATH": f"{stubs}:{rig['env']['PATH']}",
        "FA_HEALTH_URL": health.as_uri(),
        "FA_HEALTH_TIMEOUT": "2",
        "FA_HEALTH_ROLLBACK_TIMEOUT": "2",
        "FA_TEST_HEALTH": str(health),
        "FA_TEST_BROKEN": " ".join(broken),
    })
    # The running version comes from the deployed app, as on a device.
    rig["env"].pop("FA_RUNNING_VERSION", None)
    return app, log


def _release_on_main(rig, version):
    _set_version(rig["work"], version)
    _git(rig["work"], "add", "-A")
    _git(rig["work"], "commit", "-m", f"version {version}")
    _git(rig["work"], "push", "origin", "main")
    return _full_head(rig["work"])


def _deployed_version(app):
    text = (app / "app" / "config.py").read_text()
    return re.search(r'APP_VERSION = "([^"]+)"', text).group(1)


def test_a_version_that_answers_is_kept(rig, tmp_path):
    app, log = _health_rig(rig, tmp_path)
    _release_on_main(rig, "0.0.2")
    result, out = run_update(rig)
    assert result["ok"] is True, out
    assert result["hold_reason"] == ""
    assert "The new version is up (0.0.2)." in out
    assert _deployed_version(app) == "0.0.2"
    assert "rollback_reason" not in _state(rig)


def test_a_version_that_does_not_start_goes_back_to_the_last_one(rig, tmp_path):
    app, log = _health_rig(rig, tmp_path, broken=("0.0.3",))
    good = _release_on_main(rig, "0.0.2")
    result, out = run_update(rig)
    assert result["ok"] is True, out
    before = result["after"]

    _release_on_main(rig, "0.0.3")
    result, out = run_update(rig)
    msg = ("Pantry Raider 0.0.3 did not start on this device within 2 seconds, "
           "so it went back to 0.0.2. It tries again when a newer version comes out, or when you press Update now.")
    assert result["ok"] is False, out
    assert result["error"] == msg and result["hold_reason"] == msg
    assert result["after"] == result["before"] == before
    assert _deployed_version(app) == "0.0.2"
    assert rig["src"].resolve().name == f"0.0.2-{good[:7]}"
    assert json.loads((tmp_path / "health.json").read_text())["version"] == "0.0.2"
    state = _state(rig)
    assert state["highest_version"] == "0.0.2"
    assert state["rollback_reason"] == msg
    assert state["rollback_version"] == "0.0.3"
    assert state["rolled_back_at"]
    assert log.read_text().count("systemctl restart foodassistant-remote.service") == 3

    # The next release that starts clears the note.
    _release_on_main(rig, "0.0.4")
    result, out = run_update(rig)
    assert result["ok"] is True, out
    assert _deployed_version(app) == "0.0.4"
    state = _state(rig)
    assert state["highest_version"] == "0.0.4"
    assert "rollback_reason" not in state


def test_going_back_puts_the_old_deck_package_back(rig, tmp_path):
    app, log = _health_rig(rig, tmp_path, broken=("0.0.3",))
    sd_parent = tmp_path / "deck"
    sd_parent.mkdir()
    rig["env"]["SD_DST"] = str(sd_parent / "foodassistant_streamdeck")
    _add_streamdeck_package(rig)
    _release_on_main(rig, "0.0.2")
    assert run_update(rig)[0]["ok"] is True
    (rig["work"] / "streamdeck" / "foodassistant_streamdeck" / "actions.py").write_text(
        "# deck for 0.0.3\n")
    _release_on_main(rig, "0.0.3")
    result, out = run_update(rig)
    assert result["ok"] is False
    actions = Path(rig["env"]["SD_DST"]) / "actions.py"
    assert "post_deck_confirmation" in actions.read_text()


def test_the_wrong_version_answering_counts_as_not_started(rig, tmp_path):
    # Whatever is restarted keeps answering as 0.0.1: neither the new
    # version nor the one before it comes up, and the hold says so.
    app, log = _health_rig(rig, tmp_path)
    rig["env"]["FA_TEST_ANSWER"] = "0.0.1"
    _release_on_main(rig, "0.0.2")
    rig["env"]["FA_HEALTH_TIMEOUT"] = "0"
    assert run_update(rig)[0]["ok"] is True
    rig["env"]["FA_HEALTH_TIMEOUT"] = "2"
    _release_on_main(rig, "0.0.3")
    result, out = run_update(rig)
    assert result["ok"] is False
    assert result["hold_reason"] == (
        "Pantry Raider 0.0.3 was installed, but version 0.0.1 answered instead, "
        "and going back to 0.0.2 did not work either. Restart the device; if "
        "Pantry Raider still does not open, the update log is at "
        "/var/log/foodassistant-update.log.")
    assert _state(rig)["rollback_reason"] == result["hold_reason"]
    assert _state(rig)["highest_version"] == "0.0.2"


def test_a_first_update_that_does_not_start_has_nothing_to_go_back_to(rig, tmp_path):
    app, log = _health_rig(rig, tmp_path, broken=("0.0.2",))
    _release_on_main(rig, "0.0.2")
    result, out = run_update(rig)
    assert result["ok"] is False
    assert result["hold_reason"] == (
        "Pantry Raider 0.0.2 did not start on this device within 2 seconds. "
        "Restart the device, then try the update again.")
    assert _state(rig)["highest_version"] != "0.0.2"


def test_the_bridge_restarts_only_after_the_new_version_answers(rig, tmp_path):
    # A new host bridge is installed during the run, but its restart (which
    # would end this run) is scheduled on the way out.
    app, log = _health_rig(rig, tmp_path)
    _release_on_main(rig, "0.0.2")
    result, out = run_update(rig)
    assert result["ok"] is True, out
    calls = log.read_text().splitlines()
    restart = calls.index("systemctl restart foodassistant-remote.service")
    bridge = [i for i, c in enumerate(calls) if c.startswith("systemd-run")]
    assert len(bridge) == 1
    assert "foodassistant-host-bridge.service" in calls[bridge[0]]
    assert bridge[0] > restart


@pytest.mark.parametrize("spent,cap,reserve,floor,want", [
    (0, 180, 180, 60, 180),     # plenty of time: the full wait
    (250, 180, 180, 60, 130),   # a slow pull: the wait shrinks to fit
    (400, 180, 180, 60, 60),    # nearly out of time: never under the floor
    (0, 2, 180, 60, 2),         # a cap below the floor wins
    (550, 120, 10, 30, 30),     # the switch back keeps its own floor
])
def test_the_wait_fits_inside_the_bridge_limit(spent, cap, reserve, floor, want):
    funcs = ('_budget_left() { echo $(( UPDATE_BUDGET - ($(date +%s) - RUN_STARTED_AT) )); }\n'
             + _shell_function("_wait_for"))
    assert "_budget_left() { echo $(( UPDATE_BUDGET - ($(date +%s) - RUN_STARTED_AT) )); }" \
        in SCRIPT.read_text()
    r = subprocess.run(
        ["bash", "-c", funcs + f"\nUPDATE_BUDGET=560\nRUN_STARTED_AT=$(( $(date +%s) - {spent} ))\n"
         f"_wait_for {cap} {reserve} {floor}"], capture_output=True, text=True)
    # Within a second: the clock can tick between the two date calls.
    assert abs(int(r.stdout.strip()) - want) <= 1, r.stderr


def test_the_health_url_follows_the_unit_port(tmp_path):
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    funcs = _shell_function("_health_url")
    env = {**os.environ, "UNIT_DIR": str(unit_dir), "SERVICE": "foodassistant-remote.service"}
    env.pop("FA_HEALTH_URL", None)
    run = lambda kind: subprocess.run(  # noqa: E731
        ["bash", "-c", funcs + f"\n_health_url {kind}"], env=env,
        capture_output=True, text=True).stdout
    assert run("docker") == "http://127.0.0.1:9284/health"
    assert run("venv") == "http://127.0.0.1:80/health"
    (unit_dir / "foodassistant-remote.service").write_text(
        "[Service]\nExecStart=/opt/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080 --no-access-log\n")
    assert run("venv") == "http://127.0.0.1:8080/health"


def _stateful_docker(tmp_path):
    """A docker stand-in with a little state: pull moves the main tag to a new
    image, tag points a reference at an image, and up starts whatever the
    reference names, writing the /health answer for it (none when broken)."""
    d = tmp_path / "docker-state"
    d.mkdir()
    (d / "tag").write_text("sha256:old\n")
    (d / "running").write_text("sha256:old\n")
    health = tmp_path / "health.json"
    log = tmp_path / "docker.log"
    log.write_text("")
    bindir = tmp_path / "docker-bin"
    bindir.mkdir()
    ref = "ghcr.io/syracuse3dprintingorg/pantryraider:main"
    stub = bindir / "docker"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'D="{d}"\n'
        f'echo "docker $*" >> "{log}"\n'
        'case "$*" in\n'
        '  "compose images -q service") cat "$D/running"; exit 0 ;;\n'
        '  "compose ps -a -q service") echo ctr1; exit 0 ;;\n'
        f'  "inspect -f {{{{.Config.Image}}}} ctr1") echo "{ref}"; exit 0 ;;\n'
        '  "compose pull service") echo sha256:new > "$D/tag"; exit 0 ;;\n'
        f'  "tag "*" {ref}") echo "$2" > "$D/tag"; exit 0 ;;\n'
        '  "compose up -d --no-deps"*)\n'
        '    cp "$D/tag" "$D/running"\n'
        '    img="$(cat "$D/running")"\n'
        '    case " $FA_TEST_BROKEN " in *" $img "*) rm -f "$FA_TEST_HEALTH"; exit 0 ;; esac\n'
        '    v=0.0.1; [ "$img" = sha256:new ] && v=0.0.9\n'
        '    printf \'{"app": "foodassistant", "version": "%s"}\' "$v" > "$FA_TEST_HEALTH"\n'
        '    exit 0 ;;\n'
        'esac\n'
        "exit 0\n")
    stub.chmod(0o755)
    return bindir, log, health


def _docker_health_rig(rig, tmp_path, broken=()):
    install = _hosted_rig(rig, tmp_path)
    bindir, log, health = _stateful_docker(tmp_path)
    rig["env"].update({
        "PATH": f"{bindir}:{rig['env']['PATH']}",
        "FA_HEALTH_URL": health.as_uri(),
        "FA_HEALTH_TIMEOUT": "2",
        "FA_HEALTH_ROLLBACK_TIMEOUT": "2",
        "FA_TEST_HEALTH": str(health),
        "FA_TEST_BROKEN": " ".join(broken),
    })
    return install, log, health


def test_a_hosted_image_that_answers_is_kept(rig, tmp_path):
    install, log, health = _docker_health_rig(rig, tmp_path)
    result, out = run_update(rig)
    assert result["ok"] is True, out
    assert "The new version is up (0.0.9)." in out
    assert "--force-recreate" not in log.read_text()
    assert "FOODASSISTANT_TAG=main\n" in (install / ".env").read_text()


def test_a_hosted_image_that_does_not_start_goes_back_to_the_old_one(rig, tmp_path):
    install, log, health = _docker_health_rig(rig, tmp_path, broken=("sha256:new",))
    result, out = run_update(rig)
    msg = ("Pantry Raider 0.0.1 did not start on this device within 2 seconds, "
           "so it went back to 0.0.1. It tries again when a newer version comes out, or when you press Update now.")
    assert result["ok"] is False, out
    assert result["error"] == msg and result["hold_reason"] == msg
    calls = log.read_text()
    assert ("docker tag sha256:old ghcr.io/syracuse3dprintingorg/pantryraider:main"
            in calls)
    assert "docker compose up -d --no-deps --force-recreate service" in calls
    assert (tmp_path / "docker-state" / "running").read_text().strip() == "sha256:old"
    # The stack .env is as it was before the run.
    assert "FOODASSISTANT_TAG=latest\n" in (install / ".env").read_text()
    assert json.loads(health.read_text())["version"] == "0.0.1"
    assert _state(rig)["rollback_reason"] == msg


def test_a_hosted_switch_back_that_fails_says_so(rig, tmp_path):
    install, log, health = _docker_health_rig(
        rig, tmp_path, broken=("sha256:new", "sha256:old"))
    result, out = run_update(rig)
    assert result["ok"] is False
    assert result["hold_reason"].startswith(
        "Pantry Raider 0.0.1 did not start on this device within 2 seconds, and "
        "going back to 0.0.1 did not work either.")
    assert _state(rig)["rollback_reason"] == result["hold_reason"]


# -- A version that did not start is not retried on a schedule (g3n7i) --------
#
# Each try takes the app down for the health wait, so the automatic update
# (FA_UPDATE_TRIGGER=scheduled, set by the bridge) holds on a version that
# was switched back from until a different one is the target. A manual run
# (no trigger) always tries again.

_HELD = ("Pantry Raider 0.0.3 did not start on this device last time; waiting "
         "for the next release. Press Update now to try it again.")


def _rolled_back_from_003(rig, tmp_path):
    app, log = _health_rig(rig, tmp_path, broken=("0.0.3",))
    _release_on_main(rig, "0.0.2")
    assert run_update(rig)[0]["ok"] is True
    _release_on_main(rig, "0.0.3")
    result, out = run_update(rig)
    assert result["ok"] is False, out
    assert _state(rig)["rollback_version"] == "0.0.3"
    return app, log


def _restarts(log):
    return log.read_text().count("systemctl restart foodassistant-remote.service")


def test_a_scheduled_run_holds_on_the_version_that_did_not_start(rig, tmp_path):
    app, log = _rolled_back_from_003(rig, tmp_path)
    state = _state(rig)
    restarts = _restarts(log)
    rig["env"]["FA_UPDATE_TRIGGER"] = "scheduled"
    result, out = run_update(rig)
    assert result["ok"] is True, out
    assert result["error"] == ""
    assert result["hold_reason"] == _HELD
    assert result["before"] == result["after"]
    assert _restarts(log) == restarts
    assert _deployed_version(app) == "0.0.2"
    # The note in Settings and the floor stay as they were.
    assert _state(rig) == state


def test_a_manual_run_tries_the_failed_version_again(rig, tmp_path):
    app, log = _rolled_back_from_003(rig, tmp_path)
    restarts = _restarts(log)
    rig["env"].pop("FA_UPDATE_TRIGGER", None)
    result, out = run_update(rig)
    assert result["ok"] is False, out
    assert "did not start on this device within 2 seconds" in result["hold_reason"]
    assert _restarts(log) > restarts
    # Once the cause is gone, a manual run installs it.
    rig["env"]["FA_TEST_BROKEN"] = ""
    result, out = run_update(rig)
    assert result["ok"] is True, out
    assert _deployed_version(app) == "0.0.3"
    assert "rollback_version" not in _state(rig)


def test_a_scheduled_run_takes_the_next_release(rig, tmp_path):
    app, log = _rolled_back_from_003(rig, tmp_path)
    rig["env"]["FA_UPDATE_TRIGGER"] = "scheduled"
    _release_on_main(rig, "0.0.4")
    result, out = run_update(rig)
    assert result["ok"] is True, out
    assert result["hold_reason"] == ""
    assert _deployed_version(app) == "0.0.4"
    assert "rollback_version" not in _state(rig)


def test_a_scheduled_run_goes_ahead_when_the_failed_version_runs_after_all(rig, tmp_path):
    # It came up after a restart: the run goes ahead and clears the note.
    app, log = _rolled_back_from_003(rig, tmp_path)
    rig["env"]["FA_UPDATE_TRIGGER"] = "scheduled"
    rig["env"]["FA_RUNNING_VERSION"] = "0.0.3"
    rig["env"]["FA_TEST_BROKEN"] = ""
    result, out = run_update(rig)
    assert result["ok"] is True, out
    assert result["hold_reason"] != _HELD
    assert "rollback_version" not in _state(rig)


@pytest.mark.parametrize("trigger", ["", "manual", "Scheduled", "scheduled "])
def test_only_the_exact_scheduled_trigger_holds(rig, tmp_path, trigger):
    app, log = _rolled_back_from_003(rig, tmp_path)
    restarts = _restarts(log)
    rig["env"]["FA_UPDATE_TRIGGER"] = trigger
    result, out = run_update(rig)
    assert result["hold_reason"] != _HELD
    assert _restarts(log) > restarts


def test_a_scheduled_run_with_no_failed_version_installs(rig, tmp_path):
    app, log = _health_rig(rig, tmp_path)
    rig["env"]["FA_UPDATE_TRIGGER"] = "scheduled"
    _release_on_main(rig, "0.0.2")
    result, out = run_update(rig)
    assert result["ok"] is True, out
    assert _deployed_version(app) == "0.0.2"


def test_a_damaged_failed_version_is_ignored(rig, tmp_path):
    app, log = _rolled_back_from_003(rig, tmp_path)
    state = _state(rig)
    state["rollback_version"] = "0.0.3; rm -rf /"
    rig["state"].write_text(json.dumps(state))
    restarts = _restarts(log)
    rig["env"]["FA_UPDATE_TRIGGER"] = "scheduled"
    result, out = run_update(rig)
    assert result["hold_reason"] != _HELD
    assert _restarts(log) > restarts


def test_a_scheduled_hosted_run_holds_on_the_image_that_did_not_start(rig, tmp_path):
    install, log, health = _docker_health_rig(rig, tmp_path, broken=("sha256:new",))
    _release_on_main(rig, "0.0.3")
    result, out = run_update(rig)
    assert result["ok"] is False, out
    assert _state(rig)["rollback_version"] == "0.0.3"
    log.write_text("")
    rig["env"]["FA_UPDATE_TRIGGER"] = "scheduled"
    result, out = run_update(rig)
    assert result["ok"] is True, out
    assert result["hold_reason"] == _HELD
    assert "compose pull" not in log.read_text()
    assert "compose up" not in log.read_text()
    # A manual press pulls and tries it again.
    rig["env"].pop("FA_UPDATE_TRIGGER")
    result, out = run_update(rig)
    assert "compose pull" in log.read_text()
