"""Amazon affiliate product recommendations (FoodAssistant-k2kv).

Recommends common kitchen products (appliances, cookware, gadgets, storage) with
Amazon affiliate links so the project can earn from qualifying purchases. The
recommendations are tied to what the user does NOT already own (their kitchen
appliance selection) and to equipment a recipe needs but they lack.

Pure and dependency-free: the URL builder and the recommendation ranking take
plain inputs and return plain data, no I/O, so they are fully unit-testable. The
catalog uses search terms (no fabricated ASINs), so every link resolves to a
real Amazon search even if a specific product listing changes.
"""
from __future__ import annotations

import re
from urllib.parse import quote_plus

# A bare ASIN is exactly 10 alphanumeric characters (Amazon's product id). Anything
# else is treated as a free-text search term.
_ASIN_RE = re.compile(r"^[A-Za-z0-9]{10}$")


def amazon_url(query_or_asin: str, tag: str = "") -> str:
    """Build an Amazon URL for a product search term or a bare ASIN.

    A 10-char alphanumeric input is treated as an ASIN and links straight to the
    product page (``/dp/<asin>``); anything else becomes a search
    (``/s?k=<urlencoded>``). When ``tag`` is empty the URL is returned WITHOUT the
    ``tag`` query param (still a working link, just not monetized).
    """
    term = (query_or_asin or "").strip()
    tag = (tag or "").strip()
    if _ASIN_RE.match(term):
        base = f"https://www.amazon.com/dp/{term}"
        return f"{base}?tag={quote_plus(tag)}" if tag else base
    base = f"https://www.amazon.com/s?k={quote_plus(term)}"
    return f"{base}&tag={quote_plus(tag)}" if tag else base


