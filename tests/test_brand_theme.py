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


def test_cyborg_has_readability_overlay():
    entry = config.THEMES["cyborg"]
    assert entry["stylesheet"] == "static/vendor/themes/cyborg.min.css"
    assert entry["overlay"] == "static/vendor/themes/cyborg-fix.css"
    css = (_THEMES / "cyborg-fix.css").read_text(encoding="utf-8")
    # Targets form controls and placeholder contrast.
    assert ".form-control" in css
    assert "placeholder" in css
    assert "@import" not in css
