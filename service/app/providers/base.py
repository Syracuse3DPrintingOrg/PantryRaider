import json
import logging
import math
import re
from abc import ABC, abstractmethod
from datetime import date, datetime

# Prompt for identify_barcode. A bare UPC/EAN number carries no product
# information a model can decode, so the instruction is to admit that rather
# than invent a plausible brand from the digits (which is how a real Stella
# Artois scan came back as "Campbell's"). name is null unless the model truly
# recognizes the specific code.
BARCODE_IDENTIFY_PROMPT = """
You are given only a product barcode number: "{barcode}". It was not found in the Open Food Facts database. You have no other information about the product, and you cannot see it.

You cannot compute a product from barcode digits. Only answer with a product name if you specifically and confidently recognize THIS exact barcode from training. If you are not sure, the correct answer is null: a wrong guess is worse than no guess.

Return ONLY a JSON object, no markdown, no explanation:
{{
  "name": "specific product name with brand, or null if you do not recognize this exact barcode",
  "brand": "brand name or null",
  "category": "Poultry | Meat | Seafood | Dairy | Produce | Grains | Condiments | Beverages | Snacks | Frozen | Canned | Other",
  "storage_type": "refrigerated | frozen | room_temp | dry",
  "shelf_life_days": 365
}}
""".strip()
from ..models.food import AnalysisResult, FoodCategory, FoodItem, StorageType

logger = logging.getLogger(__name__)


def parse_json_response(raw: str):
    """Parse a model's JSON reply, tolerating markdown code fences."""
    text = raw.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    return json.loads(text)


# Shared nutrition-estimate prompt + result normaliser, so every provider asks
# the same way and returns the same clean {calories, protein, carbs, fat} shape
# (FoodAssistant-e6qt). Grams for the macros, totals for the given servings.
NUTRITION_PROMPT = (
    'Estimate the nutrition for {servings} serving(s) of "{name}". Reply with ONLY '
    'compact JSON and nothing else: {{"calories": <number>, "protein": <grams>, '
    '"carbs": <grams>, "fat": <grams>}}. The numbers are the TOTAL for {servings} '
    'serving(s); use your best general estimate.'
)


def nutrition_fields(data) -> dict:
    """Coerce a model's nutrition JSON into {calories, protein, carbs, fat} of
    floats (or None). Tolerant of missing/garbled values; a non-finite number
    (json.loads reads 1e999 as inf) is None too, since it cannot be sent back
    out as JSON."""
    out: dict = {}
    for key in ("calories", "protein", "carbs", "fat"):
        value = data.get(key) if isinstance(data, dict) else None
        number = _number(value)
        out[key] = round(number, 1) if number is not None else None
    return out


# Food and receipt reply parsing, shared by every provider (gemini.py
# re-exports these names, and anthropic.py, openai.py and ollama.py import
# them from there). A model's reply is untrusted input: any field can be
# missing, null, the wrong type, out of range, or a non-finite number. Each
# field is coerced on its own and falls back to its default, so one odd value
# costs that value, never the whole photo or receipt.

_NAME_MAX = 120
_UNIT_MAX = 40
_BRAND_MAX = 80
_NOTES_MAX = 500
_MAX_QUANTITY = 10000.0
# Four digits that are not part of a longer number: the year in a date copied
# off a label as printed rather than written as YYYY-MM-DD.
_YEAR_RE = re.compile(r"(?<!\d)\d{4}(?!\d)")


def clean_text(value, limit: int) -> str | None:
    """A stripped string cut to ``limit`` characters, or None when ``value``
    is not a string or is blank."""
    if not isinstance(value, str):
        return None
    text = value.strip()[:limit].rstrip()
    return text or None


def _number(value) -> float | None:
    """A finite float from a number or a numeric string, else None. A boolean
    is not a number here, even though Python counts it as an int."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _first_dict(value) -> dict:
    """The reply as one object: a list gives its first object, and anything
    that is not an object gives {}."""
    if isinstance(value, list):
        value = next((v for v in value if isinstance(v, dict)), None)
    return value if isinstance(value, dict) else {}


def _item_name(value) -> str:
    """A non-empty name (a number counts), trimmed and capped, else Unknown."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = _number(value)
        value = f"{number:g}" if number is not None else None
    return clean_text(value, _NAME_MAX) or "Unknown"


def _quantity(value) -> float:
    """A believable amount: finite, above zero and at most 10,000, else 1."""
    number = _number(value)
    if number is None or number <= 0 or number > _MAX_QUANTITY:
        return 1.0
    return number


