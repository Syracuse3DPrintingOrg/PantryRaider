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
