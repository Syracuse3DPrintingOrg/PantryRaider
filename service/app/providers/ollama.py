import json
import httpx
import base64
from .base import (VisionProvider, format_recipe_for_prompt, parse_json_response,
                   _PARSE_INGREDIENTS_PROMPT, format_ingredient_lines)
from ..models.food import AnalysisResult
from .gemini import (_parse_item, _parse_receipt, _FOOD_PROMPT, _RECEIPT_PROMPT,
                     _ENRICH_PROMPT, _RECIPE_PROMPT, _GENERATE_RECIPE_PROMPT,
                     _OPTIMIZE_RECIPE_PROMPT, _SUGGEST_INVENTORY_PROMPT)

# Reuses the same prompts as Gemini: structured JSON output works with llava/llama3.2-vision


class OllamaProvider(VisionProvider):
    def __init__(self, base_url: str, model: str = "llava:7b"):
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def _generate_text(self, prompt: str, max_tokens: int = 4096) -> str:
        payload = {"model": self.model, "prompt": prompt,
                   "stream": False, "format": "json",
                   "options": {"num_predict": max_tokens}}
        async with httpx.AsyncClient(timeout=180.0) as client:
            r = await client.post(f"{self.base_url}/api/generate", json=payload)
            r.raise_for_status()
            data = r.json()
            try:
                from ..services import usage
                usage.record_response("ollama", data)
            except Exception:
                pass
            return data["response"]

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
            _d = response.json()
            try:
                from ..services import usage
                usage.record_response("ollama", _d)
            except Exception:
                pass
            return _d["response"]

    async def analyze_food(self, image_data: bytes, mime_type: str) -> AnalysisResult:
        raw = await self._generate(_FOOD_PROMPT, image_data)
        data = parse_json_response(raw)
        item = _parse_item(data, default_confidence=0.75)
        return AnalysisResult(items=[item], image_type="food", raw_response=raw)

    async def analyze_receipt(self, image_data: bytes, mime_type: str) -> AnalysisResult:
        raw = await self._generate(_RECEIPT_PROMPT, image_data)
        data = parse_json_response(raw)
        return _parse_receipt(data, default_confidence=0.75, raw=raw)

    async def enrich_product(self, info: dict) -> dict | None:
        # Text-only generation: llava and other multimodal models handle this fine
        prompt = _ENRICH_PROMPT.format(info=json.dumps(info, ensure_ascii=False))
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(f"{self.base_url}/api/generate", json=payload)
            response.raise_for_status()
            return parse_json_response(response.json()["response"])

    async def identify_barcode(self, barcode: str) -> dict | None:
        from .base import BARCODE_IDENTIFY_PROMPT
        payload = {
            "model": self.model,
            "prompt": BARCODE_IDENTIFY_PROMPT.format(barcode=barcode),
            "stream": False,
            "format": "json",
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(f"{self.base_url}/api/generate", json=payload)
            response.raise_for_status()
            return parse_json_response(response.json()["response"])

    async def extract_recipe(self, image_data: bytes | None = None,
                             mime_type: str | None = None,
                             page_text: str | None = None) -> dict | None:
        if image_data is not None:
            prompt = _RECIPE_PROMPT.format(source="photo (recipe card, cookbook page, or handwritten note)")
            payload = {
                "model": self.model,
                "prompt": prompt,
                "images": [base64.b64encode(image_data).decode()],
                "stream": False,
                "format": "json",
            }
        else:
            prompt = _RECIPE_PROMPT.format(source="webpage text below")
            payload = {
                "model": self.model,
                "prompt": f"{prompt}\n\n--- PAGE TEXT ---\n{page_text}",
                "stream": False,
                "format": "json",
            }
        async with httpx.AsyncClient(timeout=180.0) as client:
            response = await client.post(f"{self.base_url}/api/generate", json=payload)
            response.raise_for_status()
            return parse_json_response(response.json()["response"])

    async def generate_recipe(self, name: str, extra_instructions: str = "") -> dict | None:
        prompt = _GENERATE_RECIPE_PROMPT.format(name=name)
        if extra_instructions.strip():
            prompt += "\n\nAdditional instructions from the user (follow these):\n" + extra_instructions.strip() + "\n"
        raw = await self._generate_text(prompt)
        return parse_json_response(raw)

    async def optimize_recipe(self, recipe: dict) -> dict | None:
        prompt = _OPTIMIZE_RECIPE_PROMPT.format(recipe=format_recipe_for_prompt(recipe))
        raw = await self._generate_text(prompt)
        return parse_json_response(raw)

    async def parse_ingredients(self, lines: list[str]) -> list[dict] | None:
        prompt = _PARSE_INGREDIENTS_PROMPT.format(lines=format_ingredient_lines(lines))
        raw = await self._generate_text(prompt)
        return parse_json_response(raw).get("ingredients", [])

    async def estimate_nutrition(self, name: str, servings: float = 1.0) -> dict | None:
        from .base import NUTRITION_PROMPT, nutrition_fields
        raw = await self._generate_text(NUTRITION_PROMPT.format(name=name, servings=servings))
        return nutrition_fields(parse_json_response(raw))

    async def suggest_from_inventory(self, items: list[str], limit: int = 8,
                                      preferences: str = "") -> list[dict] | None:
        pref_block = f"\nMy food preferences / restrictions:\n{preferences}\n" if preferences.strip() else ""
        prompt = _SUGGEST_INVENTORY_PROMPT.format(
            items="\n".join(f"- {i}" for i in items), limit=limit,
            preferences_block=pref_block)
        raw = await self._generate_text(prompt, max_tokens=2048)
        return parse_json_response(raw).get("suggestions", [])

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{self.base_url}/api/tags")
                return r.status_code == 200
        except Exception:
            return False
