import json
import httpx
from .base import VisionProvider
from ..models.food import AnalysisResult, FoodItem, StorageType, FoodCategory
from .gemini import _safe_storage, _safe_category, _FOOD_PROMPT, _RECEIPT_PROMPT
import base64

# Reuses the same prompts as Gemini — structured JSON output works with llava/llama3.2-vision


class OllamaProvider(VisionProvider):
    def __init__(self, base_url: str, model: str = "llava:7b"):
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def _generate(self, prompt: str, image_data: bytes) -> str:
        b64 = base64.b64encode(image_data).decode()
        payload = {
            "model": self.model,
            "prompt": prompt,
            "images": [b64],
            "stream": False,
            "format": "json",
        }
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(f"{self.base_url}/api/generate", json=payload)
            response.raise_for_status()
            return response.json()["response"]

    async def analyze_food(self, image_data: bytes, mime_type: str) -> AnalysisResult:
        raw = await self._generate(_FOOD_PROMPT, image_data)
        data = json.loads(raw)
        item = FoodItem(
            name=data.get("name", "Unknown"),
            quantity=float(data.get("quantity", 1)),
            unit=data.get("unit", "item"),
            best_by_date=data.get("best_by_date"),
            storage_type=_safe_storage(data.get("storage_type")),
            category=_safe_category(data.get("category")),
            brand=data.get("brand"),
            notes=data.get("notes"),
            confidence=float(data.get("confidence", 0.75)),
        )
        return AnalysisResult(items=[item], image_type="food", raw_response=raw)

    async def analyze_receipt(self, image_data: bytes, mime_type: str) -> AnalysisResult:
        raw = await self._generate(_RECEIPT_PROMPT, image_data)
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        items = [
            FoodItem(
                name=d.get("name", "Unknown"),
                quantity=float(d.get("quantity", 1)),
                unit=d.get("unit", "item"),
                best_by_date=d.get("best_by_date"),
                storage_type=_safe_storage(d.get("storage_type")),
                category=_safe_category(d.get("category")),
                brand=d.get("brand"),
                notes=d.get("notes"),
                confidence=float(d.get("confidence", 0.75)),
            )
            for d in data
        ]
        return AnalysisResult(items=items, image_type="receipt", raw_response=raw)

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{self.base_url}/api/tags")
                return r.status_code == 200
        except Exception:
            return False
