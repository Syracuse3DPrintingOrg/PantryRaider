"""Key image rendering with Pillow.

Draws a single key face: a coloured background, a label, and an optional large
count for status keys. Returns a plain PIL image at the requested size; the
controller converts it to the deck's native format. Kept free of any hardware
import so it can be exercised in tests.
"""
from __future__ import annotations

import io
import json
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .theme import relative_luminance, text_color_for

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

# Bundled full-colour icon set (colour emoji PNGs, one per slug). Used when the
# deck icon style is "color". Missing files fall back to the monochrome glyph.
_EMOJI_DIR = _ASSETS_DIR / "emoji"


@lru_cache(maxsize=256)
def _emoji_image(slug: str, size: int) -> "Image.Image | None":
    """Load a bundled colour icon by slug, resized to fit a `size`x`size` box,
    as an RGBA image. Returns None when the slug or file is missing."""
    if not slug:
        return None
    path = _EMOJI_DIR / f"{slug}.png"
    if not path.is_file():
        return None
    try:
        icon = Image.open(path).convert("RGBA")
    except OSError:
        return None
    icon.thumbnail((size, size), Image.LANCZOS)
    return icon


def emoji_available(slug: str) -> bool:
    """True when a bundled colour icon exists for the slug."""
    return bool(slug) and (_EMOJI_DIR / f"{slug}.png").is_file()

# Fraction of key height used for the glyph when a key has both an icon and a
# label stacked beneath it.
_ICON_FRACTION = 0.42

# Weather and forecast faces show multi-line temperature text (the current temp,
# or a high/low pair) that needs the room, so their glyph is drawn small to stay
# out of the way of the numbers (FoodAssistant-bf7a).
_WIDGET_ICON_FRACTION = 0.22

# Action kinds whose face is dominated by temperature text rather than a glyph.
_SMALL_ICON_KINDS = frozenset({"weather", "forecast"})

# Info-heavy action kinds: their face is a multi-character value (the clock
# time/date, the weather stat, the forecast high/low, today's meal name) rather
# than a glyph. Drawing a main icon on these only steals room from the value and
# truncates it, so the renderer skips the icon entirely and gives the text the
# whole key face (FoodAssistant-510y).
_TEXT_ONLY_KINDS = frozenset({"clock", "weather", "forecast", "info"})


def text_only_kind(kind: str) -> bool:
    """True when an action kind should render its value across the whole key.

    These info-heavy kinds (clock, weather, forecast, today's meal) carry a
    multi-character value that needs the full face; ``render_key`` suppresses the
    main icon for them so the text does not truncate.
    """
    return kind in _TEXT_ONLY_KINDS


# Glanceable widget kinds that earn a richer "feature" face: a vertical
# gradient, a faint corner glyph accent, and a two-tier value layout (a large
# bright primary number with smaller dim supporting text), drawn the same way
# regardless of the deck's key_style so the clock and weather tiles never look
# like flat coloured rectangles (FoodAssistant-bx6v).
_FEATURE_KINDS = frozenset({"clock", "weather", "forecast"})


def feature_face_kind(kind: str) -> str:
    """Return the kind when it should render as a richer feature face, else "".

    The controller passes the result to ``render_key``; an empty string keeps
    the plain rendering path for every other kind (including the ``info`` meal
    tile, which is left alone so its name is not number-emphasised).
    """
    return kind if kind in _FEATURE_KINDS else ""


def icon_fraction_for(kind: str) -> float:
    """Glyph height fraction for an action kind.

    Weather and forecast faces use a reduced glyph so the temperature text stays
    legible; every other kind keeps the standard size.
    """
    return _WIDGET_ICON_FRACTION if kind in _SMALL_ICON_KINDS else _ICON_FRACTION

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


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % rgb


# -- colour / style helpers (pure, unit-testable) --------------------------
#
# These power the richer key styles ("rich", "glass") and the full-colour icon
# mode. They take and return plain (r, g, b) tuples so they can be exercised in
# tests without a deck or even a rendered image.


def _clamp_channel(value: float) -> int:
    """Round and clamp a single colour channel into 0..255."""
    return max(0, min(255, int(round(value))))


