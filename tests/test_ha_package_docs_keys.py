"""The Home Assistant package docs match the packages: every request sends a key.

From 0.21 the REST commands and sensors in homeassistant/*.yaml always send
X-API-Key from secrets.yaml, so a guide that says the key is only for an
install with a password (and to uncomment the headers) leaves a kitchen with
no password without the secret, and Home Assistant then refuses to load the
package. Every guide must say to create the key, with the label Settings,
Security shows for it.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.services import access

ROOT = Path(__file__).resolve().parents[1]
_UNCOMMENT_HEADERS = re.compile(r"uncomment[^.\n]*header", re.IGNORECASE)


def _guides():
    out = sorted((ROOT / "docs").rglob("*.md")) + sorted((ROOT / "homeassistant").glob("*.md"))
    out += sorted((ROOT / "homeassistant").glob("*.yaml"))
    return [p for p in out if "/vendor/" not in p.as_posix()]


def test_no_guide_says_to_uncomment_the_headers():
    bad = []
    for path in _guides():
        text = path.read_text(encoding="utf-8")
        for m in _UNCOMMENT_HEADERS.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            bad.append(f"{path.relative_to(ROOT)}:{line}: {m.group(0)}")
    assert not bad, "\n".join(bad)


def test_the_espcontrol_guide_names_the_key_to_create():
    text = " ".join((ROOT / "docs" / "hardware" / "espcontrol.md").read_text().split())
    assert "pantry_raider_api_key" in text
    assert access.SCOPE_LABELS["ha_rest"] in text
    assert "Settings, Security" in text
    package = (ROOT / "homeassistant" / "espcontrol.yaml").read_text()
    assert "X-API-Key: !secret pantry_raider_api_key" in package
