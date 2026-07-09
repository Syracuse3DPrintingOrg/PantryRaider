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

    return img


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
