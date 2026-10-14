"""System resources for the Settings Resources pane (FoodAssistant-do2u).

Collects the hardware metrics this environment can actually provide: CPU use
(overall and per core), load average, memory, swap, disk space (the app data
directory and the system disk), uptime, CPU temperature, and on a Raspberry Pi
the power/throttle state decoded from the firmware's throttle word (fetched
from the host bridge by the route; decoding lives here).

Every parser is pure (fed text, returns values) so the whole module is
testable with captured /proc and /sys content, and every metric degrades by
omission: a reading this environment lacks (no thermal zone in a VM, no
throttle word off a Pi) simply does not appear in the result, and the UI shows
only what is present.
"""
from __future__ import annotations

import os
import shutil


# --- /proc/stat: CPU time counters ------------------------------------------

def parse_proc_stat(text: str) -> dict:
    """Parse /proc/stat cpu lines into {label: (busy, total)} jiffy counters.

    "cpu" is the all-cores aggregate; "cpu0".."cpuN" are per core. Idle time
    counts idle + iowait; total is the sum of every column present. Malformed
    lines are skipped. Pure.
    """
    out = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if not parts or not parts[0].startswith("cpu"):
            continue
        try:
            vals = [int(v) for v in parts[1:]]
        except ValueError:
            continue
        if len(vals) < 4:
            continue
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals)
        out[parts[0]] = (total - idle, total)
    return out


def cpu_percents(prev: dict, cur: dict) -> dict:
    """CPU busy percentages from two parse_proc_stat samples. Pure.

    Returns {"percent": overall, "per_core": [c0, c1, ...]}; either part is
    omitted when its counters are missing or did not advance (a zero-length
    interval never divides by zero). Percentages are rounded to one decimal
    and clamped to 0..100.
    """
    def pct(label):
        a, b = prev.get(label), cur.get(label)
        if not a or not b:
            return None
        dt = b[1] - a[1]
        if dt <= 0:
            return None
        return round(min(100.0, max(0.0, (b[0] - a[0]) * 100.0 / dt)), 1)

    out = {}
    overall = pct("cpu")
    if overall is not None:
        out["percent"] = overall
    cores = []
    i = 0
    while ("cpu%d" % i) in cur:
        p = pct("cpu%d" % i)
        if p is None:
            break
        cores.append(p)
        i += 1
    if cores:
        out["per_core"] = cores
    return out


# --- /proc/meminfo -----------------------------------------------------------

def parse_meminfo(text: str) -> dict:
    """Parse /proc/meminfo into byte counts. Pure.

    Returns {"total", "available", "used", "percent"} for RAM and, when a swap
    device exists, a "swap" sub-dict with the same shape. MemAvailable is the
    kernel's own estimate of claimable memory; on a very old kernel without it
    free + buffers + cached is used instead. Empty/garbage input returns {}.
    """
    kb = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].endswith(":"):
            try:
                kb[parts[0][:-1]] = int(parts[1])
            except ValueError:
                pass
    total = kb.get("MemTotal")
    if not total:
        return {}
    avail = kb.get("MemAvailable")
    if avail is None:
        avail = kb.get("MemFree", 0) + kb.get("Buffers", 0) + kb.get("Cached", 0)
    used = max(0, total - avail)
    out = {
        "total": total * 1024,
        "available": avail * 1024,
        "used": used * 1024,
        "percent": round(used * 100.0 / total, 1),
    }
    swap_total = kb.get("SwapTotal", 0)
    if swap_total > 0:
        swap_used = max(0, swap_total - kb.get("SwapFree", 0))
        out["swap"] = {
            "total": swap_total * 1024,
            "used": swap_used * 1024,
            "percent": round(swap_used * 100.0 / swap_total, 1),
        }
    return out


# --- /proc/uptime and /proc/loadavg -----------------------------------------

def parse_uptime(text: str):
    """Uptime seconds from /proc/uptime ("12345.67 23456.78"), or None. Pure."""
    try:
        return float((text or "").split()[0])
    except (IndexError, ValueError):
        return None


