#!/usr/bin/env python3
"""Export the Stream Deck action catalog to service/app/data/deck_catalog.json.

The catalog is the same list the host bridge serves from
GET /streamdeck/actions on a Pi (python -m foodassistant_streamdeck
--dump-actions): one dict per assignable action with name, label, kind,
group, color, icon, and description. Bundling it into the app package lets
off-Pi servers (and a Pi whose bridge is briefly unreachable) show the full
key palette in the Start Page and Stream Deck editors instead of a stale
hardcoded fallback.

Run this after adding or changing an action in
streamdeck/foodassistant_streamdeck/actions.py, then commit the JSON.
Deliberately NOT wired into the pre-commit hook: regeneration stays a
manual step, and tests/test_deck_catalog.py fails the suite whenever the
checked-in file drifts from the live registry, which is the guard.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Import the controller package straight from the repo, the same trick
# tests/conftest.py uses.
sys.path.insert(0, str(REPO_ROOT / "streamdeck"))

OUT_PATH = REPO_ROOT / "service" / "app" / "data" / "deck_catalog.json"


def main() -> int:
    from foodassistant_streamdeck.actions import catalog

    actions = catalog()
    # Keys sorted, stable indentation, trailing newline: clean diffs.
    OUT_PATH.write_text(
        json.dumps(actions, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Wrote {len(actions)} actions to {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
