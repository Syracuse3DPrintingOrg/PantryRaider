"""Well-formedness checks for the THEMES registry (FoodAssistant-f6oy).

Pure-data assertions: every theme entry has the required keys, a valid mode, and
any referenced stylesheet/overlay file actually exists under the static dir. This
guards against a typo in a vendored path (which would 404 the theme CSS) or a new
theme registered without its file shipped.
"""
from pathlib import Path

from app.config import THEMES, _DEFAULT_THEME

_STATIC_ROOT = Path(__file__).parent.parent / "service" / "app"


def test_default_theme_is_registered():
    assert _DEFAULT_THEME in THEMES


def test_every_theme_is_well_formed():
    for name, info in THEMES.items():
        assert set(info) >= {"label", "mode", "stylesheet", "overlay"}, (
            f"theme {name!r} missing required keys"
        )
        assert info["label"], f"theme {name!r} has an empty label"
        assert info["mode"] in ("light", "dark"), (
            f"theme {name!r} has invalid mode {info['mode']!r}"
        )
        # A theme uses at most one of stylesheet/overlay (the native dark/light
        # and the settings-driven custom theme use neither).
        assert not (info["stylesheet"] and info["overlay"]), (
            f"theme {name!r} sets both a stylesheet and an overlay"
        )


def test_theme_asset_paths_exist():
    for name, info in THEMES.items():
        for key in ("stylesheet", "overlay"):
            rel = info[key]
            if rel is None:
                continue
            path = _STATIC_ROOT / rel
            assert path.is_file(), (
                f"theme {name!r} {key} path {rel!r} does not exist at {path}"
            )