def format_uptime(seconds) -> str:
    """A short human reading of an uptime: "3 days, 4 hours". Pure."""
    s = int(seconds)
    if s < 60:
        return "less than a minute"
    mins, hours, days = (s // 60) % 60, (s // 3600) % 24, s // 86400
    parts = []
    if days:
        parts.append("%d day%s" % (days, "" if days == 1 else "s"))
    if hours:
        parts.append("%d hour%s" % (hours, "" if hours == 1 else "s"))
    if not days and not hours and mins:
        parts.append("%d minute%s" % (mins, "" if mins == 1 else "s"))
    return ", ".join(parts) or "less than a minute"


def parse_loadavg(text: str):
    """(1, 5, 15 minute) load averages from /proc/loadavg, or None. Pure."""
    try:
        parts = (text or "").split()
        return [float(parts[0]), float(parts[1]), float(parts[2])]
    except (IndexError, ValueError):
        return None


# --- CPU temperature ---------------------------------------------------------

def parse_thermal(text: str):
    """Degrees C from a /sys/class/thermal .../temp file (millidegrees), or
    None when unreadable or implausible. Pure."""
    try:
        milli = int((text or "").strip())
    except ValueError:
        return None
    c = milli / 1000.0
    # Some drivers report degrees directly; anything outside a plausible
    # millidegree range but inside a plausible degree range is taken as-is.
    if c < 1.0 and 1 <= milli <= 150:
        c = float(milli)
    if not (-40.0 <= c <= 150.0):
        return None
    return round(c, 1)


# --- Pi power / throttle word ------------------------------------------------

# Meaning of each bit in the firmware throttle word (vcgencmd get_throttled).
# The low bits are live conditions; the high bits are sticky since-boot flags.
# Mirrors the host bridge's table; the copy here is user-forward.
_THROTTLE_BITS = [
    (0, 16, "Under-voltage: the power supply is not keeping up"),
    (1, 17, "Speed capped to cope with power or heat"),
    (2, 18, "Throttled to protect itself"),
    (3, 19, "Soft temperature limit reached"),
]


def decode_throttle_word(word) -> dict:
    """Decode the Pi firmware throttle word into power-state readings. Pure.

    Returns {"ok", "live": [msg, ...], "since_boot": [msg, ...]}: ok is True
    when nothing is wrong right now, live lists conditions happening now, and
    since_boot lists ones that happened earlier but have cleared. A non-integer
    word returns {}.
    """
    if not isinstance(word, int):
        return {}
    live, since_boot = [], []
    for low, high, msg in _THROTTLE_BITS:
        if word & (1 << low):
            live.append(msg)
        elif word & (1 << high):
            since_boot.append(msg)
    return {"ok": not live, "live": live, "since_boot": since_boot}


# --- Disks -------------------------------------------------------------------

def disk_metrics(paths) -> list:
    """Disk usage for a list of (label, path), one entry per distinct
    filesystem. Paths that do not exist or cannot be measured are skipped;
    a later path on the same device as an earlier one is dropped, so the data
    directory and / collapse to one entry when they share a disk."""
    out, seen = [], set()
    for label, path in paths:
        try:
            dev = os.stat(path).st_dev
            if dev in seen:
                continue
            u = shutil.disk_usage(path)
        except OSError:
            continue
        seen.add(dev)
        out.append({
            "label": label,
            "path": path,
            "total": u.total,
            "used": u.used,
            "free": u.free,
            "percent": round(u.used * 100.0 / u.total, 1) if u.total else 0.0,
        })
    return out


# --- Collector ---------------------------------------------------------------

def _read(path):
    try:
        with open(path, "r") as f:
            return f.read()
    except OSError:
        return None


def sample_cpu(proc: str = "/proc") -> dict:
    """One parse_proc_stat sample; {} when /proc/stat is unreadable."""
    return parse_proc_stat(_read(os.path.join(proc, "stat")) or "")


def read_temperature(sys_root: str = "/sys"):
    """First plausible CPU temperature from the thermal zones, or None."""
    base = os.path.join(sys_root, "class", "thermal")
    try:
        zones = sorted(z for z in os.listdir(base) if z.startswith("thermal_zone"))
    except OSError:
        return None
    for zone in zones:
        c = parse_thermal(_read(os.path.join(base, zone, "temp")) or "")
        if c is not None:
            return c
    return None


def collect(data_dir: str, cpu_prev: dict, cpu_cur: dict,
            proc: str = "/proc", sys_root: str = "/sys") -> dict:
    """Gather every metric this environment offers into one dict.

    ``cpu_prev``/``cpu_cur`` are two sample_cpu() readings taken a moment
    apart (the caller owns the delay so this stays synchronous and pure-ish).
    Each section appears only when its source was readable; the result may be
    as small as {} on a fully locked-down host. Never raises.
    """
    out = {}

    cpu = cpu_percents(cpu_prev or {}, cpu_cur or {})
    load = parse_loadavg(_read(os.path.join(proc, "loadavg")) or "")
    if load is not None:
        cpu["load"] = load
    cores = len(cpu_cur or {}) - (1 if "cpu" in (cpu_cur or {}) else 0)
    if cores > 0:
        cpu["cores"] = cores
    if cpu:
        out["cpu"] = cpu

    mem = parse_meminfo(_read(os.path.join(proc, "meminfo")) or "")
    if mem:
        out["memory"] = mem

    up = parse_uptime(_read(os.path.join(proc, "uptime")) or "")
    if up is not None:
        out["uptime"] = {"seconds": int(up), "text": format_uptime(up)}

    temp = read_temperature(sys_root)
    if temp is not None:
        out["temperature"] = {"celsius": temp}

    disks = disk_metrics([("App data", data_dir), ("System disk", "/")])
    if disks:
        out["disks"] = disks

    return out
