"""Tier classifier: ready / staples / shopping sorting and scoring."""
import pytest

from app.services.recipe_match import classify_recipes, reset_staple_cache
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


# ── Food synonyms (FoodAssistant-vq81e) ─────────────────────────────────────

@pytest.mark.parametrize("wanted,have", [
    ("3 scallions, sliced", "Green Onions"),
    ("1 cup garbanzo beans", "Chickpeas"),
    ("2 courgettes", "Zucchini"),
    ("200 ml double cream", "Heavy Cream"),
    ("1/2 bunch fresh coriander", "Cilantro"),
    ("500 g minced beef", "Ground Beef"),
])
def test_synonyms_count_as_in_stock(wanted, have):
    tiers = classify_recipes([recipe("Dish", [wanted])], [stock(have)])
    assert [r["name"] for r in tiers["ready"]] == ["Dish"]


@pytest.mark.parametrize("wanted,have", [
    ("margarine", "Butter"),
    ("lime", "Lemons"),
    ("ground coriander", "Cilantro"),
    ("turkey", "Chicken"),
])
def test_related_foods_are_not_synonyms(wanted, have):
    tiers = classify_recipes([recipe("Dish", [wanted])], [stock(have)])
    assert not tiers["ready"]


def test_synonyms_reach_the_shopping_list_split():
    from app.services.recipe_match import partition_recipe_ingredients
    out = partition_recipe_ingredients(
        [{"note": "aubergine"}, {"note": "rocket"}], [{"name": "Eggplant"}])
    assert out == {"owned": ["aubergine"], "needed": ["rocket"]}


def test_plural_forms_and_synonyms_count_as_in_stock():
    tiers = classify_recipes(
        [recipe("Peach Cobbler", ["4 peaches, sliced", "heavy cream"]),
         recipe("Soup", ["2 potatoes", "chicken broth", "broccoli florets"])],
        [stock("Peach"), stock("Cream"), stock("Potato", bucket="pantry"),
         stock("Chicken Stock", bucket="pantry"), stock("Broccoli")])
    assert sorted(r["name"] for r in tiers["ready"]) == ["Peach Cobbler", "Soup"]


def test_a_different_food_with_the_same_last_word_is_not_in_stock():
    tiers = classify_recipes(
        [recipe("Mash", ["potatoes", "cream"])],
        [stock("Sweet Potatoes"), stock("Vanilla Ice Cream", bucket="frozen", days=60)])
    assert not tiers["ready"] and not tiers["staples"]


def test_the_first_matching_stock_row_wins_across_alternatives():
    # "butter or margarine" offers two heads; the stock row that comes first
    # is the one credited, exactly as a full scan would pick it.
    tiers = classify_recipes(
        [recipe("Toast", ["margarine or butter"])],
        [stock("Butter", days=2), stock("Margarine", days=1)])
    assert tiers["ready"][0]["expiring_items_used"] == ["Butter"]


def test_classify_scales_to_a_large_library():
    import time
    foods = ["chicken breast", "onion", "tomato", "potato", "milk", "rice",
             "spinach", "lemon", "beef", "salmon", "egg", "yogurt"]
    inv = [stock(f"Brand {foods[i % len(foods)]} {chr(97 + i % 26)}x")
           for i in range(400)]
    lib = [recipe(f"r{n}", [f"1 cup {foods[(n + k) % len(foods)]}"
                            for k in range(12)]) for n in range(1000)]
    start = time.perf_counter()
    classify_recipes(lib, inv)
    assert time.perf_counter() - start < 5
