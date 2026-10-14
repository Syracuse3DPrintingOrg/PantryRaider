"""The Home Assistant add-on builds exactly the release its version names.

The add-on used to build FROM the :latest image, so an update offered before
the new release's image was published quietly installed the previous release
under the new number and was never offered again. It now builds FROM the
release tag the Supervisor passes as BUILD_VERSION (the add-on's own version,
which the public sync stamps only with a published release). It also tells
the app it runs as an add-on (PANTRY_RAIDER_ADDON=1), which the access rules
need before they trust Home Assistant's sign-in for the sidebar page.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ADDON = Path(__file__).resolve().parents[1] / "homeassistant" / "addon" / "foodassistant"
DOCKERFILE = ADDON / "Dockerfile"
CONFIG = ADDON / "config.yaml"
IMAGE = "ghcr.io/syracuse3dprintingorg/pantryraider"


def _instructions() -> list[str]:
    """Dockerfile instructions with comments dropped and continuations joined."""
    out: list[str] = []
    pending = ""
    for raw in DOCKERFILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        out.append(pending + line)
        pending = ""
    if pending:
        out.append(pending)
    return out


def test_build_version_is_declared_before_the_only_from():
    lines = _instructions()
    froms = [i for i, line in enumerate(lines) if line.upper().startswith("FROM ")]
    assert len(froms) == 1
    args = [i for i, line in enumerate(lines)
            if re.fullmatch(r"ARG\s+BUILD_VERSION(=.*)?", line)]
    assert args and args[0] < froms[0]


def test_from_names_the_release_image_for_this_version():
    line = next(line for line in _instructions() if line.upper().startswith("FROM "))
    assert line.split()[1] == IMAGE + ":${BUILD_VERSION}"


def test_nothing_builds_from_latest():
    for line in _instructions():
        assert ":latest" not in line


def test_the_addon_marker_is_set():
    env = " ".join(line for line in _instructions() if line.upper().startswith("ENV "))
    assert re.search(r"\bPANTRY_RAIDER_ADDON=1\b", env)
    assert "DATA_DIR=/data" in env


def test_config_has_a_top_level_version():
    data = yaml.safe_load(CONFIG.read_text())
    assert isinstance(data, dict)
    assert str(data.get("version") or "").strip()
    assert re.search(r"(?m)^version:", CONFIG.read_text())
