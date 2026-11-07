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
import unicodedata
from functools import lru_cache
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


_VOWELS = frozenset("aeiou")


@lru_cache(maxsize=8192)
def _singular(word: str) -> str:
    """Fold a word to one form shared by its singular and plural, so
    "tomatoes" and "tomato", "berries" and "berry", "peaches" and "peach" all
    compare equal. The folded form is a key, not always a real word: a final
    consonant-plus-y becomes "ie" ("berry" and "berries" both give "berrie").
    Words that end in ss, us or is keep their s ("glass", "asparagus")."""
    w = word
    if len(w) < 4:
        return w
    if w.endswith("y") and w[-2] not in _VOWELS:
        return w[:-1] + "ie"
    if w.endswith("che"):
        return w[:-1]  # "quiche" folds to "quich", like "quiches"
    if w.endswith(("ches", "shes", "xes", "sses")):
        return w[:-2]
    if w.endswith("oes"):
        return w[:-2]
    if w.endswith(("ss", "us", "is")):
        return w
    if w.endswith("s"):
        return w[:-1]
    return w


def _fold(words) -> set[str]:
    """A word list folded the way names are (see _singular)."""
    return {_singular(w) for w in words}


def _words(text: str) -> list[str]:
    """Lowercase letter runs of ``text``, accents dropped ("jalapeño" reads
    as "jalapeno")."""
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.findall(r"[a-z]+", text)


@lru_cache(maxsize=8192)
def _tokens_cached(text: str) -> frozenset[str]:
    return frozenset(_singular(w) for w in _words(text)
                     if len(w) >= 3 and w not in _STOP_WORDS)


def _tokens(text: str) -> set[str]:
    return set(_tokens_cached(text or ""))


# One food, several names: US and UK English plus a few other common pairs.
# The token matcher only sees shared words, so without these "scallions" never
# finds the green onions in stock. Every phrase in a group gives a name the
# group's marker token, which only another name for the same food carries.
# Only true synonyms belong here. Related foods (butter and margarine, baking
# soda and baking powder, chicken and turkey) stay apart; a small embedding
# model was tried for this and could not tell the two kinds of pair apart
# (FoodAssistant-vq81e). "Coriander" alone is left out because in a US recipe
# it means the seed spice.
_SYNONYM_GROUPS = (
    ("scallion", "green onion", "spring onion"),
    ("cilantro", "fresh coriander", "coriander leaf", "coriander leave"),
    ("chickpea", "garbanzo bean", "garbanzo", "chick pea"),
    ("tahini", "tahini paste", "tahina", "sesame paste"),
    ("zucchini", "courgette"),
    ("eggplant", "aubergine", "brinjal"),
    ("bell pepper", "capsicum", "sweet pepper"),
    ("ground beef", "minced beef", "beef mince", "hamburger meat"),
    ("ground pork", "minced pork", "pork mince"),
    ("ground turkey", "minced turkey", "turkey mince"),
    ("ground lamb", "minced lamb", "lamb mince"),
    ("heavy cream", "double cream", "whipping cream", "heavy whipping cream"),
    ("powdered sugar", "confectioner sugar", "icing sugar"),
    ("superfine sugar", "caster sugar", "castor sugar"),
    ("arugula", "rocket"),
    ("shrimp", "prawn"),
    ("cornstarch", "corn starch", "cornflour"),
    ("baking soda", "bicarbonate of soda", "bicarb soda", "sodium bicarbonate"),
    ("all purpose flour", "plain flour", "ap flour"),
    ("self rising flour", "self raising flour"),
    ("snow pea", "mangetout", "mange tout"),
    ("rutabaga", "swede"),
    ("red pepper flake", "chili flake", "chilli flake", "crushed red pepper"),
    ("golden raisin", "sultana"),
    ("beet", "beetroot"),
    ("fava bean", "broad bean"),
    ("lima bean", "butter bean"),
    ("navy bean", "haricot bean"),
    ("green bean", "string bean", "french bean"),
    ("canola oil", "rapeseed oil"),
    ("skim milk", "skimmed milk"),
    ("bok choy", "pak choi", "bok choi"),
    ("daikon", "mooli"),
    ("broth", "stock"),
    ("ground chicken", "minced chicken", "chicken mince"),
)

