"""Single source of truth for version-string comparison (FoodAssistant-ny8r).

Every surface that reasons about software versions (the GitHub update check, the
satellite "up to date / behind" badge on the Devices page) uses these helpers, so
the comparison rule is defined once and tested once rather than reimplemented per
caller. Versions are dotted numbers with an optional leading ``v`` and optional
non-numeric suffixes, which are ignored (``v1.2.3`` and ``1.2.3`` compare equal).
"""
from __future__ import annotations


def normalize(version: str) -> tuple:
    """Turn a version string like 'v1.2.3' into a comparable tuple (1, 2, 3).

    Strips a leading v/V, splits on dots, and keeps only the digits of each part
    so a suffix like '1.2.3-rc1' still compares on its numbers. A missing or
    non-numeric part counts as 0, so the result is always a tuple of ints.
    """
    parts = str(version or "").lstrip("vV").split(".")
    out = []
    for p in parts:
        num = "".join(c for c in p if c.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out)


def is_version_tag(name: str) -> bool:
    """True when ``name`` looks like a version tag (e.g. v1.0.0 or 1.2)."""
    body = str(name or "").lstrip("vV")
    return bool(body) and body[0].isdigit()


def is_newer(candidate: str, baseline: str) -> bool:
    """True when ``candidate`` is a strictly newer version than ``baseline``."""
    return normalize(candidate) > normalize(baseline)


def compare_to(device_version: str, server_version: str) -> str:
    """Classify a device's version against the server's for an update badge.

    Returns "unknown" (no version reported), "behind" (older than the server),
    "current" (same), or "ahead" (newer than the server, e.g. mid-rollout).
    """
    if not device_version:
        return "unknown"
    dv, sv = normalize(device_version), normalize(server_version)
    if dv < sv:
        return "behind"
    if dv > sv:
        return "ahead"
    return "current"


def _pad(t: tuple, n: int = 3) -> tuple:
    """Right-pad a version tuple with zeros to at least ``n`` parts."""
    return tuple(list(t) + [0] * (n - len(t)))[:max(n, len(t))]


def diff_level(device_version: str, server_version: str) -> str:
    """How far a device's version is from the server's, for a traffic-light badge.

    Returns "unknown" (no version), "same" (identical major.minor.patch, green),
    "patch" (only the patch differs, yellow), or "major_minor" (the major or minor
    differs, red). Pre-1.0 the minor acts as the feature line, so a 0.6 vs 0.7
    gap is treated as major_minor.
    """
    if not device_version:
        return "unknown"
    dv, sv = _pad(normalize(device_version)), _pad(normalize(server_version))
    if dv == sv:
        return "same"
    if dv[0] != sv[0] or dv[1] != sv[1]:
        return "major_minor"
    return "patch"
