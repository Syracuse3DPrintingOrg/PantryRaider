import base64
import json
import time

from anthropic import AsyncAnthropic

from .base import VisionProvider, parse_json_response
from .gemini import (_parse_item, _parse_receipt, _FOOD_PROMPT, _RECEIPT_PROMPT,
                     _ENRICH_PROMPT, _RECIPE_PROMPT, _GENERATE_RECIPE_PROMPT,
                     _SUGGEST_INVENTORY_PROMPT)
from ..models.food import AnalysisResult

_HEALTH_CACHE_TTL = 3600  # seconds: avoid hammering the API on every /health poll


class AnthropicProvider(VisionProvider):
    def __init__(self, api_key: str, model: str = "claude-opus-4-8",
                 extra_keys: list[str] | None = None):
        self.api_key = api_key
        self.client = AsyncAnthropic(api_key=api_key)
        self.model = model
        # Spare keys kept for fallback. The primary key drives every call today;
        # rotation across these on an auth/rate-limit error is the one remaining
        # step (the Anthropic SDK raises AuthenticationError / RateLimitError,
        # which a wrapper around _generate could catch to rebuild self.client).
        self.extra_keys = [k for k in (extra_keys or []) if k and k != api_key]
        self._health_ok: bool | None = None
        self._health_ts: float = 0.0

    async def _generate(self, prompt: str, image_data: bytes = None,
                        mime_type: str = None, max_tokens: int = 4096) -> str:
        content = []
        if image_data is not None:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime_type,
                    "data": base64.standard_b64encode(image_data).decode(),
                },
            })
        content.append({"type": "text", "text": prompt})
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": content}],
        )
        try:
            from ..services import usage
            usage.record_response("anthropic", response)
        except Exception:
            pass
        return next(b.text for b in response.content if b.type == "text")

    async def analyze_food(self, image_data: bytes, mime_type: str) -> AnalysisResult:
        raw = await self._generate(_FOOD_PROMPT, image_data, mime_type, max_tokens=1024)
        data = parse_json_response(raw)
        item = _parse_item(data, default_confidence=0.85)
        return AnalysisResult(items=[item], image_type="food", raw_response=raw)

    async def analyze_receipt(self, image_data: bytes, mime_type: str) -> AnalysisResult:
        raw = await self._generate(_RECEIPT_PROMPT, image_data, mime_type, max_tokens=8192)
        data = parse_json_response(raw)
        return _parse_receipt(data, default_confidence=0.85, raw=raw)

    async def enrich_product(self, info: dict) -> dict | None:
        prompt = _ENRICH_PROMPT.format(info=json.dumps(info, ensure_ascii=False))
        raw = await self._generate(prompt, max_tokens=512)
        return parse_json_response(raw)

    async def extract_recipe(self, image_data: bytes | None = None,
                             mime_type: str | None = None,
                             page_text: str | None = None) -> dict | None:
        if image_data is not None:
            prompt = _RECIPE_PROMPT.format(source="photo (recipe card, cookbook page, or handwritten note)")
            raw = await self._generate(prompt, image_data, mime_type, max_tokens=4096)
        else:
            prompt = _RECIPE_PROMPT.format(source="webpage text below")
            raw = await self._generate(f"{prompt}\n\n--- PAGE TEXT ---\n{page_text}", max_tokens=4096)
        return parse_json_response(raw)

    async def generate_recipe(self, name: str, extra_instructions: str = "") -> dict | None:
        prompt = _GENERATE_RECIPE_PROMPT.format(name=name)
        if extra_instructions.strip():
            prompt += "\n\nAdditional instructions from the user (follow these):\n" + extra_instructions.strip() + "\n"
        raw = await self._generate(prompt, max_tokens=4096)
        return parse_json_response(raw)

    async def estimate_nutrition(self, name: str, servings: float = 1.0) -> dict | None:
        from .base import NUTRITION_PROMPT, nutrition_fields
        raw = await self._generate(NUTRITION_PROMPT.format(name=name, servings=servings), max_tokens=300)
        return nutrition_fields(parse_json_response(raw))

    async def suggest_from_inventory(self, items: list[str], limit: int = 8,
                                      preferences: str = "") -> list[dict] | None:
        pref_block = f"\nMy food preferences / restrictions:\n{preferences}\n" if preferences.strip() else ""
        prompt = _SUGGEST_INVENTORY_PROMPT.format(
            items="\n".join(f"- {i}" for i in items), limit=limit,
            preferences_block=pref_block)
        raw = await self._generate(prompt, max_tokens=2048)
        return parse_json_response(raw).get("suggestions", [])

    async def health_check(self) -> bool:
        now = time.monotonic()
        if self._health_ok is not None and now - self._health_ts < _HEALTH_CACHE_TTL:
            return self._health_ok
        try:
            await self.client.models.retrieve(self.model)
            self._health_ok = True
        except Exception:
            self._health_ok = False
        self._health_ts = now
        return self._health_ok
