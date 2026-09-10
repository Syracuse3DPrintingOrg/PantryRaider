"""Document print settings: pure mapping from the app's page size / color /
duplex settings to CUPS `lp -o` options (FoodAssistant-7xo5). No printer or
network touched."""
from __future__ import annotations

import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import print_document as pd  # noqa: E402


def test_defaults_map_to_letter_size_omitted_color_and_sides():
    # "auto" page size deliberately omits the media option so the printer's
    # own default page size wins.
    opts = pd.document_print_options("auto", "color", "one-sided")
    assert opts == {"print-color-mode": "color", "sides": "one-sided"}


def test_known_page_sizes_map_to_cups_media_names():
    assert pd.document_print_options("letter", "color", "one-sided")["media"] == "Letter"
    assert pd.document_print_options("a4", "color", "one-sided")["media"] == "A4"
    assert pd.document_print_options("legal", "color", "one-sided")["media"] == "Legal"


def test_color_modes_map_correctly():
    assert pd.document_print_options("auto", "color", "one-sided")["print-color-mode"] == "color"
    assert pd.document_print_options("auto", "monochrome", "one-sided")["print-color-mode"] == "monochrome"


def test_duplex_modes_map_correctly():
    assert pd.document_print_options("auto", "color", "one-sided")["sides"] == "one-sided"
    assert pd.document_print_options("auto", "color", "two-sided")["sides"] == "two-sided-long-edge"


def test_unknown_values_are_omitted_not_raised():
    opts = pd.document_print_options("poster", "purple", "spiral")
    assert opts == {}


def test_blank_and_none_values_are_omitted():
    assert pd.document_print_options("", "", "") == {}
    assert pd.document_print_options(None, None, None) == {}


def test_case_and_whitespace_insensitive():
    opts = pd.document_print_options(" Letter ", "COLOR", " Two-Sided ")
    assert opts == {
        "media": "Letter",
        "print-color-mode": "color",
        "sides": "two-sided-long-edge",
    }


def test_returns_plain_dict_safe_for_print_bytes_options():
    opts = pd.document_print_options("letter", "monochrome", "two-sided")
    assert isinstance(opts, dict)
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in opts.items())


# -- Header quick facts (FoodAssistant-gm4c) ---------------------------------


def test_quick_facts_includes_prep_cook_total_servings_in_order():
    recipe = {
        "prep_time": "20 minutes",
        "cook_time": "35 minutes",
        "total_time": "55 minutes",
        "servings": 4,
    }
    assert pd.format_quick_facts(recipe) == (
        "Prep 20 minutes | Cook 35 minutes | Total 55 minutes | Serves 4"
    )


def test_quick_facts_skips_missing_fields():
    assert pd.format_quick_facts({"servings": 2}) == "Serves 2"
    assert pd.format_quick_facts({}) == ""
    assert pd.format_quick_facts({"prep_time": "  "}) == ""


def test_quick_facts_reads_mealie_camel_case_and_raw_perform_time():
    recipe = {
        "prepTime": "10 minutes",
        "performTime": "15 minutes",
        "totalTime": "25 minutes",
        "recipeYield": "6 servings",
    }
    assert pd.format_quick_facts(recipe) == (
        "Prep 10 minutes | Cook 15 minutes | Total 25 minutes | Serves 6 servings"
    )


def test_quick_facts_prefers_snake_case_over_camel_case():
    recipe = {"prep_time": "5 min", "prepTime": "50 min"}
    assert pd.format_quick_facts(recipe) == "Prep 5 min"


def test_quick_facts_scaled_servings_wins_over_plain_servings():
    recipe = {"scaled_servings": 8, "servings": 4}
    assert pd.format_quick_facts(recipe) == "Serves 8"


# -- Two-column ingredients (FoodAssistant-gm4c) -----------------------------


def test_two_column_threshold():
    assert pd.use_two_column_ingredients(8) is False
    assert pd.use_two_column_ingredients(9) is True
    assert pd.use_two_column_ingredients(0) is False