# Curated starter catalog of common kitchen products. Each entry is
# (name, category, search term, appliance_key). ``appliance_key`` ties an item to
# a config.KITCHEN_APPLIANCE_KEYS id so ownership can be checked; items with no
# appliance_key ("") are general cookware/gadgets that are always reasonable to
# suggest. No ASINs are baked in: the search term resolves to a live listing.
# Categories: appliances, cookware, gadgets, storage.
PRODUCT_CATALOG = [
    # Appliances (mapped to owned-or-not appliance keys).
    {"name": "Air fryer", "category": "appliances", "term": "air fryer", "appliance_key": "air_fryer"},
    {"name": "Blender", "category": "appliances", "term": "countertop blender", "appliance_key": "blender"},
    {"name": "Immersion blender", "category": "appliances", "term": "immersion blender", "appliance_key": "immersion_blender"},
    {"name": "Stand mixer", "category": "appliances", "term": "stand mixer", "appliance_key": "stand_mixer"},
    {"name": "Hand mixer", "category": "appliances", "term": "hand mixer", "appliance_key": "hand_mixer"},
    {"name": "Food processor", "category": "appliances", "term": "food processor", "appliance_key": "food_processor"},
    {"name": "Slow cooker", "category": "appliances", "term": "slow cooker", "appliance_key": "slow_cooker"},
    {"name": "Pressure cooker (Instant Pot)", "category": "appliances", "term": "electric pressure cooker", "appliance_key": "pressure_cooker"},
    {"name": "Rice cooker", "category": "appliances", "term": "rice cooker", "appliance_key": "rice_cooker"},
    {"name": "Toaster oven", "category": "appliances", "term": "toaster oven", "appliance_key": "toaster_oven"},
    {"name": "Sous vide cooker", "category": "appliances", "term": "sous vide immersion cooker", "appliance_key": "sous_vide"},
    {"name": "Waffle iron", "category": "appliances", "term": "waffle maker", "appliance_key": "waffle_iron"},
    # Cookware (mostly general; a few map to appliance keys for specialty pans).
    {"name": "Cast iron skillet", "category": "cookware", "term": "cast iron skillet pre-seasoned", "appliance_key": "cast_iron"},
    {"name": "Dutch oven", "category": "cookware", "term": "enameled dutch oven", "appliance_key": "dutch_oven"},
    {"name": "Wok", "category": "cookware", "term": "carbon steel wok", "appliance_key": "wok"},
    {"name": "Nonstick frying pan", "category": "cookware", "term": "nonstick frying pan", "appliance_key": ""},
    {"name": "Stainless steel saucepan", "category": "cookware", "term": "stainless steel saucepan", "appliance_key": ""},
    {"name": "Stockpot", "category": "cookware", "term": "stainless steel stockpot", "appliance_key": ""},
    {"name": "Baking sheet (half sheet pan)", "category": "cookware", "term": "aluminum half sheet pan", "appliance_key": ""},
    {"name": "Glass baking dish (9x13)", "category": "cookware", "term": "glass baking dish 9x13", "appliance_key": ""},
    # Gadgets (general tools; a couple map to small-appliance keys).
    {"name": "Chef's knife", "category": "gadgets", "term": "chef knife 8 inch", "appliance_key": ""},
    {"name": "Cutting board", "category": "gadgets", "term": "wood cutting board", "appliance_key": ""},
    {"name": "Digital kitchen scale", "category": "gadgets", "term": "digital kitchen scale", "appliance_key": "kitchen_scale"},
    {"name": "Instant-read meat thermometer", "category": "gadgets", "term": "instant read meat thermometer", "appliance_key": "meat_thermometer"},
    {"name": "Mandoline slicer", "category": "gadgets", "term": "mandoline slicer", "appliance_key": "mandoline"},
    {"name": "Microplane zester", "category": "gadgets", "term": "microplane zester grater", "appliance_key": "microplane"},
    {"name": "Garlic press", "category": "gadgets", "term": "garlic press", "appliance_key": "garlic_press"},
    {"name": "Pasta roller", "category": "gadgets", "term": "hand crank pasta roller machine", "appliance_key": "pasta_roller"},
    {"name": "Pasta extruder", "category": "gadgets", "term": "pasta extruder machine", "appliance_key": "pasta_extruder"},
    {"name": "Measuring cups and spoons", "category": "gadgets", "term": "measuring cups and spoons set", "appliance_key": ""},
    {"name": "Mixing bowl set", "category": "gadgets", "term": "stainless steel mixing bowl set", "appliance_key": ""},
    {"name": "Silicone spatula set", "category": "gadgets", "term": "silicone spatula set", "appliance_key": ""},
    {"name": "Kitchen tongs", "category": "gadgets", "term": "stainless steel kitchen tongs", "appliance_key": ""},
    {"name": "Box grater", "category": "gadgets", "term": "box grater", "appliance_key": ""},
    {"name": "Colander", "category": "gadgets", "term": "stainless steel colander", "appliance_key": ""},
    # Storage (general; help keep tracked food fresher, on-theme for the app).
    {"name": "Glass food storage containers", "category": "storage", "term": "glass food storage containers with lids", "appliance_key": ""},
    {"name": "Airtight pantry canisters", "category": "storage", "term": "airtight pantry storage containers", "appliance_key": ""},
    {"name": "Reusable silicone food bags", "category": "storage", "term": "reusable silicone food storage bags", "appliance_key": ""},
    {"name": "Vacuum sealer", "category": "storage", "term": "food vacuum sealer machine", "appliance_key": ""},
    {"name": "Mason jars", "category": "storage", "term": "wide mouth mason jars", "appliance_key": ""},
    # Stand-mixer attachments (KitchenAid-style add-ons). Each maps to its own
    # appliance key so ownership is tracked independently of the mixer itself.
    {"name": "Pasta roller and cutter set", "category": "attachments", "term": "kitchenaid pasta roller cutter attachment set", "appliance_key": "sm_pasta_roller_cutter"},
    {"name": "Meat grinder attachment", "category": "attachments", "term": "kitchenaid meat grinder attachment", "appliance_key": "sm_meat_grinder"},
    {"name": "Spiralizer attachment", "category": "attachments", "term": "kitchenaid spiralizer attachment", "appliance_key": "sm_spiralizer"},
    {"name": "Food processor attachment", "category": "attachments", "term": "kitchenaid food processor attachment", "appliance_key": "sm_food_processor"},
    {"name": "Ice cream maker attachment", "category": "attachments", "term": "kitchenaid ice cream maker attachment", "appliance_key": "sm_ice_cream_maker"},
    {"name": "Grain mill attachment", "category": "attachments", "term": "kitchenaid grain mill attachment", "appliance_key": "sm_grain_mill"},
    {"name": "Sausage stuffer attachment", "category": "attachments", "term": "kitchenaid sausage stuffer attachment", "appliance_key": "sm_sausage_stuffer"},
]

# Display order and friendly labels for the category groupings on the Shop page.
CATEGORY_LABELS = [
    ("appliances", "Appliances"),
    ("attachments", "Stand mixer attachments"),
    ("cookware", "Cookware"),
    ("gadgets", "Gadgets and tools"),
    ("storage", "Storage"),
]


