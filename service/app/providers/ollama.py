import json
import httpx
import base64
from fastapi import HTTPException
from .base import (VisionProvider, format_recipe_for_prompt, parse_json_response,
                   _PARSE_INGREDIENTS_PROMPT, format_ingredient_lines)
from ..models.food import AnalysisResult
from .gemini import (_parse_item, _parse_receipt, _FOOD_PROMPT, _RECEIPT_PROMPT,
                     _ENRICH_PROMPT, _RECIPE_PROMPT, _GENERATE_RECIPE_PROMPT,
                     _OPTIMIZE_RECIPE_PROMPT, _SUGGEST_INVENTORY_PROMPT)

# Reuses the same prompts as Gemini: structured JSON output works with llava/llama3.2-vision

# Ollama 0.32.10 stopped applying a repeat penalty by default, and a small
# model without one can repeat itself until it runs out of room, so every
# request sets the long-standing 1.1 again along with a cap on the reply.
_REPEAT_PENALTY = 1.1
# Context window for the text-only calls (recipe pages, generated recipes).
# Ollama's default is far smaller, and when a prompt outgrows the window it
# keeps the END and silently drops the start.
_TEXT_NUM_CTX = 8192
# A recipe page is cut to this many characters so the page, the instructions,
# and the reply all fit in _TEXT_NUM_CTX.
_PAGE_TEXT_MAX = 12000


class OllamaProvider(VisionProvider):
    def __init__(self, base_url: str, model: str = "llava:7b",
                 transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        # Injectable transport so tests can capture the exact request with
        # httpx.MockTransport and no network (as providers/cloud.py does).
        self._transport = transport

    async def _post(self, payload: dict, timeout: float) -> str:
        """One /api/generate call; returns the model's reply text and records
        the tokens Ollama reports."""
        kwargs: dict = {"timeout": timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        async with httpx.AsyncClient(**kwargs) as client:
            r = await client.post(f"{self.base_url}/api/generate", json=payload)
        if r.status_code == 404:
            # Ollama answers 404 when the model has not been pulled yet.
            raise HTTPException(503, detail=(
                f"Ollama is running, but the model {self.model} is not "
                f"installed. Pull it with: docker exec foodassistant-ollama "
                f"ollama pull {self.model}"))
        r.raise_for_status()
        data = r.json()
        try:
            from ..services import usage
            usage.record_response("ollama", data)
        except Exception:
            pass
        return data["response"]

    async def _generate_text(self, prompt: str, max_tokens: int = 4096) -> str:
        payload = {"model": self.model, "prompt": prompt,
                   "stream": False, "format": "json",
                   "options": {"num_ctx": _TEXT_NUM_CTX, "num_predict": max_tokens,
                               "repeat_penalty": _REPEAT_PENALTY}}
        return await self._post(payload, timeout=180.0)

    async def _generate(self, prompt: str, image_data: bytes,
                        max_tokens: int = 2048) -> str:
        # max_tokens bounds a runaway reply. A food photo answers in a few
        # hundred tokens, so 2048 is generous; a receipt lists every line and
        # needs the same 8192 the other providers give it, or a long receipt
        # stops mid-JSON and cannot be read at all.
        b64 = base64.b64encode(image_data).decode()
        payload = {
            "model": self.model,
            "prompt": prompt,
            "images": [b64],
            "stream": False,
            "format": "json",
            "options": {"num_predict": max_tokens, "repeat_penalty": _REPEAT_PENALTY},
        }
        return await self._post(payload, timeout=120.0)

    async def analyze_food(self, image_data: bytes, mime_type: str) -> AnalysisResult:
        raw = await self._generate(_FOOD_PROMPT, image_data)
        data = parse_json_response(raw)
        item = _parse_item(data, default_confidence=0.75)
        return AnalysisResult(items=[item], image_type="food", raw_response=raw)

    async def analyze_receipt(self, image_data: bytes, mime_type: str) -> AnalysisResult:
        raw = await self._generate(_RECEIPT_PROMPT, image_data, max_tokens=8192)
        data = parse_json_response(raw)
        return _parse_receipt(data, default_confidence=0.75, raw=raw)

    async def extract_receipt_prices(self, image_data: bytes,
                                     mime_type: str) -> str | None:
        from ..services.receipt import build_prices_prompt
        return await self._generate(build_prices_prompt(), image_data, max_tokens=8192)

    async def enrich_product(self, info: dict) -> dict | None:
        # Text-only generation: llava and other multimodal models handle this fine
        prompt = _ENRICH_PROMPT.format(info=json.dumps(info, ensure_ascii=False))
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"num_predict": 1024, "repeat_penalty": _REPEAT_PENALTY},
        }
        return parse_json_response(await self._post(payload, timeout=60.0))

    async def identify_barcode(self, barcode: str) -> dict | None:
        from .base import BARCODE_IDENTIFY_PROMPT
        payload = {
            "model": self.model,
            "prompt": BARCODE_IDENTIFY_PROMPT.format(barcode=barcode),
            "stream": False,
            "format": "json",
            "options": {"num_predict": 1024, "repeat_penalty": _REPEAT_PENALTY},
        }
        return parse_json_response(await self._post(payload, timeout=60.0))

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
                "options": {"num_predict": 2048, "repeat_penalty": _REPEAT_PENALTY},
            }
        else:
            # Page first, instructions last: if the prompt still outgrows the
            # context window, Ollama drops the start of the page rather than
            # the instructions that say what to return.
            prompt = _RECIPE_PROMPT.format(source="webpage text above")
            text = (page_text or "")[:_PAGE_TEXT_MAX]
            payload = {
                "model": self.model,
                "prompt": f"--- PAGE TEXT ---\n{text}\n--- END OF PAGE TEXT ---\n\n{prompt}",
                "stream": False,
                "format": "json",
                "options": {"num_ctx": _TEXT_NUM_CTX, "num_predict": 2048,
                            "repeat_penalty": _REPEAT_PENALTY},
            }
        return parse_json_response(await self._post(payload, timeout=180.0))

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
        kwargs: dict = {"timeout": 5.0}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        try:
            async with httpx.AsyncClient(**kwargs) as client:
                r = await client.get(f"{self.base_url}/api/tags")
                return r.status_code == 200
        except Exception:
            return False
