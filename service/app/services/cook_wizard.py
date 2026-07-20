"""Cook wizard option lists.

The Cook wizard (the "Help me find a recipe" guided path) walks the user
through a shallow matrix of big touch buttons: cuisine, then dish type, then a
dietary narrowing step. The button values here line up with what the existing
suggestion machinery already understands, so the wizard can hand its choices
straight to ``/mealie/suggest`` without any translation:

- Cuisine values match the Cook page's cuisine pills and the areas
  ``recipes_external._CUISINE_AREAS`` maps, so the external search filters by
  area correctly.
- Diet values match the Cook page's dietary pills, which ``/mealie/suggest``
  already forwards to the external source.

Dish type (TheMealDB-style categories) steers the optional AI result and labels
the results screen; the stock-matched suggest tiers stay the primary result
source. Everything is a plain constant so this stays pure and unit-testable.

Emoji note (mirrors the Cook page, FoodAssistant-438t): every glyph here is
drawn from Emoji 12.0 (2019) or older so it renders on older devices; these are
the same glyphs the Cook page pills already ship.
"""
from __future__ import annotations

# Broad regions first, then specific cuisines. Values and emoji mirror the Cook
# page cuisine pills so a wizard pick behaves identically to toggling the pill.
_REGIONS = [
    {"value": "Asian", "label": "Asian", "emoji": "\U0001F962"},            # 🥢
    {"value": "Mediterranean", "label": "Mediterranean", "emoji": "\U0001F959"},  # 🥙
    {"value": "European", "label": "European", "emoji": "\U0001F3F0"},       # 🏰
    {"value": "Latin American", "label": "Latin American", "emoji": "\U0001F32E"},  # 🌮
    {"value": "Middle Eastern", "label": "Middle Eastern", "emoji": "\U0001F9C6"},  # 🧆
    {"value": "American", "label": "American", "emoji": "\U0001F354"},       # 🍔
    {"value": "African", "label": "African", "emoji": "\U0001F30D"},         # 🌍
    {"value": "Caribbean", "label": "Caribbean", "emoji": "\U0001F3DD"},     # 🏝️
]

_CUISINES = [
    {"value": "Italian", "label": "Italian", "emoji": "\U0001F35D"},         # 🍝
    {"value": "French", "label": "French", "emoji": "\U0001F950"},           # 🥐
    {"value": "Greek", "label": "Greek", "emoji": "\U0001F3DB"},             # 🏛️
    {"value": "Spanish", "label": "Spanish", "emoji": "\U0001F958"},         # 🥘
    {"value": "Chinese", "label": "Chinese", "emoji": "\U0001F95F"},         # 🥟
    {"value": "Japanese", "label": "Japanese", "emoji": "\U0001F363"},       # 🍣
    {"value": "Thai", "label": "Thai", "emoji": "\U0001F336"},               # 🌶️
    {"value": "Vietnamese", "label": "Vietnamese", "emoji": "\U0001F35C"},   # 🍜
    {"value": "Indian", "label": "Indian", "emoji": "\U0001F35B"},           # 🍛
    {"value": "Mexican", "label": "Mexican", "emoji": "\U0001F32F"},         # 🌯
    {"value": "Korean", "label": "Korean", "emoji": "\U0001F969"},           # 🥩
    {"value": "British", "label": "British", "emoji": "\U0001F967"},         # 🥧
]

# Dish type / course. TheMealDB-style categories, curated to the ones a home
# cook thinks in. These steer the optional AI result and title the results.
_CATEGORIES = [
    {"value": "Breakfast", "label": "Breakfast", "emoji": "\U0001F373"},     # 🍳
    {"value": "Starter", "label": "Starter", "emoji": "\U0001F95F"},         # 🥟
    {"value": "Soup", "label": "Soup", "emoji": "\U0001F372"},               # 🍲
    {"value": "Salad", "label": "Salad", "emoji": "\U0001F957"},             # 🥗
    {"value": "Main Course", "label": "Main", "emoji": "\U0001F37D"},        # 🍽️
    {"value": "Pasta", "label": "Pasta", "emoji": "\U0001F35D"},             # 🍝
    {"value": "Seafood", "label": "Seafood", "emoji": "\U0001F364"},         # 🍤
    {"value": "Side Dish", "label": "Side", "emoji": "\U0001F954"},          # 🥔
    {"value": "Dessert", "label": "Dessert", "emoji": "\U0001F370"},         # 🍰
    {"value": "Drink", "label": "Drink", "emoji": "\U0001F379"},             # 🍹
]

# Dietary narrowing step. Values match the Cook page dietary pills so the
# suggestion request filters the same way.
_DIETS = [
    {"value": "Vegetarian", "label": "Vegetarian", "emoji": "\U0001F966"},   # 🥦
    {"value": "Vegan", "label": "Vegan", "emoji": "\U0001F331"},             # 🌱
    {"value": "Gluten Free", "label": "Gluten Free", "emoji": "\U0001F33E"}, # 🌾
    {"value": "Dairy Free", "label": "Dairy Free", "emoji": "\U0001F95B"},   # 🥛
    {"value": "Keto", "label": "Keto", "emoji": "\U0001F951"},               # 🥑
    {"value": "High Protein", "label": "High Protein", "emoji": "\U0001F4AA"},  # 💪
    {"value": "Kid Friendly", "label": "Kid Friendly", "emoji": "\U0001F9D2"},  # 🧒
]


def wizard_options() -> dict:
    """The full set of guided-path button lists for the Cook wizard.

    Pure and static: returns fresh copies so a caller (or the JSON encoder)
    cannot mutate the module constants.
    """
    return {
        "cuisines": {
            "regions": [dict(o) for o in _REGIONS],
            "cuisines": [dict(o) for o in _CUISINES],
        },
        "categories": [dict(o) for o in _CATEGORIES],
        "diets": [dict(o) for o in _DIETS],
    }
