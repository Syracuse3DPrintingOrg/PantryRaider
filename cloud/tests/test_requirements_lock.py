"""The Forager image installs cloud/requirements.lock, so the lock has to stay in
step with cloud/requirements.txt.

The Dockerfile installs the lock with --require-hashes, so a pin that is missing
from it, or locked at another version, fails the image build. These checks fail
first, in the test suite, on the commit that bumps a pin and forgets to
regenerate the lock (the command is in the Dockerfile comment above the
install).
"""
from __future__ import annotations

import re
from pathlib import Path

CLOUD = Path(__file__).resolve().parents[1]
REQUIREMENTS = CLOUD / "requirements.txt"
LOCK = CLOUD / "requirements.lock"
DOCKERFILE = CLOUD / "Dockerfile"

# "name[extra]==1.2.3  # trailing comment" -> ("name", "1.2.3")
_PIN = re.compile(r"^([A-Za-z0-9._-]+)\s*(?:\[[^\]]*\])?\s*==\s*([^\s;#]+)")
# A lock entry opens at column 0; its hashes and "# via" lines are indented.
_LOCK_ENTRY = re.compile(r"^([A-Za-z0-9._-]+)\s*==\s*([^\s;]+)")


def _normalize(name: str) -> str:
    """PEP 503 name normalization, so psycopg2_binary and psycopg2-binary match."""
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

    A universal resolution can list one package twice behind disjoint markers,
    so this collects a set rather than one value.
    """
    found: dict[str, set[str]] = {}
    for raw in LOCK.read_text().splitlines():
        if raw.startswith((" ", "\t", "#")) or not raw.strip():
            continue
        m = _LOCK_ENTRY.match(raw)
        if m:
            found.setdefault(_normalize(m.group(1)), set()).add(m.group(2))
    return found


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", version)[:3])


def test_every_requirement_is_an_exact_pin():
    for line in _requirement_lines():
        assert "==" in line, f"requirements.txt line is not pinned: {line!r}"
        assert not re.search(r"[<>~!]=|[<>]", line.split("#")[0]), (
            f"requirements.txt line uses a range instead of a pin: {line!r}")


def test_lock_covers_every_direct_pin_at_the_same_version():
    locked = _locked_versions()
    for name, version in _direct_pins().items():
        assert name in locked, (
            f"{name} is pinned in cloud/requirements.txt but missing from "
            "cloud/requirements.lock; regenerate the lock")
        assert version in locked[name], (
            f"{name} is pinned at {version} in cloud/requirements.txt but the "
            f"lock has {sorted(locked[name])}; regenerate the lock")


def test_every_locked_entry_carries_hashes():
    """--require-hashes rejects the whole file if one entry has no hash."""
    lines = LOCK.read_text().splitlines()
    seen = 0
    for i, line in enumerate(lines):
        m = _LOCK_ENTRY.match(line)
        if not m:
            continue
        seen += 1
        block = []
        for follower in lines[i + 1:]:
            if follower and not follower[0].isspace():
                break
            block.append(follower)
        assert any("--hash=sha256:" in b for b in block), (
            f"{_normalize(m.group(1))} has no hash in cloud/requirements.lock")
    assert seen, "cloud/requirements.lock has no pinned entries"


def test_lock_is_universal():
    """A lock compiled for one platform drops the environment markers another
    build host needs."""
    header = "\n".join(LOCK.read_text().splitlines()[:3])
    assert "--universal" in header, (
        "cloud/requirements.lock was not compiled with --universal")


def test_image_installs_the_lock_with_hash_checking():
    dockerfile = DOCKERFILE.read_text()
    assert "--require-hashes -r requirements.lock" in dockerfile, (
        "the Forager image must install requirements.lock with --require-hashes")
    assert re.search(r"^COPY .*requirements\.lock", dockerfile, re.M), (
        "the Dockerfile must copy requirements.lock into the build")
    assert not re.search(r"pip install[^\n]*-r requirements\.txt", dockerfile), (
        "the Dockerfile must not install the unhashed requirements.txt")


def test_starlette_moves_with_fastapi():
    """Newer fastapi walks every key of a form body for each Form parameter;
    only Starlette 1.3.1 or later caps a urlencoded body at 1000 fields. The
    fastapi bump without the Starlette one let junk form keys posted to /signup
    hold the worker for tens of seconds, so the pair is checked together."""
    pins = _direct_pins()
    assert "starlette" in pins, "starlette must be pinned explicitly"
    assert _version_tuple(pins["starlette"]) >= (1, 3, 1)
    for version in _locked_versions()["starlette"]:
        assert _version_tuple(version) >= (1, 3, 1), (
            f"the lock resolves starlette=={version}, below 1.3.1")
