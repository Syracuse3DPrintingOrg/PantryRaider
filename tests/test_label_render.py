"""Label render engine: sizing, fit, badges, decorative mode, batch PDF
(FoodAssistant-23v6). Pure image composition, no printer or network."""
from __future__ import annotations

import json
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
    spec = LabelSpec = lr.LabelSpec(name="Milk", best_by="2026-07-15")
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
