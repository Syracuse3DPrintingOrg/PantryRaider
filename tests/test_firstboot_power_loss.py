"""firstboot.sh and the boot-partition helpers against power loss
(FoodAssistant-85f64): swap kept in RAM on rpi-swap images, the boot record
unit, and every config.txt / cmdline.txt edit made by rewriting the file
(temp file, fsync, rename, sync) with the read-only remount dance.

The functions run sourced (FIRSTBOOT_SOURCE_ONLY=1) with stub tools on PATH
and every path pointed into tmp_path.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IB = ROOT / "scripts" / "image-build"
FIRSTBOOT = IB / "firstboot.sh"


def _stubs(tmp_path, **bodies):
    d = tmp_path / "stubs"
    d.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    log.touch()
    for name, body in bodies.items():
        s = d / name
        s.write_text("#!/usr/bin/env bash\n"
                     f"printf '{name} %s\\n' \"$*\" >> \"{log}\"\n" + body)
        s.chmod(0o755)
    return d, log


def _source(script, tmp_path, stub_dir=None, **env):
    full = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    full.update(FIRSTBOOT_SOURCE_ONLY="1", DRY_RUN="0", REPO_DIR=str(ROOT),
                CHANNEL_FILE=str(tmp_path / "update-channel"))
    if stub_dir is not None:
        full["PATH"] = f"{stub_dir}:{full['PATH']}"
    full.update(env)
    return subprocess.run(["bash", "-c", f'source "{FIRSTBOOT}"; {script}'],
                          env=full, capture_output=True, text=True, timeout=60)


# --- swap -------------------------------------------------------------------

def test_rpi_swap_image_keeps_swap_in_ram_without_zram_tools(tmp_path):
    stubs, log = _stubs(
        tmp_path,
        **{"dpkg-query": 'case "${@: -1}" in rpi-swap) printf "install ok installed"; exit 0;; esac; exit 1\n',
           "dpkg": 'case "${@: -1}" in zram-tools) exit 0;; esac; exit 1\n',
           "systemctl": 'case "$1" in is-active) exit 3;; list-unit-files) exit 1;; esac; exit 0\n',
           "apt-get": "exit 0\n"})
    conf_dir = tmp_path / "swap.conf.d"
    r = _source("configure_zram", tmp_path, stubs, FORCE_PI="1",
                RPI_SWAP_CONF_DIR=str(conf_dir))
    assert r.returncode == 0, r.stdout + r.stderr
    conf = conf_dir / "50-zram-only.conf"
    assert "[Main]\nMechanism=zram\n" in conf.read_text()
    calls = log.read_text()
    assert "systemctl disable rpi-zram-writeback.timer" in calls
    assert "systemctl disable zramswap.service" in calls
    assert "apt-get remove -y zram-tools" in calls
    assert "apt-get install" not in calls  # zram-tools is never installed here
    assert "swapoff" not in calls
    assert "Swap stays in RAM with rpi-swap" in r.stdout


def test_zram_tools_path_is_kept_for_images_without_rpi_swap():
    body = FIRSTBOOT.read_text()
    fn = body[body.index("configure_zram() {"):]
    fn = fn[:fn.index("\n}\n")]
    # rpi-swap is checked before zram-tools is installed, and returns early.
    assert fn.index("rpi_swap_installed") < fn.index("apt_install zram-tools")
    assert "configure_rpi_swap\n    return 0" in fn


# --- boot record --------------------------------------------------------------

def test_boot_record_unit_is_installed_and_enabled(tmp_path):
    stubs, log = _stubs(tmp_path, systemctl="exit 0\n")
    unit = tmp_path / "foodassistant-boot-record.service"
    r = _source("install_boot_record", tmp_path, stubs, BOOT_RECORD_UNIT=str(unit))
    assert r.returncode == 0, r.stderr
    assert unit.read_text() == (IB / "foodassistant-boot-record.service").read_text()
    assert "systemctl enable foodassistant-boot-record.service" in log.read_text()


def test_sd_resilience_step_installs_the_boot_record():
    body = FIRSTBOOT.read_text()
    fn = body[body.index("configure_sd_resilience() {"):]
    fn = fn[:fn.index("\n}\n")]
    assert "install_boot_record" in fn
    # It runs before the fstab part, which returns early on a second run.
    assert fn.index("install_boot_record") < fn.index('local fstab="/etc/fstab"')


def test_commit_comment_says_wear_not_safety():
    text = FIRSTBOOT.read_text()
    assert "shrinking the window where a power cut" not in text
    head = text[text.index("# Step: SD-card longevity"):text.index("configure_sd_resilience() {")]
    assert "chosen for wear" in head and "widens the power-cut" in head


# --- boot partition writes ---------------------------------------------------

def _mount_stubs(tmp_path, opts):
    return _stubs(
        tmp_path,
        findmnt=('for a in "$@"; do case "$a" in TARGET) echo /boot/firmware; exit 0;; '
                 f'OPTIONS) echo {opts}; exit 0;; esac; done; exit 1\n'),
        mount="exit 0\n",
        sync="exit 0\n")


def test_boot_append_rewrites_the_file_under_a_read_write_remount(tmp_path):
    stubs, log = _mount_stubs(tmp_path, "ro,relatime,fmask=0077")
    cfg = tmp_path / "config.txt"
    cfg.write_text("dtparam=audio=on")
    inode = cfg.stat().st_ino
    r = _source(f'boot_append "{cfg}" "dtparam=spi=on"', tmp_path, stubs)
    assert r.returncode == 0, r.stderr
    assert cfg.read_text() == "dtparam=audio=on\ndtparam=spi=on\n"
    assert cfg.stat().st_ino != inode
    assert not list(tmp_path.glob(".config.txt.*"))
    calls = [ln.split()[0] + " " + " ".join(ln.split()[1:3]) for ln in log.read_text().splitlines()
             if not ln.startswith("findmnt")]
    assert calls[0] == "mount -o remount,rw"
    assert calls[-1] == "mount -o remount,ro"
    assert any(c.startswith("sync") for c in calls[1:-1])


def test_boot_append_leaves_a_writable_partition_mounted_as_it_is(tmp_path):
    stubs, log = _mount_stubs(tmp_path, "rw,relatime")
    cfg = tmp_path / "config.txt"
    cfg.write_text("a=1\n")
    r = _source(f'boot_append "{cfg}" "b=2"', tmp_path, stubs)
    assert r.returncode == 0, r.stderr
    assert cfg.read_text() == "a=1\nb=2\n"
    assert "mount " not in log.read_text()
    assert "sync" in log.read_text()  # flushed even when nothing was remounted


def test_no_boot_file_is_appended_or_rewritten_in_place():
    """Every config.txt and cmdline.txt edit goes through the rename path.
    Appends (>>) and truncating writes (>) to those files, and sed -i on
    them, are the writes a power cut catches half done."""
    scripts = {
        "firstboot.sh": FIRSTBOOT.read_text(),
        "foodassistant-update": (IB / "foodassistant-update").read_text(),
        "foodassistant-gadgets-setup": (IB / "foodassistant-gadgets-setup").read_text(),
        "foodassistant-set-rotation": (IB / "foodassistant-set-rotation").read_text(),
    }
    bad = re.compile(r'>>?\s*"\$(cfg|_cfg|_i2c_cfg|cmdline|_cmd|_cmdline|_cfgtxt)"'
                     r'|sed -i[^\n]*"\$(cfg|_cfg|cmdline|_cmd|_cmdline)"')
    for name, text in scripts.items():
        for n, line in enumerate(text.splitlines(), 1):
            code = re.sub(r"(^|\s)#.*", "", line)
            assert not bad.search(code), f"{name}:{n}: {line.strip()}"


def test_boot_writers_all_sync_and_remount():
    for name in ("foodassistant-gadgets-setup", "foodassistant-set-rotation"):
        text = (IB / name).read_text()
        assert "remount,rw" in text and "remount,ro" in text, name
        assert re.search(r"sync \"\$tmp\"", text), name
        assert "mv -f \"$tmp\"" in text, name


def _set_rotation_fn():
    text = (IB / "foodassistant-set-rotation").read_text()
    start = text.index("boot_write() {")
    return text[start:text.index("\n}\n", start) + 3]


def test_set_rotation_boot_write_replaces_the_cmdline(tmp_path):
    stubs, log = _mount_stubs(tmp_path, "ro,relatime")
    cmdline = tmp_path / "cmdline.txt"
    cmdline.write_text("console=tty1 root=PARTUUID=x-02 video=HDMI-A-1:rotate=90 rootwait\n")
    script = (_set_rotation_fn()
              + f'line="$(tr -d \'\\n\' < "{cmdline}" | sed \'s/ video=HDMI-A-[0-9]*:rotate=[0-9]*//\')"\n'
              + f'boot_write "{cmdline}" "$line\n"\n')
    env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}"}
    r = subprocess.run(["bash", "-c", "set -euo pipefail\n" + script], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert cmdline.read_text() == "console=tty1 root=PARTUUID=x-02 rootwait\n"
    calls = log.read_text()
    assert "mount -o remount,rw /boot/firmware" in calls
    assert "mount -o remount,ro /boot/firmware" in calls


def _gadgets_fns():
    text = (IB / "foodassistant-gadgets-setup").read_text()
    start = text.index("# --- Boot partition writes")
    end = text.index("# Resolve the source checkout.")
    return text[start:end]


def test_gadgets_setup_drops_the_serial_console_by_rewriting(tmp_path):
    stubs, log = _mount_stubs(tmp_path, "rw")
    cmd = tmp_path / "cmdline.txt"
    cmd.write_text("console=serial0,115200 console=tty1 root=PARTUUID=x-02\n")
    cfg = tmp_path / "config.txt"
    cfg.write_text("dtparam=audio=on\n")
    script = ("warn() { echo \"WARN $*\" >&2; }\n" + _gadgets_fns() + f'''
_boot_append "{cfg}" 'enable_uart=1'
_cline="$(tr -d '\\n' < "{cmd}")"
_cnew="$(printf '%s' "$_cline" | sed -E 's/console=(serial0|ttyAMA0|ttyS0)[,0-9]* ?//g')"
_boot_write "{cmd}" "$_cnew
"
''')
    env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}"}
    r = subprocess.run(["bash", "-c", "set -uo pipefail\n" + script], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert cmd.read_text() == "console=tty1 root=PARTUUID=x-02\n"
    assert cfg.read_text() == "dtparam=audio=on\nenable_uart=1\n"


def test_gadgets_setup_swaps_its_package_in_whole(tmp_path):
    src = tmp_path / "src" / "foodassistant_gadgets"
    src.mkdir(parents=True)
    (src / "__main__.py").write_text("# new\n")
    dst = tmp_path / "opt" / "foodassistant_gadgets"
    dst.mkdir(parents=True)
    (dst / "stale.py").write_text("# old\n")
    script = "warn() { :; }\n" + _gadgets_fns() + f'_swap_dir "{src}" "{dst}"\n'
    r = subprocess.run(["bash", "-c", "set -uo pipefail\n" + script],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in dst.iterdir()) == ["__main__.py"]
    assert sorted(p.name for p in dst.parent.iterdir()) == ["foodassistant_gadgets"]
    text = (IB / "foodassistant-gadgets-setup").read_text()
    assert 'rm -rf "$GD_DST"\n' not in text
