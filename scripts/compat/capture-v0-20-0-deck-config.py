#!/usr/bin/env python3
"""Vendor the Stream Deck config loader as it shipped in v0.20.0.

Build-time only: run it from the repository root when the fixture needs to be
made again. It reads streamdeck/foodassistant_streamdeck/config.py at the
v0.20.0 tag (git show) and writes it, with the tag, the path and the blob sha,
to tests/data/compat_v0_20_0_deck_config.json. tests/test_deck_config_e2e.py
loads that source to prove a deck still on v0.20.0 reads the config this
release writes. The test suite never runs git itself.

    python3 scripts/compat/capture-v0-20-0-deck-config.py
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TAG = "v0.20.0"
PATH = "streamdeck/foodassistant_streamdeck/config.py"
OUT = ROOT / "tests" / "data" / "compat_v0_20_0_deck_config.json"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True,
                          capture_output=True, text=True).stdout


def main() -> None:
    source = _git("show", f"{TAG}:{PATH}")
    blob = _git("rev-parse", f"{TAG}:{PATH}").strip()
    OUT.write_text(json.dumps({
        "_comment": ("The Stream Deck config loader as it shipped in v0.20.0, vendored "
                     "at build time by scripts/compat/capture-v0-20-0-deck-config.py."),
        "tag": TAG,
        "path": PATH,
        "blob_sha": blob,
        "source": source,
    }, indent=2) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(source)} bytes, blob {blob[:12]})")


if __name__ == "__main__":
    main()