def _confidence(value, default: float) -> float:
    """A 0 to 1 confidence. A percentage (90) is scaled down, anything else
    out of range is clamped, and an unusable value gives the default."""
    if isinstance(value, str):
        value = value.strip().rstrip("%")
    number = _number(value)
    if number is None:
        return default
    if number > 1:
        number /= 100
    return min(1.0, max(0.0, number))


def _safe_date(value) -> date | None:
    """A date from a model reply, or None. Never raises.

    YYYY-MM-DD is what every prompt asks for, but a model reading a label
    sometimes copies the date as printed ("03/15/2027", "March 15, 2027").
    Those are read too, as long as the text carries a four-digit year: a
    two-digit year is too ambiguous to guess, and a year on its own is not a
    date. A month and year with no day ("03/2027") reads as the first of the
    month. python-dateutil does the reading; without it installed, only
    YYYY-MM-DD is understood.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    years = {int(y) for y in _YEAR_RE.findall(text)}
    if not years or not re.search(r"[0-9A-Za-z]", _YEAR_RE.sub("", text)):
        return None
    try:
        from dateutil import parser as date_parser
        parsed = date_parser.parse(text, default=datetime(2000, 1, 1))
    except Exception:  # noqa: BLE001 - an unreadable date is just no date
        return None
    # The year dateutil settled on must be the one written in the text.
    return parsed.date() if parsed.year in years else None


def _safe_storage(value) -> StorageType:
    """The storage bucket named in a reply ("Room temp" reads as room_temp),
    refrigerated when it names none."""
    if isinstance(value, str):
        key = value.strip().lower().replace(" ", "_").replace("-", "_")
        for storage in StorageType:
            if storage.value == key:
                return storage
    return StorageType.refrigerated


def _safe_category(value) -> FoodCategory:
    """The category named in a reply, matched without regard to case, Other
    when it names none."""
    if isinstance(value, str):
        key = value.strip().lower()
        for category in FoodCategory:
            if category.value.lower() == key:
                return category
    return FoodCategory.other


def _parse_item(data, default_confidence: float) -> FoodItem:
    """Build one FoodItem from a model's reply. Never raises.

    Accepts any value: a list (some models wrap the one object in an array)
    uses its first object, and anything that is not an object reads as {},
    which is an item named Unknown with every default.
    """
    data = _first_dict(data)
    return FoodItem(
        name=_item_name(data.get("name")),
        quantity=_quantity(data.get("quantity")),
        unit=clean_text(data.get("unit"), _UNIT_MAX) or "item",
        best_by_date=_safe_date(data.get("best_by_date")),
        storage_type=_safe_storage(data.get("storage_type")),
        category=_safe_category(data.get("category")),
        brand=clean_text(data.get("brand"), _BRAND_MAX),
        notes=clean_text(data.get("notes"), _NOTES_MAX),
        confidence=_confidence(data.get("confidence"), default_confidence),
    )


def _parse_receipt(data, default_confidence: float, raw: str) -> AnalysisResult:
    """Build a receipt AnalysisResult from a parsed model reply.

    Accepts the current object form ({store, purchase_date, items}) as well as
    the legacy bare array (or single object) form, so older prompts and models
    that ignore the wrapper still work. Extracts the purchase date and store
    when present and threads the date onto every item. A row that is not an
    object is skipped, and a row that still fails to build is dropped on its
    own, so one bad line never costs the rest of the receipt.
    """
    store = None
    purchased_on = None
    if isinstance(data, dict) and "items" in data:
        store = clean_text(data.get("store"), _NAME_MAX)
        purchased_on = _safe_date(data.get("purchase_date"))
        rows = data["items"]
        if isinstance(rows, dict):
            rows = [rows]
        elif not isinstance(rows, list):
            rows = []
    elif isinstance(data, dict):
        rows = [data]
    elif isinstance(data, list):
        rows = data
    else:
        rows = []

    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            item = _parse_item(row, default_confidence=default_confidence)
            item.purchased_on = purchased_on
        except Exception:  # noqa: BLE001 - drop the row, keep the receipt
            logger.warning("Skipped a receipt line the parser could not read",
                           exc_info=True)
            continue
        items.append(item)
    return AnalysisResult(items=items, image_type="receipt",
                          purchased_on=purchased_on, store=store,
                          raw_response=raw)


# Recipe-optimize prompt (FoodAssistant-fjxy). Kept here in base (which has no
# heavy provider SDK imports) so every provider and the tests can reach it. It is
# explicit that the pass must not change the recipe's substance, only its
# formatting, and that timing cues become parser-friendly ("simmer for N
# minutes") so the app's timer parser picks them up.
_OPTIMIZE_RECIPE_PROMPT = """
Reformat the recipe below for clarity and flow. This is a formatting pass ONLY.

