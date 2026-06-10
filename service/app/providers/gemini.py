import asyncio
import json
import time
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
Return a JSON array where each element is:
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
Include only food/beverage items. Skip non-food items, taxes, fees, totals, and store info.
Infer storage_type and category from your knowledge of the product.
Return ONLY a valid JSON array. No markdown, no explanation.
""".strip()

_HEALTH_CACHE_TTL = 3600  # seconds — avoid hammering the API on every /health poll


class GeminiProvider(VisionProvider):
    def __init__(self, api_key: str, model: str = "gemini-1.5-flash"):
        genai.configure(api_key=api_key)
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
        if isinstance(data, dict):
            data = [data]
        items = [_parse_item(d, default_confidence=0.8) for d in data]
        return AnalysisResult(items=items, image_type="receipt", raw_response=raw)

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
