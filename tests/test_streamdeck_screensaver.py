"""Shared screensaver canvas: virtual-canvas mapping and deck-slice rendering
(FoodAssistant-3fdq).

Pure-logic coverage for the Stream Deck side of the kiosk screensaver: the
config field, the panel-normalized to deck-pixel geometry, and the tile
compositing. No hardware, no network.

Coordinate contract (set by the kiosk): the panel spans 0..1 on each axis and
the deck band extends past that range on its side by ``band`` panel-normalized
units. The deck's whole key area is ``full_w`` x ``full_h`` pixels.
"""
from __future__ import annotations

from PIL import Image

from foodassistant_streamdeck import config, render


# -- config ------------------------------------------------------------------


def test_screensaver_layout_defaults_off_and_loads(tmp_path):
    assert config.Config().screensaver_layout == "off"
    f = tmp_path / "config.toml"
    f.write_text('screensaver_layout = "below"\n')
    assert config.load(f).screensaver_layout == "below"


def test_screensaver_layout_unknown_value_falls_back_to_off(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('screensaver_layout = "diagonal"\n')
    assert config.load(f).screensaver_layout == "off"


# -- geometry: screensaver_logo_box ------------------------------------------
#
# A 5x3 deck of 72px keys: full canvas 360x216. Band 0.3 panel-heights (or
# panel-widths for left/right) unless stated.

FULL_W, FULL_H = 360, 216


def test_logo_on_panel_does_not_touch_the_deck():
    # Logo well inside the panel: the deck keys stay dark.
    assert render.screensaver_logo_box(
        0.4, 0.5, 0.2, 0.1, 0.3, "below", FULL_W, FULL_H) is None


def test_logo_inside_the_below_band_maps_to_deck_pixels():
    # Band below the panel spans y 1..1.3; a logo at y=1.05 sits 1/6 of the
    # way into the band, so 1/6 of the deck's height from its top.
    box = render.screensaver_logo_box(
        0.4, 1.05, 0.2, 0.1, 0.3, "below", FULL_W, FULL_H)
    assert box is not None
    x, y, w, h = box
    assert x == 0.4 * FULL_W
    assert abs(y - FULL_H / 6) < 1e-6
    assert w == 0.2 * FULL_W
    # Height scales by full_h/band: 0.1/0.3 of the deck height.
    assert abs(h - FULL_H / 3) < 1e-6


def test_logo_straddling_the_panel_edge_still_overlaps_below():
    # Bottom sliver of the logo pokes into the band: the box comes back with a
    # negative y (paste clips it), which is the sliding-on effect.
    box = render.screensaver_logo_box(
        0.4, 0.95, 0.2, 0.1, 0.3, "below", FULL_W, FULL_H)
    assert box is not None
    assert box[1] < 0
    assert box[1] + box[3] > 0


def test_above_band_maps_with_its_own_offset():
    # Band above the panel spans y -0.3..0; y=-0.15 is the band's middle.
    box = render.screensaver_logo_box(
        0.0, -0.15, 0.2, 0.1, 0.3, "above", FULL_W, FULL_H)
    assert box is not None
    assert abs(box[1] - FULL_H / 2) < 1e-6


def test_left_and_right_bands_map_the_x_axis():
    right = render.screensaver_logo_box(
        1.1, 0.5, 0.1, 0.2, 0.4, "right", FULL_W, FULL_H)
    assert right is not None
    assert abs(right[0] - 0.1 / 0.4 * FULL_W) < 1e-6
    assert right[1] == 0.5 * FULL_H
    left = render.screensaver_logo_box(
        -0.2, 0.5, 0.1, 0.2, 0.4, "left", FULL_W, FULL_H)
    assert left is not None
    assert abs(left[0] - 0.2 / 0.4 * FULL_W) < 1e-6


def test_degenerate_inputs_yield_none():
    # Zero band, zero-size logo, off layout, or an empty canvas: no slice.
    assert render.screensaver_logo_box(0.5, 1.1, 0.2, 0.1, 0.0, "below", FULL_W, FULL_H) is None
    assert render.screensaver_logo_box(0.5, 1.1, 0.0, 0.1, 0.3, "below", FULL_W, FULL_H) is None
    assert render.screensaver_logo_box(0.5, 1.1, 0.2, 0.1, 0.3, "off", FULL_W, FULL_H) is None
    assert render.screensaver_logo_box(0.5, 1.1, 0.2, 0.1, 0.3, "below", 0, FULL_H) is None


def test_logo_fully_past_the_deck_yields_none():
    # x far beyond the right edge of a right-side band.
    assert render.screensaver_logo_box(
        9.0, 0.5, 0.1, 0.2, 0.4, "right", FULL_W, FULL_H) is None


def test_motion_scale_is_uniform_across_the_seam():
    """One panel pixel of travel moves the deck image by the same amount on
    both axes when the band comes from the deck's own aspect, so the logo does
    not change speed or squash as it crosses onto the keys."""
    band = FULL_H / FULL_W  # what the kiosk computes for a square panel
    a = render.screensaver_logo_box(0.10, 1.0, 0.2, 0.1, band, "below", FULL_W, FULL_H)
    b = render.screensaver_logo_box(0.11, 1.0 + 0.01, 0.2, 0.1, band, "below", FULL_W, FULL_H)
    assert a is not None and b is not None
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    assert abs(dx - dy) < 1e-6


# -- tiles: screensaver_tiles --------------------------------------------------


def _tile_max(tile: Image.Image) -> int:
    return max(tile.convert("L").getextrema())


def test_no_box_paints_a_dark_frame():
    tiles = render.screensaver_tiles(3, 5, (72, 72), None)
    assert len(tiles) == 15
    assert all(t.size == (72, 72) for t in tiles)
    # Uniformly dark: nothing brighter than the splash backdrop anywhere.
    assert all(_tile_max(t) < 30 for t in tiles)


def test_logo_lands_on_the_expected_keys():
    # A logo box covering the top-left key only: that tile lights up, the
    # bottom-right one stays dark.
    tiles = render.screensaver_tiles(3, 5, (72, 72), (0, 0, 72, 72))
    assert _tile_max(tiles[0]) > 40
    assert _tile_max(tiles[-1]) < 30


def test_partially_off_canvas_box_is_clipped_not_raised():
    # Sliding in from above the deck: negative y clips cleanly.
    tiles = render.screensaver_tiles(3, 5, (72, 72), (0, -36, 72, 72))
    assert len(tiles) == 15
    assert _tile_max(tiles[0]) > 40


def test_missing_logo_asset_degrades_to_dark_frame(tmp_path):
    tiles = render.screensaver_tiles(
        3, 5, (72, 72), (0, 0, 72, 72), logo_path=tmp_path / "missing.png")
    assert len(tiles) == 15
    assert all(_tile_max(t) < 30 for t in tiles)


def test_degenerate_grid_returns_empty():
    assert render.screensaver_tiles(0, 5, (72, 72), None) == []
    assert render.screensaver_tiles(3, 5, (0, 72), None) == []
