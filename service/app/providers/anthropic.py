import base64
import json
import re
import time

import anthropic
from anthropic import AsyncAnthropic

from .base import (KEY_RATE_LIMITED, KEY_REJECTED, LiveModelMixin, SpareKeysMixin,
                   VisionProvider, format_recipe_for_prompt, parse_json_response,
                   _PARSE_INGREDIENTS_PROMPT, format_ingredient_lines,
                   raise_key_rejected_item)
from .gemini import (_parse_item, _parse_receipt, _FOOD_PROMPT, _RECEIPT_PROMPT,
                     _ENRICH_PROMPT, _RECIPE_PROMPT, _GENERATE_RECIPE_PROMPT,
                     _OPTIMIZE_RECIPE_PROMPT, _SUGGEST_INVENTORY_PROMPT)
from ..models.food import AnalysisResult

_HEALTH_CACHE_TTL = 3600  # seconds: avoid hammering the API on every /health poll

# Claude 5 generation models think before answering unless told otherwise, and
# that thinking counts against max_tokens. The app's calls are short JSON
# extractions, so these models run at low effort and get extra room on top of
# each call's answer budget, or a small cap (a barcode lookup asks for 256
# tokens) could be spent on thinking before any answer is written.
_THINKS_BY_DEFAULT = re.compile(r"^claude-(haiku|sonnet|opus|fable|mythos)-5(-|$)")
_THINKING_HEADROOM = 4096


def thinks_by_default(model: str) -> bool:
    """True for Claude models that run adaptive thinking when not told to."""
    return bool(_THINKS_BY_DEFAULT.match((model or "").strip()))


def request_kwargs(model: str, max_tokens: int) -> dict:
    """The model-dependent part of a messages.create call. Pure, for tests."""
    if thinks_by_default(model):
        return {"max_tokens": max_tokens + _THINKING_HEADROOM,
                "extra_body": {"output_config": {"effort": "low"}}}
    return {"max_tokens": max_tokens}


def response_text(response) -> str:
    """Join the text blocks of a reply, skipping thinking blocks."""
    text = "".join(getattr(b, "text", "") or "" for b in response.content
                   if getattr(b, "type", "") == "text")
    if not text:
        raise RuntimeError(
            "The model stopped before answering "
            f"({getattr(response, 'stop_reason', None) or 'no reason given'}).")
    return text


def key_error_kind(exc: Exception) -> str | None:
    """What an Anthropic SDK error means for the key that made the call."""
    if isinstance(exc, anthropic.AuthenticationError):
        return KEY_REJECTED
    if isinstance(exc, anthropic.RateLimitError):
        return KEY_RATE_LIMITED
    return None


def _raise_key_rejected_item(key: str, has_spare: bool) -> None:
    """Tell the owner in the Review inbox that Anthropic refused a key."""
    raise_key_rejected_item("anthropic", "Anthropic", key, has_spare)


class AnthropicProvider(SpareKeysMixin, LiveModelMixin, VisionProvider):
    key_provider_slug = "anthropic"
    key_provider_name = "Anthropic"

    def __init__(self, api_key: str, model: str = "claude-opus-4-8",
                 extra_keys: list[str] | None = None):
        # Spare keys to fall back to (see SpareKeysMixin). The SDK already
        # waits out a 429 as asked and retries, so a key still rate limited
        # after that moves straight on to a spare; a 401 marks the key bad and
        # tells the owner in the inbox.
        self._init_keys(api_key, extra_keys)
        self.client = self._make_client(api_key)
        self.model = model
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

        async def create(client):
            return await client.messages.create(
                model=self.model,
                messages=[{"role": "user", "content": content}],
                **request_kwargs(self.model, max_tokens),
            )

        response = await self._call_with_spare_keys(create, key_error_kind)
        try:
            from ..services import usage
            usage.record_response("anthropic", response)
        except Exception:
            pass
        return response_text(response)

    def _make_client(self, key: str):
        return AsyncAnthropic(api_key=key)

    def _report_rejected_key(self, key: str, has_spare: bool) -> None:
        _raise_key_rejected_item(key, has_spare)

    async def analyze_food(self, image_data: bytes, mime_type: str) -> AnalysisResult:
        raw = await self._generate(_FOOD_PROMPT, image_data, mime_type, max_tokens=1024)
        data = parse_json_response(raw)
        item = _parse_item(data, default_confidence=0.85)
        return AnalysisResult(items=[item], image_type="food", raw_response=raw)

    async def analyze_receipt(self, image_data: bytes, mime_type: str) -> AnalysisResult:
        raw = await self._generate(_RECEIPT_PROMPT, image_data, mime_type, max_tokens=8192)
        data = parse_json_response(raw)
        return _parse_receipt(data, default_confidence=0.85, raw=raw)

    async def extract_receipt_prices(self, image_data: bytes,
                                     mime_type: str) -> str | None:
        from ..services.receipt import build_prices_prompt
        return await self._generate(build_prices_prompt(), image_data, mime_type,
                                    max_tokens=8192)

    async def enrich_product(self, info: dict) -> dict | None:
        prompt = _ENRICH_PROMPT.format(info=json.dumps(info, ensure_ascii=False))
        raw = await self._generate(prompt, max_tokens=512)
        return parse_json_response(raw)

    async def identify_barcode(self, barcode: str) -> dict | None:
        from .base import BARCODE_IDENTIFY_PROMPT
        raw = await self._generate(
            BARCODE_IDENTIFY_PROMPT.format(barcode=barcode), max_tokens=256)
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

    async def optimize_recipe(self, recipe: dict) -> dict | None:
        prompt = _OPTIMIZE_RECIPE_PROMPT.format(recipe=format_recipe_for_prompt(recipe))
        raw = await self._generate(prompt, max_tokens=4096)
        return parse_json_response(raw)

    async def parse_ingredients(self, lines: list[str]) -> list[dict] | None:
        prompt = _PARSE_INGREDIENTS_PROMPT.format(lines=format_ingredient_lines(lines))
        raw = await self._generate(prompt, max_tokens=4096)
        return parse_json_response(raw).get("ingredients", [])

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