Hard rules, do NOT break them:
- Do NOT add, remove, or substitute any ingredient.
- Do NOT change any quantity, amount, unit, or measurement.
- Do NOT change the cooking method, techniques, temperatures, or the order of
  what actually happens.
- Keep it the same recipe. If something is ambiguous, leave it as written.

What you MAY do:
- Fix wording, grammar, and spelling; make steps read cleanly and in order.
- Split a step that crams several actions into one into separate numbered steps,
  and merge tiny fragments that are really one action.
- Use consistent units and consistent phrasing across steps and ingredients.
- Make every timing cue explicit and easy to spot, phrased as "for N minutes" or
  "for N hours" (for example, write "simmer for 20 minutes", not "simmer until
  reduced" when the source gives a time). Only state a time the recipe already
  implies; never invent one.

Return a JSON object with these exact fields:
{{
  "name": "recipe title",
  "description": "one or two sentence summary",
  "servings": "e.g. '4 servings' or null if not stated",
  "total_time": "e.g. '45 minutes' or null if not stated",
  "ingredients": ["1 cup flour", "2 large eggs", "..."],
  "instructions": ["First step text.", "Second step text.", "..."]
}}
Each ingredient is one string with quantity and name together. Each instruction
is one numbered step's full text, in order.
When the recipe groups its ingredients under headings (shown as "Heading:" lines
in the ingredient list), keep those exact headings with the same lines under
them, and return "ingredients" instead as an ordered list of section objects:
  "ingredients": [
    {{"section": "Meat sauce", "items": ["1 lb ground beef", "..."]}},
    {{"section": "Topping", "items": ["1 cup breadcrumbs", "..."]}}
  ]
Use the flat string list when the recipe has no headings, and never invent,
rename, or drop a heading.
Return ONLY valid JSON. No markdown, no explanation.

--- RECIPE ---
{recipe}
""".strip()


# Ingredient-parsing prompt (FoodAssistant-au59). Turns free-text ingredient
# lines ("2 cups flour, sifted") into structured quantity/unit/food/note so an
# imported recipe lands in Mealie already parsed. Kept in base (no heavy SDK
# imports) so every provider and the tests share one wording. Strict on purpose:
# one object per line, same order, nothing invented or dropped.
_PARSE_INGREDIENTS_PROMPT = """
You are turning recipe ingredient lines into structured data for a recipe
manager. Below is a numbered list of ingredient lines. For EACH line, return
one object with these fields:
- "quantity": the numeric amount as a number (for example 1, 0.5, 2). Use null
  when the line states no amount (for example "salt to taste"). Convert a
  fraction like "1/2" to 0.5 and a mixed number like "1 1/2" to 1.5.
- "unit": the unit of measure as a short string (for example "cup",
  "tablespoon", "g", "clove"). Use "" when the line has no unit.
- "food": the core food itself, without the amount, unit, or preparation (for
  example "flour", "unsalted butter", "yellow onion").
- "note": any extra detail such as preparation or state (for example "finely
  chopped", "sifted", "to taste", "optional"). Use "" when there is none.

Hard rules, do NOT break them:
- Return EXACTLY one object per input line, in the SAME order. Do not add,
  drop, merge, split, or reorder lines.
- Do not invent an ingredient, amount, or unit that is not written.
- If a line cannot be parsed, still return an object for it: put the whole line
  text in "food" and set "quantity" to null.

Return ONLY a JSON object of this exact shape, no markdown and no explanation:
{{"ingredients": [{{"quantity": 1, "unit": "cup", "food": "flour", "note": "sifted"}}]}}

--- INGREDIENT LINES ---
{lines}
""".strip()


def format_ingredient_lines(lines: list[str]) -> str:
    """Render ingredient lines as a numbered list for the parse prompt.

    Pure and shared so every provider frames the lines the same way; the numbers
    make it clear to the model exactly how many objects to return, one per line.
    """
    return "\n".join(f"{i}. {line}" for i, line in enumerate(lines or [], 1))


def format_recipe_for_prompt(recipe: dict) -> str:
    """Render an editor recipe dict as plain labelled text for an optimize prompt.

    Keeps the field boundaries unambiguous (name, servings, ingredients one per
    line, numbered steps) so the model reformats without guessing structure. When
    the recipe carries ingredient section headings (the position-aligned
    ingredient_sections list, zq7k), each group is introduced by its own
    "Heading:" line so the model can preserve the grouping (sz4h); a recipe with
    no sections renders exactly as before. Pure and shared so every provider
    frames the same recipe the same way."""
    r = recipe or {}
    lines = [f"Name: {r.get('name') or ''}"]
    if r.get("description"):
        lines.append(f"Description: {r['description']}")
    if r.get("servings"):
        lines.append(f"Servings: {r['servings']}")
    if r.get("total_time"):
        lines.append(f"Total time: {r['total_time']}")
    lines.append("Ingredients:")
    sections = r.get("ingredient_sections") or []
    prev = None
    for i, ing in enumerate(r.get("ingredients") or []):
        heading = str(sections[i] or "").strip() if i < len(sections) else ""
        if heading and heading != prev:
            lines.append(f"{heading}:")
        prev = heading or None
        lines.append(f"- {ing}")
    lines.append("Instructions:")
    for i, step in enumerate(r.get("instructions") or [], 1):
        lines.append(f"{i}. {step}")
    return "\n".join(lines)


class VisionProvider(ABC):
    @abstractmethod
    async def analyze_food(self, image_data: bytes, mime_type: str) -> AnalysisResult:
        """Analyze a photo of food items."""

    @abstractmethod
    async def analyze_receipt(self, image_data: bytes, mime_type: str) -> AnalysisResult:
        """Parse a receipt image and extract food line items."""

    async def extract_receipt_prices(self, image_data: bytes,
                                     mime_type: str) -> str | None:
        """Run the receipt price-capture prompt (services/receipt.py) over a
        receipt photo and return the model's RAW text reply, or None when the
        provider cannot run a custom vision prompt (the cloud proxy's task
        kinds are fixed, and the no-op provider has no model at all). The
        reply stays raw on purpose: services.receipt.parse_receipt_reply is
        the one place fences, junk, and partial rows get handled."""
        return None

    @abstractmethod
    async def health_check(self) -> bool:
        """Return True if the provider is reachable and configured."""

    async def enrich_product(self, info: dict) -> dict | None:
        """Normalize barcode-lookup product data (text-only, no image).

        Takes raw Open Food Facts fields and returns a dict with name,
        category, storage_type, shelf_life_days, and brand: or None if
        the provider doesn't support text enrichment.
        """
        return None

    async def generate_recipe(self, name: str, extra_instructions: str = "") -> dict | None:
        """Generate a full recipe from a dish name. Returns the same schema as
        extract_recipe, or None if the provider doesn't support text generation.

        extra_instructions is an optional free-text steer from the user (the Cook
        page custom prompt box); empty means use the default prompt unchanged."""
        return None

    async def suggest_from_inventory(self, items: list[str], limit: int = 8,
                                      preferences: str = "") -> list[dict] | None:
        """Suggest recipes from a list of available ingredients. Returns a list of
        {name, description, uses} dicts, or None if unsupported."""
        return None

    async def estimate_nutrition(self, name: str, servings: float = 1.0) -> dict | None:
        """Estimate calories + macros (grams) for a food, scaled to servings.

        Returns {calories, protein, carbs, fat} (floats or None), or None if the
        provider does not support text generation (FoodAssistant-e6qt)."""
        return None

    async def extract_recipe(self, image_data: bytes | None = None,
                             mime_type: str | None = None,
                             page_text: str | None = None) -> dict | None:
        """Extract a structured recipe from a photo OR from webpage text.

        Returns a dict with name, description, servings, total_time,
        ingredients (list[str]), instructions (list[str]): or None if
        the provider doesn't support recipe extraction.
        """
        return None

    async def optimize_recipe(self, recipe: dict) -> dict | None:
        """Reformat a recipe for clarity and flow WITHOUT changing it: same
        ingredients, quantities, and method, only clearer wording, step
        ordering/splitting, consistent units, and explicit timer cues. Returns
        the same schema as extract_recipe, or None when the provider does not
        support text generation (FoodAssistant-fjxy)."""
        return None

    async def parse_ingredients(self, lines: list[str]) -> list[dict] | None:
        """Parse free-text ingredient lines into structured data
        (FoodAssistant-au59).

        Returns a list of {quantity, unit, food, note} dicts, one per input line
        in the same order, or None when the provider does not support text
        generation. quantity is a number or null; unit/food/note are strings.
        The caller normalizes this into Mealie's structured recipeIngredient
        shape and always keeps the original line, so nothing is lost when the
        model drops a field or a whole line."""
        return None

    async def identify_barcode(self, barcode: str) -> dict | None:
        """Best-effort identify a product from a UPC/EAN that Open Food Facts
        does not have. Returns a dict (its "name" is null when the model does
        not recognize the code) or None when the provider cannot do text-only
        generation. The default is None.

        This is deliberately separate from enrich_product: enrich_product is
        told to use its knowledge of a KNOWN product to fill gaps, which makes
        it invent a plausible brand when handed only barcode digits. A bare
        barcode cannot be mapped to a product reliably, so this path uses a
        prompt that returns null rather than guess (FoodAssistant barcode fix)."""
        return None
