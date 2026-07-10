"""Native recipe store (FoodAssistant-zwwe): CRUD, search, shapes, images.

The store's contract is shape compatibility: list_with_ingredients must feed
classify_recipes unchanged, and detail must feed current_recipe.from_mealie_detail
unchanged. These tests pin both, plus slugs, parsed-ingredient storage, image
files under a tmp data_dir, and deletion. A tmp SQLite engine per module; no
network, no Docker.
"""
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent / "service"))

from app.database import Base  # noqa: E402
from app.models import db_models  # noqa: E402,F401
from app.services import recipe_store  # noqa: E402
from app.services.recipe_store import (  # noqa: E402
    RecipeStoreError, image_extension, slugify)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    engine = create_engine(f"sqlite:///{tmp_path}/store.db")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


PARSED = {
    "name": "Weeknight Chili",
    "description": "A quick pantry chili.",
    "servings": "4 servings",
    "total_time": "45 minutes",
    "ingredients": ["2 cups kidney beans", "1 tbsp chili powder", "salt to taste"],
    "instructions": ["Simmer the beans.", "Season and serve."],
    "source": "https://example.com/chili",
}


# ── Pure helpers ──────────────────────────────────────────────────────────────

def test_slugify_basics():
    assert slugify("Weeknight Chili") == "weeknight-chili"
    assert slugify("  Crème Brûlée!  ") == "creme-brulee"
    assert slugify("100% Rye") == "100-rye"
    assert slugify("") == "recipe"
    assert slugify("!!!") == "recipe"


def test_image_extension_mapping():
    assert image_extension("image/jpeg") == "jpg"
    assert image_extension("image/webp; charset=binary") == "webp"
    assert image_extension(None, "https://x.test/pic.PNG?w=1") == "png"
    assert image_extension("text/html", "https://x.test/page") == "jpg"


# ── Create and detail shape ───────────────────────────────────────────────────

def test_create_from_parsed_and_detail_shape(db):
    saved = recipe_store.create_from_parsed(db, PARSED, source="url")
    assert saved["slug"] == "weeknight-chili"
    assert saved["name"] == "Weeknight Chili"
    assert saved["recipeYield"] == "4 servings"
    assert saved["totalTime"] == "45 minutes"
    assert saved["orgURL"] == "https://example.com/chili"
    assert saved["origin"] == "url"
    assert saved["image"] is None
    # The raw line is always preserved for display and matching.
    lines = [i["display"] for i in saved["recipeIngredient"]]
    assert lines == PARSED["ingredients"]
    steps = [s["text"] for s in saved["recipeInstructions"]]
    assert steps == PARSED["instructions"]

    again = recipe_store.detail(db, "weeknight-chili")
    assert again["name"] == "Weeknight Chili"
    assert recipe_store.detail(db, "no-such") is None
    assert recipe_store.count(db) == 1


def test_create_requires_name(db):
    with pytest.raises(RecipeStoreError):
        recipe_store.create_from_parsed(db, {"name": "  ", "ingredients": ["x"]})


def test_slug_collision_gets_suffix(db):
    a = recipe_store.create_from_parsed(db, {"name": "Tacos", "ingredients": ["a"]})
    b = recipe_store.create_from_parsed(db, {"name": "Tacos!", "ingredients": ["b"]})
    assert a["slug"] == "tacos"
    assert b["slug"] == "tacos-2"


def test_source_label_never_becomes_url(db):
    saved = recipe_store.create_from_parsed(
        db, {**PARSED, "source": "themealdb"}, source="themealdb")
    assert saved["orgURL"] is None


def test_structured_ingredients_stored(db):
    structured = [
        {"quantity": 2, "unit": {"name": "cups"}, "food": {"name": "kidney beans"},
         "note": "rinsed", "originalText": "2 cups kidney beans"},
        None,  # unparsed line keeps raw text only
        {"note": "salt to taste"},  # note-only entry: no parsed food
    ]
    saved = recipe_store.create_from_parsed(db, PARSED, structured=structured)
    ings = saved["recipeIngredient"]
    assert ings[0]["food"] == {"name": "kidney beans"}
    assert ings[0]["quantity"] == 2.0
    assert ings[0]["unit"] == {"name": "cups"}
    assert ings[0]["display"] == "2 cups kidney beans"
    assert ings[1]["food"] is None
    assert ings[1]["note"] == "1 tbsp chili powder"
    assert ings[2]["food"] is None


# ── Update (the comprehensive editor) ─────────────────────────────────────────