# Two-word foods that are not a kind of their last word: cream cheese is not
# cream, ice cream is not cream. Each reads as one word of its own.
_FIXED_PHRASES = ("cream cheese", "ice cream")

# Synonym groups that are still a kind of one plain food: a recipe's "heavy
# cream" is met by a carton labeled just "Cream", and a recipe's "flour" by a
# bag of all-purpose. These keep the plain word as their head.
_MARKER_BASE = {
    "~heavy_cream": "cream",
    "~all_purpose_flour": "flour",
    "~skim_milk": "milk",
}


def _phrase_words(text: str) -> tuple[str, ...]:
    return tuple(_singular(w) for w in _words(text))


def _build_synonym_index() -> dict[str, tuple]:
    """First word of each synonym phrase -> its (phrase, marker) pairs, the
    longest phrase first so it wins at each position."""
    pairs = [(_phrase_words(phrase), f"~{group[0].replace(' ', '_')}")
             for group in _SYNONYM_GROUPS + tuple((p,) for p in _FIXED_PHRASES)
             for phrase in group]
    index: dict[str, list] = {}
    for phrase, marker in sorted(pairs, key=lambda pm: -len(pm[0])):
        index.setdefault(phrase[0], []).append((phrase, marker))
    return {k: tuple(v) for k, v in index.items()}


_SYNONYM_INDEX = _build_synonym_index()


def _with_synonyms(words: tuple[str, ...]) -> list[str]:
    """``words`` with every synonym phrase replaced by its group marker, so
    "green onion" and "scallion" both become one "~scallion" word. A marker
    for a kind of a plain food is followed by that food's word, so "heavy
    cream" reads as "~heavy_cream cream"."""
    out, i = [], 0
    while i < len(words):
        for phrase, marker in _SYNONYM_INDEX.get(words[i], ()):
            n = len(phrase)
            if words[i:i + n] == phrase:
                out.append(marker)
                if marker in _MARKER_BASE:
                    out.append(_MARKER_BASE[marker])
                i += n
                break
        else:
            out.append(words[i])
            i += 1
    return out


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


# The staple and measure lists are compared with folded tokens, so they are
# folded the same way ("pinches" and "pinch" are one word).
_STAPLE_TOKENS = _fold(_STAPLE_TOKENS)
_STAPLE_GLUE_TOKENS = _fold(_STAPLE_GLUE_TOKENS)
_FREEBIE_TOKENS = _fold(_FREEBIE_TOKENS)
_MEASURE_TOKENS = _fold(_MEASURE_TOKENS)


# ── Same-food test (stricter matching) ───────────────────────────────────────
#
# One shared word used to be enough to count an ingredient as owned, so
# "baking powder" found the baking soda and "chicken" found the chicken stock.
# Now the two names must share their head word (the last real word: "stock"
# in "chicken stock"), the recipe may only add harmless variety words
# ("yellow onion" is still an onion), and neither name may carry a word that
# changes what the food is ("coconut milk" is not milk). Any other word on
# the stock side (a brand, a flavor: "Chobani Vanilla Greek Yogurt") is fine.

# Preparation, size and measurement words that say nothing about the food.
# "Ground" is here so ground cumin is cumin; the ground meats are synonym
# groups of their own, read before this list applies, so ground beef stays
# apart from a beef roast.
_PREP_WORDS = _MEASURE_TOKENS | {
    "fresh", "freshly", "frozen", "chopped", "diced", "sliced", "minced",
    "grated", "shredded", "fine", "finely", "coarse", "coarsely", "large",
    "small", "medium", "jumbo", "whole", "raw", "ripe", "peeled", "seeded",
    "deseeded", "trimmed", "rinsed", "drained", "softened", "melted", "cold",
    "warm", "room", "temperature", "divided", "plus", "more", "about",
    "optional", "taste", "cut", "into", "inch", "cube", "cubed", "thinly",
    "thick", "thin", "roughly", "halved", "quartered", "boneless", "skinless",
    "organic", "canned", "extra", "virgin", "good", "quality", "packed",
    "lightly", "beaten", "cooked", "uncooked", "leftover", "sifted",
    "heaping", "level", "serving", "garnish", "needed", "each",
    "approximately", "few", "some", "store", "bought", "homemade", "mashed",
    "pitted", "toasted", "roasted", "dried", "baby", "unsalted", "salted",
    "low", "sodium", "fat", "free", "reduced", "kosher", "pure", "natural",
    "head", "sprig", "stalk", "bunch", "clove", "ground",
}

