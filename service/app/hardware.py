"""Host hardware detection.

Small, dependency-free probes the setup wizard uses to tailor itself to the
device it is running on. The big one is "are we on a Raspberry Pi", which
decides whether to offer the Pi deployment modes (Pi Hosted, Pi Remote) and
hide the generic "Server hosted" mode.

All probes are read-only and degrade to a safe default (False) when the source
they read is missing, so they are safe to call on any platform, including in
tests and CI where none of these files exist.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache

# Files the kernel/firmware expose with the board model string. On Raspberry Pi
# OS both contain "Raspberry Pi ..."; /proc/cpuinfo carries a "Model" line on
# older images. We read whichever is present.
_MODEL_FILES = (
    "/proc/device-tree/model",
    "/sys/firmware/devicetree/base/model",
)


def _read_model() -> str:
    """Return the board model string, or '' if none is exposed.

    The device-tree node is NUL-terminated, so strip trailing NULs/whitespace.
    """
    # Test/override hook: FOODASSISTANT_FORCE_MODEL lets tests and the demo
    # setup pretend to be (or not be) a Pi without touching the filesystem.
    forced = os.environ.get("FOODASSISTANT_FORCE_MODEL")
    if forced is not None:
        return forced
    for path in _MODEL_FILES:
        try:
            with open(path, "rb") as fh:
                return fh.read().decode("utf-8", "replace").strip("\x00").strip()
        except OSError:
            continue
    return ""


@lru_cache(maxsize=1)
def is_raspberry_pi() -> bool:
    """True when the host is a Raspberry Pi.

    Cached: the answer cannot change for the life of the process. Tests that
    exercise both branches clear the cache via ``is_raspberry_pi.cache_clear()``.
    """
    return "raspberry pi" in _read_model().lower()


@lru_cache(maxsize=1)
def board_model() -> str:
    """Human-readable board model (e.g. 'Raspberry Pi 5 Model B'), or ''."""
    return _read_model()


# Capability detection -------------------------------------------------------
#
# On a Raspberry Pi we tailor the deployment modes and heavy features to what
# the board can actually run, so a user on a weak board is not steered into a
# setup that will not work. Two coarse signals drive this:
#
#   * board tier  - parsed from board_model(): a Pi 3 / Zero / older is "low",
#                   a Pi 4 / 5 (or anything newer) is "capable".
#   * total RAM   - read from /proc/meminfo so it works without psutil.
#
# Both degrade to "unknown" when the source is missing, and callers treat
# unknown as "do not restrict" so a misdetect never blocks a capable box.

# Minimum total RAM (in MB) a board needs before we offer the local-stack Pi
# Hosted mode. The default stack is light now that Mealie is off by default
# (Grocy plus the Pantry Raider app, which run comfortably alongside the kiosk
# on a 2 GB Pi with zram swap), so this gates on the 2 GB class, not 4 GB. A
# 2 GB Pi reports a little under 2048 MB once firmware reserves its slice, so
# the threshold sits below that on purpose; a 1 GB board (and the low-tier Pi
# families below) is still excluded. The heavy opt-ins (local Ollama, Mealie)
# are separate profiles the user turns on deliberately, not gated here.
MIN_HOSTED_RAM_MB = 1500

# Board families we treat as too weak for the local stack regardless of any RAM
# reading. These are matched as whole "Pi <n>" tokens in the model string.
_LOW_TIER_PI_NUMBERS = ("0", "1", "2", "3")


def _read_total_ram_mb_proc() -> int | None:
    """Total system RAM in MB from /proc/meminfo, or None if unreadable."""
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    # Format: "MemTotal:       3884360 kB"
                    kb = int(line.split()[1])
                    return kb // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def total_ram_mb() -> int | None:
    """Total system RAM in MB, or None when it cannot be determined.

    Test/override hook: FOODASSISTANT_FORCE_RAM_MB lets tests and the demo
    setup pretend a given amount of RAM without touching /proc/meminfo. An
    empty value forces the "unknown" branch.
    """
    forced = os.environ.get("FOODASSISTANT_FORCE_RAM_MB")
    if forced is not None:
        try:
            return int(forced)
        except ValueError:
            return None
    return _read_total_ram_mb_proc()


def board_tier(model: str | None = None) -> str:
    """Coarse capability tier for the board: 'low', 'capable', or 'unknown'.

    A Pi 0/1/2/3 (and the original Zero) is 'low'; a Pi 4/5 or newer is
    'capable'. Anything we cannot classify (including non-Pi hosts and an empty
    model string) is 'unknown', so callers can fall back to a safe default
    rather than over-restricting.
    """
    name = (model if model is not None else board_model()).lower()
    if "raspberry pi" not in name:
        return "unknown"
    # Pull the family number that follows "pi" (e.g. "raspberry pi 4 model b").
    match = re.search(r"raspberry pi\s+(\d+)", name)
    if not match:
        # Pi Zero has no leading number ("Raspberry Pi Zero 2 W"); treat as low.
        if "zero" in name:
            return "low"
        return "unknown"
    number = match.group(1)
    if number in _LOW_TIER_PI_NUMBERS:
        return "low"
    return "capable"


def supports_local_stack(model: str | None = None, ram_mb: int | None = None) -> bool:
    """Whether this host can reasonably run the local stack (Grocy + extras).

    True unless we are confident the board is too weak: a low-tier Pi family,
    or a measured RAM total below MIN_HOSTED_RAM_MB. Uncertain detection
    (unknown tier, unreadable RAM) returns True so a capable box is never
    blocked by a misdetect. The RAM argument defaults to the live reading.
    """
    if board_tier(model) == "low":
        return False
    ram = ram_mb if ram_mb is not None else total_ram_mb()
    if ram is not None and ram < MIN_HOSTED_RAM_MB:
        return False
    return True
