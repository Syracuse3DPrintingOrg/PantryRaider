"""Automatic backups to an attached USB flash drive (FoodAssistant-ch6d).

Plug a formatted flash drive into the device and Pantry Raider writes backups
into a pantryraider-backups folder on it, on a schedule or on demand. Nothing
outside that folder is ever touched, the drive is never formatted, and no
custom mount logic exists here: the drive must already be mounted (the desktop
automounter, udisks, or an fstab entry all work).

Two paths share this module:

- On a Pi appliance (pi_hosted or pi_remote) the app runs in a container that
  cannot see the host's /media, so it delegates to the host bridge
  (GET /usb/status, POST /usb/backup). The bridge tars the same data dirs
  scripts/backup.sh snapshots; on a pi_remote only the app data dir exists, so
  the backup is naturally the device config.
- On a plain server the app detects the drive itself (parsing /sys/block
  removable flags plus /proc/mounts) and writes the admin backup zip. Inside
  Docker the host's drive is not visible unless its mountpoint is passed into
  the container, in which case the same detection sees it.

The detection parsing, rotation choice, and schedule decision are pure
functions so they are unit-testable without hardware.
"""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# The only directory on the drive this feature ever writes to or deletes from.
BACKUP_DIRNAME = "pantryraider-backups"
# How many backups to keep on the drive; older ones are removed after each run.
KEEP_BACKUPS = 14

# Backup file names embed a sortable timestamp. The bridge writes .tar.gz
# snapshots, the server path writes .zip app-data backups; rotation treats
# both the same and never touches any other file.
_BACKUP_NAME_RE = re.compile(r"^foodassistant-usb-\d{8}-\d{6}\.(tar\.gz|zip)$")


def is_backup_name(name: str) -> bool:
    """Whether a filename is one of ours (safe for rotation to consider)."""
    return bool(_BACKUP_NAME_RE.match(name))


