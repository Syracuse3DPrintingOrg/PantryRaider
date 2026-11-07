"""Stricter recipe matching (FoodAssistant-9856m).

One shared word used to count an ingredient as owned ("baking powder" found
the baking soda, "chicken" found the chicken stock). These pins cover the
same-food test that replaced it: shared head word, harmless variety words
only on the recipe side, and no food-changing word on the stock side.
"""
import pytest

from app.services.recipe_match import _food_key, _ingredient_keys, _owns, _singular


@pytest.mark.parametrize("ingredient,stock", [
    ("chicken", "Chicken Breast"),
    ("boneless skinless chicken thighs", "Chicken Thighs"),
    ("2 cups shredded cheddar cheese", "Cheddar"),
    ("cheddar", "Cheddar Cheese"),
    ("1 yellow onion, diced", "Onions"),
    ("3 cloves garlic, minced", "Garlic"),
    ("extra virgin olive oil", "Olive Oil"),
    ("1 cup butter, softened", "Butter"),
    ("unsalted butter", "Butter"),
    ("whole milk", "Milk"),
    ("2 large eggs", "Eggs"),
    ("eggs", "Large Brown Eggs"),
    ("baby spinach", "Spinach"),
    ("roma tomatoes", "Tomatoes"),
    ("1 (14 oz) can diced tomatoes, drained", "Diced Tomatoes"),
    ("juice of 1 lemon", "Lemons"),
    ("greek yogurt", "Chobani Vanilla Greek Yogurt"),
    ("butter or margarine", "Butter"),
    ("russet potatoes", "Potatoes"),
    ("light brown sugar", "Brown Sugar"),
    ("parmesan cheese", "Parmesan"),
    ("ground beef", "Ground Beef 80/20"),
    ("long grain white rice", "Rice"),
    ("fresh basil leaves", "Basil"),
    ("carrots, peeled and chopped", "Carrots"),
    ("1 lb pasta", "Pasta"),
    ("spaghetti", "Spaghetti"),
    ("heavy cream", "Heavy Whipping Cream"),
    ("salmon fillets", "Salmon"),
    ("bacon", "Thick Cut Bacon"),
    ("black beans, rinsed", "Black Beans"),
    ("frozen peas", "Peas"),
    ("cream cheese, softened", "Cream Cheese"),
    ("chicken broth", "Chicken Broth"),
    ("tahini", "Tahini Paste"),
])
def test_same_food_is_owned(ingredient, stock):
    assert _owns(_ingredient_keys(ingredient), _food_key(stock))


@pytest.mark.parametrize("ingredient,stock", [
    ("baking powder", "Baking Soda"),
    ("chicken", "Chicken Stock"),
    ("chicken stock", "Chicken Breast"),
    ("milk", "Coconut Milk"),
    ("coconut milk", "Milk"),
    ("butter", "Peanut Butter"),
    ("peanut butter", "Butter"),
    ("apple", "Apple Juice"),
    ("tomato", "Tomato Paste"),
    ("garlic", "Garlic Powder"),
    ("onion", "Green Onions"),
    ("sweet potatoes", "Potatoes"),
    ("sour cream", "Heavy Cream"),
    ("cream cheese", "Sour Cream"),
    ("cheese", "Cream Cheese"),
    ("rice vinegar", "Rice"),
    ("rice", "Rice Vinegar"),
    ("corn syrup", "Maple Syrup"),
    ("lime", "Lemons"),
    ("turkey", "Chicken"),
    ("chocolate milk", "Milk"),
    ("salt", "Garlic Salt"),
    ("beef broth", "Chicken Broth"),
    ("almond flour", "Flour"),
])
def test_different_food_is_not_owned(ingredient, stock):
    assert not _owns(_ingredient_keys(ingredient), _food_key(stock))


