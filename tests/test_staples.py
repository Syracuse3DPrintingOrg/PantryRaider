"""Staple-ingredient matching — the logic that has bitten us twice with
false positives ("coconut milk" as milk, bare "chicken" via "chicken stock")."""
import pytest

from app.services import mealie as m
from app.config import settings


@pytest.fixture(autouse=True)
def fresh_staples(tmp_path, monkeypatch):
    """Each test gets the bundled default staples file via a temp data dir."""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "staple_items", "")
    m.reset_staple_cache()
    yield
    m.reset_staple_cache()


def is_staple(text: str) -> bool:
    return m._is_staple_ingredient(m._tokens(text))


@pytest.mark.parametrize("ingredient", [
    "flour", "all-purpose flour", "salt", "kosher salt", "table salt",
    "eggs", "2 large eggs", "brown sugar", "granulated sugar",
    "unsalted butter", "butter", "milk", "olive oil", "extra virgin olive oil",
    "vegetable oil", "soy sauce", "hot sauce", "worcestershire sauce",
    "chicken stock", "beef stock", "vegetable stock",
    "apple cider vinegar", "white vinegar", "balsamic vinegar",
    "tomato paste", "crushed tomatoes", "canned chickpeas", "chickpeas",
    "black beans", "canned black beans", "cannellini beans",
    "dijon mustard", "mayonnaise", "ketchup", "capers", "olives",
    "garlic", "onion", "potatoes", "lemons", "limes",
    "dried oregano", "smoked paprika", "cayenne pepper", "ground cumin",
    "bay leaves", "rice", "breadcrumbs", "pasta", "parmesan",
    # Descriptor words a recipe tacks on must not defeat a file staple:
    "parmesan cheese", "grated parmesan", "freshly grated parmesan",
    "fresh garlic", "chopped onions", "sliced onion", "fresh lemons",
    # Measurement/quantity words in real recipe ingredients must not defeat a
    # staple match (the reason the "with pantry staples" tier came back empty):
    "3 tablespoons unsalted butter", "1 teaspoon kosher salt",
    "1/2 teaspoon black pepper", "2 tablespoons extra-virgin olive oil",
    "1 cup all-purpose flour", "2 cloves garlic, minced", "1 pinch ground cumin",
])
def test_staples_match(ingredient):
    assert is_staple(ingredient), f"{ingredient!r} should be a staple"


@pytest.mark.parametrize("ingredient", [
    "chicken",            # NOT a staple just because chicken stock is
    "beef",               # ditto for beef stock
    "apple",              # ditto for apple cider vinegar
    "chicken breast",
    "coconut milk",       # the original false positive
    "ground beef",
    "heavy cream",
    "salmon fillet",
    "tofu",
    # A descriptor token must not turn a non-staple food into a staple just
    # because part of the phrase looks like one:
    "blue cheese",        # "cheese" descriptor, but "blue" is not a staple
    "cream cheese",
    "coconut milk",       # "milk" is a staple word, "coconut" is not a descriptor
])
def test_non_staples_rejected(ingredient):
    assert not is_staple(ingredient), f"{ingredient!r} should NOT be a staple"


def test_settings_override_takes_priority():
    settings.staple_items = "wasabi, nori"
    m.reset_staple_cache()
    assert is_staple("wasabi")
    # File staples are bypassed entirely when the settings list is set;
    # built-in fuzzy tokens no longer apply either.
    assert not is_staple("chicken stock")


def test_user_staples_file_overrides_bundled(tmp_path):
    (tmp_path / "staples.txt").write_text("# my staples\nVegemite\n")
    m.reset_staple_cache()
    assert is_staple("vegemite")
    assert not is_staple("chicken stock")  # bundled default no longer active


def test_comments_and_blanks_ignored(tmp_path):
    (tmp_path / "staples.txt").write_text("\n# comment only\n\n")
    m.reset_staple_cache()
    # Empty effective file -> falls back to the bundled default
    assert is_staple("chicken stock")
