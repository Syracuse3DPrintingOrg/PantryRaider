"""Key image rendering with Pillow.

Draws a single key face: a coloured background, a label, and an optional large
count for status keys. Returns a plain PIL image at the requested size; the
controller converts it to the deck's native format. Kept free of any hardware
import so it can be exercised in tests.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)

# Vendored Bootstrap Icons font and its name -> codepoint table. The font lets
# PIL rasterise the same glyphs the web UI uses (see actions.ACTION_ICONS).
# Both files are optional: if either is missing the renderer falls back to a
# text-only key, so the deck still works without the binary present.
_ASSETS_DIR = Path(__file__).resolve().parent / "assets"
_ICON_FONT_PATH = _ASSETS_DIR / "bootstrap-icons.ttf"
_ICON_MAP_PATH = _ASSETS_DIR / "bootstrap-icons.json"

# Fraction of key height used for the glyph when a key has both an icon and a
# label stacked beneath it.
_ICON_FRACTION = 0.42

# Per-model pixel density (FoodAssistant-pjk).
#
# Decks differ in how many pixels a key occupies (Mini/Original roughly 72-80px,
# XL roughly 96px, Plus roughly 120px) but the physical key face is about the
# same size on every model. If we size fonts as a flat fraction of the pixel
# height, a glyph that reads well on a 72px Mini key looks oversized on a 120px
# Plus key and vice versa, so apparent legibility drifts model to model.
#
# To keep text at a consistent *physical* size we scale toward a reference key
# resolution. _REFERENCE_PX is a typical key edge in pixels; the fractions below
# are tuned against it. The density factor nudges very small keys up a touch and
# very large keys down a touch so the printed glyph height stays comparable in
# millimetres across models. We clamp the factor so an unusual model never warps
# the layout wildly.
_REFERENCE_PX = 96

# Fraction of key height for each glyph kind, measured at _REFERENCE_PX. These
# are deliberately larger than the old values so labels read across a room
# (FoodAssistant-aax): a status count dominates the key and labels are bold.
_LABEL_FRACTION = 0.30        # label-only keys
_STATUS_LABEL_FRACTION = 0.24  # the small label under a status count
_STATUS_COUNT_FRACTION = 0.55  # the large count number on a status key

# Labels are shrunk to fit if they overflow; never go below this many pixels.
_MIN_FONT_PX = 12
# Target maximum text width as a fraction of the key width.
_FIT_FRACTION = 0.90


@lru_cache(maxsize=32)
def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


@lru_cache(maxsize=1)
def _icon_codepoints() -> dict[str, int]:
    """Bootstrap Icons name -> codepoint, or empty if the map is absent."""
    try:
        data = json.loads(_ICON_MAP_PATH.read_text())
    except (OSError, ValueError):
        return {}
    return {k: int(v) for k, v in data.items() if isinstance(v, int)}


@lru_cache(maxsize=32)
def _icon_font(size: int) -> ImageFont.FreeTypeFont | None:
    """Load the vendored Bootstrap Icons font at ``size``, or None if missing."""
    try:
        return ImageFont.truetype(str(_ICON_FONT_PATH), size)
    except OSError:
        return None


def _icon_char(name: str) -> str | None:
    """Resolve a Bootstrap Icons glyph name to its character, or None.

    Accepts names with or without the ``bi-`` prefix. Returns None when the
    glyph is unknown or the codepoint table could not be loaded.
    """
    if not name:
        return None
    key = name[3:] if name.startswith("bi-") else name
    cp = _icon_codepoints().get(key)
    return chr(cp) if cp is not None else None


def icon_available(name: str) -> bool:
    """True when both the font and the named glyph are present."""
    return _icon_char(name) is not None and _icon_font(16) is not None


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    if len(v) != 6:
        return (60, 60, 60)
    return tuple(int(v[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _density_factor(key_px: int, reference_px: int) -> float:
    """Scale toward a reference key resolution, clamped to a safe band.

    A key smaller than the reference gets a slightly larger fraction and a
    larger key a slightly smaller one, so the rendered glyph stays a roughly
    constant physical size across models. We take the square root of the ratio
    so the correction is gentle, then clamp it.
    """
    if key_px <= 0:
        return 1.0
    factor = (reference_px / key_px) ** 0.5
    return max(0.80, min(1.25, factor))


def _font_px(height: int, fraction: float, *, density: float, floor: int) -> int:
    return max(floor, int(height * fraction * density))


def _fit_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    start_px: int,
    max_width: int,
    floor: int,
):
    """Return the largest font (down to ``floor``) whose text fits ``max_width``.

    Steps the size down a couple of pixels at a time. The caller wraps when this
    hits the floor and the text still overflows.
    """
    size = start_px
    font = _font(size)
    while size > floor and _text_width(draw, text, font) > max_width:
        size -= 2
        font = _font(size)
    return font


def _wrap_single_word(
    draw: ImageDraw.ImageDraw, word: str, font, max_width: int
) -> list[str]:
    """Break one long word across lines so each line fits ``max_width``.

    Used only as a last resort once shrinking has bottomed out at the floor;
    multi-word labels are not produced here, the caller handles those.
    """
    lines: list[str] = []
    current = ""
    for ch in word:
        candidate = current + ch
        if current and _text_width(draw, candidate, font) > max_width:
            lines.append(current)
            current = ch
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [word]


def render_key(
    width: int,
    height: int,
    label: str,
    color: str,
    count: int | None = None,
    alert: bool = False,
    *,
    icon: str = "",
    reference_px: int = _REFERENCE_PX,
) -> Image.Image:
    """Render one key.

    ``count`` shows a large number for status keys; when it is greater than
    zero and ``alert`` is set the background is brightened so a full fridge or
    a backlog of scans is visible across the room.

    ``icon`` is a Bootstrap Icons glyph name (with or without the ``bi-``
    prefix). When the vendored icon font and that glyph are present, the glyph
    is drawn in the upper portion of the key and the label is moved to the
    bottom. If the font or glyph is missing, or the key shows a status count,
    the key falls back to the text-only layout, so the deck still works without
    the font binary.

    ``reference_px`` is the key resolution the font fractions are tuned for; the
    actual key pixel size is compared against it so text keeps a consistent
    physical size across deck models (see the module comment). It is a keyword
    argument with a default, so the controller's existing positional call keeps
    working unchanged.
    """
    bg = _hex_to_rgb(color)
    if count and alert:
        bg = tuple(min(255, int(c * 1.35) + 25) for c in bg)  # type: ignore[assignment]

    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)

    density = _density_factor(min(width, height), reference_px)
    max_label_width = int(width * _FIT_FRACTION)

    if count is not None:
        # Status keys keep the large count and a small label; the count is the
        # focus, so no glyph is drawn here.
        num = str(count)
        num_px = _font_px(
            height, _STATUS_COUNT_FRACTION, density=density, floor=20
        )
        num_font = _fit_font(draw, num, num_px, max_label_width, floor=18)
        nw = _text_width(draw, num, num_font)
        draw.text(
            ((width - nw) / 2, height * 0.04),
            num,
            font=num_font,
            fill=(255, 255, 255),
        )
        label_y = height * 0.60
        label_px = _font_px(
            height, _STATUS_LABEL_FRACTION, density=density, floor=_MIN_FONT_PX
        )
        _draw_label(draw, label, label_px, width, height, label_y, max_label_width)
        return img

    glyph = _icon_char(icon)
    icon_font = _icon_font(_font_px(
        height, _ICON_FRACTION, density=density, floor=18
    )) if glyph else None

    if glyph and icon_font is not None:
        # Icon on top, label bottom-aligned beneath it.
        box = draw.textbbox((0, 0), glyph, font=icon_font)
        gw = box[2] - box[0]
        gh = box[3] - box[1]
        gx = (width - gw) / 2 - box[0]
        gy = height * 0.12 - box[1]
        draw.text((gx, gy), glyph, font=icon_font, fill=(235, 235, 235))
        label_px = _font_px(
            height, _STATUS_LABEL_FRACTION, density=density, floor=_MIN_FONT_PX
        )
        label_y = height * 0.66
        _draw_label(draw, label, label_px, width, height, label_y, max_label_width)
        return img

    # No icon (or font/glyph missing): centred text-only label, as before.
    label_px = _font_px(
        height, _LABEL_FRACTION, density=density, floor=_MIN_FONT_PX
    )
    label_y = (height - label_px) / 2
    _draw_label(draw, label, label_px, width, height, label_y, max_label_width)
    return img


def _draw_label(
    draw: ImageDraw.ImageDraw,
    label: str,
    start_px: int,
    width: int,
    height: int,
    label_y: float,
    max_width: int,
) -> None:
    """Draw a label, shrinking to fit and wrapping a single word as a fallback.

    First shrink the font until the label fits ~90% of the key width. If a
    single long word still overflows at the floor size, wrap it across lines and
    centre the block vertically around ``label_y``.
    """
    font = _fit_font(draw, label, start_px, max_width, floor=_MIN_FONT_PX)
    if _text_width(draw, label, font) <= max_width or " " in label:
        lw = _text_width(draw, label, font)
        draw.text(
            ((width - lw) / 2, label_y),
            label,
            font=font,
            fill=(235, 235, 235),
        )
        return

    # A single word at the floor size still overflows: wrap it.
    lines = _wrap_single_word(draw, label, font, max_width)
    box = draw.textbbox((0, 0), "Ag", font=font)
    line_h = box[3] - box[1]
    block_h = line_h * len(lines)
    y = label_y - (block_h - line_h) / 2
    for line in lines:
        lw = _text_width(draw, line, font)
        draw.text(((width - lw) / 2, y), line, font=font, fill=(235, 235, 235))
        y += line_h


def blank_key(width: int, height: int) -> Image.Image:
    return Image.new("RGB", (width, height), (16, 18, 22))
