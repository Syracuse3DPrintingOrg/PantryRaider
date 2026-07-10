"""Food and spice label rendering (FoodAssistant-23v6).

Pure image composition: given the facts of a label (a food name, when it was
added, when it is best by, and how that best-by date was worked out), lay them
out onto a PIL image sized for the physical label stock. Nothing here touches
the network or a printer; callers hand the resulting image to the print backend
(services/printing.py) or save the bytes.

Everything scales from the label's own width, height, and dpi, so the same code
prints a 2x1 inch thermal label or a 4x3 inch shipping label without special
cases. The type size is fitted to the space (large enough to read across the
kitchen, small enough not to overflow), long names wrap and then ellipsize, and
all of that layout math is kept as small pure helpers so the tests can check it
without rendering.

The best-by date carries a small badge saying where the date came from: a date
the user typed themselves, an estimate from the built-in category rules, or one
the AI worked out. That way a label is honest about how much to trust its date
at a glance.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw, ImageFont

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def prettify_date(value: str) -> str:
    """Turn an ISO date (YYYY-MM-DD) into a friendlier "Mon D, YYYY".

    Anything that is not exactly an ISO date (a blank, or a value the caller
    already formatted) is returned unchanged, so nothing is ever mangled. Pure
    and fully testable."""
    s = (value or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if not m:
        return s
    year, mon, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mon <= 12 and 1 <= day <= 31):
        return s
    return f"{_MONTHS[mon - 1]} {day}, {year}"

# Where a best-by date came from. Kept as a small set of strings (not free text)
# so the badge copy below always maps cleanly.
DateSource = Literal["manual", "default", "llm"]

# Short, user-facing badge copy for each source. Deliberately plain: "est." for
# a rule-of-thumb estimate, "AI" for an AI guess, and nothing for a date the
# user set themselves (a typed date needs no caveat). No developer jargon.
_SOURCE_BADGE: dict[str, str] = {
    "manual": "",
    "default": "est.",
    "llm": "AI",
}

DEFAULT_DPI = 203
DEFAULT_WIDTH_IN = 2.0
DEFAULT_HEIGHT_IN = 1.0


# -- Brand mark (FoodAssistant-yglw) -----------------------------------------
#
# The Pantry Raider logo as an optional, off-by-default element on printed food
# labels. Thermal label printers are 1-bit black ink on white, so the pink
# (#F2006E) app icon is thresholded down to a crisp black-and-white glyph
# rather than printed as-is (which would come out as a solid gray blob or be
# dropped by the printer driver entirely).

LOGO_ASSET = Path(__file__).resolve().parent.parent / "static" / "icons" / "logo-mark.png"

# How much of the smaller inner dimension the logo occupies, and how far its
# corner sits from the label edge, so it always reads as a small mark, not a
# competing headline, on any label size.
LOGO_FRACTION = 0.20


def logo_size_px(inner_w: int, inner_h: int, fraction: float = LOGO_FRACTION) -> int:
    """The logo's square side in pixels for an inner (inside-the-margins) area.

    Scales off the smaller of the two dimensions so a very wide or very tall
    label never gets an oversized mark. Pure and fully testable."""
    inner_w = max(0, int(inner_w))
    inner_h = max(0, int(inner_h))
    side = int(min(inner_w, inner_h) * fraction)
    return max(0, side)


def _load_logo_glyph(side_px: int, threshold: int = 160):
    """The bundled brand mark as a (black/white ink, alpha mask) pair sized to
    ``side_px`` square, or None if the asset cannot be loaded.

    The source PNG is a pink raccoon on a transparent background; the ink layer
    is thresholded to pure black/white (no gray) for a thermal printer, and the
    original alpha channel is kept as the paste mask so only the raccoon shape
    lands on the label, not a solid square. Never raises: a missing or unreadable
    asset just means no logo, so a label always still prints."""
    if side_px < 1:
        return None
    try:
        raw = Image.open(LOGO_ASSET).convert("RGBA")
    except (OSError, FileNotFoundError):
        return None
    raw = raw.resize((side_px, side_px), Image.LANCZOS)
    alpha = raw.split()[-1]
    ink = raw.convert("L").point(lambda p: 0 if p < threshold else 255)
    return ink, alpha


def draw_logo(img: "Image.Image", margin_px: int, threshold: int = 160) -> None:
    """Stamp the brand mark in the label's bottom-right corner, inside the
    margin. Mutates ``img`` in place. Best-effort: any failure to load or size
    the asset silently draws nothing, so the optional logo can never break a
    label print."""
    w, h = img.size
    side = logo_size_px(w - 2 * margin_px, h - 2 * margin_px)
    if side < 4:
        return
    glyph = _load_logo_glyph(side, threshold=threshold)
    if glyph is None:
        return
    ink, alpha = glyph
    x = max(0, w - margin_px - side)
    y = max(0, h - margin_px - side)
    img.paste(ink, (x, y), alpha)


def source_badge(source: str) -> str:
    """Short human label for a best-by date source ("est." / "AI" / "").

    Unknown values fall back to no badge, so a stored value from a future
    version never prints garbage. Pure and fully testable."""
    return _SOURCE_BADGE.get(source, "")


@dataclass
class LabelSpec:
    """Everything needed to render one food label.

    Sizes are physical (inches) plus a dpi, so the pixel canvas is derived, not
    hard-coded. ``margin_in`` is the white border kept clear on every side.
    ``best_by_source`` records how the best-by date was derived (see
    DateSource); ``extra`` is an optional single line such as a quantity or a
    storage location.
    """
    name: str
    added: str = ""
    best_by: str = ""
    best_by_source: DateSource = "manual"
    extra: str = ""
    width_in: float = DEFAULT_WIDTH_IN
    height_in: float = DEFAULT_HEIGHT_IN
    dpi: int = DEFAULT_DPI
    margin_in: float = 0.06
    # Optional Pantry Raider brand mark in the bottom-right corner (FoodAssistant-
    # yglw). Off by default: most label stock is small and every extra element
    # competes for space.
    show_logo: bool = False

    @property
    def width_px(self) -> int:
        return max(1, round(self.width_in * self.dpi))

    @property
    def height_px(self) -> int:
        return max(1, round(self.height_in * self.dpi))

    @property
    def margin_px(self) -> int:
        return max(0, round(self.margin_in * self.dpi))


def _load_font(size: int, bold: bool = False):
    """Best-effort scalable font at ``size`` pixels.

    Prefers DejaVu (crisp at small thermal sizes) wherever it is installed, and
    falls back to Pillow's bundled default font, which is itself scalable on
    modern Pillow. Never raises: label rendering must not depend on a particular
    font being present in the container."""
    size = max(6, int(size))
    names = (
        ["DejaVuSans-Bold.ttf", "DejaVuSans.ttf"] if bold
        else ["DejaVuSans.ttf"]
    )
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        # Very old Pillow: load_default takes no size (fixed bitmap font).
        return ImageFont.load_default()


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    """Width and height of ``text`` in pixels for ``font``."""
    if not text:
        return 0, 0
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return right - left, bottom - top


def _wrap_lines(draw, text: str, font, max_width: int, max_lines: int) -> list[str]:
    """Word-wrap ``text`` to fit ``max_width`` px, at most ``max_lines`` lines.

    If it still does not fit, the last kept line is ellipsized with a trailing
    "..." so a very long food name never spills past the label edge. Pure given
    a draw context; no I/O."""
    words = (text or "").split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if _text_size(draw, candidate, font)[0] <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
        if len(lines) == max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)

    # Anything that did not fit (leftover words) means we truncated: ellipsize
    # the final line so the overflow is visible as "...", not a hard cut.
    consumed = " ".join(lines).split()
    if len(consumed) < len(words) and lines:
        lines[-1] = _ellipsize(draw, lines[-1], font, max_width)
    return lines


def _ellipsize(draw, text: str, font, max_width: int) -> str:
    """Trim ``text`` until it plus "..." fits ``max_width`` px."""
    if _text_size(draw, text, font)[0] <= max_width:
        return text
    ell = "..."
    trimmed = text
    while trimmed and _text_size(draw, trimmed + ell, font)[0] > max_width:
        trimmed = trimmed[:-1].rstrip()
    return (trimmed + ell) if trimmed else ell


def _fit_font_size(draw, text: str, max_width: int, max_height: int,
                   bold: bool, start: int, min_size: int = 8) -> int:
    """Largest font size (px) at which ``text`` fits on one line inside the box.

    Shrinks from ``start`` down to ``min_size``. Used for the food name so it is
    as large as the label allows before wrapping takes over."""
    size = max(min_size, start)
    while size > min_size:
        font = _load_font(size, bold=bold)
        w, h = _text_size(draw, text, font)
        if w <= max_width and h <= max_height:
            return size
        size -= 1
    return min_size


def _draw_badge_pill(draw, xy_right_top: tuple[int, int], text: str, font,
                     pad_x: int, pad_y: int, radius: int) -> tuple[int, int]:
    """Draw a filled rounded pill (black fill, white text) whose RIGHT edge sits
    at ``xy_right_top``. Returns the pill's (left, bottom). Used for the best-by
    source badge so it reads as a small, deliberate chip rather than a plain box.
    """
    right, top = xy_right_top
    bw, bh = _text_size(draw, text, font)
    w = bw + 2 * pad_x
    h = bh + 2 * pad_y
    left = right - w
    draw.rounded_rectangle([left, top, right, top + h], radius=radius, fill=0)
    draw.text((left + pad_x, top + pad_y - 1), text, fill=255, font=font)
    return left, top + h


def render_label(spec: LabelSpec | None = None, **kwargs) -> Image.Image:
    """Compose a food label to a 1-bit-friendly grayscale image.

    Accepts a LabelSpec, or the same fields as keyword arguments. The layout is
    built for a quick read at arm's length: the food name sits prominent at the
    top, a hairline rule separates it from the dates, the best-by date is the
    hero (a small "BEST BY" eyebrow with its source chip, then the date large),
    and a compact footer carries when it was added and any extra note. Dates are
    shown friendly ("Jul 23, 2026"). Returns a mode "L" image (white background,
    black ink) sized to the spec, with the margin kept clear on every side.
    """
    if spec is None:
        spec = LabelSpec(**kwargs)

    img = Image.new("L", (spec.width_px, spec.height_px), 255)
    draw = ImageDraw.Draw(img)

    m = spec.margin_px
    inner_w = max(1, spec.width_px - 2 * m)
    inner_h = max(1, spec.height_px - 2 * m)
    right = m + inner_w

    added = prettify_date(spec.added)
    best_by = prettify_date(spec.best_by)
    badge = source_badge(spec.best_by_source)
    has_hero = bool(spec.best_by)

    # Vertical budget. The name gets the top ~42%, the rest is shared by the
    # best-by hero and the footer. Fonts are scaled off the label height so the
    # same layout holds from a 1x2 thermal label to a 4x6 shipping label.
    name_band = int(inner_h * (0.42 if has_hero else 0.55))
    name_size = _fit_font_size(draw, spec.name or "", inner_w, int(name_band * 0.62),
                               bold=True, start=name_band)
    name_font = _load_font(name_size, bold=True)
    name_lines = _wrap_lines(draw, spec.name or "", name_font, inner_w, max_lines=2)

    y = m
    line_gap = max(1, int(name_size * 0.06))
    for line in name_lines:
        draw.text((m, y), line, fill=0, font=name_font)
        y += _text_size(draw, line, name_font)[1] + line_gap

    # Hairline rule under the name, kept inside the margins so the label edges
    # stay clear. A little breathing room above and below it.
    rule_y = max(y + line_gap, m + int(name_band * 0.72))
    rule_w = max(1, int(inner_h * 0.012))
    draw.line([(m, rule_y), (right, rule_y)], fill=0, width=rule_w)
    y = rule_y + rule_w + max(2, int(inner_h * 0.05))

    footer_size = max(8, int(inner_h * 0.11))
    footer_font = _load_font(footer_size, bold=False)

    if has_hero:
        eyebrow_size = max(7, int(inner_h * 0.085))
        eyebrow_font = _load_font(eyebrow_size, bold=True)
        date_size = max(10, int(inner_h * 0.20))
        date_font = _load_font(date_size, bold=True)

        # Eyebrow "BEST BY" on the left, source chip flush right on the same row.
        eb_h = _text_size(draw, "BEST BY", eyebrow_font)[1]
        draw.text((m, y), "BEST BY", fill=0, font=eyebrow_font)
        if badge:
            badge_font = _load_font(max(7, int(eyebrow_size * 1.02)), bold=True)
            _draw_badge_pill(draw, (right, y - 1), badge, badge_font,
                             pad_x=max(2, int(eyebrow_size * 0.4)),
                             pad_y=max(1, int(eyebrow_size * 0.18)),
                             radius=max(2, int(eyebrow_size * 0.5)))
        y += eb_h + max(1, int(inner_h * 0.02))

        date_line = _ellipsize(draw, best_by, date_font, inner_w)
        draw.text((m, y), date_line, fill=0, font=date_font)
        y += _text_size(draw, date_line or "Ay", date_font)[1] + max(2, int(inner_h * 0.05))

    # Footer: "Added <date>", with the extra note appended after a dot when it
    # fits. Anchored to the bottom so short labels are not lopsided.
    footer_bits = []
    if added:
        footer_bits.append(f"Added {added}")
    if spec.extra:
        footer_bits.append(spec.extra)
    if not has_hero and best_by:
        footer_bits.insert(0, f"Best by {best_by}")
    footer = "  ·  ".join(footer_bits)
    if footer:
        footer = _ellipsize(draw, footer, footer_font, inner_w)
        fh = _text_size(draw, footer, footer_font)[1]
        fy = min(y, m + inner_h - fh)
        fy = max(fy, y)
        if fy + fh > m + inner_h:
            fy = m + inner_h - fh
        draw.text((m, fy), footer, fill=0, font=footer_font)

    if spec.show_logo:
        draw_logo(img, m)

    return img


# -- Layout engine (FoodAssistant-bwl1 / -or5e) -----------------------------
#
# A layout-driven renderer that sits alongside render_label. Where render_label
# hard-codes the shipped food design, this engine draws a list of positioned
# elements, each placed as fractions of the inner (inside-the-margins) area so a
# layout is independent of the label size and dpi. This is the foundation the
# drag-and-drop field designer builds on: the designer edits a LabelLayout, this
# renders it, and a saved layout threads back into the normal print path.
#
# Everything stays pure (no I/O, no network) and reuses the same fit/wrap/badge
# helpers as render_label so a laid-out label looks consistent with the default.

# The fields an element can bind to. "static" carries its own text; the rest
# resolve from the values dict handed to render_layout. "barcode" and "qr"
# encode a value (or the element's own text). Kept as a tuple so validation can
# check membership and drop anything unknown rather than raising.
LAYOUT_FIELDS = (
    "name",
    "added",
    "best_by",
    "best_by_date",
    "best_by_badge",
    "extra",
    "quantity",
    "location",
    "static",
    "barcode",
    "qr",
)

_ALIGNMENTS = ("left", "center", "right")


def _clamp01(value, default: float = 0.0) -> float:
    """Coerce a value to a float in [0, 1], falling back to ``default``.

    Used when loading a layout from untrusted JSON so an out-of-range or
    non-numeric fraction can never place an element off the label; it is clamped
    instead of raising. Pure."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if f < 0.0:
        return 0.0
    if f > 1.0:
        return 1.0
    return f


