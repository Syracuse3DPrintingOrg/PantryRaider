"""Detect the cookware and utensils a recipe needs (FoodAssistant-ooq3).

A light, dependency-free heuristic: scan the recipe's steps, ingredients, and
title for known equipment keywords and return a de-duplicated, ordered list of
the gear you'll likely reach for. Appliance-type items are cross-referenced
against the kitchen the user told us they have (config.KITCHEN_APPLIANCES /
settings.kitchen_appliances) so the recipe view can flag a recipe that wants
equipment they may not own. This is also the surface affiliate product
recommendations will later hang off (FoodAssistant-k2kv).

Pure: takes plain recipe text and an owned-appliance list, no I/O, so it is fully
unit-testable.
"""
from __future__ import annotations

import re

# Canonical equipment -> the keyword phrases that imply it. Order matters only
# for display (the output preserves first-seen order across this list). Each
# phrase is matched as a whole word/phrase, case-insensitive. ``appliance_key``
# links an item to a config.KITCHEN_APPLIANCES id when it is a major appliance,
# so ownership can be checked; utensils with no appliance_key are never "missing".
_EQUIPMENT = [
    # (display name, [keywords], appliance_key or None)
    ("Oven", ["oven", "bake", "baked", "baking", "roast", "roasted", "broil"], "oven"),
    ("Stovetop", ["stovetop", "stove", "saute", "sauté", "simmer", "pan-fry", "sear"], "stove"),
    ("Microwave", ["microwave"], "microwave"),
    ("Blender", ["blender", "blend", "puree", "purée", "smoothie"], "blender"),
    ("Food processor", ["food processor"], "food_processor"),
    ("Stand mixer", ["stand mixer"], "stand_mixer"),
    ("Hand mixer", ["hand mixer", "electric mixer"], "hand_mixer"),
    ("Immersion blender", ["immersion blender", "stick blender"], "immersion_blender"),
    ("Air fryer", ["air fryer", "air-fry", "air fry"], "air_fryer"),
    ("Slow cooker", ["slow cooker", "crock pot", "crockpot"], "slow_cooker"),
    ("Pressure cooker", ["pressure cooker", "instant pot"], "pressure_cooker"),
    ("Rice cooker", ["rice cooker"], "rice_cooker"),
    ("Sous vide", ["sous vide", "sous-vide"], "sous_vide"),
    ("Deep fryer", ["deep fryer", "deep-fry", "deep fry"], "deep_fryer"),
    ("Grill", ["grill", "grilled", "grilling", "barbecue", "bbq"], "grill"),
    ("Wok", ["wok", "stir-fry", "stir fry"], "wok"),
    ("Toaster", ["toaster"], "toaster"),
    ("Waffle iron", ["waffle iron", "waffle maker"], "waffle_iron"),
    ("Griddle", ["griddle"], "griddle"),
    ("Dutch oven", ["dutch oven"], "dutch_oven"),
    ("Cast iron skillet", ["cast iron", "cast-iron"], "cast_iron"),
    ("Mandoline", ["mandoline"], "mandoline"),
    ("Kitchen scale", ["kitchen scale", "weigh"], "kitchen_scale"),
    ("Meat thermometer", ["thermometer", "internal temp", "internal temperature"], "meat_thermometer"),
    ("Rolling pin", ["rolling pin", "roll out"], "rolling_pin"),
    # Plain utensils (no appliance ownership check).
    ("Chef's knife", ["knife", "chop", "dice", "mince", "slice", "julienne"], None),
    ("Cutting board", ["cutting board", "chopping board"], None),
    ("Mixing bowl", ["mixing bowl", "large bowl", "bowl"], None),
    ("Whisk", ["whisk"], None),
    ("Saucepan", ["saucepan", "sauce pan"], None),
    ("Pot", ["stockpot", "large pot", "pot", "boil", "boiling"], None),
    ("Skillet / frying pan", ["skillet", "frying pan", "fry pan", "saute pan", "sauté pan", "nonstick pan"], None),
    ("Baking sheet", ["baking sheet", "sheet pan", "cookie sheet"], None),
    ("Baking dish", ["baking dish", "casserole dish", "9x13", "8x8"], None),
    ("Spatula", ["spatula"], None),
    ("Tongs", ["tongs"], None),
    ("Wooden spoon", ["wooden spoon"], None),
    ("Colander / strainer", ["colander", "strainer", "drain", "strain"], None),
    ("Grater", ["grater", "grate", "shred"], None),
    ("Peeler", ["peeler", "peel"], None),
    ("Measuring cups", ["measuring cup", "measuring cups"], None),
    ("Ladle", ["ladle"], None),
]


def _matches(text: str, keyword: str) -> bool:
    """Whole-word/phrase, case-insensitive match of ``keyword`` in ``text``."""
    return re.search(r"(?<!\w)" + re.escape(keyword) + r"(?!\w)", text) is not None


def detect_equipment(recipe: dict) -> list[dict]:
    """Equipment a recipe likely needs, in catalog order.

    Each entry is ``{name, appliance_key}``; ``appliance_key`` is the
    KITCHEN_APPLIANCES id when the item is an owned-or-not appliance, else "".
    Scans the title, ingredient names, and step text.
    """
    parts: list[str] = [str(recipe.get("title", ""))]
    for ing in recipe.get("ingredients") or []:
        if isinstance(ing, dict):
            parts.append(str(ing.get("name", "")))
        else:
            parts.append(str(ing))
    parts.extend(str(s) for s in (recipe.get("steps") or []))
    text = " ".join(parts).lower()
    if not text.strip():
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for name, keywords, appliance_key in _EQUIPMENT:
        if name in seen:
            continue
        if any(_matches(text, kw) for kw in keywords):
            seen.add(name)
            out.append({"name": name, "appliance_key": appliance_key or ""})
    return out


def missing_appliances(equipment: list[dict], owned_keys) -> list[str]:
    """Names of detected APPLIANCES the user did not mark as owned.

    Only items with an ``appliance_key`` are checked; plain utensils are assumed
    on hand. ``owned_keys`` is settings.kitchen_appliances. An empty owned list
    means the user never set their kitchen, so nothing is flagged (avoids a wall
    of false warnings on a fresh install)."""
    owned = set(owned_keys or [])
    if not owned:
        return []
    return [e["name"] for e in equipment
            if e.get("appliance_key") and e["appliance_key"] not in owned]
