import asyncio
import json
import time
from datetime import date
import google.generativeai as genai
from .base import VisionProvider
from ..models.food import AnalysisResult, FoodItem, StorageType, FoodCategory

_FOOD_PROMPT = """
Analyze this image of food. Return a JSON object with these exact fields:
{
  "name": "specific food name, e.g. chicken breast, sharp cheddar, roma tomatoes",
  "quantity": 1.0,
  "unit": "lbs | oz | pieces | package | bunch | etc",
  "best_by_date": "YYYY-MM-DD if visible on packaging, otherwise null",
  "storage_type": "refrigerated | frozen | room_temp | dry",
  "category": "Poultry | Meat | Seafood | Dairy | Produce | Grains | Condiments | Beverages | Snacks | Frozen | Canned | Other",
  "brand": "brand name or null",
  "notes": "any other useful details or null",
  "confidence": 0.95
}
Be as specific as possible with the name. If you see a best-by date, use-by date, or sell-by date on packaging, extract it.
Return ONLY valid JSON. No markdown, no explanation.
""".strip()

_RECEIPT_PROMPT = """
Analyze this grocery receipt image. Extract every food or beverage item purchased.
Return a JSON object with these exact fields:
{
  "store": "store name printed on the receipt, or null if not visible",
  "purchase_date": "YYYY-MM-DD date of purchase printed on the receipt, or null if not visible",
  "items": [
    {
      "name": "specific food name",
      "quantity": 1.0,
      "unit": "item | lbs | oz | etc",
      "best_by_date": null,
      "storage_type": "refrigerated | frozen | room_temp | dry",
      "category": "Poultry | Meat | Seafood | Dairy | Produce | Grains | Condiments | Beverages | Snacks | Frozen | Canned | Other",
      "brand": "brand name or null",
      "notes": null,
      "confidence": 0.85
    }
  ]
}
Include only food/beverage items in "items". Skip non-food items, taxes, fees, and totals.
Infer storage_type and category from your knowledge of the product.
For purchase_date, convert any printed date into YYYY-MM-DD; use null if no date is legible.
Return ONLY valid JSON. No markdown, no explanation.
""".strip()

_ENRICH_PROMPT = """
You are normalizing a grocery product scanned by barcode for a home food inventory.
Open Food Facts returned this raw data (fields may be missing, generic, or badly cased):

{info}

Use your knowledge of the actual product. Return a JSON object with these exact fields:
{{
  "name": "clean display name including brand, e.g. 'Kewpie Mayonnaise', 'Dr Pepper Zero Sugar'",
  "category": "Poultry | Meat | Seafood | Dairy | Produce | Grains | Condiments | Beverages | Snacks | Frozen | Canned | Other",
  "storage_type": "refrigerated | frozen | room_temp | dry",
  "shelf_life_days": 60,
  "brand": "brand name or null"
}}
storage_type is where this product is typically kept at home (e.g. Kewpie mayonnaise
is refrigerated, canned soup is dry, soda is room_temp). shelf_life_days is a realistic
integer estimate of days from purchase until best-by for that storage.
Return ONLY valid JSON. No markdown, no explanation.
""".strip()

_RECIPE_PROMPT = """
Extract the complete recipe from the provided {source}. Transcribe faithfully —
do not invent ingredients or steps that are not present.
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
is one numbered step's full text, in order. If handwriting is partly illegible,
make your best guess and keep going.
Return ONLY valid JSON. No markdown, no explanation.
""".strip()

_GENERATE_RECIPE_PROMPT = """
Write a complete, accurate recipe for "{name}".
Return a JSON object with these exact fields:
{{
  "name": "recipe title",
  "description": "one or two sentence summary",
  "servings": "e.g. '4 servings'",
  "total_time": "e.g. '45 minutes'",
  "ingredients": ["1 cup flour", "2 large eggs", "..."],
  "instructions": ["First step text.", "Second step text.", "..."]
}}
Each ingredient is one string with quantity and name together.
Each instruction is one numbered step's full text, in order.
Return ONLY valid JSON. No markdown, no explanation.
""".strip()

_SUGGEST_INVENTORY_PROMPT = """
I have the following ingredients available:
{items}
{preferences_block}
Suggest up to {limit} recipes I could make primarily with these ingredients
plus common pantry staples. Prioritize recipes that use the most of the
listed items, especially ones expiring soon. Avoid listing obvious single-item
dishes.
Return a JSON object:
{{
  "suggestions": [
    {{"name": "recipe name", "description": "one-sentence description", "uses": ["item1", "item2"]}}
  ]
}}
Return ONLY valid JSON. No markdown, no explanation.
""".strip()

_HEALTH_CACHE_TTL = 3600  # seconds: avoid hammering the API on every /health poll


