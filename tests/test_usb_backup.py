"""Pure-logic tests for USB flash-drive backups (FoodAssistant-ch6d).

Covers the drive-detection parsing (removable flags plus /proc/mounts), the
target choice, the keep-the-newest-14 rotation, and the schedule decision.
No hardware, network, or Docker needed.

Run: python -m pytest tests/test_usb_backup.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))

from app.services import usb_backup as ub  # noqa: E402


# --- /proc/mounts parsing ---------------------------------------------------

MOUNTS = """\
proc /proc proc rw,nosuid 0 0
/dev/mmcblk0p2 / ext4 rw,noatime 0 0
/dev/mmcblk0p1 /boot/firmware vfat rw,relatime 0 0
tmpfs /run tmpfs rw,nosuid 0 0
/dev/sda1 /media/pi/PANTRY\\040USB vfat rw,nosuid,nodev 0 0
/dev/sdb1 /mnt/readonly ext4 ro,relatime 0 0
"""


def test_parse_mounts_decodes_octal_escapes():
    entries = ub.parse_mounts(MOUNTS)
    devices = {e[0] for e in entries}
    assert "/dev/sda1" in devices
    sda = next(e for e in entries if e[0] == "/dev/sda1")
    assert sda[1] == "/media/pi/PANTRY USB"
    assert sda[2] == "vfat"
    assert "rw" in sda[3].split(",")


def test_parse_mounts_skips_short_lines():
    assert ub.parse_mounts("garbage line\n\n") == []


# --- partition-to-disk matching ---------------------------------------------

def test_disk_for_partition_basic_and_nvme():
    assert ub.disk_for_partition("sda1", ["sda"]) == "sda"
    assert ub.disk_for_partition("sda", ["sda"]) == "sda"
    assert ub.disk_for_partition("nvme0n1p2", ["nvme0n1"]) == "nvme0n1"
    assert ub.disk_for_partition("mmcblk0p1", ["mmcblk0"]) == "mmcblk0"


def test_disk_for_partition_never_matches_a_longer_disk_name():
    # sdab1 is a partition of sdab, not of sda.
    assert ub.disk_for_partition("sdab1", ["sda"]) is None
    assert ub.disk_for_partition("sdab1", ["sda", "sdab"]) == "sdab"


# --- candidate filtering ----------------------------------------------------

def test_candidates_only_removable_rw_non_system():
    mounts = ub.parse_mounts(MOUNTS)
    # sda and sdb removable; sdb1 is mounted read-only so only sda1 qualifies.
    cands = ub.usb_mount_candidates(["sda", "sdb"], mounts)
    assert cands == [("/dev/sda1", "/media/pi/PANTRY USB")]


def test_candidates_never_offer_root_or_boot():
    # A Pi booted from a removable USB SSD: its system partitions must be
    # skipped even though the disk reports removable=1.
    mounts = ub.parse_mounts(
        "/dev/sda2 / ext4 rw,noatime 0 0\n"
        "/dev/sda1 /boot/firmware vfat rw 0 0\n"
        "/dev/sdb1 /media/usb vfat rw 0 0\n"
    )
    assert ub.usb_mount_candidates(["sda", "sdb"], mounts) == [("/dev/sdb1", "/media/usb")]


def test_candidates_empty_when_nothing_removable():
    mounts = ub.parse_mounts(MOUNTS)
    assert ub.usb_mount_candidates([], mounts) == []


def test_pick_backup_mount_prefers_media_then_mnt():
    cands = [("/dev/sdb1", "/srv/other"), ("/dev/sda1", "/mnt/usb"),
             ("/dev/sdc1", "/media/pi/STICK")]
    assert ub.pick_backup_mount(cands) == ("/dev/sdc1", "/media/pi/STICK")
    assert ub.pick_backup_mount([("/dev/sda1", "/mnt/usb")]) == ("/dev/sda1", "/mnt/usb")
    assert ub.pick_backup_mount([("/dev/sda1", "/srv/other")]) == ("/dev/sda1", "/srv/other")
    assert ub.pick_backup_mount([]) is None


def test_removable_disks_reads_sysfs_flags(tmp_path):
    (tmp_path / "sda").mkdir()
    (tmp_path / "sda" / "removable").write_text("1\n")
    (tmp_path / "mmcblk0").mkdir()
    (tmp_path / "mmcblk0" / "removable").write_text("0\n")
    (tmp_path / "loop0").mkdir()  # no removable file at all
    assert ub.removable_disks(str(tmp_path)) == ["sda"]
    assert ub.removable_disks(str(tmp_path / "missing")) == []


# --- backup names and rotation ----------------------------------------------

def test_backup_name_pattern():
    assert ub.is_backup_name("foodassistant-usb-20260703-021500.tar.gz")
    assert ub.is_backup_name("foodassistant-usb-20260703-021500.zip")
    assert not ub.is_backup_name("foodassistant-20260703-021500.tar.gz")
    assert not ub.is_backup_name("vacation-photos.zip")
    assert not ub.is_backup_name(".foodassistant-usb-20260703-021500.zip.part")


def test_rotation_keeps_newest_and_ignores_foreign_files():
    names = [f"foodassistant-usb-202607{d:02d}-020000.tar.gz" for d in range(1, 21)]
    names += ["notes.txt", "vacation-photos.zip"]
    victims = ub.rotation_victims(names, keep=14)
    assert len(victims) == 6
    # The oldest six of ours, and never anyone else's files.
    assert victims == sorted(n for n in names if ub.is_backup_name(n))[:6]
    assert "notes.txt" not in victims and "vacation-photos.zip" not in victims


def test_rotation_no_victims_at_or_under_keep():
    names = [f"foodassistant-usb-202607{d:02d}-020000.zip" for d in range(1, 15)]
    assert ub.rotation_victims(names, keep=14) == []
    assert ub.rotation_victims([], keep=14) == []


def test_rotation_mixed_suffixes_sort_chronologically():
    names = ["foodassistant-usb-20260101-000000.zip",
             "foodassistant-usb-20260301-000000.tar.gz",
             "foodassistant-usb-20260201-000000.zip"]
    assert ub.rotation_victims(names, keep=2) == ["foodassistant-usb-20260101-000000.zip"]


# --- schedule decision --------------------------------------------------------

def test_is_due_disabled_at_zero_or_negative():
    assert ub.is_due(0, 0, 1e9) is False
    assert ub.is_due(-5, 0, 1e9) is False


def test_is_due_first_run_and_interval_elapsed():
    now = 1_000_000.0
    assert ub.is_due(24, 0, now) is True                       # never ran: due
    assert ub.is_due(24, now - 23 * 3600, now) is False        # too soon
    assert ub.is_due(24, now - 24 * 3600, now) is True         # exactly due
    assert ub.is_due(6, now - 7 * 3600, now) is True


def test_backup_filename_is_sortable_and_matches_pattern():
    name = ub.backup_filename("zip", now=1_750_000_000)
    assert ub.is_backup_name(name)
