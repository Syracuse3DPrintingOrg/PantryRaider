"""Every script that runs on Linux must check out with LF line endings.

A Windows git client converts text files to CRLF unless .gitattributes says
otherwise. For a script with a shebang that turns "#!/bin/bash" into
"#!/bin/bash\\r", which fails with "bad interpreter" on the Pi. The extensionless
helpers under scripts/image-build/ match no "*.sh" rule, so they need rules of
their own; this test fails when a script with a shebang is not covered.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".ico", ".gz", ".woff", ".woff2", ".ttf"}


def _git(*args: str, stdin: str | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, input=stdin, capture_output=True,
        text=True, check=True,
    ).stdout


def _tracked_files() -> list[str]:
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    try:
        return _git("ls-files", "-z").split("\0")
    except subprocess.CalledProcessError:
        pytest.skip("git ls-files failed")


def _has_shebang(rel: str) -> bool:
    path = ROOT / rel
    if not rel or path.suffix.lower() in BINARY_SUFFIXES or not path.is_file():
        return False
    with path.open("rb") as fh:
        return fh.read(2) == b"#!"


def _eol(paths: list[str]) -> dict[str, str]:
    out = _git("check-attr", "-z", "--stdin", "eol", stdin="\0".join(paths) + "\0")
    parts = out.split("\0")
    # -z output is path, attribute, value triples.
    return {parts[i]: parts[i + 2] for i in range(0, len(parts) - 2, 3)}


def test_every_script_with_a_shebang_checks_out_as_lf():
    scripts = [f for f in _tracked_files() if _has_shebang(f)]
    assert scripts, "expected to find scripts with a shebang"
    eol = _eol(scripts)
    missing = sorted(f for f in scripts if eol.get(f) != "lf")
    assert not missing, (
        "add an 'eol=lf' rule to .gitattributes for: " + ", ".join(missing)
    )


def test_the_image_build_helpers_are_covered():
    helpers = sorted(
        str(p.relative_to(ROOT))
        for p in (ROOT / "scripts" / "image-build").glob("foodassistant-*")
        if p.is_file()
    )
    assert helpers
    _tracked_files()
    eol = _eol(helpers)
    assert all(eol.get(h) == "lf" for h in helpers), eol
