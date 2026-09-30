"""Pantry Raider brand theme + Cyborg readability (FoodAssistant-qoax, -4t5y).

Guards:
  * the brand theme exists, is the default, and heads the picker;
  * existing installs that already persisted a theme are not force-migrated;
  * Cyborg carries the input-readability overlay.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "service"))

from app import config  # noqa: E402

_THEMES = _ROOT / "service/app/static/vendor/themes"


def test_brand_theme_registered():
    assert "pantryraider" in config.THEMES
    entry = config.THEMES["pantryraider"]
    assert entry["mode"] == "dark"
    assert entry["stylesheet"] is None
    assert entry["overlay"] == "static/vendor/themes/pantryraider.css"
    # Overlay file is present and self-contained (no CDN import).
    css = (_THEMES / "pantryraider.css").read_text(encoding="utf-8")
    assert "F2006E" in css or "f2006e" in css.lower()
    assert "@import" not in css and "http" not in css


def test_brand_theme_is_default():
    assert config._DEFAULT_THEME == "pantryraider"


def test_brand_theme_heads_the_picker():
    assert next(iter(config.THEMES)) == "pantryraider"


def test_unset_install_resolves_to_brand_default():
    # A fresh install with no persisted ui_theme falls back to the new default.
    fresh = config.Settings()
    assert fresh.ui_theme == "pantryraider"
    assert config.theme_info(fresh.ui_theme)["overlay"] == \
        "static/vendor/themes/pantryraider.css"


def test_existing_dark_install_is_not_force_migrated():
    # An install that already chose "dark" keeps rendering dark, no migration.
    info = config.theme_info("dark")
    assert info["mode"] == "dark"
    assert info["stylesheet"] is None
    assert info["overlay"] is None
    # The "dark" theme is still present (not removed or renamed).
    assert "dark" in config.THEMES


def test_wizard_finish_does_not_post_a_hardcoded_theme():
    """The first-time wizard must not write a theme the user never picked.

    The Appearance pane (and its #ui_theme select) only renders once the app is
    configured, so during the wizard buildPayload() finds no element. A literal
    fallback there posted "dark" on every fresh install, which /setup/save wrote
    straight into settings.json: the brand default above was correct and was
    being overwritten before the user ever saw a setting. The field has to be
    omitted instead, so exclude_unset leaves the default alone.
    """
    src = (_ROOT / "service/app/static/js/setup/panes.js").read_text(encoding="utf-8")
    line = next((ln for ln in src.splitlines() if ln.strip().startswith("ui_theme:")), None)
    assert line is not None, "buildPayload no longer sends ui_theme; update this guard"
    assert "'dark'" not in line and '"dark"' not in line, (
        "buildPayload must not fall back to a hardcoded theme: the wizard has no "
        f"#ui_theme control, so this posts that value on every fresh install: {line!r}")
    assert "undefined" in line, (
        "with no #ui_theme control the field must be undefined so JSON.stringify "
        f"drops it and the stored/default theme survives: {line!r}")


def test_cyborg_has_readability_overlay():
    entry = config.THEMES["cyborg"]
    assert entry["stylesheet"] == "static/vendor/themes/cyborg.min.css"
    assert entry["overlay"] == "static/vendor/themes/cyborg-fix.css"
    css = (_THEMES / "cyborg-fix.css").read_text(encoding="utf-8")
    # Targets form controls and placeholder contrast.
    assert ".form-control" in css
    assert "placeholder" in css
    assert "@import" not in css


# --- Active nav fills clear WCAG AA (FoodAssistant-aflys) ---------------------

def _srgb_luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    chans = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in chans]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_srgb_luminance(a), _srgb_luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _rule(css: str, selector: str) -> str:
    import re
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, f"{selector} has no rule in pantryraider.css"
    return m.group(1)


def test_active_nav_fills_meet_wcag_aa_with_white_text():
    """White on the brand pink #F2006E is 4.20:1, under the 4.5:1 AA needs for
    normal-size text. The active nav pill, the page sub-pill and the floating
    nav all put white labels on their fill, so they use --pr-pink-deep."""
    import re
    css = (_THEMES / "pantryraider.css").read_text(encoding="utf-8")
    deep = re.search(r"--pr-pink-deep:\s*(#[0-9a-fA-F]{6})", css).group(1)
    assert _contrast("#ffffff", deep) >= 4.5, (deep, _contrast("#ffffff", deep))
    # The brand pink itself is the reason for the swap.
    assert _contrast("#ffffff", "#F2006E") < 4.5

    pills = _rule(css, ".nav-pills .nav-link.active")
    assert "background-color: var(--pr-pink-deep)" in pills, pills
    # base.css loads after this overlay and styles these from --bs-primary, so
    # the overrides need the extra html selector to win on specificity.
    sub = _rule(css, "html .subpill-btn.active")
    assert "background: var(--pr-pink-deep)" in sub and "border-color: var(--pr-pink-deep)" in sub, sub
    flt = _rule(css, "html .float-nav-link.active")
    assert "background: var(--pr-pink-deep)" in flt, flt

    base = (_ROOT / "service/app/templates/base.html").read_text(encoding="utf-8")
    assert base.index("theme_overlay") < base.index("static/css/base.css"), (
        "base.css no longer loads after the overlay; revisit the html prefix")
