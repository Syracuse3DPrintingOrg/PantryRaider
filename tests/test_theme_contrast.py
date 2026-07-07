"""Theme contrast hardening (FoodAssistant-tjlm).

Two guards:
  * the bundled theme CSS files contain no stray markup (an authoring artifact
    left XML tags like </content> in three overlays, which broke CSS parsing of
    every rule after them); and
  * base.html and setup.html carry the contrast-hardening rules that make the
    active side-menu pill, the Save (outline-info) buttons, and accent badges
    legible on top of the bundled themes.
"""
from __future__ import annotations

from pathlib import Path

_THEMES = Path(__file__).resolve().parents[1] / "service/app/static/vendor/themes"
_TEMPLATES = Path(__file__).resolve().parents[1] / "service/app/templates"

# Anything that should never appear inside a CSS file: leaked tool/markup tags.
_FORBIDDEN = ("</content>", "</invoke>", "<invoke", "<parameter", "antml:")


def test_theme_css_has_no_stray_markup():
    offenders = {}
    for css in sorted(_THEMES.glob("*.css")):
        text = css.read_text(encoding="utf-8", errors="replace")
        hits = [tok for tok in _FORBIDDEN if tok in text]
        if hits:
            offenders[css.name] = hits
    assert not offenders, f"stray markup in theme CSS: {offenders}"


def test_overlay_css_files_balance_braces():
    # A truncated or corrupted overlay shows up as unbalanced braces; the small
    # authored overlays (not the large vendored Bootswatch min files) should
    # parse cleanly.
    authored = ("synthwave.css", "solarized.css", "midnight.css", "forest.css",
                "ios-light.css", "ios-dark.css", "outrun.css", "vaporwave.css")
    for name in authored:
        text = (_THEMES / name).read_text(encoding="utf-8")
        assert text.count("{") == text.count("}"), f"unbalanced braces in {name}"


def test_base_template_has_contrast_hardening():
    base = (_TEMPLATES / "base.html").read_text(encoding="utf-8")
    # Active pill forced to readable text over the saturated pill background.
    assert ".nav-pills .nav-link.active" in base
    assert "#fff" in base
    # Save (outline-info) buttons get a readable colour per mode.
    assert ".btn-outline-info" in base
    # Accent badges get dark text where the background is light/bright.
    assert ".badge.bg-success" in base


def test_setup_template_mirrors_contrast_hardening():
    setup = (_TEMPLATES / "setup.html").read_text(encoding="utf-8")
    assert ".side-menu .nav-link.active" in setup
    assert ".btn-outline-info" in setup
