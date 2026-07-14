"""Advanced display safe-area margins: the pure-logic clamp and the save path.

The margins are a device-local per-edge inset (pixels) that pulls the kiosk UI in
from a panel edge that clips. The clamp keeps a stray value from ever pushing the
whole interface off-screen, so it is the piece most worth pinning.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "service"))

from app.config import (  # noqa: E402
    DISPLAY_MARGIN_MAX,
    clamp_display_margin,
)


def test_clamp_passes_through_in_range():
    assert clamp_display_margin(0) == 0
    assert clamp_display_margin(20) == 20
    assert clamp_display_margin(DISPLAY_MARGIN_MAX) == DISPLAY_MARGIN_MAX


def test_clamp_floors_negatives_to_zero():
    assert clamp_display_margin(-1) == 0
    assert clamp_display_margin(-500) == 0


def test_clamp_caps_at_max():
    assert clamp_display_margin(DISPLAY_MARGIN_MAX + 1) == DISPLAY_MARGIN_MAX
    assert clamp_display_margin(100000) == DISPLAY_MARGIN_MAX


def test_clamp_accepts_numeric_strings_and_floats():
    assert clamp_display_margin("24") == 24
    assert clamp_display_margin(24.7) == 25  # rounds to a whole pixel
    assert clamp_display_margin("18.2") == 18


def test_clamp_rejects_garbage_as_zero():
    assert clamp_display_margin(None) == 0
    assert clamp_display_margin("") == 0
    assert clamp_display_margin("abc") == 0
    assert clamp_display_margin([1, 2]) == 0


def test_save_clamps_out_of_range_margins(tmp_path):
    """A save request with an over-range margin is clamped, not stored raw."""
    from app.config import Settings

    s = Settings()
    object.__setattr__(s, "data_dir", str(tmp_path))
    s.save({
        "display_margin_top": 9999,
        "display_margin_right": 24,
        "display_margin_bottom": -5,
        "display_margin_left": "12",
    })
    assert s.display_margin_top == DISPLAY_MARGIN_MAX
    assert s.display_margin_right == 24
    assert s.display_margin_bottom == 0
    assert s.display_margin_left == 12


def test_margins_default_to_zero():
    from app.config import Settings

    s = Settings()
    assert s.display_margin_top == 0
    assert s.display_margin_right == 0
    assert s.display_margin_bottom == 0
    assert s.display_margin_left == 0


def test_margins_are_device_local_not_satellite_pulled():
    """Each panel differs, so a satellite must keep its own margins."""
    from app.config import SATELLITE_PULL_FIELDS, _SAVEABLE

    for edge in ("top", "right", "bottom", "left"):
        key = f"display_margin_{edge}"
        assert key in _SAVEABLE
        assert key not in SATELLITE_PULL_FIELDS