def test_update_from_parsed_rewrites_core_fields(db):
    recipe_store.create_from_parsed(db, PARSED, source="url")
    edited = {
        "name": "Weeknight Chili Deluxe",
        "description": "Now with cornbread on the side.",
        "servings": "6 servings",
        "prep_time": "10 minutes",
        "total_time": "50 minutes",
        "ingredients": ["3 cups kidney beans", "2 tbsp chili powder"],
        "instructions": ["Simmer longer.", "Serve with cornbread.", "Enjoy."],
    }
    saved = recipe_store.update_from_parsed(db, "weeknight-chili", edited)
    # Identity is preserved: the slug never changes on a rename, so cook counts,
    # the meal plan, and the Current Recipe keep pointing at this recipe.
    assert saved["slug"] == "weeknight-chili"
    assert saved["name"] == "Weeknight Chili Deluxe"
    assert saved["recipeYield"] == "6 servings"
    assert saved["prepTime"] == "10 minutes"
    assert saved["totalTime"] == "50 minutes"
    assert saved["description"] == "Now with cornbread on the side."
    # Ingredient and step lists are replaced wholesale, in order.
    assert [i["display"] for i in saved["recipeIngredient"]] == edited["ingredients"]
    assert [s["text"] for s in saved["recipeInstructions"]] == edited["instructions"]
    # Origin fields survive the edit.
    assert saved["origin"] == "url"
    assert saved["orgURL"] == "https://example.com/chili"
    assert recipe_store.count(db) == 1


def test_update_from_parsed_structured_round_trip(db):
    recipe_store.create_from_parsed(db, PARSED)
    structured = [
        {"quantity": 3, "unit": {"name": "cups"}, "food": {"name": "black beans"},
         "note": "drained"},
        None,
    ]
    saved = recipe_store.update_from_parsed(
        db, "weeknight-chili",
        {"name": "Black Bean Chili",
         "ingredients": ["3 cups black beans", "1 tsp cumin"],
         "instructions": ["Cook."]},
        structured=structured)
    ings = saved["recipeIngredient"]
    assert ings[0]["food"] == {"name": "black beans"}
    assert ings[0]["quantity"] == 3.0
    assert ings[0]["unit"] == {"name": "cups"}
    assert ings[0]["display"] == "3 cups black beans"
    # A line with no structured parse keeps only its raw text.
    assert ings[1]["food"] is None
    assert ings[1]["display"] == "1 tsp cumin"
    # No orphaned child rows after replacing the lists.
    from app.models.db_models import RecipeIngredient, RecipeStep
    assert db.query(RecipeIngredient).count() == 2
    assert db.query(RecipeStep).count() == 1


def test_update_preserves_image(db):
    saved = recipe_store.create_from_parsed(db, PARSED)
    recipe_store.attach_image(db, saved["slug"], b"png-bytes", "image/png")
    updated = recipe_store.update_from_parsed(
        db, saved["slug"],
        {"name": "Renamed", "ingredients": ["a"], "instructions": ["b"]})
    assert updated["image"] is not None


def test_update_requires_name_and_known_slug(db):
    recipe_store.create_from_parsed(db, PARSED)
    with pytest.raises(RecipeStoreError):
        recipe_store.update_from_parsed(db, "weeknight-chili", {"name": "  "})
    with pytest.raises(RecipeStoreError):
        recipe_store.update_from_parsed(db, "no-such", {"name": "X"})


# ── Compatibility with the existing consumers ─────────────────────────────────

def test_detail_feeds_current_recipe_normalizer(db):
    structured = [
        {"quantity": 2, "unit": {"name": "cups"}, "food": {"name": "kidney beans"}},
    ]
    recipe_store.create_from_parsed(db, PARSED, structured=structured)
    from app.services import current_recipe
    normalized = current_recipe.from_mealie_detail(
        recipe_store.detail(db, "weeknight-chili"), "weeknight-chili")
    assert normalized["title"] == "Weeknight Chili"
    assert normalized["servings"] == 4
    assert normalized["id"] == "weeknight-chili"
    names = [i["name"] for i in normalized["ingredients"]]
    assert names[0] == "kidney beans"          # parsed food wins
    assert names[1] == "1 tbsp chili powder"   # raw line kept
    assert normalized["steps"] == PARSED["instructions"]
    assert normalized["ingredients"][0]["quantity"] == 2.0


