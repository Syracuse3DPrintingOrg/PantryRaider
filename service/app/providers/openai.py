import base64
import json

import httpx

from .base import VisionProvider, parse_json_response
from .gemini import (_parse_item, _parse_receipt, _FOOD_PROMPT, _RECEIPT_PROMPT,
                     _ENRICH_PROMPT, _RECIPE_PROMPT, _GENERATE_RECIPE_PROMPT,
                     _SUGGEST_INVENTORY_PROMPT)
from ..models.food import AnalysisResult


class OpenAIProvider(VisionProvider):
    def __init__(self, api_key: str, model: str = "gpt-4o-mini",
                 base_url: str = "https://api.openai.com/v1",
                 extra_keys: list[str] | None = None):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        # Spare keys to fall back to when the primary is rate-limited (429) or
        # rejected (401). The primary is always tried first; a working spare is
        # promoted to primary so later calls skip the dead key.
        self.extra_keys = [k for k in (extra_keys or []) if k and k != api_key]

    @property
    def _keys(self) -> list[str]:
        return [self.api_key, *self.extra_keys]

    async def _generate(self, prompt: str, image_data: bytes = None,
                        mime_type: str = None, max_tokens: int = 4096) -> str:
        content = []
        if image_data is not None:
            b64 = base64.standard_b64encode(image_data).decode()
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{b64}"},
            })
        content.append({"type": "text", "text": prompt})
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        keys = self._keys
        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=120.0) as client:
            for idx, key in enumerate(keys):
                r = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json=payload,
                )
                # Only an auth/quota failure is worth retrying on another key.
                if r.status_code in (401, 429) and idx + 1 < len(keys):
                    last_error = httpx.HTTPStatusError(
                        f"HTTP {r.status_code}", request=r.request, response=r)
                    continue
                r.raise_for_status()
                self._promote(key)
                return r.json()["choices"][0]["message"]["content"]
        raise last_error if last_error else RuntimeError("No OpenAI API key configured")

    def _promote(self, key: str) -> None:
        """Make the key that just worked the primary so the next call uses it."""
        if key != self.api_key and key in self.extra_keys:
            self.extra_keys.remove(key)
            self.extra_keys.insert(0, self.api_key)
            self.api_key = key

    async def analyze_food(self, image_data: bytes, mime_type: str) -> AnalysisResult:
        raw = await self._generate(_FOOD_PROMPT, image_data, mime_type, max_tokens=1024)
        data = parse_json_response(raw)
        item = _parse_item(data, default_confidence=0.85)
        return AnalysisResult(items=[item], image_type="food", raw_response=raw)

    async def analyze_receipt(self, image_data: bytes, mime_type: str) -> AnalysisResult:
        raw = await self._generate(_RECEIPT_PROMPT, image_data, mime_type, max_tokens=8192)
        data = parse_json_response(raw)
        # json_object mode sometimes wraps the items array under a different key
        # than "items"; normalize so _parse_receipt's "items" branch applies.
        if isinstance(data, dict) and "items" not in data:
            for v in data.values():
                if isinstance(v, list):
                    data = {**data, "items": v}
                    break
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

    async def generate_recipe(self, name: str) -> dict | None:
        prompt = _GENERATE_RECIPE_PROMPT.format(name=name)
        raw = await self._generate(prompt, max_tokens=4096)
        return parse_json_response(raw)

    async def suggest_from_inventory(self, items: list[str], limit: int = 8,
                                      preferences: str = "") -> list[dict] | None:
        pref_block = f"\nMy food preferences / restrictions:\n{preferences}\n" if preferences.strip() else ""
        prompt = _SUGGEST_INVENTORY_PROMPT.format(
            items="\n".join(f"- {i}" for i in items), limit=limit,
            preferences_block=pref_block)
        raw = await self._generate(prompt, max_tokens=2048)
        return parse_json_response(raw).get("suggestions", [])

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                r = await client.get(
                    f"{self.base_url}/models/{self.model}",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                return r.status_code == 200
        except Exception:
            return False
