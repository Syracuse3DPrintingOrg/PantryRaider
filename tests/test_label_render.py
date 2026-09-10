"""Label render engine: sizing, fit, badges, decorative mode, batch PDF
(FoodAssistant-23v6). Pure image composition, no printer or network."""
from __future__ import annotations

import json
import math
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


# -- Layout engine (FoodAssistant-bwl1 / -or5e) -----------------------------


def _edges_clear(img):
    """Assert the outer edge rows/columns are all white (label edges blank)."""
    px = img.load()
    w, h = img.size
    for y in range(h):
        assert px[0, y] == 255 and px[w - 1, y] == 255
    for x in range(w):
        assert px[x, 0] == 255 and px[x, h - 1] == 255


def _has_ink(img, x0, x1):
    px = img.load()
    _w, h = img.size
    for x in range(x0, x1):
        for y in range(h):
            if px[x, y] == 0:
                return True
    return False


def test_element_round_trip():
    el = lr.LabelElement(field="name", x=0.1, y=0.2, w=0.8, h=0.3,
                         align="right", bold=True, size_scale=0.5,
                         text="hi", uppercase=True)
    again = lr.LabelElement.from_dict(el.to_dict())
    assert again == el


def test_element_from_dict_clamps_and_drops():
    # Out-of-range fractions clamp to [0, 1].
    el = lr.LabelElement.from_dict({"field": "name", "x": 5, "y": -3, "w": 0.5, "h": 2})
    assert el.x == 1.0 and el.y == 0.0 and el.w == 0.5 and el.h == 1.0
    # Unknown field, non-dict, and bad align all handled.
    assert lr.LabelElement.from_dict({"field": "bogus"}) is None
    assert lr.LabelElement.from_dict("not a dict") is None
    assert lr.LabelElement.from_dict({"field": "name", "align": "sideways"}).align == "left"
    # A non-numeric or non-positive size_scale falls back to 1.0.
    assert lr.LabelElement.from_dict({"field": "name", "size_scale": "big"}).size_scale == 1.0
    assert lr.LabelElement.from_dict({"field": "name", "size_scale": 0}).size_scale == 1.0


def test_layout_round_trip_and_malformed_elements():
    layout = lr.LabelLayout(width_in=3.0, height_in=2.0, dpi=300, margin_in=0.1,
                            elements=[lr.LabelElement(field="name", w=1.0, h=0.5)])
    again = lr.LabelLayout.from_dict(layout.to_dict())
    assert again.width_in == 3.0 and again.height_in == 2.0
    assert again.dpi == 300 and again.margin_in == 0.1
    assert len(again.elements) == 1 and again.elements[0].field == "name"
    # Malformed elements are dropped, not raised.
    dirty = lr.LabelLayout.from_dict({
        "width_in": 2.0, "height_in": 1.0, "dpi": 203,
        "elements": [
            {"field": "name", "x": 0, "y": 0, "w": 1, "h": 0.4},
            {"field": "unknown"},
            "garbage",
            {"field": "best_by_date", "x": 0, "y": 0.5, "w": 1, "h": 0.4},
        ],
    })
    assert [e.field for e in dirty.elements] == ["name", "best_by_date"]
    # Garbage sizes fall back to defaults rather than raising.
    fallback = lr.LabelLayout.from_dict({"width_in": "wide", "dpi": "many"})
    assert fallback.width_in == lr.DEFAULT_WIDTH_IN and fallback.dpi == lr.DEFAULT_DPI


def test_render_layout_size_and_edges():
    layout = lr.LabelLayout(width_in=2.0, height_in=1.0, dpi=203, elements=[
        lr.LabelElement(field="name", x=0, y=0, w=1, h=0.4, bold=True),
        lr.LabelElement(field="best_by_date", x=0, y=0.5, w=1, h=0.4),
    ])
    img = lr.render_layout(layout, {"name": "Chicken Stock", "best_by": "2026-07-22"})
    assert img.mode == "L"
    assert img.size == (round(2.0 * 203), round(1.0 * 203))
    assert img.getextrema()[0] == 0  # drew ink
    _edges_clear(img)


