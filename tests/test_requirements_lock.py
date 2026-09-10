"""The image installs service/requirements.lock, so the lock has to stay in step
with service/requirements.txt.

The lock went stale once before: it was regenerated, then four dependencies were
added to requirements.txt over the following releases without a new lock, so the
lock resolved a tree the app could no longer run on. Nothing caught it because
nothing consumed the lock. Now the Dockerfile installs from it with
--require-hashes, which means a lock that is missing a pin fails the build
outright. These checks fail first, in the test suite, on the commit that
forgets it.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
REQUIREMENTS = REPO / "service" / "requirements.txt"
LOCK = REPO / "service" / "requirements.lock"
DOCKERFILE = REPO / "service" / "Dockerfile"

# "name[extra]==1.2.3  # trailing comment" -> ("name", "1.2.3")
_PIN = re.compile(r"^([A-Za-z0-9._-]+)\s*(?:\[[^\]]*\])?\s*==\s*([^\s;#]+)")
# A lock entry opens at column 0; its hashes and "# via" lines are indented.
_LOCK_ENTRY = re.compile(r"^([A-Za-z0-9._-]+)\s*==\s*([^\s;]+)")


def _normalize(name: str) -> str:
    """PEP 503 name normalization, so Pillow, pillow and recipe_scrapers match."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_lines() -> list[str]:
    lines = []
    for raw in REQUIREMENTS.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def _direct_pins() -> dict[str, str]:
    pins = {}
    for line in _requirement_lines():
        m = _PIN.match(line)
        assert m, f"requirements.txt line is not an exact pin: {line!r}"
        pins[_normalize(m.group(1))] = m.group(2)
    return pins


def _locked_versions() -> dict[str, set[str]]:
    """Name -> every version the lock carries for it.

    A universal resolution can list one package twice behind disjoint markers
    (a Python version split, say), so this collects a set rather than one value.
    """
    found: dict[str, set[str]] = {}
    for raw in LOCK.read_text().splitlines():
        if raw.startswith((" ", "\t", "#")) or not raw.strip():
            continue
        m = _LOCK_ENTRY.match(raw)
        if m:
            found.setdefault(_normalize(m.group(1)), set()).add(m.group(2))
    return found


def test_every_requirement_is_an_exact_pin():
    """No floors. `anthropic>=0.45.0` silently shipped a different SDK on every
    rebuild, which is exactly what a lock is supposed to prevent."""
    for line in _requirement_lines():
        assert "==" in line, f"requirements.txt line is not pinned: {line!r}"
        assert not re.search(r"[<>~!]=|[<>]", line.split("#")[0]), (
            f"requirements.txt line uses a range instead of a pin: {line!r}")


def test_lock_covers_every_direct_pin_at_the_same_version():
    locked = _locked_versions()
    for name, version in _direct_pins().items():
        assert name in locked, (
            f"{name} is pinned in requirements.txt but missing from "
            "requirements.lock; regenerate the lock (README: Dependencies)")
        assert version in locked[name], (
            f"{name} is pinned at {version} in requirements.txt but the lock "
            f"has {sorted(locked[name])}; regenerate the lock")


def test_every_locked_entry_carries_hashes():
    """--require-hashes rejects the whole file if one entry has no hash."""
    lines = LOCK.read_text().splitlines()
    seen = 0
    for i, line in enumerate(lines):
        m = _LOCK_ENTRY.match(line)
        if not m:
            continue
        seen += 1
        # An entry runs until the next column-0 line; its hashes are indented.
        block = []
        for follower in lines[i + 1:]:
            if follower and not follower[0].isspace():
                break
            block.append(follower)
        assert any("--hash=sha256:" in b for b in block), (
            f"{_normalize(m.group(1))} has no hash in requirements.lock")
    assert seen, "requirements.lock has no pinned entries"


def test_lock_is_universal_so_arm64_builds_resolve():
    """The published image is multi-arch. A lock compiled for one architecture
    drops the environment markers the other needs."""
    header = "\n".join(LOCK.read_text().splitlines()[:3])
    assert "--universal" in header, (
        "requirements.lock was not compiled with --universal; an arm64 build "
        "can miss platform-marked dependencies")


def test_image_installs_the_lock_with_hash_checking():
    dockerfile = DOCKERFILE.read_text()
    assert "--require-hashes -r requirements.lock" in dockerfile, (
        "the image must install requirements.lock with --require-hashes")
    assert "service/requirements.lock" in dockerfile, (
        "the Dockerfile must copy requirements.lock into the build")


def test_security_floors_hold():
    """Versions below these carry known advisories reachable from this app's own
    routes, so a future edit must not walk them back."""
    pins = _direct_pins()
    floors = {
        # Unbounded CPU on a malformed multipart body (CVE-2024-53981); every
        # photo, receipt, and recipe upload route parses one.
        "python-multipart": (0, 0, 18),
        # Sandbox escapes fixed in 3.1.6; the app renders user-supplied text.
        "jinja2": (3, 1, 6),
        # The "Import from PDF" route hands an uploaded file straight to this
        # parser, and the cloud companion pins the same floor.
        "pypdf": (6, 4, 0),
    }
    for name, floor in floors.items():
        parts = tuple(int(p) for p in re.findall(r"\d+", pins[name])[:3])
        assert parts >= floor, (
            f"{name}=={pins[name]} is below the required {'.'.join(map(str, floor))}")


def test_resolved_starlette_carries_the_request_handling_fixes():
    """starlette is transitive (fastapi picks it), so the floor has to be checked
    on the lock, not on requirements.txt. Keeping fastapi on a line that caps
    starlette below 1.0 is what holds this at a 0.49.1-or-newer 0.x release."""
    versions = _locked_versions()["starlette"]
    for version in versions:
        parts = tuple(int(p) for p in re.findall(r"\d+", version)[:3])
        assert parts >= (0, 49, 1), f"starlette=={version} is below 0.49.1"