class GeminiProvider(VisionProvider):
    def __init__(self, api_key: str, model: str = "gemini-2.5-flash",
                 extra_keys: list[str] | None = None):
        genai.configure(api_key=api_key)
        self.api_key = api_key
        # Spare keys kept for fallback. genai.configure sets a process-global
        # key, so rotating here would clobber other instances; the primary key
        # is used for every call today. Stored so a future rotation point (e.g.
        # a per-call genai client) can reach them.
        self.extra_keys = [k for k in (extra_keys or []) if k and k != api_key]
        self.model_name = model
        self.model = genai.GenerativeModel(
            model,
            generation_config={"response_mime_type": "application/json"},
        )
        self._health_ok: bool | None = None
        self._health_ts: float = 0.0

    async def analyze_food(self, image_data: bytes, mime_type: str) -> AnalysisResult:
        image_part = {"mime_type": mime_type, "data": image_data}
        response = await self.model.generate_content_async([_FOOD_PROMPT, image_part])
        raw = response.text
        data = json.loads(raw)
        item = _parse_item(data, default_confidence=0.8)
        return AnalysisResult(items=[item], image_type="food", raw_response=raw)

    async def analyze_receipt(self, image_data: bytes, mime_type: str) -> AnalysisResult:
        image_part = {"mime_type": mime_type, "data": image_data}
        response = await self.model.generate_content_async([_RECEIPT_PROMPT, image_part])
        raw = response.text
        data = json.loads(raw)
        return _parse_receipt(data, default_confidence=0.8, raw=raw)

    async def enrich_product(self, info: dict) -> dict | None:
        prompt = _ENRICH_PROMPT.format(info=json.dumps(info, ensure_ascii=False))
        response = await self.model.generate_content_async([prompt])
        return json.loads(response.text)

    async def extract_recipe(self, image_data: bytes | None = None,
                             mime_type: str | None = None,
                             page_text: str | None = None) -> dict | None:
        if image_data is not None:
            parts = [_RECIPE_PROMPT.format(source="photo (recipe card, cookbook page, or handwritten note)"),
                     {"mime_type": mime_type, "data": image_data}]
        else:
            prompt = _RECIPE_PROMPT.format(source="webpage text below")
            parts = [f"{prompt}\n\n--- PAGE TEXT ---\n{page_text}"]
        response = await self.model.generate_content_async(parts)
        return json.loads(response.text)

    async def generate_recipe(self, name: str, extra_instructions: str = "") -> dict | None:
        prompt = _GENERATE_RECIPE_PROMPT.format(name=name)
        if extra_instructions.strip():
            prompt += "\n\nAdditional instructions from the user (follow these):\n" + extra_instructions.strip() + "\n"
        response = await self.model.generate_content_async([prompt])
        return json.loads(response.text)

    async def estimate_nutrition(self, name: str, servings: float = 1.0) -> dict | None:
        from .base import NUTRITION_PROMPT, nutrition_fields, parse_json_response
        response = await self.model.generate_content_async(
            [NUTRITION_PROMPT.format(name=name, servings=servings)])
        return nutrition_fields(parse_json_response(response.text))

    async def suggest_from_inventory(self, items: list[str], limit: int = 8,
                                      preferences: str = "") -> list[dict] | None:
        pref_block = f"\nMy food preferences / restrictions:\n{preferences}\n" if preferences.strip() else ""
        prompt = _SUGGEST_INVENTORY_PROMPT.format(
            items="\n".join(f"- {i}" for i in items), limit=limit,
            preferences_block=pref_block)
        response = await self.model.generate_content_async([prompt])
        return json.loads(response.text).get("suggestions", [])

    async def health_check(self) -> bool:
        # Metadata lookup, not a billed generation; cached to keep /health cheap.
        now = time.monotonic()
        if self._health_ok is not None and now - self._health_ts < _HEALTH_CACHE_TTL:
            return self._health_ok
        try:
            await asyncio.to_thread(genai.get_model, f"models/{self.model_name}")
            self._health_ok = True
        except Exception:
            self._health_ok = False
        self._health_ts = now
        return self._health_ok


def _parse_item(data: dict, default_confidence: float) -> FoodItem:
    return FoodItem(
        name=data.get("name", "Unknown"),
        quantity=float(data.get("quantity", 1) or 1),
        unit=data.get("unit") or "item",
        best_by_date=data.get("best_by_date"),
        storage_type=_safe_storage(data.get("storage_type")),
        category=_safe_category(data.get("category")),
        brand=data.get("brand"),
        notes=data.get("notes"),
        confidence=float(data.get("confidence", default_confidence)),
    )


def _safe_date(value) -> date | None:
    """Parse a YYYY-MM-DD string into a date, returning None on anything else."""
    if isinstance(value, date):
        return value
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _parse_receipt(data, default_confidence: float, raw: str) -> AnalysisResult:
    """Build a receipt AnalysisResult from a parsed model reply.

    Accepts the current object form ({store, purchase_date, items}) as well as
    the legacy bare array (or single object) form, so older prompts and models
    that ignore the wrapper still work. Extracts the purchase date and store
    when present and threads the date onto every item.
    """
    store = None
    purchased_on = None
    if isinstance(data, dict) and "items" in data and isinstance(data["items"], list):
        store = data.get("store") or None
        purchased_on = _safe_date(data.get("purchase_date"))
        rows = data["items"]
    elif isinstance(data, dict):
        rows = [data]
    else:
        rows = data

    items = []
    for d in rows:
        item = _parse_item(d, default_confidence=default_confidence)
        item.purchased_on = purchased_on
        items.append(item)
    return AnalysisResult(items=items, image_type="receipt",
                          purchased_on=purchased_on, store=store,
                          raw_response=raw)


def _safe_storage(value: str | None) -> StorageType:
    try:
        return StorageType(value)
    except (ValueError, TypeError):
        return StorageType.refrigerated


def _safe_category(value: str | None) -> FoodCategory:
    try:
        return FoodCategory(value)
    except (ValueError, TypeError):
        return FoodCategory.other