def test_recipe_to_blocks_uses_columns_block_for_long_ingredient_lists():
    recipe = {"title": "Big Batch", "ingredients": [f"item {i}" for i in range(12)]}
    blocks = pd.recipe_to_blocks(recipe)
    styles = [b.style for b in blocks]
    assert "columns" in styles
    columns_block = next(b for b in blocks if b.style == "columns")
    assert len(columns_block.items) == 12
    assert columns_block.items[0] == "- item 0"


def test_recipe_to_blocks_uses_plain_bullets_for_short_ingredient_lists():
    recipe = {"title": "Toast", "ingredients": ["bread", "butter"]}
    blocks = pd.recipe_to_blocks(recipe)
    assert "columns" not in [b.style for b in blocks]
    assert [b.text for b in blocks if b.style == "body"] == ["- bread", "- butter"]


def test_recipe_to_blocks_header_order_is_title_then_quickfacts():
    recipe = {"title": "Soup", "total_time": "30 minutes", "ingredients": ["water"]}
    blocks = pd.recipe_to_blocks(recipe)
    assert blocks[0].style == "title"
    assert blocks[1].style == "quickfacts"
    assert blocks[1].text == "Total 30 minutes"


def test_recipe_to_blocks_omits_quickfacts_when_no_facts_present():
    recipe = {"title": "Water", "ingredients": ["water"]}
    blocks = pd.recipe_to_blocks(recipe)
    assert "quickfacts" not in [b.style for b in blocks]


# -- One-page-fit scaling (FoodAssistant-gm4c) -------------------------------


def test_fit_scale_is_full_size_for_short_content():
    blocks = [pd.Block("title", "Toast"), pd.Block("body", "- bread")]
    assert pd.fit_scale(blocks) == 1.0


def test_fit_scale_shrinks_for_long_content():
    blocks = [pd.Block("body", "x" * 200) for _ in range(40)]
    scale = pd.fit_scale(blocks)
    assert 0.72 <= scale < 1.0


def test_fit_scale_never_drops_below_floor():
    blocks = [pd.Block("step", "x" * 500) for _ in range(200)]
    assert pd.fit_scale(blocks) == 0.72


def test_content_char_count_weights_title_and_heading_more_than_body():
    title_only = pd.content_char_count([pd.Block("title", "abcdefghij")])
    body_only = pd.content_char_count([pd.Block("body", "abcdefghij")])
    assert title_only > body_only


def test_content_char_count_counts_columns_items():
    blocks = [pd.Block("columns", items=["a" * 10, "b" * 10])]
    assert pd.content_char_count(blocks) == 10  # 20 chars halved by columns


# -- Full render smoke tests (rendering only, not pixel-level) --------------


def test_render_recipe_pdf_bytes_produces_valid_pdf_header():
    recipe = {
        "title": "Weeknight Pasta",
        "prep_time": "10 minutes",
        "cook_time": "15 minutes",
        "total_time": "25 minutes",
        "servings": 4,
        "ingredients": [f"{i} cup ingredient {i}" for i in range(1, 10)],
        "steps": ["Boil water.", "Cook pasta.", "Serve."],
        "notes": "Freezes well.",
    }
    pdf = pd.render_recipe_pdf_bytes(recipe)
    assert pdf.startswith(b"%PDF")


def test_render_recipe_pdf_bytes_with_long_ingredient_list_still_renders():
    recipe = {
        "title": "Big Batch Chili",
        "ingredients": [f"{i} cup ingredient {i}" for i in range(1, 20)],
        "steps": ["Combine everything.", "Simmer for an hour."],
    }
    pdf = pd.render_recipe_pdf_bytes(recipe)
    assert pdf.startswith(b"%PDF")


def test_render_document_pdf_bytes_without_logo_still_renders():
    blocks = [pd.Block("title", "Plain Text"), pd.Block("body", "Some content.")]
    pdf = pd.render_document_pdf_bytes(blocks, show_logo=False)
    assert pdf.startswith(b"%PDF")


# -- Branded header + logo (FoodAssistant-tj4e) ------------------------------


