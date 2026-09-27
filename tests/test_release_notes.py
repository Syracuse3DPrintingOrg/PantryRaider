"""Release descriptions come from CHANGELOG.md (scripts/release-notes.sh).

Both release workflows write the GitHub Release body from this script, so the
release page shows the changelog entry the CHANGELOG header promises, plus the
license and source note the GPL Cub firmware needs. Runs the real script
against a small throwaway changelog.

Run: python -m pytest tests/test_release_notes.py -q
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "release-notes.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None,
                                reason="bash is not available")

CHANGELOG = """\
# Changelog

Some preamble that is never part of a release.

## [Unreleased]

### Fixed

- **Pending fix.** Waiting for the next release.

## [0.19.20] - 2026-09-20

### Added

- **Not this one.** A version that only shares a prefix.

## [0.19.2] - 2026-09-10

### Fixed

- **Cub firmware ships again.** Every board builds.

### Changed

- **Faster remote screens.**

## [0.19.0] - 2026-09-01

- **Older release.**
"""

FOOTER = ("Bandit Cub firmware in this release is GPL-3.0-or-later. Complete "
          "source: https://github.com/Syracuse3DPrintingOrg/PantryRaider/tree/"
          "v{v}/esphome")


def _notes(tmp_path: Path, version: str, text: str = CHANGELOG) -> str:
    log = tmp_path / "CHANGELOG.md"
    log.write_text(text)
    out = subprocess.run(["bash", str(SCRIPT), version, str(log)],
                         capture_output=True, text=True, check=True)
    return out.stdout


def test_prints_the_matching_section_and_nothing_else(tmp_path):
    notes = _notes(tmp_path, "v0.19.2")
    assert notes.startswith("### Fixed\n\n- **Cub firmware ships again.**")
    assert "- **Faster remote screens.**" in notes
    # Not the header itself, and not the neighbours on either side, including
    # a version that merely starts with the same digits.
    assert "## [" not in notes
    assert "Not this one" not in notes
    assert "Older release" not in notes
    assert "Pending fix" not in notes
    assert "preamble" not in notes


def test_accepts_a_version_without_the_v(tmp_path):
    assert _notes(tmp_path, "0.19.2") == _notes(tmp_path, "v0.19.2")


def test_falls_back_to_unreleased_when_the_version_has_no_header(tmp_path):
    notes = _notes(tmp_path, "v0.19.6")
    assert notes.startswith("### Fixed\n\n- **Pending fix.**")
    assert "Cub firmware ships again" not in notes
    assert "## [" not in notes


def test_footer_names_the_license_and_the_tagged_source(tmp_path):
    for version in ("v0.19.2", "v0.19.6"):
        notes = _notes(tmp_path, version)
        assert notes.rstrip("\n").endswith(FOOTER.format(v=version[1:]))
        # One blank line between the changelog text and the footer.
        assert "\n\n" + FOOTER.format(v=version[1:]) in notes


def test_footer_alone_when_the_changelog_has_nothing_to_say(tmp_path):
    notes = _notes(tmp_path, "v1.2.3", "# Changelog\n\n## [Unreleased]\n\n")
    assert notes == FOOTER.format(v="1.2.3") + "\n"


def test_missing_version_is_a_usage_error(tmp_path):
    out = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True)
    assert out.returncode != 0
    assert "usage" in out.stderr


def test_the_real_changelog_produces_notes():
    # Whatever version is current, the shipped CHANGELOG yields a body that
    # ends in the firmware footer (no header and no Unreleased text would
    # still leave the footer).
    out = subprocess.run(["bash", str(SCRIPT), "v0.0.0"],
                         capture_output=True, text=True, check=True)
    assert out.stdout.rstrip("\n").endswith(FOOTER.format(v="0.0.0"))
