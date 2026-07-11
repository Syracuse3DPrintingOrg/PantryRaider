"""Tier classifier: ready / staples / shopping sorting and scoring."""
import pytest

from app.services.mealie import classify_recipes, reset_staple_cache
from app.config import settings


@pytest.fixture(autouse=True)
def fixed_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "staple_items", "")
    monkeypatch.setattr(settings, "perishable_days", 14)
    monkeypatch.setattr(settings, "expiring_soon_days", 5)
    reset_staple_cache()
    yield
    reset_staple_cache()


def recipe(name, ingredients, **extra):
    return {"name": name, "slug": name, "id": name,
            "recipeIngredient": [{"note": i} for i in ingredients], **extra}


def stock(name, days=10, bucket="refrigerated"):
    return {"name": name, "days_remaining": days, "storage_bucket": bucket}


def test_all_in_stock_is_ready():
    tiers = classify_recipes(
        [recipe("Chicken & Rice Bowl", ["chicken breast", "white rice"])],
        [stock("Chicken Breast"), stock("Rice", bucket="pantry")])
    assert [r["name"] for r in tiers["ready"]] == ["Chicken & Rice Bowl"]
    assert not tiers["staples"] and not tiers["shopping"]


def test_stock_plus_staples_is_staples_tier():
    tiers = classify_recipes(
        [recipe("Fried Chicken", ["chicken thighs", "flour", "eggs", "salt"])],
        [stock("Chicken Thighs")])
    assert [r["name"] for r in tiers["staples"]] == ["Fried Chicken"]
    r = tiers["staples"][0]
    assert sorted(r["staple_ingredients"]) == ["eggs", "flour", "salt"]


def test_web_recipes_share_the_staples_tier(monkeypatch):
    """A web recipe makeable from stock + staples lands in the same staples tier
    as a Mealie one, since /suggest now classifies both pools together (uo73)."""
    mealie_r = recipe("Mealie Chicken", ["chicken", "salt", "butter"], source="mealie")
    web_r = recipe("Web Omelette", ["eggs", "salt", "butter"],
                   source="themealdb", external_id="e1")
    tiers = classify_recipes([mealie_r, web_r],
                             [stock("Chicken breast"), stock("Eggs")])
    names_sources = {(r["name"], r["source"]) for r in tiers["staples"]}
    assert ("Mealie Chicken", "mealie") in names_sources
    assert ("Web Omelette", "themealdb") in names_sources


def test_perishable_plus_missing_is_shopping_tier():
    tiers = classify_recipes(
        [recipe("Salmon Curry", ["salmon", "coconut milk", "curry paste"])],
        [stock("Salmon", days=2)])
    assert [r["name"] for r in tiers["shopping"]] == ["Salmon Curry"]
    assert set(tiers["shopping"][0]["unmatched_ingredients"]) == {"coconut milk", "curry paste"}


def test_non_perishable_with_missing_is_dropped():
    # Pantry item with far-off expiry + missing ingredients: not worth a shop run
    tiers = classify_recipes(
        [recipe("Bean Surprise", ["dried lentils", "weird truffle"])],
        [stock("Dried Lentils", days=300, bucket="pantry")])
    assert not any(tiers.values())


def test_recipe_with_no_stock_match_is_dropped():
    tiers = classify_recipes(
        [recipe("Pancakes", ["flour", "eggs", "milk"])],   # staples only
        [stock("Salmon", days=2)])
    assert not any(tiers.values())


def test_water_never_counts():
    tiers = classify_recipes(
        [recipe("Boiled Potatoes", ["potatoes", "boiling water"])],
        [stock("Potatoes", bucket="pantry")])
    assert [r["name"] for r in tiers["ready"]] == ["Boiled Potatoes"]


def test_expiring_items_listed_and_boost_score():
    expiring = recipe("Use the Chicken", ["chicken", "rice"])
    fresh = recipe("Rice Bowl", ["rice", "soy sauce"])
    tiers = classify_recipes(
        [fresh, expiring],
        [stock("Chicken", days=1), stock("Rice", days=200, bucket="pantry")])
    ready = tiers["ready"] + tiers["staples"]
    use_chicken = next(r for r in ready if r["name"] == "Use the Chicken")
    assert use_chicken["expiring_items_used"] == ["Chicken"]


def test_descriptor_bearing_staple_keeps_recipe_in_staples_tier():
    # Regression: a recipe fully covered by stock plus a file staple that the
    # recipe writes with a descriptor word ("parmesan cheese", not bare
    # "Parmesan") must land in the staples tier. Before the containment fix the
    # exact-equality phrase match failed on the extra "cheese" token, dropping
    # the recipe into shopping (or out entirely) and leaving ready/staples empty.
    tiers = classify_recipes(
        [recipe("Pasta al Parmigiano",
                ["spaghetti", "parmesan cheese", "olive oil", "garlic"])],
        [stock("Spaghetti", days=400, bucket="pantry")])
    assert [r["name"] for r in tiers["staples"]] == ["Pasta al Parmigiano"]
    assert not tiers["shopping"]
    r = tiers["staples"][0]
    assert "parmesan cheese" in r["staple_ingredients"]
    assert not r["unmatched_ingredients"]


def test_descriptor_bearing_staple_keeps_recipe_ready():
    # Same fix on the ready tier: every ingredient is either in stock or a
    # descriptor-bearing file staple, so nothing is unmatched.
    tiers = classify_recipes(
        [recipe("Cacio e Pepe", ["spaghetti", "grated parmesan"])],
        [stock("Spaghetti", days=400, bucket="pantry")])
    assert [r["name"] for r in tiers["staples"]] == ["Cacio e Pepe"]
    assert not tiers["ready"] and not tiers["shopping"]


def test_top_per_tier_caps_results():
    recipes = [recipe(f"R{i}", ["chicken"]) for i in range(10)]
    tiers = classify_recipes(recipes, [stock("Chicken")], top_per_tier=3)
    assert len(tiers["ready"]) == 3
