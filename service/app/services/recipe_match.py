"""Recipe-versus-stock matching, shared by every recipe backend.

Decides which recipes you can cook from what is on hand (classify_recipes),
which of one recipe's ingredients you already own and which you need to buy
(partition_recipe_ingredients), and which ingredients count as pantry staples.
Works on the Mealie-style read shapes both the native recipe store and a
Mealie install answer with, so nothing here depends on which backend holds
the recipes. Pure functions apart from the staples file cache.
"""
import math
import re
from pathlib import Path

from ..config import settings
from . import recipe_source
from .units import conversion_factor


# ── Quantity parsing ─────────────────────────────────────────────────────────

def _to_quantity(value) -> float | None:
    """Coerce a model's quantity into a float, or None when there is no amount.

    Tolerates numbers, numeric strings, simple fractions ("1/2"), and mixed
    numbers ("1 1/2"); anything unrecognized becomes None so "to taste" style
    lines stay amount-free rather than guessing a number.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _sane_quantity(float(value))
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return _sane_quantity(float(text))
    except ValueError:
        pass
    total = 0.0
    matched = False
    for part in text.split():
        try:
            if "/" in part:
                num, den = part.split("/", 1)
                total += float(num) / float(den)
            else:
                total += float(part)
            matched = True
        except (ValueError, ZeroDivisionError):
            return None
    return _sane_quantity(total) if matched else None


# No kitchen amount comes near this; anything larger is a model or parse error.
_MAX_QUANTITY = 10000


def _sane_quantity(value: float) -> float | None:
    """The amount when it is a finite, non-negative number below
    _MAX_QUANTITY, else None. "inf", "nan" and 1e999 all parse as floats, and
    would otherwise be written into Mealie or the recipe store as an amount."""
    if not math.isfinite(value) or value < 0 or value >= _MAX_QUANTITY:
        return None
    return value


# ── Recipe ↔ inventory matching ──────────────────────────────────────────────

_STOP_WORDS = {
    "the", "and", "with", "for", "fresh", "large", "small", "medium", "cup",
    "cups", "tbsp", "tsp", "oz", "lb", "gram", "grams", "ml", "can", "cans",
    "package", "bag", "box", "jar", "bottle", "piece", "pieces", "of", "to",
    "or", "optional", "taste", "chopped", "diced", "sliced", "minced",
}


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z]+", (text or "").lower())
    return {w.rstrip("s") for w in words if len(w) >= 3 and w not in _STOP_WORDS}


def _ingredient_text(ing: dict) -> str:
    food = ing.get("food") or {}
    return food.get("name") or ing.get("display") or ing.get("note") or ""


# Things assumed on hand even if not tracked in inventory.
# Tier 2 ("with pantry staples") recipes may use these freely.
_STAPLE_TOKENS = {
    "egg", "butter", "flour", "sugar", "salt", "pepper", "oil", "olive",
    "milk", "garlic", "onion", "vinegar", "baking", "soda", "vanilla",
    "honey", "mustard", "ketchup", "mayonnaise", "mayo", "soy",
    # common dried spices
    "cumin", "paprika", "oregano", "cinnamon", "nutmeg", "thyme",
    "rosemary", "cayenne", "turmeric", "curry",
}

# Descriptor words allowed alongside staples ("brown sugar", "ground pepper",
# "vanilla extract") without making e.g. "coconut milk" count as milk: an
# ingredient is a staple only if ALL its tokens are staples or descriptors.
_STAPLE_GLUE_TOKENS = {
    "sauce", "powder", "ground", "white", "brown", "black", "red", "dried",
    "extract", "granulated", "unsalted", "salted", "vegetable", "canola",
    "sunflower", "virgin", "extra", "light", "dark", "sea", "kosher", "whole",
    "skim", "plain", "all", "purpose", "self", "raising", "clove", "cloves",
}

# Water (in any temperature) never counts against a recipe.
_FREEBIE_TOKENS = {"water", "ice", "boiling", "warm", "cold", "hot", "tap"}

# Measurement, quantity, and packaging words that ride along in a real recipe
# ingredient ("3 tablespoons unsalted butter", "1 teaspoon kosher salt") but say
# nothing about what the food is. Ignored when deciding if an ingredient is a
# staple, so a verbose ingredient still matches "butter"/"salt"/"olive oil"
# instead of falling out of the "with pantry staples" tier (the reason that tier
# often came back empty). Stock matching is unaffected: it intersects on the food
# word regardless of these.
_MEASURE_TOKENS = {
    "teaspoon", "teaspoons", "tsp", "tablespoon", "tablespoons", "tbsp", "tbs",
    "cup", "cups", "ounce", "ounces", "oz", "pound", "pounds", "lb", "lbs",
    "gram", "grams", "gm", "gms", "kilogram", "kilograms", "kg",
    "milliliter", "milliliters", "ml", "liter", "liters", "litre", "litres",
    "pint", "pints", "quart", "quarts", "gallon", "gallons",
    "pinch", "pinches", "dash", "dashes", "handful", "splash", "drizzle",
    "can", "cans", "package", "packages", "pkg", "packet", "packets",
    "jar", "jars", "box", "boxes", "bag", "bags", "bottle", "bottles",
    "container", "containers", "slice", "slices", "piece", "pieces",
    "sprig", "sprigs", "stalk", "stalks", "bunch", "bunches", "knob",
}


def _load_staples_file() -> list[frozenset[str]] | None:
    """Read staples.txt and return each line as a frozen token set.

    Uses phrase-level matching rather than a merged token soup so that
    "Chicken stock" in the file does NOT make bare "chicken" a staple.

    Search order:
      1. <data_dir>/staples.txt : user-customisable, gitignored volume file
      2. <app>/data/staples_default.txt: bundled default, shipped with the image
    Returns None only when both files are absent or yield no phrases.
    """
    candidates = [
        Path(settings.data_dir) / "staples.txt",
        Path(__file__).parent.parent / "data" / "staples_default.txt",
    ]
    for staples_path in candidates:
        if not staples_path.exists():
            continue
        phrases: list[frozenset[str]] = []
        for line in staples_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                toks = _tokens(line)
                if toks:
                    phrases.append(frozenset(toks))
        if phrases:
            return phrases
    return None


# Cached list of phrase token-sets built from staples.txt (invalidated on save).
# Per-process scope is acceptable: the file changes only through a settings
# save, which calls reset_staple_cache() in the same process that serves it.
_staple_phrases_cache: list[frozenset[str]] | None = None
_staple_phrases_loaded: bool = False


def _active_staple_phrases() -> list[frozenset[str]] | None:
    """Phrase token-sets from staples.txt, loaded once and cached."""
    global _staple_phrases_cache, _staple_phrases_loaded
    if not _staple_phrases_loaded:
        _staple_phrases_cache = _load_staples_file()
        _staple_phrases_loaded = True
    return _staple_phrases_cache


def reset_staple_cache() -> None:
    """Invalidate the staples file cache (call after settings save)."""
    global _staple_phrases_loaded
    _staple_phrases_loaded = False


def _active_staple_tokens() -> set[str]:
    """Fuzzy staple token set: settings UI field → built-in fallback.

    The staples.txt file is handled separately via phrase matching in
    classify_recipes (see _active_staple_phrases) to avoid false positives
    like "chicken" matching because "chicken stock" is in the file.
    """
    if settings.staple_items.strip():
        toks: set[str] = set()
        for item in settings.staple_items.split(","):
            toks |= _tokens(item)
        if toks:
            return toks
    return _STAPLE_TOKENS


# Modifier/packaging tokens stripped from phrase token sets before matching.
# "canned chickpeas" → core tokens {"chickpea"}, so "chickpeas" in a recipe matches.
# "chicken stock" → core tokens {"chicken", "stock"} (stock is NOT a modifier),
# so bare "chicken" does NOT match.
_PHRASE_MODIFIERS = {
    "canned", "dried", "smoked", "ground", "crushed", "whole",
    "hard", "dry", "extra", "virgin", "unsalted", "salted",
    "granulated", "confectioner", "table", "white", "brown",
    "black", "red", "grating", "large", "small", "medium",
}


# Generic descriptor words a recipe may tack onto a staple without changing
# what it is: "parmesan cheese" is still the Parmesan staple, "grated parmesan"
# and "fresh garlic" likewise. These are allowed as EXTRA ingredient tokens
# beyond a staple phrase's core. A distinct food word (e.g. "coconut" in
# "coconut milk") is deliberately NOT here, so "coconut milk" stays non-staple.
_STAPLE_DESCRIPTORS = (
    _PHRASE_MODIFIERS
    | _STAPLE_GLUE_TOKENS
    | _MEASURE_TOKENS
    | {
        "cheese", "fresh", "chopped", "diced", "sliced", "minced", "grated",
        "shredded", "freshly", "fine", "finely", "coarse", "coarsely", "good",
        "quality", "best", "pure", "raw", "flaky", "fillet", "fillets",
        "leaf", "leave", "stick", "stalk", "head", "bunch",
    }
)


def _phrase_core(phrase: frozenset[str]) -> frozenset[str]:
    """Return phrase tokens with modifier-only tokens removed."""
    core = phrase - _PHRASE_MODIFIERS
    return core if core else phrase   # keep original if modifiers consumed everything


def _is_staple_ingredient(ing_toks: set[str]) -> bool:
    """True if the ingredient tokens match any active staple definition.

    Two pathways:
    1. Fuzzy token check: all ingredient tokens are staple/glue/freebie tokens
       (handles loose pantry items like "flour", "unsalted butter", "table salt").
    2. Phrase core match: a staple phrase's core tokens are contained in the
       ingredient's tokens, and any leftover ingredient tokens are benign
       descriptors. "chickpeas" matches "canned chickpeas" and "parmesan cheese"
       matches "Parmesan", but bare "chicken" does NOT match "chicken stock"
       (the phrase carries an extra token the ingredient lacks) and "coconut
       milk" does NOT match "Milk" ("coconut" is not a descriptor).
    """
    staple_toks = _active_staple_tokens()
    # Pathway 1: original fuzzy check. Measurement/quantity words are ignored so
    # a verbose ingredient ("3 tablespoons unsalted butter") still matches.
    if (ing_toks & staple_toks
            and ing_toks <= (staple_toks | _STAPLE_GLUE_TOKENS | _FREEBIE_TOKENS | _MEASURE_TOKENS)):
        return True
    # Pathway 2: phrase containment match (requires file to be present).
    # A staple phrase matches when its core tokens are all present in the
    # ingredient AND every leftover ingredient token is a benign descriptor
    # (prep words, packaging, "cheese", etc.). This lets real recipe ingredients
    # carry descriptors the file phrase omits ("parmesan cheese" -> "Parmesan",
    # "grated parmesan" -> "Parmesan") while still rejecting "chicken" against
    # "chicken stock" (the phrase has the extra token "stock", not the
    # ingredient) and "coconut milk" against "Milk" ("coconut" is not a
    # descriptor). Skipped when the settings field is set: that list replaces
    # the file.
    if settings.staple_items.strip():
        return False
    phrases = _active_staple_phrases()
    if phrases:
        ing_core = ing_toks - _PHRASE_MODIFIERS
        if ing_core:
            for phrase in phrases:
                phrase_core = _phrase_core(phrase)
                if phrase_core <= ing_core and (ing_core - phrase_core) <= _STAPLE_DESCRIPTORS:
                    return True
    return False


def _stock_covers_amount(ing: dict, item: dict,
                         units: list[dict] | None,
                         conversions: list[dict] | None) -> bool | None:
    """Whether a stock row's amount covers the ingredient's own amount.

    Answers True or False only when the question is actually answerable: the
    conversion tables were provided, the ingredient carries a numeric quantity
    and a unit name, the stock row knows its product id, amount, and stock
    unit, and Grocy can convert between the two units (units.conversion_factor:
    same name, a product-specific or global row, or one transitive hop).
    Anything else returns None, which keeps the name-only matching exactly as
    before, so a recipe is never penalized for units nobody defined.
    """
    if units is None or conversions is None or not isinstance(ing, dict):
        return None
    qty = _to_quantity(ing.get("quantity"))
    if not qty or qty <= 0:
        return None
    unit = ing.get("unit")
    ing_unit = unit.get("name") if isinstance(unit, dict) else unit
    if not str(ing_unit or "").strip():
        return None
    if not item.get("product_id") or not str(item.get("unit") or "").strip():
        return None
    try:
        amount = float(item.get("amount"))
    except (TypeError, ValueError):
        return None
    factor = conversion_factor(item.get("product_id"), ing_unit,
                               item.get("unit"), units, conversions)
    if factor is None:
        return None
    return amount + 1e-9 >= qty * factor


def partition_recipe_ingredients(ingredients: list[dict], stock: list[dict],
                                 units: list[dict] | None = None,
                                 conversions: list[dict] | None = None) -> dict:
    """Split one recipe's ingredients into what you own and what to buy.

    Reuses the exact matching the suggestion ranker already does: an ingredient
    counts as owned when its food word matches an inventory item (in Grocy
    stock) OR it is on your staples list (the things you always keep on hand);
    everything left is what you actually need to buy. Water and other freebies
    never land on either list. Pure and shared so the shopping-list route and
    its tests agree on the same answer.

    When Grocy's quantity units and conversion rows are passed, a name match is
    additionally checked by amount: an ingredient whose converted amount
    exceeds what is in stock counts as needed, not owned, so "200 g" against a
    half-empty bag lands on the list. Without a resolvable conversion (or
    without the tables) the match stays name-only, exactly as before.

    Returns {"owned": [...], "needed": [...]} preserving the recipe's order.
    """
    inv = []
    for s in stock or []:
        toks = _tokens(s["name"])
        if toks:
            inv.append((toks, s))
    owned: list[str] = []
    needed: list[str] = []
    for ing in ingredients or []:
        text = _ingredient_text(ing).strip()
        ing_toks = _tokens(text)
        if not text or not ing_toks or ing_toks <= _FREEBIE_TOKENS:
            continue
        in_stock = any(
            ing_toks & inv_toks
            and _stock_covers_amount(ing, item, units, conversions) is not False
            for inv_toks, item in inv)
        if in_stock or _is_staple_ingredient(ing_toks):
            owned.append(text)
        else:
            needed.append(text)
    return {"owned": owned, "needed": needed}


def _is_perishable(stock_item: dict) -> bool:
    """Refrigerated items and anything expiring within the configured window."""
    if stock_item.get("storage_bucket") == "refrigerated":
        return True
    d = stock_item.get("days_remaining")
    return d is not None and d <= settings.perishable_days


def classify_recipes(recipes: list[dict], stock: list[dict],
                     top_per_tier: int = 8,
                     units: list[dict] | None = None,
                     conversions: list[dict] | None = None) -> dict[str, list[dict]]:
    """Sort recipes into three cookability tiers against current inventory.

    ready   : every ingredient matches an item in stock
    staples : in stock + common pantry staples (eggs, butter, flour, ...)
    shopping: uses at least one perishable stock item but needs a shop run

    Recipes that need shopping without using any perishables are dropped:
    they don't help eat down the inventory.

    When Grocy's quantity units and conversion rows are passed, a name match is
    additionally checked by amount (see _stock_covers_amount): an ingredient
    the stock cannot cover counts as unmatched, so the recipe honestly needs a
    shop run, while the stock it would eat down still earns its perishable and
    expiring credit. Without a resolvable conversion (or without the tables)
    matching stays name-only, exactly as before.
    """
    inv = []
    for s in stock:
        toks = _tokens(s["name"])
        if toks:
            inv.append({"name": s["name"], "tokens": toks,
                        "days_remaining": s.get("days_remaining"),
                        "perishable": _is_perishable(s),
                        "product_id": s.get("product_id"),
                        "amount": s.get("amount"),
                        "unit": s.get("unit")})

    soon = settings.expiring_soon_days
    tiers: dict[str, list[dict]] = {"ready": [], "staples": [], "shopping": []}
    for r in recipes:
        ingredients = r.get("recipeIngredient") or []
        if not ingredients:
            continue
        matched, staples, unmatched, expiring_used = [], [], [], []
        uses_perishable = False
        for ing in ingredients:
            text = _ingredient_text(ing).strip()
            ing_toks = _tokens(text)
            if not text or not ing_toks or ing_toks <= _FREEBIE_TOKENS:
                continue
            hit = None
            short_hit = None
            for s in inv:
                if not (ing_toks & s["tokens"]):
                    continue
                if _stock_covers_amount(ing, s, units, conversions) is False:
                    # In stock, but not enough of it once units are converted.
                    short_hit = short_hit or s
                    continue
                hit = s
                break
            if hit:
                matched.append(text)
                if hit["perishable"]:
                    uses_perishable = True
                d = hit["days_remaining"]
                if d is not None and d <= soon and hit["name"] not in expiring_used:
                    expiring_used.append(hit["name"])
            elif _is_staple_ingredient(ing_toks):
                staples.append(text)
            else:
                unmatched.append(text)
                if short_hit is not None:
                    # The recipe wants more than is on hand, so the ingredient
                    # needs buying, but cooking it still eats down the stock
                    # that IS there, so the perishable and expiring credit
                    # stays honest.
                    if short_hit["perishable"]:
                        uses_perishable = True
                    d = short_hit["days_remaining"]
                    if (d is not None and d <= soon
                            and short_hit["name"] not in expiring_used):
                        expiring_used.append(short_hit["name"])

        # A recipe with no full matches can still earn a place in the shopping
        # tier when its short-stocked ingredients use up perishables (only
        # possible with conversion data; without it this gate reads exactly as
        # the old "if not matched").
        if not matched and not uses_perishable:
            continue
        if not unmatched and not staples:
            tier = "ready"
        elif not unmatched:
            tier = "staples"
        elif uses_perishable:
            tier = "shopping"
        else:
            continue

        coverage = len(matched) / max(len(matched) + len(staples) + len(unmatched), 1)
        score = coverage + 0.35 * len(expiring_used)
        tiers[tier].append({
            "name": r.get("name"),
            "slug": r.get("slug"),
            "id": r.get("id"),
            "source": r.get("source", "mealie"),
            # Where this candidate came from, so the Cook page shows the same
            # source chip as the Recipes page (FoodAssistant-5frk). Mealie detail
            # carries orgURL, which marks an imported recipe.
            "badge": recipe_source.source_badge(
                r.get("source", "mealie"), bool(r.get("orgURL"))),
            "external_id": r.get("external_id"),
            "image": r.get("image"),
            "description": (r.get("description") or "")[:160],
            "total_ingredients": len(ingredients),
            "matched_ingredients": matched,
            "staple_ingredients": staples,
            "unmatched_ingredients": unmatched,
            "expiring_items_used": expiring_used,
            "coverage": round(coverage, 2),
            "score": round(score, 3),
        })

    for tier in tiers.values():
        tier.sort(key=lambda x: (len(x["expiring_items_used"]), x["score"]), reverse=True)
    return {k: v[:top_per_tier] for k, v in tiers.items()}