def _lighten(rgb: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    """Move ``rgb`` toward white by ``amount`` (0..1). 0 returns it unchanged."""
    amount = max(0.0, min(1.0, amount))
    return tuple(_clamp_channel(c + (255 - c) * amount) for c in rgb)  # type: ignore[return-value]


def _darken(rgb: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    """Move ``rgb`` toward black by ``amount`` (0..1). 0 returns it unchanged."""
    amount = max(0.0, min(1.0, amount))
    return tuple(_clamp_channel(c * (1.0 - amount)) for c in rgb)  # type: ignore[return-value]


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    """Linear blend from ``a`` (t=0) to ``b`` (t=1)."""
    t = max(0.0, min(1.0, t))
    return tuple(_clamp_channel(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


def _vertical_gradient(
    size: tuple[int, int],
    top_rgb: tuple[int, int, int],
    bottom_rgb: tuple[int, int, int],
) -> Image.Image:
    """An RGB image filled with a top-to-bottom blend of two colours.

    Row 0 is ``top_rgb`` and the last row is ``bottom_rgb``, every row in
    between a linear mix. Returns an image of exactly ``size`` (width, height).
    """
    width, height = size
    img = Image.new("RGB", (width, height), top_rgb)
    if height <= 1 or width <= 0:
        return img
    draw = ImageDraw.Draw(img)
    last = height - 1
    for y in range(height):
        draw.line([(0, y), (width, y)], fill=_mix(top_rgb, bottom_rgb, y / last))
    return img


def _rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    """An "L" mode mask (255 inside a rounded rectangle, 0 outside)."""
    width, height = size
    mask = Image.new("L", (width, height), 0)
    d = ImageDraw.Draw(mask)
    r = max(0, min(radius, min(width, height) // 2))
    d.rounded_rectangle([0, 0, width - 1, height - 1], radius=r, fill=255)
    return mask


def _glass_panel(
    size: tuple[int, int],
    base_rgb: tuple[int, int, int],
) -> Image.Image:
    """Render a glassmorphism key face for ``base_rgb`` at ``size``.

    A darkened base, an inset rounded panel filled with a translucent lighter
    tint of the key colour, a brighter top-edge highlight, and a soft light
    inner stroke. Returns an opaque RGB image of exactly ``size`` so it can drop
    straight into ``render_key``.
    """
    width, height = size
    # Darkened opaque base so the translucent panel reads as floating glass.
    base = _darken(base_rgb, 0.55)
    img = Image.new("RGBA", (width, height), base + (255,))

    inset = max(1, min(width, height) // 12)
    radius = max(2, min(width, height) // 6)
    left, top = inset, inset
    right, bottom = width - 1 - inset, height - 1 - inset
    if right <= left or bottom <= top:
        return img.convert("RGB")

    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    # Translucent lighter tint of the key colour for the panel body.
    tint = _lighten(base_rgb, 0.25)
    od.rounded_rectangle(
        [left, top, right, bottom], radius=radius, fill=tint + (140,)
    )
    # Brighter top-edge highlight: a thin near-white band along the panel top.
    highlight_h = max(1, (bottom - top) // 5)
    od.rounded_rectangle(
        [left, top, right, top + highlight_h],
        radius=radius,
        fill=_lighten(base_rgb, 0.55) + (90,),
    )
    # Soft 1px light inner stroke around the panel edge for definition.
    od.rounded_rectangle(
        [left, top, right, bottom], radius=radius, outline=(255, 255, 255, 70), width=1
    )
    img = Image.alpha_composite(img, overlay)
    return img.convert("RGB")


def _mid_color(style: str, bg: tuple[int, int, int]) -> tuple[int, int, int]:
    """The representative colour text/icon contrast is judged against.

    For the gradient and glass styles the visible mid-tone differs from the raw
    key colour, so contrast helpers use this rather than ``bg`` directly.
    """
    if style == "rich":
        # Gradient runs from a lightened top to the base; the mid row sits about
        # a tenth above the base, which is what most of the label overlaps.
        return _lighten(bg, 0.10)
    if style == "glass":
        # The panel tint over a darkened base reads close to a light mix.
        return _mix(_darken(bg, 0.55), _lighten(bg, 0.25), 0.55)
    if style == "clean":
        # The face is a fixed dark colour, so contrast is judged against it.
        return _CLEAN_BG
    return bg


def _icon_fill(
    icon_color: str,
    action_name: str,
    mid_rgb: tuple[int, int, int],
    text_fill: tuple[int, int, int],
) -> tuple[int, int, int]:
    """Resolve the glyph colour for the requested icon mode.

    "mono" keeps the luminance-adapted ``text_fill`` (today's behaviour). "full"
    paints the glyph in the action's vivid accent, but only when that accent is
    far enough from the key's mid-tone luminance to stay legible; otherwise it
    falls back to ``text_fill`` so a glyph never washes into its background.
    """
    if icon_color != "full":
        return text_fill
    from .theme import role_accent  # local import keeps theme/render decoupled

    accent = _hex_to_rgb(role_accent(action_name))
    lum_accent = relative_luminance(_rgb_to_hex(accent))
    lum_mid = relative_luminance(_rgb_to_hex(mid_rgb))
    # Too close in luminance to the background: fall back to the contrast colour.
    if abs(lum_accent - lum_mid) < 0.18:
        return text_fill
    return accent


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


def _wrap_text(
    draw: ImageDraw.ImageDraw, text: str, font, max_width: int
) -> list[str]:
    """Word-wrap ``text`` so each line fits ``max_width``.

    Wraps on spaces; a single word still too wide at this size is broken across
    lines via ``_wrap_single_word`` so nothing runs off the key. Used for the
    supporting description on a weather feature face (e.g. "Partly Cloudy").
    """
    words = text.split()
    if not words:
        return [text]
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and _text_width(draw, candidate, font) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    out: list[str] = []
    for line in lines:
        if " " not in line and _text_width(draw, line, font) > max_width:
            out.extend(_wrap_single_word(draw, line, font, max_width))
        else:
            out.append(line)
    return out or [text]


def _styled_background(
    width: int, height: int, bg: tuple[int, int, int], style: str
) -> Image.Image:
    """Build the key-face background image for the chosen style.

    "minimal" is a flat fill (the old behaviour). "rich" is a vertical gradient
    from a lightened top to the base colour with a thin darker inner border for
    definition. "glass" is a glassmorphism inset panel. Always returns an opaque
    RGB image of exactly (width, height).
    """
    if style == "clean":
        # No coloured background: a uniform dark face with a faint accent border,
        # so a full-colour icon stands out. The accent border keeps a hint of the
        # action's colour without the heavy fill.
        img = Image.new("RGB", (width, height), _CLEAN_BG)
        ImageDraw.Draw(img).rectangle(
            [0, 0, width - 1, height - 1], outline=_darken(bg, 0.15), width=2
        )
        return img
    if style == "glass":
        return _glass_panel((width, height), bg)
    if style == "rich":
        top = _lighten(bg, 0.28)
        img = _vertical_gradient((width, height), top, bg)
        # Thin darker inner border so the face reads as a distinct tile.
        border = _darken(bg, 0.45)
        ImageDraw.Draw(img).rectangle(
            [0, 0, width - 1, height - 1], outline=border, width=1
        )
        return img
    # "minimal": the original flat solid fill.
    return Image.new("RGB", (width, height), bg)


# The dark face colour used by the "clean" (no coloured background) style.
_CLEAN_BG = (22, 24, 29)


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
    key_style: str = "minimal",
    icon_color: str = "mono",
    action_name: str = "",
    emoji: str = "",
    icon_fraction: float = _ICON_FRACTION,
    text_only: bool = False,
    feature_face: str = "",
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

    ``key_style`` selects the face treatment: "minimal" (the old flat fill,
    the safe default for legacy callers), "rich" (a subtle vertical gradient
    with a thin inner border), or "glass" (a glassmorphism inset panel).
    ``icon_color`` is "mono" (the luminance-adapted text colour, default) or
    "full" (the action's vivid accent, guarded for legibility). ``action_name``
    feeds the full-colour accent lookup; it is optional so positional callers
    keep working.

    ``icon_fraction`` is the glyph height as a fraction of the key, defaulting to
    the standard size. Weather and forecast faces pass a smaller value (see
    ``icon_fraction_for``) so the temperature text stays legible.

    ``text_only`` suppresses the main icon entirely for info-heavy kinds (clock,
    weather, forecast, today's meal), so their multi-character value gets the
    whole key face instead of being truncated by the glyph above it. Status keys
    (``count`` set) keep their own glyph-free layout and ignore this flag.

    ``feature_face`` (the kind: "clock", "weather", or "forecast") routes the key
    to the richer feature renderer: a gradient face with a faint glyph accent and
    an emphasised value, drawn regardless of ``key_style`` so the glanceable
    widgets never look flat (FoodAssistant-bx6v). Empty keeps the plain path.
    """
    bg = _hex_to_rgb(color)
    if count and alert:
        bg = tuple(min(255, int(c * 1.35) + 25) for c in bg)  # type: ignore[assignment]

    if feature_face in _FEATURE_KINDS and count is None:
        density = _density_factor(min(width, height), reference_px)
        return _render_feature_face(
            width, height, label, bg, feature_face, icon, density=density
        )

    style = key_style if key_style in ("minimal", "rich", "glass", "clean") else "minimal"
    img = _styled_background(width, height, bg, style)
    draw = ImageDraw.Draw(img)

    # Text (label, count, and icon) colour adapts to the key's mid-tone
    # luminance, so a light key gets dark text and a dark key gets light text.
    # The mid-tone differs from the raw colour for the gradient/glass styles.
    mid = _mid_color(style, bg)
    text_fill = _hex_to_rgb(text_color_for(_rgb_to_hex(mid)))
    glyph_fill = _icon_fill(icon_color, action_name, mid, text_fill)

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
            fill=text_fill,
        )
        label_y = height * 0.60
        label_px = _font_px(
            height, _STATUS_LABEL_FRACTION, density=density, floor=_MIN_FONT_PX
        )
        _draw_label(draw, label, label_px, width, height, label_y, max_label_width, text_fill)
        return img

    if text_only:
        # Info-heavy kinds (clock, weather, forecast, today's meal): skip the
        # main icon and give the multi-character value the whole key face so it
        # does not get truncated by a glyph above it (FoodAssistant-510y).
        label_px = _font_px(
            height, _LABEL_FRACTION, density=density, floor=_MIN_FONT_PX
        )
        label_y = (height - label_px) / 2
        _draw_label(draw, label, label_px, width, height, label_y, max_label_width, text_fill)
        return img

    # Full-colour icon set: composite a bundled colour emoji PNG when the deck
    # icon style is "color" and one exists for this action. Falls through to the
    # monochrome glyph below when there is no colour icon, so nothing goes blank.
    if icon_color == "color":
        icon_px = _font_px(height, icon_fraction, density=density, floor=18)
        color_icon = _emoji_image(emoji, int(icon_px * 1.35))
        if color_icon is not None:
            cx = (width - color_icon.width) // 2
            cy = int(height * 0.10)
            base = img.convert("RGBA")
            base.alpha_composite(color_icon, (cx, cy))
            img = base.convert("RGB")
            draw = ImageDraw.Draw(img)
            label_px = _font_px(
                height, _STATUS_LABEL_FRACTION, density=density, floor=_MIN_FONT_PX
            )
            label_y = height * 0.66
            _draw_label(draw, label, label_px, width, height, label_y, max_label_width, text_fill)
            return img

    glyph = _icon_char(icon)
    icon_font = _icon_font(_font_px(
        height, icon_fraction, density=density, floor=18
    )) if glyph else None

    if glyph and icon_font is not None:
        # Icon on top, label bottom-aligned beneath it.
        box = draw.textbbox((0, 0), glyph, font=icon_font)
        gw = box[2] - box[0]
        gx = (width - gw) / 2 - box[0]
        gy = height * 0.12 - box[1]
        draw.text((gx, gy), glyph, font=icon_font, fill=glyph_fill)
        label_px = _font_px(
            height, _STATUS_LABEL_FRACTION, density=density, floor=_MIN_FONT_PX
        )
        label_y = height * 0.66
        _draw_label(draw, label, label_px, width, height, label_y, max_label_width, text_fill)
        return img

    # No icon (or font/glyph missing): centred text-only label, as before.
    label_px = _font_px(
        height, _LABEL_FRACTION, density=density, floor=_MIN_FONT_PX
    )
    label_y = (height - label_px) / 2
    _draw_label(draw, label, label_px, width, height, label_y, max_label_width, text_fill)
    return img


def _draw_label(
    draw: ImageDraw.ImageDraw,
    label: str,
    start_px: int,
    width: int,
    height: int,
    label_y: float,
    max_width: int,
    fill: tuple[int, int, int] = (235, 235, 235),
) -> None:
    """Draw a label, shrinking to fit and wrapping a single word as a fallback.

    First shrink the font until the label fits ~90% of the key width. If a
    single long word still overflows at the floor size, wrap it across lines and
    centre the block vertically around ``label_y``. ``fill`` is the text colour,
    chosen by the caller for contrast against the key background.
    """
    font = _fit_font(draw, label, start_px, max_width, floor=_MIN_FONT_PX)
    explicit = "\n" in label
    if not explicit and (_text_width(draw, label, font) <= max_width or " " in label):
        lw = _text_width(draw, label, font)
        draw.text(
            ((width - lw) / 2, label_y),
            label,
            font=font,
            fill=fill,
        )
        return

    # Multi-line: explicit newlines in the label (e.g. "Screen\nOff"), or a
    # single word too long for one line at the floor size. Render it as a block.
    if explicit:
        lines = label.split("\n")
    else:
        lines = _wrap_single_word(draw, label, font, max_width)

    # Shrink so the whole block fits the vertical space from label_y down to near
    # the bottom edge, then bottom-clamp, so no line falls off the key. The font
    # tracked here starts from the width-fitted size and only gets smaller.
    size = max(_MIN_FONT_PX, min(start_px, getattr(font, "size", start_px)))
    font = _font(size)
    budget = max(1, int(height * 0.97 - label_y))
    box = draw.textbbox((0, 0), "Ag", font=font)
    line_h = box[3] - box[1]
    while size > _MIN_FONT_PX and line_h * len(lines) > budget:
        size -= 2
        font = _font(size)
        box = draw.textbbox((0, 0), "Ag", font=font)
        line_h = box[3] - box[1]

    block_h = line_h * len(lines)
    # Centre around label_y, then clamp the block to stay fully on the key.
    y = label_y - (block_h - line_h) / 2
    y = min(y, height * 0.97 - block_h)
    y = max(y, height * 0.02)
    for line in lines:
        lw = _text_width(draw, line, font)
        draw.text(((width - lw) / 2, y), line, font=font, fill=fill)
        y += line_h


def _feature_display_lines(label: str, kind: str) -> list[tuple[str, bool]]:
    """Ordered (text, is_primary) lines for a feature face.

    The emphasised (primary) line is rendered large; the rest small. The split
    is kind-aware so the big value is always just the number, never the number
    plus trailing words that would run off a small key (FoodAssistant-bx6v):

    - clock: the time leads (line 0), the date sits under it.
    - weather, current conditions ("86°F Partly Cloudy", possibly with the
      condition pre-split by a newline): the temperature is isolated as the
      primary and the whole description becomes one supporting string that the
      renderer word-wraps. Temperature-led is detected by a digit in the first
      whitespace token.
    - weather stats ("Feels\n75°F", "Wind\n12\nmph") and forecast
      ("Today\nH72 L55"): keep the existing line order and emphasise the line
      that carries the number; when no line has a digit the first line leads.
    """
    if kind == "clock":
        parts = label.split("\n")
        return [(parts[0], True)] + [(p, False) for p in parts[1:] if p]

    if kind == "weather":
        tokens = label.split()
        if tokens and any(ch.isdigit() for ch in tokens[0]):
            temp = tokens[0]
            rest = " ".join(tokens[1:]).strip()
            return [(temp, True)] + ([(rest, False)] if rest else [])

    # weather stat or forecast: keep the lines, emphasise the number line.
    lines = [ln for ln in label.split("\n") if ln != ""] or [label]
    primary_idx = next(
        (i for i, ln in enumerate(lines) if any(ch.isdigit() for ch in ln)), 0
    )
    return [(ln, i == primary_idx) for i, ln in enumerate(lines)]


def _render_feature_face(
    width: int,
    height: int,
    label: str,
    bg: tuple[int, int, int],
    kind: str,
    icon: str,
    *,
    density: float,
) -> Image.Image:
    """Render a polished face for a glanceable widget (clock, weather, forecast).

    Unlike the flat text-only path these always get a vertical gradient, a faint
    corner glyph accent, and a two-tier text layout (a large bright primary value
    with smaller dim supporting lines), independent of the deck's key_style, so
    the at-a-glance widgets never look like flat coloured rectangles (bx6v). The
    supporting text word-wraps and the whole block shrinks to fit, so a long
    condition like "Partly Cloudy" stays on the key instead of running off it.
    The corner glyph is deliberately small so it stays clear of the value and
    does not re-truncate it (the concern that drove FoodAssistant-510y).
    """
    top = _lighten(bg, 0.32)
    bottom = _darken(bg, 0.42)
    img = _vertical_gradient((width, height), top, bottom)
    draw = ImageDraw.Draw(img)
    # Thin darker inner border so each tile reads as a distinct face.
    draw.rectangle([0, 0, width - 1, height - 1], outline=_darken(bg, 0.6), width=1)

    # Contrast the text against the lower-middle of the gradient, where it sits.
    mid = _mix(top, bottom, 0.6)
    primary_fill = _hex_to_rgb(text_color_for(_rgb_to_hex(mid)))
    secondary_fill = _mix(primary_fill, mid, 0.4)

    # Faint glyph accent in the top-left corner.
    glyph = _icon_char(icon)
    g_font = _icon_font(_font_px(height, 0.20, density=density, floor=12)) if glyph else None
    if glyph and g_font is not None:
        gbox = draw.textbbox((0, 0), glyph, font=g_font)
        draw.text(
            (max(2, int(width * 0.06)) - gbox[0], max(2, int(height * 0.05)) - gbox[1]),
            glyph,
            font=g_font,
            fill=_mix(primary_fill, bottom, 0.55),
        )

    display = _feature_display_lines(label, kind) or [(label, True)]
    max_w = int(width * _FIT_FRACTION)

    # Size the primary value to fill the width (it is short, e.g. "86°F"); the
    # supporting text is a clear step smaller and word-wrapped to the width.
    primary_text = next((t for t, is_p in display if is_p), display[0][0])
    primary_px = _font_px(height, 0.42, density=density, floor=16)
    primary_font = _fit_font(draw, primary_text, primary_px, max_w, floor=14)
    primary_size = int(getattr(primary_font, "size", primary_px))
    secondary_size = max(_MIN_FONT_PX, int(primary_size * 0.50))

    def _physical_lines(p_font, s_size):
        """Expand the display lines into drawable (text, font, primary) rows,
        word-wrapping the supporting text at the current secondary size."""
        s_font = _font(s_size)
        rows: list[tuple] = []
        for text, is_p in display:
            if is_p:
                rows.append((text, p_font, True))
            else:
                for wrapped in _wrap_text(draw, text, s_font, max_w):
                    rows.append((wrapped, s_font, False))
        return rows

    # Shrink the supporting text, then the value, until the whole stack fits the
    # vertical budget, so a long wrapped description never overruns the key.
    gap = max(1, int(height * 0.04))
    budget = int(height * 0.90)
    while True:
        rows = _physical_lines(primary_font, secondary_size)
        heights = [draw.textbbox((0, 0), t or "Ag", font=f)[3]
                   - draw.textbbox((0, 0), t or "Ag", font=f)[1] for t, f, _p in rows]
        total = sum(heights) + gap * (len(rows) - 1)
        if total <= budget:
            break
        if secondary_size > _MIN_FONT_PX:
            secondary_size -= 2
        elif primary_size > 14:
            primary_size -= 2
            primary_font = _font(primary_size)
        else:
            break

    block_h = sum(heights) + gap * (len(rows) - 1)
    y = max(int(height * 0.05), (height - block_h) // 2)
    for (text, font, is_p), line_h in zip(rows, heights):
        box = draw.textbbox((0, 0), text, font=font)
        lw = box[2] - box[0]
        fill = primary_fill if is_p else secondary_fill
        draw.text(((width - lw) / 2 - box[0], y - box[1]), text, font=font, fill=fill)
        y += line_h + gap
    return img


# -- camera snapshot helpers (pure, unit-testable) -------------------------
#
# These turn a JPEG snapshot (a single key face) or one source image (the
# full-deck overlay) into deck-ready RGB tiles. They never touch the hardware,
# so the controller can fetch bytes and hand them straight in.


def _center_crop_to_aspect(image: "Image.Image", target_w: int, target_h: int) -> "Image.Image":
    """Center-crop ``image`` to the aspect ratio of ``target_w`` x ``target_h``.

    Returns a crop of the source (no resize) whose width/height ratio matches the
    target. A degenerate target (zero side) returns the image unchanged.
    """
    src_w, src_h = image.size
    if target_w <= 0 or target_h <= 0 or src_w <= 0 or src_h <= 0:
        return image
    # Compare aspect ratios with cross-multiplication to avoid float drift.
    if src_w * target_h > target_w * src_h:
        # Source is wider than the target: trim the sides.
        new_w = max(1, round(src_h * target_w / target_h))
        left = (src_w - new_w) // 2
        return image.crop((left, 0, left + new_w, src_h))
    # Source is taller than the target: trim the top and bottom.
    new_h = max(1, round(src_w * target_h / target_w))
    top = (src_h - new_h) // 2
    return image.crop((0, top, src_w, top + new_h))


def image_from_jpeg(data: bytes, size: tuple[int, int]) -> "Image.Image | None":
    """Decode JPEG ``data`` into an RGB image cropped and resized to ``size``.

    Center-crops the decoded frame to the target aspect, then resizes to exactly
    ``size`` (width, height). Returns None when the bytes are missing or cannot be
    decoded, so a draw loop can fall back to a normal key face rather than crash.
    """
    if not data:
        return None
    width, height = size
    if width <= 0 or height <= 0:
        return None
    try:
        with Image.open(io.BytesIO(data)) as src:
            src.load()
            image = src.convert("RGB")
    except (OSError, ValueError):
        return None
    cropped = _center_crop_to_aspect(image, width, height)
    return cropped.resize((width, height), Image.LANCZOS)


def slice_full_image(
    image: "Image.Image",
    rows: int,
    cols: int,
    key_size: tuple[int, int],
    spacing: int = 0,
) -> list["Image.Image"]:
    """Slice one source image into ``rows`` x ``cols`` per-key RGB tiles.

    The source is scaled to cover the full physical deck area, accounting for the
    inter-key gaps: a key face is ``key_size`` (w, h) and successive keys are
    ``spacing`` pixels apart, so the whole deck spans
    ``(cols*kw + (cols-1)*spacing, rows*kh + (rows-1)*spacing)``. The image is
    center-cropped to that aspect, resized to it, and then each key (r, c) takes
    the region at ``(c*(kw+spacing), r*(kh+spacing), +kw, +kh)``. Tiles come back
    in row-major order (index = r*cols + c), so a tile drops straight onto its
    physical key. Pure: no hardware, fully unit-testable.
    """
    kw, kh = key_size
    rows = max(0, int(rows))
    cols = max(0, int(cols))
    spacing = max(0, int(spacing))
    if rows == 0 or cols == 0 or kw <= 0 or kh <= 0:
        return []
    full_w = cols * kw + (cols - 1) * spacing
    full_h = rows * kh + (rows - 1) * spacing
    rgb = image.convert("RGB")
    cropped = _center_crop_to_aspect(rgb, full_w, full_h)
    full = cropped.resize((full_w, full_h), Image.LANCZOS)
    tiles: list[Image.Image] = []
    for r in range(rows):
        for c in range(cols):
            x = c * (kw + spacing)
            y = r * (kh + spacing)
            tiles.append(full.crop((x, y, x + kw, y + kh)))
    return tiles


def message_across_deck(
    rows: int, cols: int, key_size: tuple[int, int], text: str
) -> list["Image.Image"]:
    """Render a short ``text`` centred across the whole deck as per-key tiles.

    Used by the full-deck overlay when there is no camera or the snapshot fails,
    so the deck shows a readable "No camera" rather than going blank. Returns
    ``rows*cols`` RGB tiles in row-major order, each ``key_size`` (w, h).
    """
    kw, kh = key_size
    rows = max(0, int(rows))
    cols = max(0, int(cols))
    if rows == 0 or cols == 0 or kw <= 0 or kh <= 0:
        return []
    full_w = cols * kw
    full_h = rows * kh
    canvas = Image.new("RGB", (full_w, full_h), (16, 18, 22))
    draw = ImageDraw.Draw(canvas)
    px = _font_px(full_h, 0.20, density=1.0, floor=_MIN_FONT_PX)
    font = _fit_font(draw, text, px, int(full_w * _FIT_FRACTION), floor=_MIN_FONT_PX)
    box = draw.textbbox((0, 0), text, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    draw.text(
        ((full_w - tw) / 2 - box[0], (full_h - th) / 2 - box[1]),
        text,
        font=font,
        fill=(235, 235, 235),
    )
    tiles: list[Image.Image] = []
    for r in range(rows):
        for c in range(cols):
            tiles.append(canvas.crop((c * kw, r * kh, c * kw + kw, r * kh + kh)))
    return tiles


def blank_key(width: int, height: int) -> Image.Image:
    return Image.new("RGB", (width, height), (16, 18, 22))


# Boot splash (FoodAssistant-v32r): the Pantry Raider raccoon mark, copied from
# the app's logo-mark.png into the deck assets so the package is self-contained.
# Optional like the icon font: a missing file just skips the splash.
_SPLASH_PATH = _ASSETS_DIR / "splash.png"

# Dark backdrop behind the splash mark; matches blank_key so the frame reads as
# the deck's own idle face rather than a stray image.
_SPLASH_BG = (16, 18, 22)


def splash_tiles(
    rows: int,
    cols: int,
    key_size: tuple[int, int],
    spacing: int = 0,
    logo_path: "Path | str | None" = None,
) -> list["Image.Image"]:
    """Per-key tiles of the boot splash: the brand mark centred across the deck.

    The controller paints this as its very first frame after the deck opens, so
    the boot gap shows the Pantry Raider raccoon instead of the Elgato factory
    logo; the real page replaces it once the controller finishes starting up.
    The mark is composited onto a dark full-deck canvas (fit inside the deck's
    shorter span so it never crops) and sliced with the same spacing-aware
    geometry as ``slice_full_image``, row-major. Pure and defensive: a missing
    or unreadable asset returns an empty list so the caller simply skips the
    splash.
    """
    kw, kh = key_size
    rows = max(0, int(rows))
    cols = max(0, int(cols))
    spacing = max(0, int(spacing))
    if rows == 0 or cols == 0 or kw <= 0 or kh <= 0:
        return []
    path = Path(logo_path) if logo_path else _SPLASH_PATH
    try:
        with Image.open(path) as src:
            src.load()
            logo = src.convert("RGBA")
    except (OSError, ValueError):
        return []
    full_w = cols * kw + (cols - 1) * spacing
    full_h = rows * kh + (rows - 1) * spacing
    canvas = Image.new("RGB", (full_w, full_h), _SPLASH_BG)
    box = int(min(full_w, full_h) * 0.80)
    if box > 0 and logo.width > 0 and logo.height > 0:
        # Scale (up or down) so the mark fills the box; thumbnail() only
        # shrinks, which would leave the 128px source tiny on a large deck.
        scale = box / max(logo.width, logo.height)
        logo = logo.resize(
            (max(1, round(logo.width * scale)), max(1, round(logo.height * scale))),
            Image.LANCZOS,
        )
        canvas.paste(
            logo,
            ((full_w - logo.width) // 2, (full_h - logo.height) // 2),
            logo,
        )
    return slice_full_image(canvas, rows, cols, key_size, spacing)


# -- shared screensaver canvas (FoodAssistant-3fdq) -------------------------
#
# While the kiosk screensaver's bouncing logo is up and the deck is configured
# as part of the canvas (screensaver_layout above/below/left/right), the
# controller polls the app for the logo's position and paints the slice that
# crosses the deck. The geometry and compositing here are pure so the
# virtual-canvas mapping is fully unit-testable without hardware.
#
# Coordinate contract (set by the kiosk, which drives the animation): the
# panel spans x 0..1 and y 0..1 in panel-normalized units, and the deck band
# extends past that range on its side by ``band`` (the band's size along its
# axis, also in panel-normalized units, computed by the kiosk from the deck's
# key-grid aspect so the two surfaces share one physical scale). The logo box
# (x, y, w, h) arrives in the same units and may straddle the boundary.


def screensaver_logo_box(
    x: float, y: float, w: float, h: float,
    band: float, layout: str,
    full_w: int, full_h: int,
) -> tuple[float, float, float, float] | None:
    """Map the panel-normalized logo box onto deck full-canvas pixels.

    ``full_w`` x ``full_h`` is the deck's whole key area in pixels (as built by
    ``slice_full_image``). Returns the logo's (x, y, w, h) in those pixels, or
    None when the logo does not overlap the deck band at all (the keys stay
    dark). The band's long side always maps onto the deck's matching span
    (width for above/below, height for left/right), so motion crosses the gap
    between panel and deck at a consistent physical speed.
    """
    if band <= 0 or w <= 0 or h <= 0 or full_w <= 0 or full_h <= 0:
        return None
    if layout in ("above", "below"):
        # Band-local vertical position: 0 at the band edge nearest the top of
        # the deck image. For "below" the band runs y 1..1+band; for "above"
        # it runs y -band..0.
        offset = 1.0 if layout == "below" else -band
        px = x * full_w
        py = (y - offset) / band * full_h
        pw = w * full_w
        ph = h / band * full_h
    elif layout in ("left", "right"):
        offset = 1.0 if layout == "right" else -band
        px = (x - offset) / band * full_w
        py = y * full_h
        pw = w / band * full_w
        ph = h * full_h
    else:
        return None
    # No overlap with the deck's pixel canvas: nothing to draw.
    if px + pw <= 0 or py + ph <= 0 or px >= full_w or py >= full_h:
        return None
    return (px, py, pw, ph)


def screensaver_tiles(
    rows: int,
    cols: int,
    key_size: tuple[int, int],
    box: tuple[float, float, float, float] | None,
    spacing: int = 0,
    logo_path: "Path | str | None" = None,
) -> list["Image.Image"]:
    """Per-key tiles of the screensaver frame: the brand mark at ``box``.

    ``box`` is the logo's (x, y, w, h) in deck full-canvas pixels (from
    ``screensaver_logo_box``); None paints a plain dark frame (the logo is
    elsewhere on the panel). The mark is drawn onto a dark canvas the size of
    the whole key area and sliced with the same spacing-aware geometry as the
    boot splash, row-major. Pure and defensive: a missing or unreadable logo
    asset degrades to the dark frame rather than raising.
    """
    kw, kh = key_size
    rows = max(0, int(rows))
    cols = max(0, int(cols))
    spacing = max(0, int(spacing))
    if rows == 0 or cols == 0 or kw <= 0 or kh <= 0:
        return []
    full_w = cols * kw + (cols - 1) * spacing
    full_h = rows * kh + (rows - 1) * spacing
    canvas = Image.new("RGB", (full_w, full_h), _SPLASH_BG)
    if box is not None:
        path = Path(logo_path) if logo_path else _SPLASH_PATH
        logo = None
        try:
            with Image.open(path) as src:
                src.load()
                logo = src.convert("RGBA")
        except (OSError, ValueError):
            logo = None
        bx, by, bw, bh = box
        if logo is not None and bw >= 1 and bh >= 1:
            logo = logo.resize(
                (max(1, round(bw)), max(1, round(bh))), Image.LANCZOS
            )
            # paste() clips a partially off-canvas mark, which is exactly the
            # sliding-onto-the-deck effect.
            canvas.paste(logo, (round(bx), round(by)), logo)
    return slice_full_image(canvas, rows, cols, key_size, spacing)
