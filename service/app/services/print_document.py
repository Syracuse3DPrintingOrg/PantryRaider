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
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

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

    ``style`` is one of "title", "quickfacts", "heading", "body", or "step";
    ``text`` is the line's content. A block with empty text is a deliberate
    blank line (spacer). ``items`` is only set on a "columns" block (the
    two-column ingredients layout): a flat list of bullet lines to split
    across the page width instead of running down a single column.
    """
    style: str
    text: str = ""
    items: list[str] = field(default_factory=list)


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


def _first_str(*values) -> str:
    """The first non-blank value, stringified and stripped, or "".

    Lets a formatter read several possible key spellings for the same fact
    (a raw Mealie field, the native store's snake_case, a scraped import)
    without a chain of `or` clauses at every call site."""
    for v in values:
        s = str(v or "").strip()
        if s:
            return s
    return ""


def format_quick_facts(recipe: dict) -> str:
    """The header's quick-facts line: prep time, cook time, total time, and
    servings, each shown only when present, in that order, separated by " | ".

    Tolerant of the several shapes a recipe dict arrives in: the native
    store and current-recipe serializer use snake_case
    (``prep_time``/``cook_time``/``total_time``), a raw Mealie recipe detail
    uses camelCase plus ``performTime`` for cook time. Returns "" when none of
    prep, cook, total, or servings is present, so the caller can skip the row
    entirely for a bare recipe. Pure and fully testable."""
    d = recipe or {}
    prep = _first_str(d.get("prep_time"), d.get("prepTime"))
    cook = _first_str(d.get("cook_time"), d.get("cookTime"), d.get("performTime"))
    total = _first_str(d.get("total_time"), d.get("totalTime"))
    servings = (d.get("scaled_servings") or d.get("servings")
                or d.get("recipeYield"))

    parts: list[str] = []
    if prep:
        parts.append(f"Prep {prep}")
    if cook:
        parts.append(f"Cook {cook}")
    if total:
        parts.append(f"Total {total}")
    if servings:
        s = servings.strip() if isinstance(servings, str) else _fmt_qty(servings)
        if s:
            parts.append(f"Serves {s}")
    return " | ".join(parts)


# A short list of ingredients reads best top-to-bottom in a single column; past
# this count, splitting into two columns uses the page width instead of
# running a long list down the page and pushing the steps onto page two.
_TWO_COLUMN_THRESHOLD = 8


def use_two_column_ingredients(count: int) -> bool:
    """Whether an ingredients list of ``count`` items should lay out as two
    columns rather than one. Pure."""
    return int(count or 0) > _TWO_COLUMN_THRESHOLD


def recipe_to_blocks(recipe: dict) -> list[Block]:
    """Flatten a recipe dict into an ordered list of styled text blocks.

    Tolerant of the shapes the app already produces (the current-recipe
    serializer, a Mealie-derived dict, or an AI/import dict): it reads a
    title, a quick-facts row (prep/cook/total time and servings, whichever
    are present), ingredients, numbered steps, and notes, skipping any
    section that is absent. A longer ingredients list lays out as two
    columns (see ``use_two_column_ingredients``) so it fits the page width
    instead of running down it. Pure and fully testable."""
    d = recipe or {}
    blocks: list[Block] = []

    title = str(d.get("title") or d.get("name") or "Recipe").strip() or "Recipe"
    blocks.append(Block("title", title))

    facts = format_quick_facts(d)
    if facts:
        blocks.append(Block("quickfacts", facts))

    # Ingredients are a flat list today. Grouped/sectioned ingredients (a
    # "Sauce" / "Dough" style shape) are being added to the recipe elsewhere;
    # when that lands, this stays a small addition: emit a subheading block per
    # section followed by its own bullets (or per-section "columns" blocks),
    # reusing the same bullet formatting below. Nothing here assumes sections
    # exist, so the flat path keeps working unchanged.
    ings = d.get("ingredients") or []
    lines = [_ingredient_line(i) for i in ings]
    lines = [ln for ln in lines if ln]
    if lines:
        blocks.append(Block("heading", "Ingredients"))
        bullets = [f"- {ln}" for ln in lines]
        if use_two_column_ingredients(len(lines)):
            blocks.append(Block("columns", items=bullets))
        else:
            for bullet in bullets:
                blocks.append(Block("body", bullet))

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


# Point sizes per style, scaled to the page dpi (and the one-page-fit scale
# below) at render time.
_STYLE_PT = {
    "title": 22, "quickfacts": 12, "heading": 15, "step": 11, "body": 11,
    "columns": 11,
}
_STYLE_BOLD = {
    "title": True, "quickfacts": False, "heading": True, "step": False,
    "body": False, "columns": False,
}
_STYLE_SPACE_BEFORE = {
    "title": 0, "quickfacts": 2, "heading": 12, "step": 4, "body": 0,
    "columns": 0,
}


# -- One-page-fit (FoodAssistant-gm4c) ---------------------------------------
#
# Most recipes should print on a single page. Rather than measure a real
# render (which would make the "does it fit" decision depend on Pillow), the
# content is scored by a cheap, pure character count, weighted so a title or
# heading counts for more per character than a wrapped body line (they carry
# extra line-height and space-before that a flat character count would miss).
# A recipe under the threshold prints at full size; a longer one shrinks
# fonts and spacing down toward a floor, which buys back enough lines to
# land most everyday recipes on one page without making a short recipe look
# needlessly cramped.

# Roughly the content one letter page holds at 100% scale: a title, a
# quick-facts row, a dozen or so ingredients, and a handful of steps.
_FIT_FULL_CHARS = 2600
_FIT_MIN_SCALE = 0.72


def content_char_count(blocks: list[Block]) -> int:
    """A cheap, pure size estimate for a list of blocks, weighted by style.

    A title or heading line is weighted heavier than its character count (it
    is drawn in a larger font and carries extra space-before), so two
    recipes with the same total character count but different structure
    still score close to their actual printed height. Pure."""
    total = 0
    for b in blocks:
        if b.style == "columns":
            # Two columns roughly halve the vertical space a flat list of
            # the same items would take.
            total += sum(len(it) for it in b.items) // 2
            continue
        n = len(b.text)
        if b.style == "title":
            total += n * 3 + 20
        elif b.style == "heading":
            total += n * 2 + 40
        else:
            total += n
    return total


def fit_scale(blocks: list[Block]) -> float:
    """The font-scale multiplier (``_FIT_MIN_SCALE``..1.0) for this content.

    Content at or under ``_FIT_FULL_CHARS`` prints at full size (1.0). Past
    that, the scale falls off linearly, reaching ``_FIT_MIN_SCALE`` at twice
    the threshold and staying there for anything longer, so a very long
    recipe still shrinks to a floor rather than becoming unreadable. Pure."""
    n = content_char_count(blocks)
    if n <= _FIT_FULL_CHARS:
        return 1.0
    over = (n - _FIT_FULL_CHARS) / _FIT_FULL_CHARS
    scale = 1.0 - min(1.0, over) * (1.0 - _FIT_MIN_SCALE)
    return round(max(_FIT_MIN_SCALE, scale), 3)


# The recipe's own header rows (a title and, when present, the quick-facts
# line under it). A hairline rule closes this block off from the ingredients
# and steps below.
_HEADER_STYLES = ("title", "quickfacts")
_RULE_GAP_PT = 6


# -- Brand treatment (FoodAssistant-tj4e) ------------------------------------
#
# The page renders in RGB so the Pantry Raider pink can carry the brand: the
# raccoon mark, the "Pantry Raider" wordmark, and the header rule. Everything
# the reader must actually READ (the recipe title, ingredients, and steps)
# stays near-black on white, so the page is just as legible on a monochrome
# printer, where the pink simply falls to a neutral gray. The pink is never
# load-bearing for legibility, only decoration.

_PAGE_BG = (255, 255, 255)
_INK = (17, 17, 17)           # near-black body / title / heading / step text
_BRAND_PINK = (242, 0, 110)   # #F2006E: logo tint, wordmark, header rule
_RULE_GRAY = (176, 176, 176)  # subtle rule under the recipe's title block

_BRAND_WORDMARK = "Pantry Raider"

# Header sizes in points, scaled to the page dpi and the one-page-fit scale at
# render time (like the body styles). The logo is a square target the mark is
# fit into; the wordmark sits to its right, vertically centered against it.
_BRAND_LOGO_PT = 34
_BRAND_WORDMARK_PT = 24
_BRAND_GUTTER_PT = 10          # gap between the mark and the wordmark
_BRAND_RULE_GAP_PT = 8         # breathing room above and below the pink rule
_BRAND_RULE_PT = 2             # pink header-rule thickness

# Pages after the first carry a slim running header (wordmark + hairline)
# instead of the full logo band, so a multi-page printout still reads as one
# document without repeating the mark on every sheet.
_RUNNING_WORDMARK_PT = 11

# The color brand mark for the printed page. Unlike the thermal-label glyph
# (services/label_render._load_logo_glyph, thresholded to 1-bit black), a full
# page can print the mark in its actual pink, so this loads the PNG in color.
# logo-mark.png is the square mark; logo.png is a larger fallback.
_ICONS_DIR = Path(__file__).resolve().parent.parent / "static" / "icons"
_LOGO_ASSETS = (_ICONS_DIR / "logo-mark.png", _ICONS_DIR / "logo.png")


def _load_logo_image(side_px: int):
    """The Pantry Raider mark as an RGBA image fit to a ``side_px`` square, or
    None if no asset can be loaded.

    Loads the color PNG (the square mark first, the full logo as a fallback)
    and scales it to fit the square while keeping its aspect ratio. Never
    raises: a missing or unreadable asset just means the header falls back to
    the wordmark alone, so a document always still prints."""
    if side_px < 1:
        return None
    for asset in _LOGO_ASSETS:
        try:
            raw = Image.open(asset).convert("RGBA")
        except (OSError, FileNotFoundError):
            continue
        w, h = raw.size
        if w < 1 or h < 1:
            continue
        fit = min(side_px / w, side_px / h)
        new_w = max(1, int(round(w * fit)))
        new_h = max(1, int(round(h * fit)))
        return raw.resize((new_w, new_h), Image.LANCZOS)
    return None


def _draw_brand_header(img: "Image.Image", draw: "ImageDraw.ImageDraw",
                       margin_px: int, inner_w: int, line_px, show_logo: bool) -> int:
    """Draw the Pantry Raider header band at the top of the first page and
    return the y where the recipe content should start.

    The band is the color raccoon mark (when the asset loads and ``show_logo``
    is on) followed by the "Pantry Raider" wordmark in the brand pink, closed
    off by a thin pink rule. If the logo asset is missing or turned off, the
    wordmark alone still brands the page. The pink carries nothing the reader
    must read, so a monochrome print loses no information."""
    top = margin_px
    logo = _load_logo_image(line_px(_BRAND_LOGO_PT)) if show_logo else None
    logo_side = line_px(_BRAND_LOGO_PT)

    x = margin_px
    if logo is not None:
        ly = top + max(0, (logo_side - logo.height) // 2)
        img.paste(logo, (x, ly), logo)
        x += logo.width + line_px(_BRAND_GUTTER_PT)

    wordmark_font = _load_font(line_px(_BRAND_WORDMARK_PT), bold=True)
    wm_h = _text_size(draw, _BRAND_WORDMARK, wordmark_font)[1]
    band_h = max(logo_side if logo is not None else 0, wm_h)
    wy = top + max(0, (band_h - wm_h) // 2)
    draw.text((x, wy), _BRAND_WORDMARK, fill=_BRAND_PINK, font=wordmark_font)

    rule_y = top + band_h + line_px(_BRAND_RULE_GAP_PT)
    rule_w = max(2, line_px(_BRAND_RULE_PT))
    draw.line([(margin_px, rule_y), (margin_px + inner_w, rule_y)],
              fill=_BRAND_PINK, width=rule_w)
    return rule_y + rule_w + line_px(_BRAND_RULE_GAP_PT)


def _draw_running_header(draw: "ImageDraw.ImageDraw", margin_px: int,
                         inner_w: int, line_px) -> int:
    """A slim brand strip for pages after the first: the wordmark small in the
    brand pink over a hairline gray rule. Returns the y where content resumes."""
    top = margin_px
    font = _load_font(line_px(_RUNNING_WORDMARK_PT), bold=True)
    wm_h = _text_size(draw, _BRAND_WORDMARK, font)[1]
    draw.text((margin_px, top), _BRAND_WORDMARK, fill=_BRAND_PINK, font=font)
    rule_y = top + wm_h + line_px(4)
    draw.line([(margin_px, rule_y), (margin_px + inner_w, rule_y)],
              fill=_RULE_GRAY, width=1)
    return rule_y + line_px(6)


def render_document_pdf_bytes(
    blocks: list[Block],
    *,
    width_in: float = LETTER_WIDTH_IN,
    height_in: float = LETTER_HEIGHT_IN,
    dpi: int = DOC_DPI,
    margin_in: float = MARGIN_IN,
    show_logo: bool = True,
) -> bytes:
    """Lay styled text blocks onto one or more letter pages and return a PDF.

    The first page opens with the Pantry Raider brand header (the pink raccoon
    mark and wordmark, then a thin pink rule); pages after it carry a slim
    running header. Below the header the recipe flows: text wraps to the page
    width and spills onto a new page at the bottom margin, so a long recipe
    spans as many pages as it needs. Each page is set to its physical size so a
    printer driver lays it out true to size.

    Font sizes and spacing scale down for longer content (``fit_scale``), so a
    typical recipe lands on one page. A leading run of "title"/"quickfacts"
    blocks is the recipe's own header and gets a subtle rule under it before the
    ingredients begin. A "columns" block (a long ingredients list) lays its
    items out as two columns instead of one. The page renders in RGB so the
    brand pink can print in color, but all recipe text stays near-black on
    white, so it reads just as clearly on a monochrome printer.

    ``show_logo`` gates only the raccoon image; the wordmark always brands the
    header, so turning the logo off (or a missing asset) still prints a clean,
    branded page."""
    width_px = max(1, round(width_in * dpi))
    height_px = max(1, round(height_in * dpi))
    margin_px = max(0, round(margin_in * dpi))
    inner_w = max(1, width_px - 2 * margin_px)
    bottom = height_px - margin_px

    scale = fit_scale(blocks)

    def _line_px(pt: float) -> int:
        return max(1, round(pt * scale * dpi / 72))

    def _new_page(first: bool):
        page = Image.new("RGB", (width_px, height_px), _PAGE_BG)
        page_draw = ImageDraw.Draw(page)
        if first:
            start_y = _draw_brand_header(page, page_draw, margin_px, inner_w,
                                         _line_px, show_logo)
        else:
            start_y = _draw_running_header(page_draw, margin_px, inner_w, _line_px)
        return page, page_draw, start_y

    pages: list[Image.Image] = []
    img, draw, y = _new_page(first=True)

    header_open = bool(blocks) and blocks[0].style in _HEADER_STYLES
    for i, block in enumerate(blocks):
        px = _line_px(_STYLE_PT.get(block.style, 11))
        bold = _STYLE_BOLD.get(block.style, False)
        space_before = _line_px(_STYLE_SPACE_BEFORE.get(block.style, 0))
        font = _load_font(px, bold=bold)

        if block.style == "columns":
            y = _draw_columns(draw, block.items, font, px, margin_px, inner_w, y)
        elif not block.text:
            # A blank body block is an intentional spacer.
            y += px
        else:
            wrapped = _wrap_lines(draw, block.text, font, inner_w, max_lines=100)
            y += space_before
            for line in wrapped:
                line_h = _text_size(draw, "Ay", font)[1]
                if y + line_h > bottom:
                    pages.append(img)
                    img, draw, y = _new_page(first=False)
                draw.text((margin_px, y), line, fill=_INK, font=font)
                y += line_h + max(1, int(px * 0.25))

        # Close the recipe header once the next block is not a header row, and
        # drop a subtle rule under it before the ingredients start.
        next_style = blocks[i + 1].style if i + 1 < len(blocks) else None
        is_header_row = block.style in _HEADER_STYLES
        if header_open and is_header_row and next_style not in _HEADER_STYLES:
            header_open = False
            y += _line_px(_RULE_GAP_PT)
            draw.line([(margin_px, y), (margin_px + inner_w, y)],
                      fill=_RULE_GRAY, width=1)
            y += _line_px(_RULE_GAP_PT)

    pages.append(img)

    buf = io.BytesIO()
    resolution = pages[0].width / max(0.01, width_in)
    first, rest = pages[0], pages[1:]
    first.save(buf, format="PDF", save_all=True, append_images=rest,
               resolution=resolution)
    return buf.getvalue()


def _draw_columns(draw, items: list[str], font, px: int, margin_px: int,
                  inner_w: int, y: int) -> int:
    """Draw ``items`` (bullet lines) as two side-by-side columns starting at
    ``y``, and return the y position below the taller column.

    Each column flows independently (an item that wraps to two lines in the
    left column does not force a matching gap in the right one), which keeps
    the layout simple for what is, in practice, always a short ingredient
    phrase. Runs off the bottom margin are simply not drawn: by the time a
    list is long enough to need columns, ``fit_scale`` has already shrunk the
    font to make that vanishingly rare in ordinary use."""
    items = [it for it in items if it]
    if not items:
        return y
    mid = math.ceil(len(items) / 2)
    gutter = max(1, int(inner_w * 0.04))
    col_w = max(1, (inner_w - gutter) // 2)
    line_h = _text_size(draw, "Ay", font)[1]
    gap = max(1, int(px * 0.25))

    bottoms = []
    for col_index, col_items in enumerate((items[:mid], items[mid:])):
        cx = margin_px + col_index * (col_w + gutter)
        cy = y
        for item in col_items:
            for line in _wrap_lines(draw, item, font, col_w, max_lines=4):
                draw.text((cx, cy), line, fill=_INK, font=font)
                cy += line_h + gap
        bottoms.append(cy)
    return max(bottoms) if bottoms else y


def render_recipe_pdf_bytes(recipe: dict, **kwargs) -> bytes:
    """Convenience: a recipe dict straight to a printable PDF."""
    return render_document_pdf_bytes(recipe_to_blocks(recipe), **kwargs)