# Words a recipe adds to name a variety of the same food. Only these may
# appear in the ingredient without appearing in the stock name.
_VARIETY_WORDS = {
    "yellow", "white", "red", "green", "purple", "black", "brown", "golden",
    "russet", "yukon", "gold", "roma", "vine", "heirloom", "granny", "smith",
    "gala", "honeycrisp", "english", "persian", "hothouse", "italian", "flat",
    "leaf", "curly", "greek", "regular", "long", "grain", "jasmine",
    "basmati", "arborio", "vidalia", "spanish", "light", "dark", "plain",
}

# Words that turn one food into a different one when they sit in front of
# it: coconut milk, peanut butter, sour cream, corn syrup, jalapeno peppers.
# Never ignored on either side.
_IDENTITY_WORDS = {
    "coconut", "almond", "oat", "soy", "soya", "rice", "cashew", "peanut",
    "sesame", "hazelnut", "goat", "sour", "condensed", "evaporated",
    "chocolate", "corn", "maple", "garlic", "onion", "apple", "cream",
    "buttermilk", "powdered", "ice", "bell", "jalapeno", "chili", "chile",
    "chilli", "habanero", "poblano", "serrano", "chipotle", "cayenne",
    "banana", "ghost", "scotch", "bonnet", "anaheim", "cubanelle", "thai",
}

# Identity words that only matter in front of one food: brown sugar is not
# sugar, a sweet potato is not a potato, but brown eggs are still eggs.
_HEAD_IDENTITY = {
    "sugar": {"brown", "coconut", "cane", "turbinado", "demerara", "muscovado",
              "palm", "date"},
    "potato": {"sweet"},
    "rice": {"brown", "wild", "sticky", "glutinous"},
}

# Cuts and class nouns that may trail the real food word: chicken breast is
# chicken, cheddar cheese is cheddar, broccoli florets are broccoli.
_TRAILING_WORDS = {
    "breast", "thigh", "drumstick", "wing", "leg", "tender", "tenderloin",
    "fillet", "filet", "loin", "chop", "cutlet", "cheese", "leaf", "leave",
    "floret",
}

# Names are compared after plurals fold (see _singular), so the word lists
# are folded the same way.
_PREP_WORDS = _fold(_PREP_WORDS)
_VARIETY_WORDS = _fold(_VARIETY_WORDS)
_IDENTITY_WORDS = _fold(_IDENTITY_WORDS)
_HEAD_IDENTITY = {_singular(k): _fold(v) for k, v in _HEAD_IDENTITY.items()}
_TRAILING_WORDS = _fold(_TRAILING_WORDS)
_STOP_FOLDED = _fold(_STOP_WORDS)

_JUICE_OF = re.compile(r"\b(?:juice|zest)\s+of\b")
_PARENS = re.compile(r"\([^)]*\)")
# "Lemon juice" in a recipe is met by whole lemons as well as a bottle.
_CITRUS_PART = re.compile(r"\b(lemons?|limes?|oranges?)\s+(?:juice|zest)\b")


@lru_cache(maxsize=8192)
def _food_key(text: str) -> tuple[frozenset, str] | None:
    """(food words, head word) for one name, or None when nothing is left."""
    text = _PARENS.sub(" ", _JUICE_OF.sub(" ", (text or "").lower()))
    main = text.split(",")[0]
    for candidate in (main, text):
        words = [w for w in _with_synonyms(_phrase_words(candidate))
                 if w.startswith("~") or (len(w) >= 3 and w not in _STOP_FOLDED
                                          and w not in _PREP_WORDS)]
        while len(words) > 1 and words[-1] in _TRAILING_WORDS:
            words.pop()
        if words:
            return frozenset(words), words[-1]
    return None


