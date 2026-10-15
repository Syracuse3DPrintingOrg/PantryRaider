import asyncio
import base64
import email.utils
import hashlib
import json
import logging
import re
import time

import httpx

from .base import (LiveModelMixin, VisionProvider, format_recipe_for_prompt, parse_json_response,
                   _PARSE_INGREDIENTS_PROMPT, format_ingredient_lines)
from .gemini import (_parse_item, _parse_receipt, _FOOD_PROMPT, _RECEIPT_PROMPT,
                     _ENRICH_PROMPT, _RECIPE_PROMPT, _GENERATE_RECIPE_PROMPT,
                     _OPTIMIZE_RECIPE_PROMPT, _SUGGEST_INVENTORY_PROMPT)
from ..models.food import AnalysisResult


# Reasoning models (the gpt-5 and gpt-6 families, and the o-series) reject the
# old max_tokens field and count their reasoning against
# max_completion_tokens, which every current chat model accepts. The app's
# calls are short JSON extractions, so reasoning models run at low effort and
# get extra room on top of each call's answer budget.
_REASONING_MODEL = re.compile(r"^(gpt-[5-9]|o\d)")
_REASONING_HEADROOM = 4096


log = logging.getLogger(__name__)

# A 429 is a "slow down", not a dead key: wait as long as OpenAI asks (capped,
# so a request never hangs for minutes) and try the same key once more before
# a spare. With no Retry-After header, a short pause.
_RETRY_AFTER_CAP_S = 20.0
_RETRY_AFTER_DEFAULT_S = 2.0

KEY_REJECTED_KIND = "ai_key_rejected"


def retry_after_seconds(headers, now: float | None = None) -> float:
    """Seconds to wait after a 429, from retry-after-ms or Retry-After (a number
    of seconds or an HTTP date), capped at _RETRY_AFTER_CAP_S. Pure, for tests."""
    wait = None
    try:
        ms = headers.get("retry-after-ms")
        if ms is not None:
            wait = float(ms) / 1000.0
    except (TypeError, ValueError):
        wait = None
    if wait is None:
        raw = headers.get("retry-after")
        if raw is not None:
            try:
                wait = float(raw)
            except (TypeError, ValueError):
                try:
                    when = email.utils.parsedate_to_datetime(raw)
                    wait = when.timestamp() - (time.time() if now is None else now)
                except (TypeError, ValueError, IndexError, OverflowError):
                    wait = None
    if wait is None or wait != wait:  # missing, or NaN
        wait = _RETRY_AFTER_DEFAULT_S
    return max(0.0, min(wait, _RETRY_AFTER_CAP_S))


def key_fingerprint(key: str) -> str:
    """A short, non-reversible id for a key, for the inbox item's dedupe key."""
    return hashlib.sha256((key or "").encode()).hexdigest()[:12]


def _raise_key_rejected_item(key: str, has_spare: bool) -> None:
    """Tell the owner in the Review inbox that OpenAI refused a key. Runs in a
    worker thread (it opens a database session). Never raises."""
    try:
        from ..database import SessionLocal
        from ..services import action_items
        tail = key[-4:] if len(key) >= 8 else ""
        which = f"the OpenAI API key ending in {tail}" if tail else "an OpenAI API key"
        body = (f"OpenAI turned down {which} as not valid, so Pantry Raider has "
                "stopped using it. ")
        body += ("Your other OpenAI keys are being used in its place. " if has_spare
                 else "AI features that use OpenAI will not work until it is replaced. ")
        body += "Open Settings, Scanning & AI to replace or remove it."
        db = SessionLocal()
        try:
            action_items.create(db, KEY_REJECTED_KIND, "OpenAI refused an API key",
                                body=body, level="warning",
                                dedupe_key=f"{KEY_REJECTED_KIND}:openai:{key_fingerprint(key)}")
        finally:
            db.close()
    except Exception:
        log.debug("Could not raise the rejected OpenAI key item", exc_info=True)


def is_reasoning_model(model: str) -> bool:
    return bool(_REASONING_MODEL.match((model or "").strip()))


def token_fields(model: str, max_tokens: int) -> dict:
    """The model-dependent part of a chat/completions payload. Pure, for tests."""
    if is_reasoning_model(model):
        return {"max_completion_tokens": max_tokens + _REASONING_HEADROOM,
                "reasoning_effort": "low"}
    return {"max_completion_tokens": max_tokens}


class OpenAIProvider(LiveModelMixin, VisionProvider):
    def __init__(self, api_key: str, model: str = "gpt-6-luna",
                 base_url: str = "https://api.openai.com/v1",
                 extra_keys: list[str] | None = None):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        # Spare keys to fall back to. The primary is always tried first; a
        # working spare is promoted to primary so later calls skip a bad key.
        # A 429 waits as asked and retries the same key before a spare; a 401
        # marks the key bad (for the life of this provider, which is rebuilt
        # when settings are saved) and tells the owner in the inbox.
        self.extra_keys = [k for k in (extra_keys or []) if k and k != api_key]
        self._bad_keys: set[str] = set()

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
            "response_format": {"type": "json_object"},
            **token_fields(self.model, max_tokens),
        }
        keys = [k for k in self._keys if k not in self._bad_keys]
        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=120.0) as client:
            async def post(key: str) -> httpx.Response:
                return await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json=payload,
                )

            for idx, key in enumerate(keys):
                has_next = idx + 1 < len(keys)
                r = await post(key)
                if r.status_code == 429:
                    # Rate limited: wait as asked, then the same key once more.
                    await asyncio.sleep(retry_after_seconds(r.headers))
                    r = await post(key)
                    if r.status_code == 429 and has_next:
                        last_error = httpx.HTTPStatusError(
                            "HTTP 429", request=r.request, response=r)
                        continue
                if r.status_code == 401:
                    await self._mark_bad(key, has_spare=has_next)
                    if has_next:
                        last_error = httpx.HTTPStatusError(
                            "HTTP 401", request=r.request, response=r)
                        continue
                r.raise_for_status()
                self._promote(key)
                data = r.json()
                try:
                    from ..services import usage
                    usage.record_response("openai", data)
                except Exception:
                    pass
                return data["choices"][0]["message"]["content"]
        if last_error:
            raise last_error
        if self._bad_keys:
            raise RuntimeError("OpenAI turned down the API key. Replace it in "
                               "Settings, Scanning & AI.")
        raise RuntimeError("No OpenAI API key configured")

    async def _mark_bad(self, key: str, has_spare: bool) -> None:
        """Stop using a key OpenAI refused, and say so once in the inbox."""
        if key in self._bad_keys:
            return
        self._bad_keys.add(key)
        log.warning("OpenAI refused an API key (401); it will not be used again "
                    "until the settings are saved")
        await asyncio.to_thread(_raise_key_rejected_item, key, has_spare)

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
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                r = await client.get(
                    f"{self.base_url}/models/{self.model}",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                return r.status_code == 200
        except Exception:
            return False
