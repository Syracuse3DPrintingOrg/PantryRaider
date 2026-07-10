"""Full-page document rendering for the printer (FoodAssistant-fb8x).

The label renderer (services/label_render.py) is built for small thermal label
stock: one short block of text per label. A recipe printout is a different job,
a full letter-size page with a title, an ingredients list, and numbered steps,
possibly running onto more than one page. This module turns a recipe (or a
block of raw text) into that page, or pages, and hands the bytes to the print
backend.

The formatting is split into two halves so the shaping is testable without ever
touching Pillow: ``recipe_to_blocks`` and ``html_to_text`` are pure functions
that turn structured input into a flat list of styled text blocks, and
``render_document_pdf_bytes`` lays those blocks out onto page images. Nothing
here talks to a printer or the network.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass

from PIL import Image, ImageDraw

from .label_render import _load_font, _text_size, _wrap_lines

# US Letter at a comfortable print resolution. 150 dpi keeps the page image
# light while staying crisp for text.
LETTER_WIDTH_IN = 8.5
LETTER_HEIGHT_IN = 11.0
DOC_DPI = 150
MARGIN_IN = 0.6


# -- Advanced document print settings (FoodAssistant-7xo5) -------------------
#
# Page size, color mode, and duplex for the DOCUMENT printer (recipes and other
# full-page printouts), mapped to the CUPS "lp -o" option names. Kept as a
# small, pure lookup so a bad or unknown stored value degrades to CUPS's own
# default rather than being sent through, and so the mapping is unit-testable
# without a print backend.

# CUPS media names for the page sizes offered in the settings pane. "auto"
# deliberately maps to no media option at all, letting the queue's own default
# page size win (useful when a printer is loaded with something other than
# Letter or A4).
DOCUMENT_PAGE_SIZES: dict[str, str] = {
    "auto": "",
    "letter": "Letter",
    "a4": "A4",
    "legal": "Legal",
}

DOCUMENT_COLOR_MODES: dict[str, str] = {
    "color": "color",
    "monochrome": "monochrome",
}

DOCUMENT_DUPLEX_MODES: dict[str, str] = {
    "one-sided": "one-sided",
    "two-sided": "two-sided-long-edge",
}


def document_print_options(page_size: str = "auto", color_mode: str = "color",
                           duplex: str = "one-sided") -> dict:
    """Map the document printer's advanced settings to CUPS ``lp -o`` options.

    Unknown or blank values are simply omitted (never raise, never invent an
    option CUPS might reject), so a stale or hand-edited setting degrades to
    the printer's own default for that option rather than breaking the print.
    Returns a dict of CUPS option name to value, ready for
    services/printing.py's ``print_bytes(options=...)``. Pure."""
    options: dict[str, str] = {}
    media = DOCUMENT_PAGE_SIZES.get((page_size or "").strip().lower())
    if media:
        options["media"] = media
    color = DOCUMENT_COLOR_MODES.get((color_mode or "").strip().lower())
    if color:
        options["print-color-mode"] = color
    sides = DOCUMENT_DUPLEX_MODES.get((duplex or "").strip().lower())
    if sides:
        options["sides"] = sides
    return options


@dataclass
class Block:
    """One run of text with a role, so the renderer can size and space it.

    ``style`` is one of "title", "heading", "body", or "step"; ``text`` is the
    line's content. A block with empty text is a deliberate blank line (spacer).
    """
    style: str
    text: str = ""


def _fmt_qty(qty) -> str:
    """Render an ingredient quantity without a trailing ".0" on whole numbers."""
    if qty is None:
        return ""
    try:
        f = float(qty)
    except (TypeError, ValueError):
        return str(qty).strip()
    if f == int(f):
        return str(int(f))
    # Trim to at most 2 decimals, then strip trailing zeros.
    return f"{f:.2f}".rstrip("0").rstrip(".")


def _ingredient_line(ing) -> str:
    """One ingredient rendered as 'quantity unit name', tolerant of shapes.

    Accepts a dict {name, quantity/scaled_quantity, unit} or a bare string."""
    if isinstance(ing, str):
        return ing.strip()
    if not isinstance(ing, dict):
        return str(ing or "").strip()
    name = str(ing.get("name") or "").strip()
    qty = ing.get("scaled_quantity")
    if qty is None:
        qty = ing.get("quantity")
    unit = str(ing.get("unit") or "").strip()
    parts = [p for p in (_fmt_qty(qty), unit, name) if p]
    return " ".join(parts).strip()


