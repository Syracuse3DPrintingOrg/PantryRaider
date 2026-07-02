#!/usr/bin/env python3
"""Build the GitHub wiki from the repository docs.

The wiki is a mirror, not a second source of truth: every page is generated
from a file already in the repo (README, CHANGELOG, and docs/*.md), so the docs
stay the one place to edit. A GitHub Action (.github/workflows/wiki-sync.yml)
runs this on every push to main and pushes the result to the <repo>.wiki.git
repository, so the wiki never drifts from the docs.

Output is a flat set of <Page-Name>.md files (the layout the GitHub wiki expects)
plus _Sidebar.md and _Footer.md for navigation. Links between mirrored pages are
rewritten to wiki page links; links to images and other repo files are rewritten
to absolute raw/blob URLs so they resolve from the wiki.

Usage: python scripts/build-wiki.py --out <dir> [--repo owner/name] [--branch main]
"""
from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

# Source file (repo-relative) -> (Wiki page name, sidebar section, sidebar label).
# Order here is the order pages appear in the sidebar within each section.
PAGES: list[tuple[str, str, str, str]] = [
    ("README.md",                               "Home",                  "Overview",  "Home"),
    ("docs/platforms.md",                       "Platforms",             "Overview",  "Platforms & deployment modes"),
    ("docs/personalization.md",                 "Personalization",       "Overview",  "Personalization & on-screen features"),
    ("docs/settings-matrix.md",                 "Settings-Reference",    "Overview",  "Settings reference"),
    ("docs/recipe-backend-comparison.md",       "Recipe-Backends",       "Overview",  "Recipe backends"),
    ("docs/hardware.md",                        "Hardware",              "Hardware",  "Hardware overview"),
    ("docs/hardware/supported-hardware.md",     "Supported-Hardware",    "Hardware",  "Supported hardware"),
    ("docs/hardware/sd-image.md",               "Building-the-SD-Image", "Hardware",  "Building the SD image"),
    ("docs/hardware/waveshare-barcode-scanner.md", "Barcode-Scanner-Setup", "Hardware", "Barcode scanner setup"),
    ("docs/api.md",                             "API",                   "Reference", "HTTP API"),
    ("docs/AI_DECLARATIONS.md",                 "AI-Declarations",       "Reference", "AI declarations"),
    ("CONTRIBUTING.md",                         "Contributing",          "Project",   "Contributing"),
    ("SECURITY.md",                             "Security",              "Project",   "Security policy"),
    ("CODE_OF_CONDUCT.md",                      "Code-of-Conduct",       "Project",   "Code of conduct"),
    ("CHANGELOG.md",                            "Changelog",             "Project",   "Changelog"),
]

SIDEBAR_SECTIONS = ["Overview", "Hardware", "Reference", "Project"]

_IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".woff", ".woff2"}
# Markdown inline links and images: ![alt](target) and [text](target).
_LINK_RE = re.compile(r"(!?)\[([^\]]*)\]\(([^)]+)\)")


def _src_to_page() -> dict[str, str]:
    """Map a repo-relative source path to its wiki page name."""
    return {src: name for src, name, _sec, _lbl in PAGES}


def _rewrite_target(target: str, src_dir: str, page_map: dict[str, str],
                    repo: str, branch: str) -> str:
    """Rewrite one link target for the wiki context."""
    target = target.strip()
    # Leave external links, in-page anchors, and live-app routes alone.
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", target) or target.startswith("#") \
            or target.startswith("mailto:") or target.startswith("/ui/"):
        return target
    # Split a trailing #anchor and an optional title (we keep targets simple).
    anchor = ""
    if "#" in target:
        target, anchor = target.split("#", 1)
        anchor = "#" + anchor
    if not target:  # was a bare "#anchor" inside the same page
        return anchor
    # Resolve relative to the source file's directory, into a repo-root path.
    rel = os.path.normpath(os.path.join(src_dir, target.lstrip("/")))
    rel = rel.replace(os.sep, "/")
    if rel in page_map:
        return page_map[rel] + anchor
    ext = os.path.splitext(rel)[1].lower()
    if ext in _IMG_EXT:
        return f"https://raw.githubusercontent.com/{repo}/{branch}/{rel}"
    # Any other in-repo file (an unmirrored .md, LICENSE, the demo html): point
    # at the rendered file on GitHub so the link still works from the wiki.
    return f"https://github.com/{repo}/blob/{branch}/{rel}{anchor}"


def _rewrite_links(text: str, src_path: str, page_map: dict[str, str],
                   repo: str, branch: str) -> str:
    src_dir = os.path.dirname(src_path)

    def repl(m: re.Match) -> str:
        bang, label, target = m.group(1), m.group(2), m.group(3)
        new_target = _rewrite_target(target, src_dir, page_map, repo, branch)
        return f"{bang}[{label}]({new_target})"

    return _LINK_RE.sub(repl, text)


def _build_sidebar() -> str:
    lines = ["## Pantry Raider wiki", ""]
    for section in SIDEBAR_SECTIONS:
        items = [(name, lbl) for src, name, sec, lbl in PAGES if sec == section]
        if not items:
            continue
        lines.append(f"### {section}")
        for name, lbl in items:
            lines.append(f"- [{lbl}]({name})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _footer(repo: str, branch: str) -> str:
    return (
        f"---\n\n_This wiki is generated automatically from the repository docs "
        f"on every push to `{branch}`. To change a page, edit its source under "
        f"[`docs/`](https://github.com/{repo}/tree/{branch}/docs) (or the README) "
        f"rather than editing the wiki directly, or your edit will be overwritten "
        f"on the next sync._\n"
    )


def build(root: Path, out: Path, repo: str, branch: str) -> int:
    out.mkdir(parents=True, exist_ok=True)
    page_map = _src_to_page()
    written = 0
    for src, name, _sec, _lbl in PAGES:
        sp = root / src
        if not sp.exists():
            print(f"  skip (missing): {src}")
            continue
        text = sp.read_text(encoding="utf-8")
        text = _rewrite_links(text, src, page_map, repo, branch)
        (out / f"{name}.md").write_text(text, encoding="utf-8")
        written += 1
        print(f"  {src} -> {name}.md")
    (out / "_Sidebar.md").write_text(_build_sidebar(), encoding="utf-8")
    (out / "_Footer.md").write_text(_footer(repo, branch), encoding="utf-8")
    print(f"Wrote {written} pages + _Sidebar + _Footer to {out}")
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the GitHub wiki from docs.")
    ap.add_argument("--out", default="_wiki", help="output directory")
    ap.add_argument("--repo", default="Syracuse3DPrintingOrg/PantryRaider",
                    help="owner/name for absolute links")
    ap.add_argument("--branch", default="main", help="branch for absolute links")
    args = ap.parse_args()
    root = Path(__file__).resolve().parents[1]
    build(root, Path(args.out), args.repo, args.branch)


if __name__ == "__main__":
    main()
