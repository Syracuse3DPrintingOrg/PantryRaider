"""Parser tests for uploaded recipe files (generic JSON, schema.org JSON-LD,
Mealie export JSON), plus ingredient/instruction normalization and error cases.

Pure-function tests: parse_recipe_file never touches the network.
"""
import json

import pytest

from app.services.recipes_import import parse_recipe_file


def _b(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


# ── Generic recipe JSON ────────────────────────────────────────────────────────

def test_generic_json_strings():
    recipe = parse_recipe_file("soup.json", _b({
        "name": "Tomato Soup",
        "description": "Quick and cozy.",
        "servings": "4 bowls",
        "ingredients": ["2 tomatoes", "1 onion"],
        "instructions": ["Chop.", "Simmer."],
        "source": "https://example.com/soup",
    }))
    assert recipe["name"] == "Tomato Soup"
    assert recipe["description"] == "Quick and cozy."
    assert recipe["servings"] == "4 bowls"
    assert recipe["ingredients"] == ["2 tomatoes", "1 onion"]
    assert recipe["instructions"] == ["Chop.", "Simmer."]
    assert recipe["source"] == "https://example.com/soup"


def test_generic_json_title_and_steps_aliases():
    recipe = parse_recipe_file("x.json", _b({
        "title": "Pancakes",
        "ingredients": ["flour"],
        "steps": ["Mix", "Cook"],
    }))
    assert recipe["name"] == "Pancakes"
    assert recipe["instructions"] == ["Mix", "Cook"]


def test_generic_json_object_ingredients_and_instructions():
    recipe = parse_recipe_file("x.json", _b({
        "name": "Salad",
        "ingredients": [{"name": "lettuce"}, {"text": "tomato"}],
        "instructions": [{"text": "Toss it all together."}],
    }))
    assert recipe["ingredients"] == ["lettuce", "tomato"]
    assert recipe["instructions"] == ["Toss it all together."]


# ── schema.org Recipe JSON-LD ──────────────────────────────────────────────────

def test_schema_org_object():
    recipe = parse_recipe_file("page.json", _b({
        "@context": "https://schema.org",
        "@type": "Recipe",
        "name": "Banana Bread",
        "description": "Moist loaf.",
        "recipeYield": "1 loaf",
        "recipeIngredient": ["2 bananas", "1 cup flour"],
        "recipeInstructions": [
            {"@type": "HowToStep", "text": "Mash bananas."},
            {"@type": "HowToStep", "text": "Bake."},
        ],
    }))
    assert recipe["name"] == "Banana Bread"
    assert recipe["servings"] == "1 loaf"
    assert recipe["ingredients"] == ["2 bananas", "1 cup flour"]
    assert recipe["instructions"] == ["Mash bananas.", "Bake."]


def test_schema_org_graph_array():
    recipe = parse_recipe_file("ld.json", _b({
        "@graph": [
            {"@type": "WebSite", "name": "Some Blog"},
            {"@type": "Recipe", "name": "Guacamole",
             "recipeIngredient": ["avocado"],
             "recipeInstructions": ["Smash"]},
        ]
    }))
    assert recipe["name"] == "Guacamole"
    assert recipe["ingredients"] == ["avocado"]


def test_schema_org_top_level_array():
    recipe = parse_recipe_file("ld.json", _b([
        {"@type": "Organization", "name": "Foo"},
        {"@type": ["Thing", "Recipe"], "name": "Hummus",
         "recipeIngredient": ["chickpeas"],
         "recipeInstructions": [{"text": "Blend"}]},
    ]))
    assert recipe["name"] == "Hummus"
    assert recipe["instructions"] == ["Blend"]


def test_schema_org_script_wrapper():
    html = (
        "<html><head>"
        '<script type="application/ld+json">'
        '{"@type": "Recipe", "name": "Pesto",'
        ' "recipeIngredient": ["basil", "pine nuts"],'
        ' "recipeInstructions": "Blend everything."}'
        "</script></head><body>x</body></html>"
    )
    recipe = parse_recipe_file("page.html", html.encode("utf-8"))
    assert recipe["name"] == "Pesto"
    assert recipe["ingredients"] == ["basil", "pine nuts"]
    assert recipe["instructions"] == ["Blend everything."]


def test_schema_org_string_instructions_split_and_strip_numbers():
    recipe = parse_recipe_file("x.json", _b({
        "@type": "Recipe",
        "name": "Eggs",
        "recipeIngredient": ["eggs"],
        "recipeInstructions": "1. Crack eggs\n2. Whisk\n3. Cook",
    }))
    assert recipe["instructions"] == ["Crack eggs", "Whisk", "Cook"]


def test_schema_org_howtosection_flattened():
    recipe = parse_recipe_file("x.json", _b({
        "@type": "Recipe",
        "name": "Cake",
        "recipeIngredient": ["flour"],
        "recipeInstructions": [
            {"@type": "HowToSection", "itemListElement": [
                {"@type": "HowToStep", "text": "Mix"},
                {"@type": "HowToStep", "text": "Bake"},
            ]},
        ],
    }))
    assert recipe["instructions"] == ["Mix", "Bake"]


def test_recipe_yield_as_list_and_number():
    assert parse_recipe_file("a.json", _b({
        "@type": "Recipe", "name": "A",
        "recipeYield": ["6", "6 servings"],
        "recipeIngredient": ["x"], "recipeInstructions": ["y"],
    }))["servings"] == "6"
    assert parse_recipe_file("b.json", _b({
        "name": "B", "servings": 8,
        "ingredients": ["x"], "instructions": ["y"],
    }))["servings"] == "8"


# ── Mealie export JSON ─────────────────────────────────────────────────────────

def test_mealie_export_shape():
    recipe = parse_recipe_file("mealie.json", _b({
        "name": "Carbonara",
        "description": "Roman classic.",
        "recipeYield": "2",
        "recipeIngredient": [
            {"note": "200g spaghetti"},
            {"food": {"name": "guanciale"}},
            {"display": "2 eggs"},
        ],
        "recipeInstructions": [
            {"text": "Boil pasta."},
            {"text": "Combine."},
        ],
    }))
    assert recipe["name"] == "Carbonara"
    assert recipe["servings"] == "2"
    assert recipe["ingredients"] == ["200g spaghetti", "guanciale", "2 eggs"]
    assert recipe["instructions"] == ["Boil pasta.", "Combine."]


# ── Error cases ────────────────────────────────────────────────────────────────

def test_empty_file_raises():
    with pytest.raises(ValueError, match="empty"):
        parse_recipe_file("x.json", b"   ")


def test_invalid_json_raises():
    with pytest.raises(ValueError, match="not valid recipe JSON"):
        parse_recipe_file("x.json", b"{not json")


def test_non_recipe_json_raises():
    with pytest.raises(ValueError, match="Could not find a recipe"):
        parse_recipe_file("x.json", _b({"hello": "world", "count": 3}))


def test_typed_recipe_without_name_raises():
    # An explicitly typed Recipe with no name reaches the name check.
    with pytest.raises(ValueError, match="no name"):
        parse_recipe_file("x.json", _b({
            "@type": "Recipe",
            "recipeIngredient": ["x"], "recipeInstructions": ["y"],
        }))


def test_untyped_recipe_without_name_raises():
    # Without a name and without a Recipe type, it isn't recognized as a recipe.
    with pytest.raises(ValueError, match="Could not find a recipe"):
        parse_recipe_file("x.json", _b({
            "ingredients": ["x"], "instructions": ["y"],
        }))