def recipe_to_blocks(recipe: dict) -> list[Block]:
    """Flatten a recipe dict into an ordered list of styled text blocks.

    Tolerant of the shapes the app already produces (the current-recipe
    serializer, a Mealie-derived dict, or an AI/import dict): it reads a title,
    an optional servings line, ingredients, numbered steps, and notes, skipping
    any section that is absent. Pure and fully testable."""
    d = recipe or {}
    blocks: list[Block] = []

    title = str(d.get("title") or d.get("name") or "Recipe").strip() or "Recipe"
    blocks.append(Block("title", title))

    servings = d.get("scaled_servings") or d.get("servings")
    if servings:
        blocks.append(Block("body", f"Serves {_fmt_qty(servings)}"))

    ings = d.get("ingredients") or []
    lines = [_ingredient_line(i) for i in ings]
    lines = [ln for ln in lines if ln]
    if lines:
        blocks.append(Block("heading", "Ingredients"))
        for ln in lines:
            blocks.append(Block("body", f"- {ln}"))

    steps = d.get("steps") or d.get("instructions") or []
    steps = [str(s).strip() for s in steps if str(s).strip()]
    if steps:
        blocks.append(Block("heading", "Steps"))
        for i, step in enumerate(steps, 1):
            blocks.append(Block("step", f"{i}. {step}"))

    notes = str(d.get("notes") or d.get("description") or "").strip()
    if notes:
        blocks.append(Block("heading", "Notes"))
        blocks.append(Block("body", notes))

    return blocks


_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_TAG_RE = re.compile(r"</(p|div|h[1-6]|li|tr|br)\s*>|<br\s*/?>", re.IGNORECASE)


def html_to_text(html: str) -> str:
    """Reduce a snippet of HTML to plain text, one line per block element.

    Not a full HTML engine: it keeps the readable text and turns block-level
    tag closes (and <br>) into line breaks so a pasted recipe still prints as
    tidy lines. Common entities are unescaped. Pure and testable."""
    import html as _html

    text = html or ""
    # Newline at the close of any block element (or a <br>) before stripping tags.
    text = _BLOCK_TAG_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = _html.unescape(text)
    # Collapse runs of blank lines and trailing spaces.
    lines = [ln.strip() for ln in text.splitlines()]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()


def text_to_blocks(text: str, title: str = "") -> list[Block]:
    """Turn a block of plain text into body blocks, with an optional title.

    A blank line becomes a spacer so paragraph breaks survive to the page."""
    blocks: list[Block] = []
    if title.strip():
        blocks.append(Block("title", title.strip()))
    for ln in (text or "").splitlines():
        blocks.append(Block("body", ln.rstrip()))
    if not blocks:
        blocks.append(Block("body", ""))
    return blocks


# Point sizes per style, scaled to the page dpi at render time.
_STYLE_PT = {"title": 22, "heading": 15, "step": 11, "body": 11}
_STYLE_BOLD = {"title": True, "heading": True, "step": False, "body": False}
_STYLE_SPACE_BEFORE = {"title": 0, "heading": 12, "step": 4, "body": 0}


def render_document_pdf_bytes(
    blocks: list[Block],
    *,
    width_in: float = LETTER_WIDTH_IN,
    height_in: float = LETTER_HEIGHT_IN,
    dpi: int = DOC_DPI,
    margin_in: float = MARGIN_IN,
) -> bytes:
    """Lay styled text blocks onto one or more letter pages and return a PDF.

    Text wraps to the page width and flows onto a new page when it reaches the
    bottom margin, so a long recipe spans as many pages as it needs. Each page
    is set to its physical size so a printer driver lays it out true to size."""
    width_px = max(1, round(width_in * dpi))
    height_px = max(1, round(height_in * dpi))
    margin_px = max(0, round(margin_in * dpi))
    inner_w = max(1, width_px - 2 * margin_px)
    bottom = height_px - margin_px

    def _new_page() -> tuple[Image.Image, ImageDraw.ImageDraw]:
        img = Image.new("L", (width_px, height_px), 255)
        return img, ImageDraw.Draw(img)

    pages: list[Image.Image] = []
    img, draw = _new_page()
    y = margin_px

    def _line_px(pt: int) -> int:
        return max(1, round(pt * dpi / 72))

    for block in blocks:
        px = _line_px(_STYLE_PT.get(block.style, 11))
        bold = _STYLE_BOLD.get(block.style, False)
        space_before = _line_px(_STYLE_SPACE_BEFORE.get(block.style, 0))
        font = _load_font(px, bold=bold)
        # A blank body block is an intentional spacer.
        if not block.text:
            y += px
            continue
        wrapped = _wrap_lines(draw, block.text, font, inner_w, max_lines=100)
        y += space_before
        for line in wrapped:
            line_h = _text_size(draw, "Ay", font)[1]
            if y + line_h > bottom:
                pages.append(img)
                img, draw = _new_page()
                y = margin_px
            draw.text((margin_px, y), line, fill=0, font=font)
            y += line_h + max(1, int(px * 0.25))

    pages.append(img)

    buf = io.BytesIO()
    resolution = pages[0].width / max(0.01, width_in)
    first, rest = pages[0], pages[1:]
    first.save(buf, format="PDF", save_all=True, append_images=rest,
               resolution=resolution)
    return buf.getvalue()


def render_recipe_pdf_bytes(recipe: dict, **kwargs) -> bytes:
    """Convenience: a recipe dict straight to a printable PDF."""
    return render_document_pdf_bytes(recipe_to_blocks(recipe), **kwargs)
