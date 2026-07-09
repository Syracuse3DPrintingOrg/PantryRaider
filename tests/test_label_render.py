"""Label render engine: sizing, fit, badges, decorative mode, batch PDF
(FoodAssistant-23v6). Pure image composition, no printer or network."""
from __future__ import annotations

import re
import sys
from pathlib import Path

from PIL import Image

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import label_render as lr  # noqa: E402


def test_image_dimensions_match_spec():
    spec = lr.LabelSpec(name="Milk", width_in=2.0, height_in=1.0, dpi=203)
    img = lr.render_label(spec)
    assert img.size == (round(2.0 * 203), round(1.0 * 203))
    assert img.mode == "L"


def test_custom_size_scales_pixels():
    spec = lr.LabelSpec(name="Big", width_in=4.0, height_in=3.0, dpi=300)
    img = lr.render_label(spec)
    assert img.size == (1200, 900)


def test_long_name_does_not_overflow_width():
    spec = lr.LabelSpec(
        name="Extra Sharp Aged Vermont Cheddar Cheese Wedge Reserve Batch",
        width_in=2.0, height_in=1.0, dpi=203,
    )
    img = lr.render_label(spec)
    # No black pixel should touch the far edges: the name is wrapped/ellipsized
    # and the whole layout stays inside the margins.
    px = img.load()
    w, h = img.size
    for y in range(h):
        assert px[w - 1, y] == 255  # right edge column stays white
    for x in range(w):
        assert px[x, 0] == 255 and px[x, h - 1] == 255  # top and bottom rows white


def test_source_badge_copy_maps_correctly():
    assert lr.source_badge("manual") == ""
    assert lr.source_badge("default") == "est."
    assert lr.source_badge("llm") == "AI"
    assert lr.source_badge("something-unknown") == ""


def test_wrap_ellipsizes_overflow():
    img = Image.new("L", (200, 60), 255)
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    font = lr._load_font(28, bold=True)
    lines = lr._wrap_lines(
        draw, "supercalifragilisticexpialidocious antidisestablishmentarianism words",
        font, max_width=150, max_lines=1,
    )
    assert len(lines) == 1
    assert lines[0].endswith("...")


def test_decorative_omits_dates():
    # A decorative label draws only its text: rendering with vs without dates in
    # a normal label differs, but the decorative variant takes no dates at all.
    deco = lr.render_decorative_label("Cinnamon", width_in=2.0, height_in=1.0, dpi=203)
    assert deco.mode == "L"
    assert deco.size == (406, 203)
    # Sanity: it drew something (has black pixels) but no date logic is invoked.
    assert deco.getextrema()[0] == 0  # at least one black pixel


def test_png_and_pdf_bytes():
    img = lr.render_label(lr.LabelSpec(name="Eggs", added="2026-07-08",
                                       best_by="2026-07-22", best_by_source="default"))
    png = lr.render_to_png_bytes(img)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pdf = lr.render_to_pdf_bytes(img, label_size=(2.0, 1.0))
    assert pdf[:5] == b"%PDF-"


def test_batch_pdf_page_count_matches_labels():
    specs = [
        lr.LabelSpec(name=f"Item {i}", added="2026-07-08", best_by="2026-08-01",
                     best_by_source="llm")
        for i in range(5)
    ]
    pdf = lr.render_batch_pdf_bytes(specs)
    assert pdf[:5] == b"%PDF-"
    # Pillow can write but not read PDFs, so count pages from the PDF itself:
    # one MediaBox per page, and the page-tree /Count reflects the total.
    assert pdf.count(b"/MediaBox") == 5
    assert re.search(rb"/Count (\d+)", pdf).group(1) == b"5"


def test_best_by_badge_variants_all_render():
    for source in ("manual", "default", "llm"):
        img = lr.render_label(lr.LabelSpec(
            name="Yogurt", added="2026-07-08", best_by="2026-07-20",
            best_by_source=source, extra="Fridge, 2 cups",
        ))
        assert img.size == (406, 203)
        assert img.getextrema()[0] == 0  # drew text


def test_prettify_date_iso_and_passthrough():
    assert lr.prettify_date("2026-07-23") == "Jul 23, 2026"
    assert lr.prettify_date("2026-01-05") == "Jan 5, 2026"
    # Non-ISO or already-formatted values pass through untouched.
    assert lr.prettify_date("next week") == "next week"
    assert lr.prettify_date("") == ""
    assert lr.prettify_date("2026-13-40") == "2026-13-40"  # invalid month/day
