"""Cook page icon compatibility (FoodAssistant-438t).

The "Tune My Suggestions" panel uses plain emoji characters as pill icons, so
every glyph must render on the fonts a real client actually has: the Windows
Segoe UI Emoji font (stuck around Emoji 12.0 on Windows 10) and Debian's
fonts-noto-color-emoji on the Pi kiosk. Two rules keep the icons visible
everywhere:

  1. No emoji newer than Emoji 12.0 (2019). The Unicode 13/14 food glyphs
     (olive, jar, ...) render as empty boxes on older platforms; that is
     exactly the "midwest mayo has no icon" bug.
  2. No flag sequences (regional indicator pairs). Windows Chrome draws them
     as bare letters like "GR" instead of an icon.

These are pure checks against the template text: no app import, no network.
"""
from __future__ import annotations

import re
from pathlib import Path

COOK = (Path(__file__).resolve().parents[1]
        / "service" / "app" / "templates" / "cook.html")

# Codepoints from the Emoji 13.0+ additions that live in blocks older clients
# already cover partially. Everything in the U+1FA70 block beyond the Emoji
# 12.0 set is disallowed, as is anything at U+1FB00 and above.
_EMOJI12_1FA_BLOCK = (
    set(range(0x1FA70, 0x1FA74))   # ballet shoes .. thread (E12)
    | set(range(0x1FA78, 0x1FA7B))  # drop of blood .. stethoscope (E12)
    | set(range(0x1FA80, 0x1FA83))  # yo-yo .. parachute (E12)
    | set(range(0x1FA90, 0x1FA96))  # ringed planet .. banjo (E12)
)


def _emoji_codepoints(text: str):
    for ch in text:
        cp = ord(ch)
        if cp >= 0x1F000 or 0x2190 <= cp <= 0x2BFF:
            yield ch, cp


def test_no_post_emoji12_glyphs():
    src = COOK.read_text(encoding="utf-8")
    bad = []
    for ch, cp in _emoji_codepoints(src):
        if 0x1FA00 <= cp <= 0x1FAFF and cp not in _EMOJI12_1FA_BLOCK:
            bad.append((ch, cp))
        if cp >= 0x1FB00:
            bad.append((ch, cp))
    assert not bad, (
        "cook.html uses emoji newer than Emoji 12.0, which render as empty "
        f"boxes on older clients: {[f'U+{cp:05X} {ch}' for ch, cp in bad]}"
    )


def test_no_flag_sequences():
    src = COOK.read_text(encoding="utf-8")
    flags = [ch for ch, cp in _emoji_codepoints(src)
             if 0x1F1E6 <= cp <= 0x1F1FF]
    assert not flags, (
        "cook.html uses flag emoji, which Windows renders as bare letters "
        f"instead of an icon: {flags}"
    )


def test_every_pill_and_slider_label_has_an_icon():
    """Each preference pill and slider end-label must carry SOME emoji so no
    entry (a la "midwest mayo") shows up icon-less."""
    src = COOK.read_text(encoding="utf-8")
    spans = re.findall(
        r'<span class="(?:diet-pill|cuisine-pill|tag-pill|end-label[^"]*)"'
        r'[^>]*>(.*?)</span>', src, re.S)
    assert len(spans) > 50, "expected the full pill set in cook.html"
    missing = [s.strip() for s in spans
               if not any(True for _ in _emoji_codepoints(s))]
    assert not missing, f"pills without an emoji icon: {missing}"