def _missing_names_to_keys(recipe_missing) -> set[str]:
    """Best-effort map of recipe "missing equipment" names to appliance keys.

    ``utensils.missing_appliances`` returns display NAMES (e.g. "Air fryer"); the
    catalog keys on appliance KEYS. We match by case-insensitive substring against
    catalog product names so a recipe's missing item surfaces its product even
    when the wording differs slightly. Pure helper.
    """
    wanted = {str(n).strip().lower() for n in (recipe_missing or []) if str(n).strip()}
    keys: set[str] = set()
    if not wanted:
        return keys
    for item in PRODUCT_CATALOG:
        if not item["appliance_key"]:
            continue
        name = item["name"].lower()
        if any(w in name or name in w for w in wanted):
            keys.add(item["appliance_key"])
    return keys


def recommendations(owned_appliance_keys, tag="", recipe_missing=None) -> list[dict]:
    """Recommended products with built affiliate URLs, best-first.

    Priority order:
      1. Equipment a recipe needs that the user lacks (``recipe_missing``).
      2. Appliances the user does NOT own (by appliance_key).
      3. Everything else (general cookware, gadgets, storage, owned appliances).

    Each returned item is the catalog dict plus ``url`` (affiliate link),
    ``reason`` (why it is recommended), and ``highlighted`` (True when the item
    is a recipe-missing or un-owned pick, so the Shop page can make it stand
    out). Pure: pass owned keys / tag / missing.
    """
    owned = {str(k) for k in (owned_appliance_keys or [])}
    missing_keys = _missing_names_to_keys(recipe_missing)

    def rank(item: dict) -> int:
        key = item["appliance_key"]
        if key and key in missing_keys:
            return 0
        if key and key not in owned:
            return 1
        return 2

    def reason(item: dict) -> str:
        key = item["appliance_key"]
        if key and key in missing_keys:
            return "A recipe you looked at needs this"
        if key and key not in owned:
            return "You have not marked this as owned"
        return "Popular kitchen pick"

    # Stand-mixer attachments are only relevant to someone who owns a stand
    # mixer, so hide that whole category otherwise (Pantry Raider).
    owns_mixer = "stand_mixer" in owned
    out = []
    for item in PRODUCT_CATALOG:
        if item.get("category") == "attachments" and not owns_mixer:
            continue
        r = rank(item)
        out.append({
            **item,
            "url": amazon_url(item["term"], tag),
            "reason": reason(item),
            # Ranks 0 (recipe-missing) and 1 (un-owned) are the picks worth
            # calling out; rank 2 is general/owned filler.
            "highlighted": r < 2,
        })
    # Stable sort by rank keeps catalog order within each tier.
    out.sort(key=rank)
    return out


def top_recommendations(owned_appliance_keys, tag="", recipe_missing=None,
                        limit=6) -> list[dict]:
    """The best ``limit`` recommendations (recipe-missing first, then un-owned).

    Drives the pinned "Recommended for you" section on the Shop page. Returns
    only highlighted items (so an all-owned kitchen yields an empty list rather
    than padding with filler), best-first, capped at ``limit``. Pure.
    """
    recs = recommendations(owned_appliance_keys, tag, recipe_missing)
    picks = [r for r in recs if r.get("highlighted")]
    return picks[: max(0, int(limit))]


def grouped_recommendations(owned_appliance_keys, tag="", recipe_missing=None) -> list[dict]:
    """Recommendations grouped by category for the Shop page.

    Returns a list of ``{category, label, products}`` in CATEGORY_LABELS order;
    within each group the products keep the recommendations() priority order, so
    the most relevant picks float to the top of their card. (The list key is
    ``products`` rather than ``items`` so it does not collide with dict.items in
    Jinja templates.) Pure.
    """
    recs = recommendations(owned_appliance_keys, tag, recipe_missing)
    by_cat: dict[str, list[dict]] = {}
    for item in recs:
        by_cat.setdefault(item["category"], []).append(item)
    groups = []
    for cat, label in CATEGORY_LABELS:
        products = by_cat.get(cat, [])
        if products:
            groups.append({"category": cat, "label": label, "products": products})
    return groups


# Required FTC / Amazon Associates disclosure, surfaced wherever affiliate links
# appear. Kept here so the wording lives in one place.
DISCLOSURE = ("Some links here are affiliate links. If you buy through them the "
              "creator may earn a small commission at no extra cost to you, which "
              "helps keep Pantry Raider free and open source. Thanks for your support.")