def _same_food(ing_key, stock_key) -> bool:
    """True when an ingredient and a stock item name the same food."""
    if not ing_key or not stock_key:
        return False
    ing, head = ing_key
    stock, stock_head = stock_key
    if head != stock_head:
        return False
    ing_extra = ing - stock
    stock_extra = stock - ing
    identity = _IDENTITY_WORDS | _HEAD_IDENTITY.get(head, set())
    if (ing_extra | stock_extra) & identity:
        return False
    # Two different kinds of one food (self-rising and all-purpose flour).
    if (any(w in _MARKER_BASE for w in ing_extra)
            and any(w in _MARKER_BASE for w in stock_extra)):
        return False
    return not (ing_extra - _VARIETY_WORDS - _MARKER_BASE.keys())


@lru_cache(maxsize=8192)
def _ingredient_keys_cached(text: str) -> tuple:
    parts = re.split(r"\bor\b|/", text)
    keys = [k for k in (_food_key(p) for p in parts) if k]
    keys = keys or [k for k in [_food_key(text)] if k]
    if _CITRUS_PART.search(text.lower()):
        whole = _food_key(_CITRUS_PART.sub(r"\1", text.lower()))
        if whole and whole not in keys:
            keys.append(whole)
    return tuple(keys)


def _ingredient_keys(text: str) -> list:
    """One food key per alternative: "butter or margarine" offers both, and
    "lemon juice" offers the juice and the lemons."""
    return list(_ingredient_keys_cached(text or ""))


def _owns(ing_keys: list, stock_key) -> bool:
    return any(_same_food(k, stock_key) for k in ing_keys)


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
_STAPLE_DESCRIPTORS = _fold(_STAPLE_DESCRIPTORS)


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
        # A product kept in several locations has one stock row per location;
        # what covers the recipe is the product's total (#73).
        amount = float(item.get("product_amount", item.get("amount")))
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
        key = _food_key(s["name"])
        if key:
            inv.append((key, s))
    by_head = _index_by_head(inv, lambda pair: pair[0])
    owned: list[str] = []
    needed: list[str] = []
    for ing in ingredients or []:
        text = _ingredient_text(ing).strip()
        ing_toks = _tokens(text)
        if not text or not ing_toks or ing_toks <= _FREEBIE_TOKENS:
            continue
        ing_keys = _ingredient_keys(text)
        in_stock = any(
            _owns(ing_keys, inv_key)
            and _stock_covers_amount(ing, item, units, conversions) is not False
            for inv_key, item in _candidates(by_head, ing_keys))
        if in_stock or _is_staple_ingredient(ing_toks):
            owned.append(text)
        else:
            needed.append(text)
    return {"owned": owned, "needed": needed}


def _index_by_head(inv: list, key_of) -> dict[str, list]:
    """Stock entries grouped by their food key's head word, each group in
    stock order. Two names can only be the same food when their heads agree,
    so an ingredient only needs to look at its own head's group."""
    index: dict[str, list] = {}
    for pos, entry in enumerate(inv):
        key = key_of(entry)
        if key:
            index.setdefault(key[1], []).append((pos, entry))
    return index


def _candidates(by_head: dict, ing_keys: list) -> list:
    """The stock entries sharing a head with any of ``ing_keys``, in stock
    order (so the first match is the same one a full scan would find)."""
    groups = [by_head[h] for h in {k[1] for k in ing_keys} if h in by_head]
    if len(groups) == 1:
        return [entry for _pos, entry in groups[0]]
    merged = sorted((pair for g in groups for pair in g), key=lambda pe: pe[0])
    return [entry for _pos, entry in merged]


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
            inv.append({"name": s["name"], "key": _food_key(s["name"]),
                        "days_remaining": s.get("days_remaining"),
                        "perishable": _is_perishable(s),
                        "product_id": s.get("product_id"),
                        "amount": s.get("amount"),
                        "product_amount": s.get("product_amount", s.get("amount")),
                        "unit": s.get("unit")})
    by_head = _index_by_head(inv, lambda s: s["key"])

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
            ing_keys = _ingredient_keys(text)
            hit = None
            short_hit = None
            for s in _candidates(by_head, ing_keys):
                if not _owns(ing_keys, s["key"]):
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
