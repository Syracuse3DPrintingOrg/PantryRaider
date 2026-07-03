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