def backup_filename(suffix: str, now: float | None = None) -> str:
    """A timestamped backup filename, e.g. foodassistant-usb-20260703-021500.zip."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    return f"foodassistant-usb-{stamp}.{suffix}"


def rotation_victims(names: list[str], keep: int = KEEP_BACKUPS) -> list[str]:
    """Pure: which backup files to delete so only the newest `keep` remain.

    Only filenames matching our pattern are considered; the embedded timestamp
    makes a plain sort chronological. Anything else on the drive is ignored.
    """
    ours = sorted(n for n in names if is_backup_name(n))
    if keep <= 0:
        return ours
    return ours[:-keep] if len(ours) > keep else []


def is_due(interval_hours: int, last_run: float, now: float) -> bool:
    """Pure schedule decision: run when the interval is on and has elapsed.

    interval_hours  0 (or negative) disables the schedule entirely.
    last_run        unix time of the last successful backup; 0 = never, which
                    makes the first pass after enabling run right away.
    """
    if not interval_hours or interval_hours <= 0:
        return False
    return (now - last_run) >= interval_hours * 3600


# --- Drive detection (pure parsers plus thin /sys and /proc readers) --------

def parse_mounts(text: str) -> list[tuple[str, str, str, str]]:
    """Parse /proc/mounts text into (device, mountpoint, fstype, options).

    Mountpoints with spaces arrive octal-escaped (\\040); they are decoded so
    callers get real paths.
    """
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        dev, mnt, fstype, opts = parts[:4]
        out.append((dev, _unescape_mount(mnt), fstype, opts))
    return out


def _unescape_mount(s: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def disk_for_partition(partition: str, disks: list[str]) -> str | None:
    """Pure: the removable disk a partition name belongs to, or None.

    sda1 belongs to sda; nvme0n1p2 belongs to nvme0n1. Matching requires the
    remainder to be a partition suffix so sdab1 never matches disk sda.
    """
    for d in sorted(disks, key=len, reverse=True):
        if partition == d:
            return d
        if partition.startswith(d) and re.fullmatch(r"p?\d+", partition[len(d):]):
            return d
    return None


def usb_mount_candidates(removable_disks: list[str],
                         mounts: list[tuple[str, str, str, str]]) -> list[tuple[str, str]]:
    """Pure: mounted, writable filesystems living on a removable disk.

    Filters out anything that is not a real block device, anything mounted
    read-only, and (safety) the root and boot filesystems, so a Pi booted from
    a removable USB SSD never has its system disk offered as a backup target.
    Returns (device, mountpoint) pairs.
    """
    out = []
    for dev, mnt, _fstype, opts in mounts:
        if not dev.startswith("/dev/"):
            continue
        if disk_for_partition(os.path.basename(dev), removable_disks) is None:
            continue
        if mnt == "/" or mnt == "/boot" or mnt.startswith("/boot/"):
            continue
        if "ro" in opts.split(","):
            continue
        out.append((dev, mnt))
    return out


def pick_backup_mount(candidates: list[tuple[str, str]]) -> tuple[str, str] | None:
    """Pure: choose the drive to back up to, preferring normal automount spots."""
    for prefix in ("/media/", "/run/media/", "/mnt/"):
        for dev, mnt in candidates:
            if mnt.startswith(prefix) or mnt == prefix.rstrip("/"):
                return (dev, mnt)
    return candidates[0] if candidates else None


def removable_disks(sys_block: str = "/sys/block") -> list[str]:
    """Disk names whose /sys/block/<name>/removable flag is 1."""
    names = []
    try:
        entries = sorted(os.listdir(sys_block))
    except OSError:
        return []
    for name in entries:
        try:
            flag = Path(sys_block, name, "removable").read_text().strip()
        except OSError:
            continue
        if flag == "1":
            names.append(name)
    return names


def _local_detect(sys_block: str = "/sys/block",
                  proc_mounts: str = "/proc/mounts") -> tuple[str, str] | None:
    """Find a mounted removable drive on this host (server path, no bridge)."""
    disks = removable_disks(sys_block)
    if not disks:
        return None
    try:
        text = Path(proc_mounts).read_text()
    except OSError:
        return None
    return pick_backup_mount(usb_mount_candidates(disks, parse_mounts(text)))


def _dir_status(mountpoint: str, device: str) -> dict:
    """Status dict for a detected drive: free space plus last-backup info."""
    st = os.statvfs(mountpoint)
    bdir = Path(mountpoint) / BACKUP_DIRNAME
    backups: list[str] = []
    if bdir.is_dir():
        backups = sorted(n for n in os.listdir(bdir) if is_backup_name(n))
    last = backups[-1] if backups else ""
    last_time = 0.0
    if last:
        try:
            last_time = (bdir / last).stat().st_mtime
        except OSError:
            last_time = 0.0
    return {
        "ok": True, "detected": True, "device": device, "mountpoint": mountpoint,
        "free_bytes": st.f_bavail * st.f_frsize,
        "total_bytes": st.f_blocks * st.f_frsize,
        "backups_dir": str(bdir), "backups": len(backups),
        "last_backup": last, "last_backup_time": last_time,
    }


# --- Status and run: bridge on a Pi appliance, local on a server ------------

_BRIDGE = "http://127.0.0.1:9299"


async def status() -> dict:
    """Drive status for the settings UI. Never raises."""
    from ..config import settings
    if settings.is_pi_appliance():
        return await _bridge_get("/usb/status")
    try:
        pick = _local_detect()
        if not pick:
            return {"ok": True, "detected": False}
        return _dir_status(pick[1], pick[0])
    except Exception as e:
        return {"ok": False, "detected": False, "error": str(e)}


async def run_backup() -> dict:
    """Write one backup to the drive now, rotate, and record the run time."""
    from ..config import settings
    if settings.is_pi_appliance():
        result = await _bridge_post("/usb/backup", timeout=600.0)
    else:
        from fastapi.concurrency import run_in_threadpool
        result = await run_in_threadpool(_local_run_backup)
    if result.get("ok"):
        try:
            settings.save({"usb_backup_last": time.time()})
        except Exception:
            logger.warning("USB backup ran but the run time could not be saved")
    return result


def _local_run_backup() -> dict:
    """Server path: write the full app-data zip into the drive's backup folder."""
    pick = _local_detect()
    if not pick:
        return {"ok": False, "error":
                "No USB drive found. Plug in a drive that is already formatted "
                "and mounted, then try again."}
    device, mountpoint = pick
    # The drive stays in the user's hands, so the backup includes secrets and
    # is restore-complete (unlike the redacted browser download).
    from ..routers.admin import _build_zip
    zip_bytes, _ = _build_zip(include_secrets=True)
    bdir = Path(mountpoint) / BACKUP_DIRNAME
    try:
        bdir.mkdir(parents=True, exist_ok=True)
        name = backup_filename("zip")
        tmp = bdir / f".{name}.part"
        tmp.write_bytes(zip_bytes)
        os.replace(tmp, bdir / name)
    except OSError as e:
        return {"ok": False, "error": f"Could not write to the drive: {e}"}
    deleted = []
    for victim in rotation_victims(os.listdir(bdir)):
        try:
            (bdir / victim).unlink()
            deleted.append(victim)
        except OSError:
            pass
    return {"ok": True, "file": str(bdir / name), "bytes": len(zip_bytes),
            "deleted": deleted, "device": device}


async def _bridge_get(path: str, timeout: float = 8.0) -> dict:
    from .bridge import bridge_client
    try:
        async with bridge_client(timeout=timeout) as c:
            r = await c.get(f"{_BRIDGE}{path}")
        return r.json()
    except Exception as e:
        return {"ok": False, "detected": False,
                "error": f"Could not reach the device helper ({e.__class__.__name__})."}


async def _bridge_post(path: str, timeout: float = 8.0) -> dict:
    from .bridge import bridge_client
    try:
        async with bridge_client(timeout=timeout) as c:
            r = await c.post(f"{_BRIDGE}{path}", json={})
        return r.json()
    except Exception as e:
        return {"ok": False,
                "error": f"Could not reach the device helper ({e.__class__.__name__})."}
