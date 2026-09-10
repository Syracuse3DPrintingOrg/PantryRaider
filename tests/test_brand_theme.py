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