@dataclass
class LabelElement:
    """One positioned field on a label.

    ``x, y, w, h`` are fractions 0..1 of the inner (inside-the-margins) area, so
    the same element lands in the same relative spot on any label size or dpi.
    ``field`` is one of LAYOUT_FIELDS. ``align`` is the horizontal text
    alignment; ``bold`` picks the bold font; ``size_scale`` scales the text
    relative to a fit-to-box fit (1.0 = as large as the box allows, 0.5 = half
    that); ``text`` supplies the content for the "static" field (and an optional
    value for barcode/qr); ``uppercase`` upper-cases the resolved text.
    """
    field: str = "static"
    x: float = 0.0
    y: float = 0.0
    w: float = 1.0
    h: float = 1.0
    align: str = "left"
    bold: bool = False
    size_scale: float = 1.0
    text: str = ""
    uppercase: bool = False

    def to_dict(self) -> dict:
        return {
            "field": self.field,
            "x": self.x, "y": self.y, "w": self.w, "h": self.h,
            "align": self.align,
            "bold": bool(self.bold),
            "size_scale": self.size_scale,
            "text": self.text,
            "uppercase": bool(self.uppercase),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LabelElement | None":
        """Build an element from an untrusted dict, or None if unusable.

        Unknown fields are dropped, fractions are clamped to [0, 1], and a bad
        ``field`` or a non-dict input yields None so render_layout can simply
        skip it. Never raises."""
        if not isinstance(data, dict):
            return None
        field_name = data.get("field")
        if field_name not in LAYOUT_FIELDS:
            return None
        align = data.get("align")
        if align not in _ALIGNMENTS:
            align = "left"
        try:
            scale = float(data.get("size_scale", 1.0))
        except (TypeError, ValueError):
            scale = 1.0
        if scale <= 0:
            scale = 1.0
        return cls(
            field=field_name,
            x=_clamp01(data.get("x", 0.0)),
            y=_clamp01(data.get("y", 0.0)),
            w=_clamp01(data.get("w", 1.0), default=1.0),
            h=_clamp01(data.get("h", 1.0), default=1.0),
            align=align,
            bold=bool(data.get("bold", False)),
            size_scale=scale,
            text=str(data.get("text", "") or ""),
            uppercase=bool(data.get("uppercase", False)),
        )


@dataclass
class LabelLayout:
    """A full label design: the stock size plus a list of positioned elements.

    Sizes are physical (inches) with a dpi, mirroring LabelSpec, so the pixel
    canvas is derived. ``margin_in`` is the white border kept clear on every
    side (the label edges stay blank, exactly like the default renderer)."""
    width_in: float = DEFAULT_WIDTH_IN
    height_in: float = DEFAULT_HEIGHT_IN
    dpi: int = DEFAULT_DPI
    margin_in: float = 0.06
    elements: list = field(default_factory=list)

    @property
    def width_px(self) -> int:
        return max(1, round(self.width_in * self.dpi))

    @property
    def height_px(self) -> int:
        return max(1, round(self.height_in * self.dpi))

    @property
    def margin_px(self) -> int:
        return max(0, round(self.margin_in * self.dpi))

    def to_dict(self) -> dict:
        return {
            "width_in": self.width_in,
            "height_in": self.height_in,
            "dpi": self.dpi,
            "margin_in": self.margin_in,
            "elements": [e.to_dict() for e in self.elements],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LabelLayout":
        """Build a layout from an untrusted dict, ignoring malformed pieces.

        Missing sizes fall back to the defaults; malformed elements are dropped
        rather than raising, so a hand-edited or partially-corrupt layout still
        renders whatever is valid. Never raises on structure."""
        if not isinstance(data, dict):
            data = {}

        def _num(key, default):
            try:
                v = float(data.get(key, default))
                return v if v > 0 else default
            except (TypeError, ValueError):
                return default

        try:
            dpi = int(data.get("dpi", DEFAULT_DPI))
        except (TypeError, ValueError):
            dpi = DEFAULT_DPI
        if dpi <= 0:
            dpi = DEFAULT_DPI
        # margin_in may legitimately be 0 (edge-to-edge), so treat it separately
        # from the positive-only size fields: clamp to >= 0, fall back on garbage.
        try:
            margin = float(data.get("margin_in", 0.06))
            if margin < 0:
                margin = 0.06
        except (TypeError, ValueError):
            margin = 0.06
        raw_elements = data.get("elements")
        elements: list = []
        if isinstance(raw_elements, list):
            for item in raw_elements:
                el = LabelElement.from_dict(item)
                if el is not None:
                    elements.append(el)
        return cls(
            width_in=_num("width_in", DEFAULT_WIDTH_IN),
            height_in=_num("height_in", DEFAULT_HEIGHT_IN),
            dpi=dpi,
            margin_in=margin,
            elements=elements,
        )


def _element_display_text(el: LabelElement, values: dict) -> str:
    """Resolve the text an element should show from ``values``.

    Dates run through prettify_date; "static" uses the element's own text. The
    badge and barcode/qr fields are handled separately by the renderer and get
    "" here. Applies ``uppercase`` last. Pure."""
    f = el.field
    if f == "static":
        text = el.text or ""
    elif f == "name":
        text = str(values.get("name", "") or "")
    elif f == "added":
        text = prettify_date(str(values.get("added", "") or ""))
    elif f in ("best_by", "best_by_date"):
        text = prettify_date(str(values.get("best_by", "") or ""))
    elif f == "extra":
        text = str(values.get("extra", "") or "")
    elif f == "quantity":
        text = str(values.get("quantity", "") or "")
    elif f == "location":
        text = str(values.get("location", "") or "")
    else:
        text = ""
    if el.uppercase:
        text = text.upper()
    return text


def _fit_wrapped(draw, text: str, box_w: int, box_h: int, bold: bool,
                 max_lines: int = 3) -> tuple[int, list[str]]:
    """Largest font size (px) and wrapped lines that fit ``text`` in the box.

    Grows nothing: it starts at the box height and shrinks until the wrapped
    block fits both the width and the height, so the result is the fit-to-box
    size. Returns (size, lines). Pure given a draw context."""
    if not text:
        return 6, [""]
    size = max(6, int(box_h))
    while size > 6:
        font = _load_font(size, bold=bold)
        lines = _wrap_lines(draw, text, font, box_w, max_lines=max_lines)
        line_h = _text_size(draw, "Ay", font)[1]
        gap = max(1, int(size * 0.08))
        total_h = len(lines) * line_h + max(0, len(lines) - 1) * gap
        widest = max((_text_size(draw, ln, font)[0] for ln in lines), default=0)
        if total_h <= box_h and widest <= box_w:
            return size, lines
        size -= 1
    font = _load_font(6, bold=bold)
    return 6, _wrap_lines(draw, text, font, box_w, max_lines=max_lines)


def _draw_aligned_text(draw, text: str, bx: int, by: int, bw: int, bh: int,
                       align: str, bold: bool, size_scale: float) -> None:
    """Fit ``text`` to the box, scale it, and draw it aligned and centred
    vertically. Nothing is drawn for empty text. Pure given the draw context."""
    if not text:
        return
    fit_size, _lines = _fit_wrapped(draw, text, bw, bh, bold)
    size = max(6, int(fit_size * (size_scale or 1.0)))
    font = _load_font(size, bold=bold)
    lines = _wrap_lines(draw, text, font, bw, max_lines=3)
    line_h = _text_size(draw, "Ay", font)[1]
    gap = max(1, int(size * 0.08))
    block_h = len(lines) * line_h + max(0, len(lines) - 1) * gap
    # Keep the block inside the box top edge even if a large size_scale overflows.
    y = by + max(0, (bh - block_h) // 2)
    for line in lines:
        lw = _text_size(draw, line, font)[0]
        if align == "center":
            x = bx + max(0, (bw - lw) // 2)
        elif align == "right":
            x = bx + max(0, bw - lw)
        else:
            x = bx
        draw.text((x, y), line, fill=0, font=font)
        y += line_h + gap


def _render_qr_image(text: str, side_px: int) -> "Image.Image | None":
    """A square QR code image at ``side_px``, or None if QR cannot be made.

    Uses the bundled qrcode library. Defensive: any failure (empty text, import
    trouble) returns None so the caller can fall back to plain text and a render
    never crashes."""
    if not text or side_px < 1:
        return None
    try:
        import qrcode
        qr = qrcode.QRCode(border=1, box_size=1,
                           error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("L")
        return img.resize((side_px, side_px), Image.NEAREST)
    except Exception:  # noqa: BLE001
        return None


def _render_barcode_image(text: str, box_w: int, box_h: int) -> "Image.Image | None":
    """A linear (Code128) barcode image, or None when no barcode library is
    installed. Optional: the project does not bundle python-barcode, so this
    normally returns None and the renderer prints the digits as text instead. If
    the library is present it is used, and any failure still degrades to None so
    a render never crashes."""
    if not text or box_w < 1 or box_h < 1:
        return None
    try:
        import barcode  # python-barcode, optional
        from barcode.writer import ImageWriter
        code = barcode.get("code128", text, writer=ImageWriter())
        img = code.render(writer_options={"write_text": False,
                                          "module_height": 8.0, "quiet_zone": 1.0})
        return img.convert("L").resize((box_w, box_h), Image.NEAREST)
    except Exception:  # noqa: BLE001
        return None


def _paste_in_box(img: "Image.Image", canvas: "Image.Image", bx: int, by: int,
                  bw: int, bh: int, align: str) -> None:
    """Paste ``img`` into the box, aligned horizontally and centred vertically.
    Used for QR and barcode images so they sit where the element is placed."""
    iw, ih = img.size
    if align == "center":
        x = bx + max(0, (bw - iw) // 2)
    elif align == "right":
        x = bx + max(0, bw - iw)
    else:
        x = bx
    y = by + max(0, (bh - ih) // 2)
    canvas.paste(img, (x, y))


def render_layout(layout: LabelLayout, values: dict) -> Image.Image:
    """Compose a label from a LabelLayout and a values dict.

    Each element is placed in its fractional box within the inner (inside the
    margins) area and drawn: text fields are fit, wrapped, and aligned; the
    best_by_badge draws the filled source chip; barcode/qr encode a value (or the
    element's own text) and degrade to plain text when no encoder is available.
    An element whose value is empty simply draws nothing. Returns a mode "L"
    image (white background, black ink) with the label edges kept clear, so the
    edge-stays-white invariant of the default renderer holds. Pure."""
    values = values or {}
    img = Image.new("L", (layout.width_px, layout.height_px), 255)
    draw = ImageDraw.Draw(img)

    m = layout.margin_px
    inner_w = max(1, layout.width_px - 2 * m)
    inner_h = max(1, layout.height_px - 2 * m)

    for el in layout.elements:
        # Fractional box -> pixel box, clamped to the inner area so nothing ever
        # spills into the margin (keeps the label edges blank).
        bx = m + int(round(el.x * inner_w))
        by = m + int(round(el.y * inner_h))
        bw = max(1, int(round(el.w * inner_w)))
        bh = max(1, int(round(el.h * inner_h)))
        bw = min(bw, m + inner_w - bx)
        bh = min(bh, m + inner_h - by)
        if bw < 1 or bh < 1:
            continue

        if el.field == "best_by_badge":
            badge = source_badge(str(values.get("best_by_source", "") or ""))
            if badge:
                fsize = max(7, int(bh * 0.8))
                font = _load_font(fsize, bold=True)
                _draw_badge_pill(draw, (bx + bw, by), badge, font,
                                 pad_x=max(2, int(fsize * 0.4)),
                                 pad_y=max(1, int(fsize * 0.18)),
                                 radius=max(2, int(fsize * 0.5)))
            continue

        if el.field == "qr":
            qr_text = el.text or str(values.get("name", "") or "")
            side = min(bw, bh)
            qr_img = _render_qr_image(qr_text, side)
            if qr_img is not None:
                _paste_in_box(qr_img, img, bx, by, bw, bh, el.align)
            else:
                _draw_aligned_text(draw, qr_text, bx, by, bw, bh,
                                   el.align, el.bold, el.size_scale)
            continue

        if el.field == "barcode":
            bc_text = el.text or str(values.get("name", "") or "")
            bc_img = _render_barcode_image(bc_text, bw, bh)
            if bc_img is not None:
                _paste_in_box(bc_img, img, bx, by, bw, bh, el.align)
            else:
                # No barcode encoder available: show the digits/text so the
                # element is never blank and the render never fails.
                _draw_aligned_text(draw, bc_text, bx, by, bw, bh,
                                   el.align, el.bold, el.size_scale)
            continue

        text = _element_display_text(el, values)
        _draw_aligned_text(draw, text, bx, by, bw, bh,
                           el.align, el.bold, el.size_scale)

    if values.get("show_logo"):
        draw_logo(img, m)

    return img


def default_food_layout(width_in: float = DEFAULT_WIDTH_IN,
                        height_in: float = DEFAULT_HEIGHT_IN,
                        dpi: int = DEFAULT_DPI,
                        margin_in: float = 0.06) -> LabelLayout:
    """The shipped food design expressed as layout elements.

    This is the seed the drag-and-drop designer starts from: a prominent name
    band, a "BEST BY" eyebrow with its source chip, the best-by date as the
    hero, and a compact footer. render_label remains the default print path (so
    an untouched label is pixel-for-pixel today's design); this layout mirrors it
    closely for the designer and renders the same fields in the same places."""
    return LabelLayout(
        width_in=width_in, height_in=height_in, dpi=dpi, margin_in=margin_in,
        elements=[
            LabelElement(field="name", x=0.0, y=0.0, w=1.0, h=0.40,
                         align="left", bold=True),
            LabelElement(field="static", text="BEST BY", x=0.0, y=0.46,
                         w=0.6, h=0.12, align="left", bold=True, uppercase=True),
            LabelElement(field="best_by_badge", x=0.6, y=0.44, w=0.4, h=0.14,
                         align="right"),
            LabelElement(field="best_by_date", x=0.0, y=0.58, w=1.0, h=0.24,
                         align="left", bold=True),
            LabelElement(field="added", x=0.0, y=0.86, w=1.0, h=0.12,
                         align="left", bold=False),
        ],
    )


# -- Format presets (FoodAssistant-bwl1) ------------------------------------
#
# Common label stock sizes a user can pick in one click. Each preset carries a
# size and a starting layout so choosing "3x2 label" both resizes the stock and
# seeds a sensible design the designer can then tweak. Kept as plain data so a
# new preset is a one-line addition and every preset is easy to unit-test.


def _centered_name_layout(width_in: float, height_in: float,
                          dpi: int = DEFAULT_DPI) -> LabelLayout:
    """A single centred name filling the label (spice jars, canisters, bins)."""
    return LabelLayout(
        width_in=width_in, height_in=height_in, dpi=dpi, margin_in=0.06,
        elements=[LabelElement(field="name", x=0.0, y=0.0, w=1.0, h=1.0,
                               align="center", bold=True)],
    )


def mm_to_in(mm: float) -> float:
    """Millimeters to inches, rounded to 3 decimal places.

    Every stored preset size is inches (the renderer and the rest of the app
    work in inches throughout), so a metric label preset is defined in mm for
    a readable name and converted once here. Pure and unit-tested directly so
    the conversion can't silently drift."""
    return round(mm / 25.4, 3)


def _metric_preset(key: str, width_mm: float, height_mm: float, name: str,
                   dpi: int = DEFAULT_DPI) -> dict:
    """A label preset defined by its metric size in mm, converted to inches.

    ``name`` should already read in mm (e.g. "40 x 30mm label") since that is
    what a user with metric label stock is shopping for."""
    width_in = mm_to_in(width_mm)
    height_in = mm_to_in(height_mm)
    return {"key": key, "name": name, "width_in": width_in,
            "height_in": height_in, "dpi": dpi,
            "layout": default_food_layout(width_in, height_in, dpi)}


LABEL_PRESETS = [
    {"key": "2x1", "name": "2 x 1 in small label",
     "width_in": 2.0, "height_in": 1.0, "dpi": 203,
     "layout": default_food_layout(2.0, 1.0, 203)},
    {"key": "1x2_address", "name": "1 x 2 in address",
     "width_in": 1.0, "height_in": 2.0, "dpi": 203,
     "layout": default_food_layout(1.0, 2.0, 203)},
    {"key": "2.25x1.25", "name": "2.25 x 1.25 in label",
     "width_in": 2.25, "height_in": 1.25, "dpi": 203,
     "layout": default_food_layout(2.25, 1.25, 203)},
    {"key": "3x2", "name": "3 x 2 in label",
     "width_in": 3.0, "height_in": 2.0, "dpi": 203,
     "layout": default_food_layout(3.0, 2.0, 203)},
    {"key": "4x6_shipping", "name": "4 x 6 in shipping",
     "width_in": 4.0, "height_in": 6.0, "dpi": 203,
     "layout": default_food_layout(4.0, 6.0, 203)},
    {"key": "spice_square", "name": "1.5 x 1.5 in spice (square)",
     "width_in": 1.5, "height_in": 1.5, "dpi": 203,
     "layout": _centered_name_layout(1.5, 1.5, 203)},
    # Metric label stock (FoodAssistant-fgcq): common sizes sold in mm, stored
    # as inches like every other preset since the renderer works in inches.
    _metric_preset("40x30mm", 40, 30, "40 x 30mm label"),
    _metric_preset("50x30mm", 50, 30, "50 x 30mm label"),
    _metric_preset("40x60mm", 40, 60, "40 x 60mm label"),
    _metric_preset("62x29mm", 62, 29, "62 x 29mm label"),
]


def preset_by_key(key: str) -> "dict | None":
    """The preset with this key, or None. The returned dict is the live entry
    (its "layout" is a LabelLayout); callers that mutate should copy first."""
    for preset in LABEL_PRESETS:
        if preset["key"] == key:
            return preset
    return None


def presets_summary() -> list[dict]:
    """A compact list of presets for a UI dropdown: key, name, and size only
    (no layout payload). Pure."""
    return [
        {"key": p["key"], "name": p["name"],
         "width_in": p["width_in"], "height_in": p["height_in"]}
        for p in LABEL_PRESETS
    ]


def presets_detail() -> list[dict]:
    """The presets for the label designer: each carries its size, dpi, and a
    starting layout as a plain dict.

    The designer both resizes the stock and seeds a design when a preset is
    picked, so it needs the layout up front (presets_summary omits it for the
    lighter size-only dropdown). The "layout" is a serialized LabelLayout, safe
    to hand straight to the browser. Pure."""
    return [
        {"key": p["key"], "name": p["name"],
         "width_in": p["width_in"], "height_in": p["height_in"],
         "dpi": p.get("dpi", DEFAULT_DPI),
         "layout": p["layout"].to_dict()}
        for p in LABEL_PRESETS
    ]


def render_decorative_label(text: str, *, width_in: float = DEFAULT_WIDTH_IN,
                            height_in: float = DEFAULT_HEIGHT_IN,
                            dpi: int = DEFAULT_DPI, margin_in: float = 0.06,
                            bold: bool = True) -> Image.Image:
    """A dateless decorative label (spice jars, canisters, storage bins).

    Just the text, centered and set as large as the stock allows, wrapping onto
    up to three lines. No dates and no badges: this variant is for naming a
    container, not tracking spoilage. Returns a mode "L" image."""
    spec = LabelSpec(name=text, width_in=width_in, height_in=height_in,
                     dpi=dpi, margin_in=margin_in)
    img = Image.new("L", (spec.width_px, spec.height_px), 255)
    draw = ImageDraw.Draw(img)

    m = spec.margin_px
    inner_w = max(1, spec.width_px - 2 * m)
    inner_h = max(1, spec.height_px - 2 * m)

    # Grow the type until wrapping to <= 3 lines no longer fits the height.
    best_size = 8
    best_lines = [text or ""]
    size = 8
    while size <= inner_h:
        font = _load_font(size, bold=bold)
        lines = _wrap_lines(draw, text or "", font, inner_w, max_lines=3)
        total_h = sum(_text_size(draw, ln, font)[1] for ln in lines)
        total_h += max(0, len(lines) - 1) * int(size * 0.15)
        widest = max((_text_size(draw, ln, font)[0] for ln in lines), default=0)
        if total_h <= inner_h and widest <= inner_w:
            best_size, best_lines = size, lines
            size += 2
        else:
            break

    font = _load_font(best_size, bold=bold)
    gap = int(best_size * 0.15)
    line_heights = [_text_size(draw, ln, font)[1] for ln in best_lines]
    block_h = sum(line_heights) + max(0, len(best_lines) - 1) * gap
    y = m + max(0, (inner_h - block_h) // 2)
    for ln, lh in zip(best_lines, line_heights):
        lw = _text_size(draw, ln, font)[0]
        x = m + max(0, (inner_w - lw) // 2)
        draw.text((x, y), ln, fill=0, font=font)
        y += lh + gap
    return img


def render_to_png_bytes(img: Image.Image) -> bytes:
    """Encode a rendered label as PNG bytes for a caller or the print backend."""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_to_pdf_bytes(img_or_specs, label_size: tuple[float, float] | None = None) -> bytes:
    """Encode label(s) as a PDF, one label per page at its physical size.

    Accepts a single PIL image, a single LabelSpec, or a list of images/specs
    (mixed is fine). ``label_size`` is an optional (width_in, height_in)
    override applied only to bare images (specs already carry their own size).
    Each page is set to the label's physical dimensions so a printer driver
    lays it out at true size. Returns the PDF as bytes."""
    items = img_or_specs if isinstance(img_or_specs, (list, tuple)) else [img_or_specs]
    pages: list[Image.Image] = []
    dims_in: list[tuple[float, float]] = []
    for item in items:
        if isinstance(item, LabelSpec):
            pages.append(render_label(item).convert("L"))
            dims_in.append((item.width_in, item.height_in))
        else:
            img = item.convert("L")
            pages.append(img)
            if label_size:
                dims_in.append((label_size[0], label_size[1]))
            else:
                # Infer inches from pixels at the default dpi.
                dims_in.append((img.width / DEFAULT_DPI, img.height / DEFAULT_DPI))

    if not pages:
        raise ValueError("no labels to render")

    # PDF points are 1/72 inch; setting each image's DPI makes Pillow lay the
    # page out at the physical size we want.
    buf = io.BytesIO()
    resolutions = [(p.width / max(0.01, d[0])) for p, d in zip(pages, dims_in)]
    first, rest = pages[0], pages[1:]
    first.save(
        buf, format="PDF", save_all=True, append_images=rest,
        resolution=resolutions[0],
    )
    return buf.getvalue()


def render_batch_pdf_bytes(specs: list[LabelSpec]) -> bytes:
    """Tile many food labels into one PDF, one label per page.

    Convenience wrapper over render_to_pdf_bytes for the "print a batch of
    labels when importing stock" flow: pass a LabelSpec per item and get back a
    single multi-page PDF, each page sized to the label stock. The page count
    equals the number of specs."""
    if not specs:
        raise ValueError("no labels to render")
    return render_to_pdf_bytes(list(specs))
