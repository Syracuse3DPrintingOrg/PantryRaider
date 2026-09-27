"""The pre-built SD image's first-boot hook: in and back out of cmdline.txt.

prepare-image.sh appends a systemd.run hook to cmdline.txt so the first boot
runs foodassistant-firstrun.sh. On Bookworm, Raspberry Pi Imager's own
firstrun.sh stripped every systemd.run entry afterwards. The release image is
Raspberry Pi OS Trixie, which has no such script (its first-boot setup is
cloud-init), so unless foodassistant-firstrun.sh removes its own hook, every
boot runs it again and reboots, and the device never reaches a normal boot.

These run the real scripts against a temp boot directory, with the firstrun
paths redirected into tmp_path and a stub systemctl on PATH that only records
its arguments, so nothing touches the host.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

IMAGE_BUILD = Path(__file__).resolve().parent.parent / "scripts" / "image-build"
PREPARE = IMAGE_BUILD / "prepare-image.sh"
FIRSTRUN = IMAGE_BUILD / "foodassistant-firstrun.sh"

HOOK = (" systemd.run=/boot/firmware/foodassistant-firstrun.sh"
        " systemd.run_success_action=reboot"
        " systemd.unit=kernel-command-line.target")

# A minimal line, and the cmdline.txt of the Raspberry Pi OS Trixie base the
# release image is built on.
ORIGINAL_LINES = [
    "console=tty1 root=PARTUUID=abcd-02 rootwait",
    "console=serial0,115200 console=tty1 root=PARTUUID=4d8fd085-02"
    " rootfstype=ext4 fsck.repair=yes rootwait resize",
]

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("bash", "sh", "sed")),
    reason="bash, sh and sed are required to run the image scripts",
)


def _prepare(boot):
    subprocess.run(["bash", str(PREPARE), "--boot-dir", str(boot)],
                   check=True, capture_output=True, text=True)


def _prepared_boot(tmp_path, original):
    boot = tmp_path / "bootfs"
    boot.mkdir()
    (boot / "cmdline.txt").write_text(original + "\n")
    _prepare(boot)
    return boot


@pytest.fixture()
def firstrun(tmp_path):
    """Run foodassistant-firstrun.sh with every path under tmp_path.

    Returns (run, calls): run(boot, **env) runs the script once, and calls()
    lists the argument lines the stub systemctl received so far."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    log = tmp_path / "systemctl.log"
    stub = stubs / "systemctl"
    stub.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\nexit 0\n')
    stub.chmod(0o755)
    (tmp_path / "units").mkdir()

    def run(boot, **env_overrides):
        env = {
            **os.environ,
            "PATH": f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}",
            "FOODASSISTANT_BOOT": str(boot),
            "FOODASSISTANT_SETUP_DST": str(tmp_path / "opt-setup"),
            "FOODASSISTANT_UNIT_DIR": str(tmp_path / "units"),
            **env_overrides,
        }
        return subprocess.run(["sh", str(FIRSTRUN)], env=env,
                              capture_output=True, text=True)

    def calls():
        return log.read_text().splitlines() if log.exists() else []

    return run, calls


def test_firstrun_script_parses():
    result = subprocess.run(["sh", "-n", str(FIRSTRUN)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("original", ORIGINAL_LINES)
def test_prepare_appends_the_hook_once(tmp_path, original):
    boot = _prepared_boot(tmp_path, original)
    assert (boot / "cmdline.txt").read_text() == original + HOOK + "\n"
    assert (boot / "foodassistant-firstrun.sh").is_file()
    assert (boot / "foodassistant-setup" / "foodassistant-firstboot.service").is_file()

    # Preparing the same card again must not stack a second hook.
    _prepare(boot)
    assert (boot / "cmdline.txt").read_text().count("systemd.run=") == 1


@pytest.mark.parametrize("original", ORIGINAL_LINES)
def test_firstrun_removes_its_own_hook_and_leaves_provisioning_to_the_next_boot(
        tmp_path, firstrun, original):
    boot = _prepared_boot(tmp_path, original)
    run, calls = firstrun

    result = run(boot)

    assert result.returncode == 0, result.stdout + result.stderr
    # The line is exactly what it was before the image was prepared, so the
    # reboot that follows is a normal multi-user boot.
    assert (boot / "cmdline.txt").read_text() == original + "\n"
    assert "enable foodassistant-firstboot.service" in calls()
    # Starting the provisioner here would run it inside the hook boot, before
    # cloud-init has created the login user and the network. That includes a
    # start behind a flag, such as "--no-block start" or "enable --now".
    assert not [c for c in calls()
                if {"start", "restart", "--now"} & set(c.split())], calls()
    assert (tmp_path / "units" / "foodassistant-firstboot.service").is_file()
    assert os.access(tmp_path / "opt-setup" / "firstboot.sh", os.X_OK)


def test_a_second_firstrun_leaves_cmdline_unchanged(tmp_path, firstrun):
    original = ORIGINAL_LINES[0]
    boot = _prepared_boot(tmp_path, original)
    run, _ = firstrun

    assert run(boot).returncode == 0
    assert run(boot).returncode == 0
    assert (boot / "cmdline.txt").read_text() == original + "\n"


def test_only_the_hook_is_removed_from_the_line(tmp_path, firstrun):
    """Anything around the hook stays, including other systemd.* parameters
    (the provisioner's quiet-boot set has two), so a looser pattern that
    strips every systemd.* entry fails here."""
    boot = tmp_path / "bootfs"
    boot.mkdir()
    before = "console=tty1 root=PARTUUID=abcd-02 rootwait systemd.show_status=false"
    after = " quiet splash rd.systemd.show_status=false"
    (boot / "cmdline.txt").write_text(before + HOOK + after + "\n")
    run, _ = firstrun

    assert run(boot).returncode == 0
    assert (boot / "cmdline.txt").read_text() == before + after + "\n"


def test_the_hook_comes_out_even_when_a_later_step_fails(tmp_path, firstrun):
    """A failure after the hook is removed still leaves a bootable card."""
    original = ORIGINAL_LINES[0]
    boot = _prepared_boot(tmp_path, original)
    run, _ = firstrun
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")

    result = run(boot, FOODASSISTANT_SETUP_DST=str(blocker / "setup"))

    assert result.returncode != 0
    assert (boot / "cmdline.txt").read_text() == original + "\n"