def test_render_layout_alignment_places_ink():
    def side_ink(align):
        layout = lr.LabelLayout(width_in=2.0, height_in=1.0, dpi=203, elements=[
            lr.LabelElement(field="name", x=0, y=0.3, w=1.0, h=0.4,
                            align=align, bold=True)])
        img = lr.render_layout(layout, {"name": "Hi"})
        w, _h = img.size
        return _has_ink(img, 0, w // 3), _has_ink(img, 2 * w // 3, w)

    left_l, left_r = side_ink("left")
    assert left_l and not left_r
    right_l, right_r = side_ink("right")
    assert right_r and not right_l
    cl, cr = side_ink("center")
    assert not cl and not cr  # centred short text sits in the middle band


def test_render_layout_empty_value_draws_nothing():
    layout = lr.LabelLayout(elements=[lr.LabelElement(field="name")])
    img = lr.render_layout(layout, {})  # no name given
    assert img.getextrema() == (255, 255)  # all white, no crash


def test_render_layout_badge_and_qr_render():
    # The badge element draws the filled source chip for an estimated date.
    badge_layout = lr.LabelLayout(elements=[
        lr.LabelElement(field="best_by_badge", x=0.5, y=0.1, w=0.5, h=0.2)])
    img = lr.render_layout(badge_layout, {"best_by_source": "llm"})
    assert img.getextrema()[0] == 0
    # A manual date has no badge, so nothing is drawn.
    blank = lr.render_layout(badge_layout, {"best_by_source": "manual"})
    assert blank.getextrema() == (255, 255)
    # QR encodes text and draws ink; edges stay clear.
    qr_layout = lr.LabelLayout(width_in=2.0, height_in=1.0, dpi=203, elements=[
        lr.LabelElement(field="qr", text="https://example.com", x=0, y=0,
                        w=0.5, h=1.0, align="left")])
    qr = lr.render_layout(qr_layout, {})
    assert qr.getextrema()[0] == 0
    _edges_clear(qr)


def test_render_layout_barcode_degrades_to_text():
    # No barcode library is bundled, so the barcode field prints its digits as
    # text instead of crashing or drawing nothing.
    layout = lr.LabelLayout(width_in=2.0, height_in=1.0, dpi=203, elements=[
        lr.LabelElement(field="barcode", text="012345678905", x=0, y=0.3,
                        w=1.0, h=0.4)])
    img = lr.render_layout(layout, {})
    assert img.getextrema()[0] == 0  # drew the digits


def test_default_food_layout_renders_within_margins():
    layout = lr.default_food_layout(2.0, 1.0, 203)
    img = lr.render_layout(layout, {
        "name": "Eggs", "added": "2026-07-08",
        "best_by": "2026-07-22", "best_by_source": "default"})
    assert img.size == (406, 203)
    assert img.getextrema()[0] == 0
    _edges_clear(img)


def test_presets_lookup_and_summary():
    assert lr.preset_by_key("3x2")["width_in"] == 3.0
    assert lr.preset_by_key("3x2")["height_in"] == 2.0
    assert lr.preset_by_key("spice_square")["width_in"] == 1.5
    assert lr.preset_by_key("nope") is None
    summary = lr.presets_summary()
    keys = {p["key"] for p in summary}
    assert {"2x1", "1x2_address", "2.25x1.25", "3x2", "4x6_shipping",
            "spice_square"} <= keys
    for entry in summary:
        assert set(entry.keys()) == {"key", "name", "width_in", "height_in"}


def test_mm_to_in():
    assert lr.mm_to_in(25.4) == 1.0
    assert lr.mm_to_in(0) == 0.0
    assert abs(lr.mm_to_in(40) - 1.575) < 0.001


def test_metric_presets_present_and_labeled_in_mm():
    keys = {"40x30mm", "50x30mm", "40x60mm", "62x29mm"}
    assert keys <= {p["key"] for p in lr.LABEL_PRESETS}
    for key, (w_mm, h_mm) in {
        "40x30mm": (40, 30), "50x30mm": (50, 30),
        "40x60mm": (40, 60), "62x29mm": (62, 29),
    }.items():
        preset = lr.preset_by_key(key)
        assert preset is not None
        assert "mm" in preset["name"]
        assert preset["width_in"] == lr.mm_to_in(w_mm)
        assert preset["height_in"] == lr.mm_to_in(h_mm)
    # Existing inch presets are untouched by the addition.
    assert lr.preset_by_key("3x2")["width_in"] == 3.0


# -- Brand mark (FoodAssistant-yglw) -----------------------------------------


def test_logo_size_px_scales_off_smaller_dimension():
    # A wide label: the logo is capped by the (smaller) height.
    assert lr.logo_size_px(400, 200) == int(200 * lr.LOGO_FRACTION)
    # A tall label: capped by the (smaller) width.
    assert lr.logo_size_px(150, 600) == int(150 * lr.LOGO_FRACTION)
    # Square.
    assert lr.logo_size_px(100, 100) == int(100 * lr.LOGO_FRACTION)


def test_logo_size_px_never_negative():
    assert lr.logo_size_px(0, 0) == 0
    assert lr.logo_size_px(-5, 40) == 0


def test_logo_asset_exists_and_loads():
    assert lr.LOGO_ASSET.exists()
    glyph = lr._load_logo_glyph(32)
    assert glyph is not None
    ink, alpha = glyph
    assert ink.size == (32, 32)
    assert alpha.size == (32, 32)
    # Thresholded to pure black/white ink, no gray.
    assert set(ink.getdata()) <= {0, 255}


def test_load_logo_glyph_missing_asset_returns_none(monkeypatch):
    monkeypatch.setattr(lr, "LOGO_ASSET", Path("/nonexistent/logo.png"))
    assert lr._load_logo_glyph(32) is None


def test_load_logo_glyph_zero_size_returns_none():
    assert lr._load_logo_glyph(0) is None


def test_render_label_show_logo_off_by_default_matches_without_flag():
    spec = lr.LabelSpec(name="Milk", best_by="2026-07-15")
    assert spec.show_logo is False
    img_default = lr.render_label(spec)
    img_explicit_off = lr.render_label(lr.LabelSpec(
        name="Milk", best_by="2026-07-15", show_logo=False))
    assert list(img_default.getdata()) == list(img_explicit_off.getdata())


def test_render_label_show_logo_changes_pixels():
    base = lr.render_label(lr.LabelSpec(name="Milk", best_by="2026-07-15"))
    with_logo = lr.render_label(lr.LabelSpec(
        name="Milk", best_by="2026-07-15", show_logo=True))
    assert list(base.getdata()) != list(with_logo.getdata())


def test_render_layout_show_logo_via_values():
    layout = lr.default_food_layout()
    values = {"name": "Milk", "best_by": "2026-07-15", "best_by_source": "manual"}
    base = lr.render_layout(layout, values)
    with_logo = lr.render_layout(layout, {**values, "show_logo": True})
    assert list(base.getdata()) != list(with_logo.getdata())


def test_draw_logo_too_small_area_draws_nothing():
    img = Image.new("L", (10, 10), 255)
    before = list(img.getdata())
    lr.draw_logo(img, margin_px=4)  # inner area 2x2, well under the min side
    assert list(img.getdata()) == before


# -- Outline / box border (FoodAssistant-rhqa) -------------------------------


def test_element_outline_round_trips():
    el = lr.LabelElement(field="static", text="Hi", outline=True)
    d = el.to_dict()
    assert d["outline"] is True
    back = lr.LabelElement.from_dict(d)
    assert back.outline is True
    # Defaults to False when absent, and coerces a truthy JSON value.
    assert lr.LabelElement.from_dict({"field": "static"}).outline is False
    assert lr.LabelElement.from_dict({"field": "static", "outline": 1}).outline is True


def test_render_layout_outline_draws_a_border():
    # An element with outline draws a frame even with no text content: this
    # isolates the border from any text ink so the assertion is unambiguous.
    layout = lr.LabelLayout(width_in=2.0, height_in=1.0, dpi=203, elements=[
        lr.LabelElement(field="static", text="", x=0.1, y=0.1, w=0.5, h=0.5,
                        outline=True)])
    img = lr.render_layout(layout, {})
    assert img.getextrema()[0] == 0  # the border drew ink
    # Without outline, the same empty static element draws nothing at all.
    plain = lr.LabelLayout(width_in=2.0, height_in=1.0, dpi=203, elements=[
        lr.LabelElement(field="static", text="", x=0.1, y=0.1, w=0.5, h=0.5,
                        outline=False)])
    assert lr.render_layout(plain, {}).getextrema() == (255, 255)


def test_render_layout_outline_combines_with_text():
    layout = lr.LabelLayout(width_in=2.0, height_in=1.0, dpi=203, elements=[
        lr.LabelElement(field="name", x=0.1, y=0.1, w=0.6, h=0.6,
                        align="left", outline=True)])
    img = lr.render_layout(layout, {"name": "Eggs"})
    assert img.getextrema()[0] == 0
    _edges_clear(img)


# -- Saved layout presets (FoodAssistant-rhqa) -------------------------------


def _sample_layout_dict():
    return lr.default_food_layout(2.0, 1.0, 203).to_dict()


def test_validate_layout_presets_round_trips_a_clean_list():
    presets = [{"name": "Fridge", "layout": _sample_layout_dict()},
               {"name": "Pantry", "layout": _sample_layout_dict()}]
    out = lr.validate_layout_presets(presets)
    assert [p["name"] for p in out] == ["Fridge", "Pantry"]
    assert out[0]["layout"]["elements"]  # a real layout survived


def test_validate_layout_presets_accepts_json_string():
    raw = json.dumps([{"name": "Fridge", "layout": _sample_layout_dict()}])
    out = lr.validate_layout_presets(raw)
    assert len(out) == 1 and out[0]["name"] == "Fridge"
    assert lr.validate_layout_presets("") == []
    assert lr.validate_layout_presets("not json") == []
    assert lr.validate_layout_presets("   ") == []


def test_validate_layout_presets_drops_malformed_entries():
    presets = [
        {"name": "", "layout": _sample_layout_dict()},   # no name
        {"name": "NoLayout"},                             # no layout
        {"name": "BadLayout", "layout": {"elements": []}},  # no usable elements
        "not a dict",
        {"name": "Good", "layout": _sample_layout_dict()},
    ]
    out = lr.validate_layout_presets(presets)
    assert [p["name"] for p in out] == ["Good"]


def test_validate_layout_presets_first_name_wins_on_duplicate():
    a = lr.default_food_layout(2.0, 1.0, 203).to_dict()
    b = lr.default_food_layout(3.0, 2.0, 203).to_dict()
    presets = [{"name": "Same", "layout": a}, {"name": "Same", "layout": b}]
    out = lr.validate_layout_presets(presets)
    assert len(out) == 1
    assert out[0]["layout"]["width_in"] == 2.0  # the first entry, not the second


def test_validate_layout_presets_not_a_list_returns_empty():
    assert lr.validate_layout_presets({"name": "x"}) == []
    assert lr.validate_layout_presets(None) == []
    assert lr.validate_layout_presets(42) == []


def test_layout_presets_json_helpers_round_trip():
    presets = [{"name": "Fridge", "layout": _sample_layout_dict()}]
    raw = lr.layout_presets_to_json(presets)
    assert isinstance(raw, str)
    back = lr.layout_presets_from_json(raw)
    assert back == lr.validate_layout_presets(presets)
    # An empty/garbage stored value degrades to an empty list, not a crash.
    assert lr.layout_presets_from_json("") == []
    assert lr.layout_presets_from_json("{garbage") == []


# -- Icon/symbol glyphs (FoodAssistant-nxr8) ---------------------------------


def test_icon_glyph_known_and_unknown_keys():
    assert lr.icon_glyph("star") == lr.ICON_GLYPHS["star"]
    assert lr.icon_glyph("not-a-real-icon") == ""
    assert lr.icon_glyph("") == ""
    assert lr.icon_glyph(None) == ""


def test_render_layout_icon_field_draws_glyph():
    layout = lr.LabelLayout(width_in=2.0, height_in=1.0, dpi=203, elements=[
        lr.LabelElement(field="icon", text="star", x=0.2, y=0.2, w=0.4, h=0.4,
                        align="center")])
    img = lr.render_layout(layout, {})
    assert img.getextrema()[0] == 0
    # An unknown icon key draws nothing (no crash, no stray ink).
    blank_layout = lr.LabelLayout(width_in=2.0, height_in=1.0, dpi=203, elements=[
        lr.LabelElement(field="icon", text="bogus", x=0.2, y=0.2, w=0.4, h=0.4)])
    assert lr.render_layout(blank_layout, {}).getextrema() == (255, 255)


def test_icon_element_round_trips_through_dict():
    el = lr.LabelElement(field="icon", text="snowflake")
    back = lr.LabelElement.from_dict(el.to_dict())
    assert back.field == "icon"
    assert back.text == "snowflake"


# -- QR payload builder (FoodAssistant-nxr8) ---------------------------------


def test_build_qr_payload_url_and_text_passthrough():
    assert lr.build_qr_payload("url", url="https://example.com") == "https://example.com"
    assert lr.build_qr_payload("url", text="fallback") == "fallback"
    assert lr.build_qr_payload("text", text="  plain text  ") == "plain text"
    # An unrecognized kind falls back to "text" behaviour.
    assert lr.build_qr_payload("bogus", text="x") == "x"


def test_build_qr_payload_vcard_builds_minimal_record():
    payload = lr.build_qr_payload("vcard", vcard_name="Dan", vcard_org="Pantry Raider",
                                  vcard_note="Return to the kitchen")
    assert payload.startswith("BEGIN:VCARD")
    assert payload.endswith("END:VCARD")
    assert "FN:Dan" in payload
    assert "ORG:Pantry Raider" in payload
    assert "NOTE:Return to the kitchen" in payload


def test_build_qr_payload_vcard_omits_blank_optional_lines():
    payload = lr.build_qr_payload("vcard", vcard_name="Dan")
    assert "ORG:" not in payload
    assert "NOTE:" not in payload


def test_build_qr_payload_vcard_falls_back_to_text_without_a_name():
    # vcard_name blank, text also blank: nothing usable to encode.
    assert lr.build_qr_payload("vcard") == ""
    # vcard_name blank but text given: text is used as the vcard name.
    payload = lr.build_qr_payload("vcard", text="Dan")
    assert "FN:Dan" in payload


def test_render_layout_qr_kind_affects_encoded_payload():
    # Same box, two qr_kind values with different content: both draw ink and
    # stay within the printable area (a proxy for "it actually encoded
    # something", since the pixels of a QR blob are opaque to compare directly).
    plain = lr.LabelLayout(width_in=2.0, height_in=1.0, dpi=203, elements=[
        lr.LabelElement(field="qr", text="https://example.com", qr_kind="url",
                        x=0, y=0, w=0.5, h=1.0)])
    vcard = lr.LabelLayout(width_in=2.0, height_in=1.0, dpi=203, elements=[
        lr.LabelElement(field="qr", text="Dan", qr_kind="vcard", qr_extra="note",
                        x=0, y=0, w=0.5, h=1.0)])
    img_plain = lr.render_layout(plain, {})
    img_vcard = lr.render_layout(vcard, {})
    assert img_plain.getextrema()[0] == 0
    assert img_vcard.getextrema()[0] == 0
    _edges_clear(img_plain)
    _edges_clear(img_vcard)


def test_qr_element_qr_kind_round_trips_and_defaults():
    el = lr.LabelElement(field="qr", text="Dan", qr_kind="vcard", qr_extra="hi")
    back = lr.LabelElement.from_dict(el.to_dict())
    assert back.qr_kind == "vcard"
    assert back.qr_extra == "hi"
    # An unrecognized qr_kind in stored JSON falls back to "url" rather than
    # raising or carrying through garbage.
    bogus = lr.LabelElement.from_dict({"field": "qr", "qr_kind": "not-a-kind"})
    assert bogus.qr_kind == "url"


# -- Decorative label layout touches (FoodAssistant-nxr8) --------------------


def test_decorative_outline_adds_a_border():
    plain = lr.render_decorative_label("Salt", width_in=2.0, height_in=1.0, dpi=203)
    outlined = lr.render_decorative_label("Salt", width_in=2.0, height_in=1.0,
                                          dpi=203, outline=True)
    assert list(plain.getdata()) != list(outlined.getdata())
    assert outlined.getextrema()[0] == 0


def test_decorative_icon_adds_a_glyph_and_shifts_text():
    plain = lr.render_decorative_label("Salt", width_in=2.0, height_in=1.0, dpi=203)
    with_icon = lr.render_decorative_label("Salt", width_in=2.0, height_in=1.0,
                                           dpi=203, icon="star")
    assert list(plain.getdata()) != list(with_icon.getdata())
    assert with_icon.getextrema()[0] == 0


def test_decorative_unknown_icon_key_behaves_like_no_icon():
    plain = lr.render_decorative_label("Salt", width_in=2.0, height_in=1.0, dpi=203)
    bogus_icon = lr.render_decorative_label("Salt", width_in=2.0, height_in=1.0,
                                            dpi=203, icon="not-a-real-icon")
    assert list(plain.getdata()) == list(bogus_icon.getdata())


# -- Grocy stock-entry code on labels (FoodAssistant-28f3) --------------------


def test_build_grocycode_entry_and_product_forms():
    assert lr.build_grocycode(12, "6a28c889c1193") == "grcy:p:12:6a28c889c1193"
    assert lr.build_grocycode("12", " 6a28c889c1193 ") == "grcy:p:12:6a28c889c1193"
    # Product-only form when no entry id is known.
    assert lr.build_grocycode(12) == "grcy:p:12"
    assert lr.build_grocycode(12, "") == "grcy:p:12"


def test_build_grocycode_junk_product_id_yields_empty():
    assert lr.build_grocycode(None) == ""
    assert lr.build_grocycode("nope") == ""
    assert lr.build_grocycode(0) == ""
    assert lr.build_grocycode(-3, "abc") == ""


def test_grocycode_qr_side_scales_off_smaller_dimension():
    # Wide label: capped by the (smaller) height, like the brand mark.
    assert lr.grocycode_qr_side(400, 200) == int(200 * lr.GROCYCODE_FRACTION)
    # Tall label: capped by the width.
    assert lr.grocycode_qr_side(150, 600) == int(150 * lr.GROCYCODE_FRACTION)


def test_grocycode_qr_side_zero_when_unscannably_small():
    # Below the scannable minimum the label prints without the code entirely.
    tiny = int((lr.GROCYCODE_MIN_SIDE - 1) / lr.GROCYCODE_FRACTION)
    assert lr.grocycode_qr_side(tiny, tiny) == 0
    assert lr.grocycode_qr_side(0, 0) == 0
    assert lr.grocycode_qr_side(-5, 40) == 0


def test_render_label_without_grocycode_renders_exactly_as_before():
    # The pin: a spec that never mentions grocycode and one that explicitly
    # passes the empty default are pixel-identical, so every existing label
    # call renders exactly as it did before the field existed.
    spec = lr.LabelSpec(name="Milk", added="2026-07-08", best_by="2026-07-15",
                        best_by_source="default", extra="Fridge")
    assert spec.grocycode == ""
    explicit = lr.LabelSpec(name="Milk", added="2026-07-08", best_by="2026-07-15",
                            best_by_source="default", extra="Fridge", grocycode="")
    assert list(lr.render_label(spec).getdata()) == list(lr.render_label(explicit).getdata())


def test_render_label_grocycode_draws_code_in_bottom_right():
    plain = lr.LabelSpec(name="Stew", best_by="2026-07-22", added="2026-07-08")
    coded = lr.LabelSpec(name="Stew", best_by="2026-07-22", added="2026-07-08",
                         grocycode="grcy:p:12:6a28c889c1193")
    img_plain = lr.render_label(plain)
    img_coded = lr.render_label(coded)
    assert list(img_plain.getdata()) != list(img_coded.getdata())
    _edges_clear(img_coded)

    # The QR square occupies the bottom-right corner, inside the margins.
    m = coded.margin_px
    inner_w = coded.width_px - 2 * m
    inner_h = coded.height_px - 2 * m
    side = lr.grocycode_qr_side(inner_w, inner_h)
    assert side > 0
    px_coded = img_coded.load()
    px_plain = img_plain.load()
    x0, y0 = m + inner_w - side, m + inner_h - side

    def corner_has_ink(px):
        for x in range(x0, m + inner_w):
            for y in range(y0, m + inner_h):
                if px[x, y] == 0:
                    return True
        return False

    assert corner_has_ink(px_coded)
    # That corner is clear on the plain label (the text column keeps left).
    assert not corner_has_ink(px_plain)


def test_render_label_grocycode_never_overlaps_the_text_column():
    # A long name, a date, and a long footer, all squeezed next to the code:
    # a gap column between the text area and the QR square stays white from
    # the hero row down, so text and code never run together.
    coded = lr.LabelSpec(
        name="Extra Sharp Aged Vermont Cheddar Wedge",
        added="2026-07-08", best_by="2026-07-22", best_by_source="default",
        extra="Fridge, back left shelf, second bin",
        grocycode="grcy:p:1234:6a28c889c1193")
    img = lr.render_label(coded)
    m = coded.margin_px
    inner_w = coded.width_px - 2 * m
    inner_h = coded.height_px - 2 * m
    side = lr.grocycode_qr_side(inner_w, inner_h)
    gap = max(2, int(inner_h * 0.04))
    px = img.load()
    # The single pixel column right before the QR block: white on every row of
    # the QR's vertical span (the gap keeps text and code apart).
    x = m + inner_w - side - 1
    for y in range(m + inner_h - side, m + inner_h):
        assert px[x, y] == 255
    assert gap >= 2


def test_render_label_grocycode_skips_logo_in_shared_corner():
    # The brand mark and the entry code share the bottom-right corner; the
    # scannable code wins, so a coded label with the logo on matches a coded
    # label with it off.
    with_logo = lr.render_label(lr.LabelSpec(
        name="Stew", best_by="2026-07-22", show_logo=True,
        grocycode="grcy:p:12:6a28c889c1193"))
    without_logo = lr.render_label(lr.LabelSpec(
        name="Stew", best_by="2026-07-22", show_logo=False,
        grocycode="grcy:p:12:6a28c889c1193"))
    assert list(with_logo.getdata()) == list(without_logo.getdata())


def test_render_label_grocycode_tiny_label_prints_plain():
    # A label too small for a scannable square renders exactly like the plain
    # one instead of carrying a decorative smudge.
    tiny_plain = lr.render_label(lr.LabelSpec(
        name="Jam", width_in=0.6, height_in=0.25, dpi=203))
    tiny_coded = lr.render_label(lr.LabelSpec(
        name="Jam", width_in=0.6, height_in=0.25, dpi=203,
        grocycode="grcy:p:12:6a28c889c1193"))
    assert list(tiny_plain.getdata()) == list(tiny_coded.getdata())


# -- Label stock shapes (FoodAssistant-bprm) ---------------------------------


def test_normalize_shape_table():
    assert lr.normalize_shape("rectangle") == "rectangle"
    assert lr.normalize_shape("rect") == "rectangle"
    assert lr.normalize_shape("rectangular") == "rectangle"
    assert lr.normalize_shape(" SQUARE ") == "square"
    assert lr.normalize_shape("Round") == "round"
    assert lr.normalize_shape("") == "rectangle"
    assert lr.normalize_shape(None) == "rectangle"
    assert lr.normalize_shape("oval") == "rectangle"


def test_cut_line_width_scales_with_dpi():
    assert lr.cut_line_width(72) == 1
    assert lr.cut_line_width(203) == 2
    assert lr.cut_line_width(600) == 5
    # Garbage falls back to the default dpi instead of raising.
    assert lr.cut_line_width("not-a-dpi") == lr.cut_line_width(lr.DEFAULT_DPI)


def test_inscribed_box_truth_table():
    # Exact pins so the math cannot silently drift.
    assert lr.inscribed_box(200, 200, 0) == (30, 30, 141)
    assert lr.inscribed_box(200, 200, 12) == (38, 38, 124)
    assert lr.inscribed_box(406, 406, 14) == (70, 70, 267)
    # Non-square stock: the circle comes from the shorter side, still centered.
    assert lr.inscribed_box(300, 200, 0) == (80, 30, 141)
    assert lr.inscribed_box(50, 50, 5) == (11, 11, 28)


def test_inscribed_box_geometry_invariants():
    for w, h, inset in ((200, 200, 0), (200, 200, 12), (406, 406, 14),
                        (300, 200, 0), (50, 50, 5), (231, 231, 7)):
        x, y, side = lr.inscribed_box(w, h, inset)
        cx, cy = w / 2.0, h / 2.0
        r = min(w, h) / 2.0 - inset
        # Every corner of the box sits inside (or on) the usable circle,
        # within the half-pixel slop of centering.
        for px_, py_ in ((x, y), (x + side, y), (x, y + side),
                         (x + side, y + side)):
            assert math.hypot(px_ - cx, py_ - cy) <= r + 1.0
        # The box is centered within a pixel...
        assert abs((x + side / 2.0) - cx) <= 1.0
        assert abs((y + side / 2.0) - cy) <= 1.0
        # ...and it is the LARGEST such square: two more pixels break out.
        assert (side + 2) / 2.0 * math.sqrt(2) > r


def test_inscribed_box_degenerate_inset_still_renders():
    # An inset larger than the radius cannot collapse the box to nothing.
    x, y, side = lr.inscribed_box(40, 40, 30)
    assert side >= 1
    assert x >= 0 and y >= 0 and x + side <= 40 and y + side <= 40


def test_rect_shape_render_is_pixel_identical():
    # The pin for FoodAssistant-bprm: shape support must not move a single
    # pixel of the shipped rectangular rendering.
    spec = lr.LabelSpec(name="Leftover Chili", added="2026-07-01",
                        best_by="2026-07-15", best_by_source="default",
                        width_in=2.0, height_in=1.0, dpi=203,
                        grocycode="grcy:p:12:55")
    base = lr.render_label(spec)
    assert lr.render_shaped_label(spec, "rectangle").tobytes() == base.tobytes()
    # Unknown shape values fall back to the rectangular render, unchanged.
    assert lr.render_shaped_label(spec, "oval").tobytes() == base.tobytes()


def test_square_shape_is_plain_composition_with_clear_edges():
    # A square label is just a stock whose sides are equal: same renderer,
    # and the layout degrades gracefully (nothing touches the edges even with
    # a long wrapped name, a hero date, and an entry code on board).
    spec = lr.LabelSpec(name="Extra Sharp Aged Vermont Cheddar Cheese Wedge",
                        added="2026-07-01", best_by="2026-07-15",
                        best_by_source="default",
                        width_in=1.5, height_in=1.5, dpi=203,
                        grocycode="grcy:p:9:31")
    img = lr.render_shaped_label(spec, "square")
    assert img.size == (spec.width_px, spec.height_px)
    assert img.tobytes() == lr.render_label(spec).tobytes()
    px = img.load()
    w, h = img.size
    for y in range(h):
        assert px[0, y] == 255 and px[w - 1, y] == 255
    for x in range(w):
        assert px[x, 0] == 255 and px[x, h - 1] == 255


def test_round_label_ink_stays_inside_the_cut_circle():
    # Everything drawn (name, hero date, badge, entry code, and the cut-line
    # ring itself) must land inside the cut circle; the corners of the
    # rectangular canvas around it stay background.
    spec = lr.LabelSpec(name="Strawberry Jam", added="2026-07-01",
                        best_by="2026-07-15", best_by_source="llm",
                        width_in=2.0, height_in=2.0, dpi=203,
                        grocycode="grcy:p:12:55")
    img = lr.render_shaped_label(spec, "round")
    assert img.size == (spec.width_px, spec.height_px)
    px = img.load()
    w, h = img.size
    cx, cy, r = w / 2.0, h / 2.0, min(w, h) / 2.0
    dark = 0
    for y in range(h):
        for x in range(w):
            if px[x, y] < 128:
                dark += 1
                assert math.hypot(x + 0.5 - cx, y + 0.5 - cy) <= r + 1.0
    assert dark > 0  # the label actually rendered content


def test_round_label_corners_are_background():
    spec = lr.LabelSpec(name="Peach Preserves", best_by="2026-08-01",
                        width_in=2.0, height_in=2.0, dpi=203)
    img = lr.render_shaped_label(spec, "round")
    px = img.load()
    w, h = img.size
    for x, y in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        assert px[x, y] == 255


def test_round_cut_line_ring_is_optional():
    spec = lr.LabelSpec(name="Apple Butter", width_in=2.0, height_in=2.0,
                        dpi=203)
    ringed = lr.render_shaped_label(spec, "round", cut_line=True)
    plain = lr.render_shaped_label(spec, "round", cut_line=False)
    w, h = ringed.size
    px_r, px_p = ringed.load(), plain.load()
    # The ring shows at the circle's edge midpoints; without it they stay white.
    edges = ((w // 2, 0), (w // 2, h - 1), (0, h // 2), (w - 1, h // 2))
    assert all(px_r[x, y] < 128 for x, y in edges)
    assert all(px_p[x, y] == 255 for x, y in edges)


def test_round_inner_spec_carries_fields_and_drops_margin():
    spec = lr.LabelSpec(name="Chutney", best_by="2026-09-01",
                        best_by_source="default", width_in=2.0, height_in=2.0,
                        dpi=203, grocycode="grcy:p:3:9")
    inner = lr.round_inner_spec(spec)
    assert inner.name == spec.name
    assert inner.grocycode == spec.grocycode
    assert inner.margin_in == 0.0
    assert inner.width_in == inner.height_in
    # The inner canvas matches the inscribed content box of the cut circle.
    inset = spec.margin_px + lr.cut_line_width(spec.dpi)
    _, _, side = lr.inscribed_box(spec.width_px, spec.height_px, inset)
    assert inner.width_px == side


def test_round_decorative_label_stays_inside_circle():
    img = lr.render_decorative_label("Basil", width_in=1.5, height_in=1.5,
                                     dpi=203, shape="round")
    px = img.load()
    w, h = img.size
    cx, cy, r = w / 2.0, h / 2.0, min(w, h) / 2.0
    for x, y in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        assert px[x, y] == 255
    dark = 0
    for y in range(h):
        for x in range(w):
            if px[x, y] < 128:
                dark += 1
                assert math.hypot(x + 0.5 - cx, y + 0.5 - cy) <= r + 1.0
    assert dark > 0


def test_rect_decorative_unchanged_by_shape_param():
    base = lr.render_decorative_label("Basil", width_in=2.0, height_in=1.0,
                                      dpi=203)
    shaped = lr.render_decorative_label("Basil", width_in=2.0, height_in=1.0,
                                        dpi=203, shape="rectangle")
    assert base.tobytes() == shaped.tobytes()


def test_batch_pdf_accepts_shape():
    specs = [lr.LabelSpec(name="A", width_in=2.0, height_in=2.0),
             lr.LabelSpec(name="B", width_in=2.0, height_in=2.0)]
    pdf = lr.render_batch_pdf_bytes(specs, shape="round")
    assert pdf[:5] == b"%PDF-"
    # The default keeps the rectangular batch byte-path unchanged in kind.
    assert lr.render_batch_pdf_bytes(specs)[:5] == b"%PDF-"