def _sample_recipe() -> dict:
    return {
        "title": "Weeknight Skillet Pasta",
        "prep_time": "10 minutes",
        "cook_time": "20 minutes",
        "total_time": "30 minutes",
        "servings": 4,
        "ingredients": [
            "12 oz pasta", "2 tbsp olive oil", "3 cloves garlic",
            "1 can crushed tomatoes", "1/2 tsp red pepper flakes",
            "1/4 cup parmesan", "salt", "fresh basil",
        ],
        "steps": [
            "Boil the pasta until just shy of al dente.",
            "Warm the oil and soften the garlic.",
            "Add the tomatoes and pepper flakes and simmer.",
            "Toss the pasta through the sauce.",
            "Finish with parmesan and basil.",
        ],
    }


def test_recipe_to_blocks_has_title_metadata_and_section_headings():
    blocks = pd.recipe_to_blocks(_sample_recipe())
    styles = [b.style for b in blocks]
    # A clear title, a metadata (quick-facts) row, and both section headings.
    assert blocks[0].style == "title"
    assert blocks[0].text == "Weeknight Skillet Pasta"
    assert "quickfacts" in styles
    facts = next(b.text for b in blocks if b.style == "quickfacts")
    assert facts == "Prep 10 minutes | Cook 20 minutes | Total 30 minutes | Serves 4"
    headings = [b.text for b in blocks if b.style == "heading"]
    assert "Ingredients" in headings
    assert "Steps" in headings


def test_recipe_to_blocks_omits_blank_metadata_fields():
    # Only cook time is present; prep/total/servings are blank or absent and
    # must not leave stray "Prep" / "Total" / "Serves" fragments in the row.
    recipe = {
        "title": "Slow Roast",
        "prep_time": "  ",
        "cook_time": "3 hours",
        "ingredients": ["1 roast"],
    }
    blocks = pd.recipe_to_blocks(recipe)
    facts = next(b.text for b in blocks if b.style == "quickfacts")
    assert facts == "Cook 3 hours"
    assert "Prep" not in facts
    assert "Serves" not in facts


def test_load_logo_image_returns_rgba_and_degrades_on_tiny_size():
    # A real asset loads as an RGBA image fit within the requested square.
    logo = pd._load_logo_image(80)
    assert logo is not None
    assert logo.mode == "RGBA"
    assert logo.width <= 80 and logo.height <= 80
    # A nonsensical size can never crash the header; it just means no mark.
    assert pd._load_logo_image(0) is None


def test_render_full_recipe_returns_valid_nonempty_pdf():
    pdf = pd.render_recipe_pdf_bytes(_sample_recipe())
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 1000


def test_render_still_valid_pdf_when_logo_asset_missing(monkeypatch):
    # Simulate a container where the brand asset cannot be loaded: the header
    # must fall back to the wordmark alone and the page must still render.
    monkeypatch.setattr(pd, "_load_logo_image", lambda side_px: None)
    pdf = pd.render_recipe_pdf_bytes(_sample_recipe())
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 1000


def test_render_header_paints_brand_pink_on_the_page(monkeypatch):
    # Prove the accent actually lands: render one full-size page image (the same
    # code path render_document_pdf_bytes uses per page) and confirm the brand
    # pink appears in the header band. Reaches in via a tiny render so the check
    # stays pure and does not parse the PDF.
    from PIL import Image

    captured = {}
    real_new = Image.new

    def _capture_new(mode, size, color=0):
        page = real_new(mode, size, color)
        captured.setdefault("first", page)
        return page

    monkeypatch.setattr(Image, "new", _capture_new)
    pd.render_recipe_pdf_bytes(_sample_recipe())
    page = captured["first"]
    assert page.mode == "RGB"
    colors = {c for _, c in page.getcolors(maxcolors=1_000_000) or []}
    # The exact brand pink (#F2006E) is present from the wordmark/rule.
    assert (242, 0, 110) in colors
    # And near-black recipe text is present too (legible in mono).
    assert (17, 17, 17) in colors
