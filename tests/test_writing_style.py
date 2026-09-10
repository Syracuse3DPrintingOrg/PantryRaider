"""The repo's writing-style rules, enforced over everything a reader sees.

AGENTS.md sets two hard rules for all project copy (docs, README, CHANGELOG,
UI text, comments, commit messages): no em dashes, and no box diagrams. Until
now they were pinned by three spot checks on individual strings
(test_pi_health, test_grocy_config_error, test_weather), so a stray em dash
anywhere else shipped quietly and got caught, if at all, in review.

Scope is the copy the project itself writes: the top-level docs, the docs site,
the templates and browser scripts, the app and its companion packages, the Home
Assistant configs, and the shell tooling. Vendored third-party code and the
dashboard worker are excluded because their wording is not ours to fix.

Box diagrams are keyed on the four corner characters, not on the horizontal
run: a plain horizontal is used deliberately as a comment divider in first-party
files, and keying on it would fail on day one while saying nothing about
diagrams.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]

EM_DASH = "—"
BOX_CORNERS = ("┌", "┐", "└", "┘")

# Single files, then (directory, extensions to walk).
_FILES = ["README.md", "CHANGELOG.md", "CONTRIBUTING.md"]
_TREES = [
    ("docs", (".md",)),
    ("service/app/templates", (".html",)),
    ("service/app/static/js", (".js",)),
    ("service/app", (".py",)),
    ("streamdeck", (".py", ".md")),
    ("gadgets", (".py", ".md")),
    ("homeassistant", (".py", ".md", ".yaml", ".yml")),
    ("scripts", (".py", ".sh", ".md")),
]
# Third-party code and the internal worker: not our prose.
_SKIP = ("docs/demo/vendor", "service/app/static/js/vendor", "dashboard",
         "__pycache__", "node_modules", ".git")


def _tracked_files() -> list[Path]:
    out: list[Path] = []
    for name in _FILES:
        p = _ROOT / name
        if p.is_file():
            out.append(p)
    for rel, suffixes in _TREES:
        base = _ROOT / rel
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if p.suffix not in suffixes or not p.is_file():
                continue
            posix = p.relative_to(_ROOT).as_posix()
            if any(skip in posix for skip in _SKIP):
                continue
            out.append(p)
    return sorted(set(out))


def _hits(text: str, needles) -> list[str]:
    """Every line of `text` containing one of `needles`, numbered."""
    return [f"line {n}: {line.strip()[:90]}"
            for n, line in enumerate(text.splitlines(), 1)
            if any(needle in line for needle in needles)]


def _offenders(needles) -> list[str]:
    found = []
    for path in _tracked_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for hit in _hits(text, needles):
            found.append(f"{path.relative_to(_ROOT)}: {hit}")
    return found


def test_the_guard_actually_reads_the_tree():
    """A broken glob would make every check below pass while reading nothing."""
    files = _tracked_files()
    names = {p.relative_to(_ROOT).as_posix() for p in files}
    assert len(files) > 200, f"only {len(files)} files matched; the walk is broken"
    assert "README.md" in names
    assert any(n.startswith("service/app/templates/") for n in names)
    assert any(n.startswith("service/app/static/js/") for n in names)
    assert not any("/vendor/" in n for n in names), "vendored code is not ours to style"


def test_no_em_dash_in_project_copy():
    offenders = _offenders([EM_DASH])
    assert offenders == [], (
        "em dashes are not used in this project (AGENTS.md: Writing Style). "
        "Use a comma, parentheses, a colon, or rewrite the sentence:\n"
        + "\n".join(offenders))


def test_no_box_diagrams_in_project_copy():
    offenders = _offenders(BOX_CORNERS)
    assert offenders == [], (
        "box diagrams are not used in this project (AGENTS.md: Writing Style). "
        "Describe the structure in prose or a table:\n" + "\n".join(offenders))


@pytest.mark.parametrize("bad", [
    "The update runs nightly — unless you turn it off.",
    "┌──┐",
])
def test_the_rules_catch_what_they_are_meant_to_catch(bad):
    """The check is line based, so prove it fires on the shapes it targets
    rather than trusting that a clean tree means a working rule."""
    assert _hits(bad, [EM_DASH, *BOX_CORNERS])