def test_list_with_ingredients_feeds_classifier(db):
    recipe_store.create_from_parsed(db, PARSED)
    rows = recipe_store.list_with_ingredients(db)
    assert len(rows) == 1
    assert rows[0]["source"] == "mealie"   # wire-compat sentinel for "my library"
    assert "orgURL" not in rows[0]
    from app.services.mealie import classify_recipes
    stock = [{"name": "Kidney beans", "days_remaining": 3,
              "storage_bucket": "refrigerated"},
             {"name": "Chili powder", "days_remaining": None,
              "storage_bucket": "pantry"}]
    tiers = classify_recipes(rows, stock)
    found = [r for tier in tiers.values() for r in tier]
    assert found and found[0]["slug"] == "weeknight-chili"


# ── Search ────────────────────────────────────────────────────────────────────

def test_list_recipes_search(db):
    recipe_store.create_from_parsed(db, {"name": "Beef Stew", "ingredients": ["beef"],
                                         "tags": ["dinner", "Winter Warmers"]})
    recipe_store.create_from_parsed(db, {"name": "Green Salad", "ingredients": ["greens"]})
    assert len(recipe_store.list_recipes(db)) == 2
    assert [r["name"] for r in recipe_store.list_recipes(db, "stew")] == ["Beef Stew"]
    assert [r["name"] for r in recipe_store.list_recipes(db, "winter")] == ["Beef Stew"]
    assert recipe_store.list_recipes(db, "zzz") == []


# ── Parse-in-place ────────────────────────────────────────────────────────────

def test_set_parsed_ingredients(db):
    recipe_store.create_from_parsed(db, PARSED)
    structured = [
        {"quantity": "2", "unit": {"name": "cups"}, "food": {"name": "kidney beans"}},
        {"note": "1 tbsp chili powder"},
        {"quantity": None, "food": {"name": "salt"}},
    ]
    n = recipe_store.set_parsed_ingredients(db, "weeknight-chili", structured)
    assert n == 2
    d = recipe_store.detail(db, "weeknight-chili")
    assert d["recipeIngredient"][0]["food"] == {"name": "kidney beans"}
    # Raw text is untouched by parsing.
    assert d["recipeIngredient"][0]["display"] == "2 cups kidney beans"
    assert d["recipeIngredient"][1]["food"] is None
    assert d["recipeIngredient"][2]["food"] == {"name": "salt"}


def test_set_parsed_ingredients_unknown_slug(db):
    with pytest.raises(RecipeStoreError):
        recipe_store.set_parsed_ingredients(db, "nope", [])


# ── Images ────────────────────────────────────────────────────────────────────

def test_attach_and_delete_image(db, tmp_path):
    saved = recipe_store.create_from_parsed(db, PARSED)
    url = recipe_store.attach_image(db, saved["slug"], b"png-bytes", "image/png")
    r = recipe_store.get_by_slug(db, saved["slug"])
    assert url == f"/recipes/images/{r.id}"
    path = tmp_path / "recipe-images" / f"{r.id}.png"
    assert path.read_bytes() == b"png-bytes"
    assert recipe_store.detail(db, saved["slug"])["image"] == url

    # Replacing with a different type removes the old file.
    recipe_store.attach_image(db, saved["slug"], b"jpg-bytes", "image/jpeg")
    assert not path.exists()
    assert (tmp_path / "recipe-images" / f"{r.id}.jpg").exists()

    assert recipe_store.delete_recipe(db, saved["slug"]) is True
    assert not (tmp_path / "recipe-images" / f"{r.id}.jpg").exists()
    assert recipe_store.detail(db, saved["slug"]) is None
    assert recipe_store.delete_recipe(db, saved["slug"]) is False


def test_attach_image_rejects_empty_and_huge(db):
    saved = recipe_store.create_from_parsed(db, PARSED)
    assert recipe_store.attach_image(db, saved["slug"], b"") is None
    big = b"x" * (recipe_store.MAX_IMAGE_BYTES + 1)
    assert recipe_store.attach_image(db, saved["slug"], big) is None
    assert recipe_store.attach_image(db, "no-such", b"data") is None


def test_image_file_refuses_traversal(db):
    recipe_store.create_from_parsed(db, PARSED)
    r = recipe_store.get_by_slug(db, "weeknight-chili")
    r.image_path = "../../etc/passwd"
    assert recipe_store.image_file(r) is None
    r.image_path = ".hidden"
    assert recipe_store.image_file(r) is None
    r.image_path = "7.jpg"
    assert recipe_store.image_file(r) is not None


def test_delete_removes_child_rows(db):
    saved = recipe_store.create_from_parsed(db, PARSED)
    from app.models.db_models import RecipeIngredient, RecipeStep
    assert db.query(RecipeIngredient).count() == 3
    recipe_store.delete_recipe(db, saved["slug"])
    assert db.query(RecipeIngredient).count() == 0
    assert db.query(RecipeStep).count() == 0