# Plurals fold to one form, not just a dropped "s" (FoodAssistant review).
@pytest.mark.parametrize("ingredient,stock", [
    ("2 tomatoes, diced", "Tomato"),
    ("tomato", "Roma Tomatoes"),
    ("potatoes", "Potato"),
    ("1 potato", "Russet Potatoes"),
    ("berries", "Mixed Berries"),
    ("fresh blueberries", "Blueberry"),
    ("strawberry", "Strawberries"),
    ("peaches", "Peach"),
    ("peach", "Peaches"),
    ("cherries", "Cherry"),
    ("radishes", "Radish"),
    ("asparagus", "Asparagus"),
    ("quiche", "Quiches"),
    ("jalapeño", "Jalapeno"),
])
def test_plural_and_singular_are_the_same_food(ingredient, stock):
    assert _owns(_ingredient_keys(ingredient), _food_key(stock))


# A word that names a different food blocks the match on either side.
@pytest.mark.parametrize("ingredient,stock", [
    ("cream", "Cream Cheese"),
    ("cream cheese", "Heavy Cream"),
    ("cream", "Vanilla Ice Cream"),
    ("heavy cream", "Vanilla Ice Cream"),
    ("black pepper", "Jalapeno Peppers"),
    ("pepper", "Serrano Peppers"),
    ("ground black pepper", "Chili Peppers"),
    ("potatoes", "Sweet Potatoes"),
    ("sugar", "Brown Sugar"),
    ("brown sugar", "Sugar"),
    ("sugar", "Powdered Sugar"),
    ("sugar", "Coconut Sugar"),
    ("rice", "Brown Rice"),
    ("self-rising flour", "All Purpose Flour"),
    ("ground beef", "Beef Chuck Roast"),
    ("beef", "Ground Beef"),
    ("ground chicken", "Chicken Breast"),
    ("chicken", "Chicken Broth"),
])
def test_identity_words_keep_foods_apart(ingredient, stock):
    assert not _owns(_ingredient_keys(ingredient), _food_key(stock))


# Brand, flavor and prep words on the stock side are fine; names that only
# differ by a variety, a synonym, or how the food was cut still match.
@pytest.mark.parametrize("ingredient,stock", [
    ("eggs", "Large Brown Eggs"),
    ("greek yogurt", "Chobani Vanilla Greek Yogurt"),
    ("all-purpose flour", "Flour"),
    ("flour", "King Arthur All-Purpose Flour"),
    ("heavy cream", "Cream"),
    ("cream", "Heavy Whipping Cream"),
    ("whipping cream", "Cream"),
    ("skim milk", "Milk"),
    ("chicken broth", "Chicken Stock"),
    ("chicken stock", "Chicken Broth"),
    ("1 tsp ground cumin", "Cumin"),
    ("cumin", "Ground Cumin"),
    ("ground ginger", "Ginger"),
    ("broccoli florets", "Broccoli"),
    ("2 tbsp lemon juice", "Lemons"),
    ("lemon juice", "Lemon Juice"),
    ("zest of 1 lime", "Limes"),
    ("lime zest", "Limes"),
    ("black pepper", "Pepper"),
    ("sugar", "Domino Granulated Sugar"),
    ("ice cream", "Vanilla Ice Cream"),
    ("ground beef", "Ground Beef"),
])
def test_brand_and_prep_words_still_match(ingredient, stock):
    assert _owns(_ingredient_keys(ingredient), _food_key(stock))


def test_apple_juice_stock_is_still_not_apples():
    assert not _owns(_ingredient_keys("apples"), _food_key("Apple Juice"))
    assert not _owns(_ingredient_keys("lemon"), _food_key("Lemon Juice"))


@pytest.mark.parametrize("plural,singular", [
    ("tomatoes", "tomato"), ("potatoes", "potato"), ("berries", "berry"),
    ("peaches", "peach"), ("dishes", "dish"), ("boxes", "box"),
    ("glasses", "glass"), ("olives", "olive"), ("cookies", "cookie"),
])
def test_singular_folds_both_forms_alike(plural, singular):
    assert _singular(plural) == _singular(singular)


@pytest.mark.parametrize("word", ["glass", "asparagus", "hummus", "swiss",
                                  "boneless", "couscous"])
def test_singular_keeps_words_that_only_look_plural(word):
    assert _singular(word) == word
