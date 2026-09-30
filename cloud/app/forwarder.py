"""The upstream side of the AI proxy.

The proxy endpoint owns entitlement checks, quota gates, and ledger writes;
this module owns only the call to the actual LLM provider. Production runs
GeminiForwarder (Gemini 2.5 Flash over the plain REST API, no SDK); tests
and local dev use StubForwarder. CLOUD_AI_FORWARDER selects which.

Hard rule for every implementation: image bytes are held in memory for the
duration of the upstream call and discarded. They are never written to the
database, logs, or disk.
"""
from __future__ import annotations

import base64
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from .config import settings


@dataclass
class ForwardResult:
    """What a forwarder returns: the provider's response payload and the
    tokens the provider's response reported (charged to the account)."""

    result: dict
    tokens: int


class ForwarderError(Exception):
    """An upstream failure, carrying the HTTP status and structured detail
    the proxy endpoint returns to the install. Detail bodies must never
    contain the upstream API key or raw upstream response text."""

    def __init__(self, status: int, detail: dict):
        self.status = status
        self.detail = detail
        super().__init__(detail.get("error", "upstream_error"))


class AIForwarder(ABC):
    @abstractmethod
    async def forward(self, kind: str, image_data: bytes | None,
                      mime_type: str, text: str) -> ForwardResult:
        """Run one proxied AI task.

        kind is 'food', 'receipt', or 'enrich' (the proxy's task kinds), or
        'recipe' (the portal upload's formatting step); image tasks carry the
        bytes and mime type, enrichment carries text. Implementations must
        not persist the image anywhere."""


class StubForwarder(AIForwarder):
    """Deterministic forwarder for tests and local development."""

    # A nominal charge so the ledger, quota gate, and 402 path exercise end
    # to end without an upstream call.
    STUB_TOKENS = 1000

    async def forward(self, kind: str, image_data: bytes | None,
                      mime_type: str, text: str) -> ForwardResult:
        if kind == "recipe":
            # A deterministic, well-formed recipe draft so the portal upload
            # path (and its tests) exercise end to end without an upstream
            # call. Real formatting happens only under the gemini forwarder.
            return ForwardResult(
                result={"text": json.dumps({
                    "title": "Shared Recipe",
                    "ingredients": ["1 cup flour", "2 eggs", "1 pinch salt"],
                    "steps": ["Mix the ingredients.", "Bake until golden."],
                })},
                tokens=self.STUB_TOKENS,
            )
        return ForwardResult(
            result={
                "stub": True,
                "kind": kind,
                "items": [],
                "note": "AI forwarding is not wired up yet.",
            },
            tokens=self.STUB_TOKENS,
        )


# The food, receipt and enrich prompts are verbatim copies of the app's own
# (_FOOD_PROMPT, _RECEIPT_PROMPT and _ENRICH_PROMPT in
# service/app/providers/gemini.py), so a Forager scan asks for exactly the JSON
# a direct Gemini scan asks for and the app reads both replies the same way.
# They are copies, not imports, because this service shares nothing with the
# app at import time. Change them together with the app's, never on their own;
# tests/test_forwarder_prompts.py compares them with the app's file.
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

# A template: the product data fills {info} (see GeminiForwarder._prompt),
# which is why the braces of its JSON example are doubled.
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

_PROMPTS = {
    "food": _FOOD_PROMPT,
    "receipt": _RECEIPT_PROMPT,
    "enrich": _ENRICH_PROMPT,
    # Forager's own task (the portal upload's formatting step), not a copy of
    # an app prompt. Its reply is handed back as plain text for
    # recipe_format.parse_recipe_draft.
    "recipe": (
        "A home cook is sharing a recipe. Reformat it into clean, consistent "
        "text. Extract only what is already present: do not add, remove, "
        "substitute, or change any ingredient, quantity, or technique. Fix only "
        "wording, spelling, and layout for clarity, keeping every quantity and "
        "instruction exactly as written. Reply with JSON only, no prose and no "
        "code fence, of the form {\"title\": string, \"ingredients\": [string], "
        "\"steps\": [string]}. Put each ingredient (with its quantity) as one "
        "list item and each instruction as one step. If a part is unreadable or "
        "missing, leave it out rather than inventing it."
    ),
}

# The kinds whose reply is one of the app's JSON shapes: Gemini is asked for
# JSON outright, and the parsed fields ride back alongside the text (see
# _reply_result).
_JSON_KINDS = frozenset({"food", "receipt", "enrich"})

# Output limits per kind, as (maxOutputTokens, thinkingBudget). The cap bounds
# what one call can cost. Gemini 2.5 counts its thinking toward that cap (a
# hard cutoff that includes thought tokens), so each kind also gives thinking
# an explicit budget well inside it: a model left to think freely can spend the
# whole cap before writing a word, and the answer comes back cut off. What is
# left over is many times the size of a real answer. The budgets suit the
# Gemini 2.5 line (the gemini_model default) and later; a model without
# thinking, such as gemini-2.0-flash, refuses any thinkingConfig with a 400.
_OUTPUT_LIMITS = {
    "food": (2048, 1024),
    "receipt": (8192, 2048),
    "enrich": (1024, 512),
    "recipe": (8192, 2048),
}

# One code fence around the whole reply, stripped the way the app strips it
# (parse_json_response in service/app/providers/base.py).
_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def _generation_config(kind: str) -> dict:
    """The generationConfig for one task kind."""
    max_tokens, thinking_budget = _OUTPUT_LIMITS[kind]
    config: dict = {"maxOutputTokens": max_tokens,
                    "thinkingConfig": {"thinkingBudget": thinking_budget}}
    if kind in _JSON_KINDS:
        config["responseMimeType"] = "application/json"
    return config


def _parse_reply(reply: str):
    """The reply's JSON value, or None when it is not JSON.

    Also None when the value holds NaN or an out-of-range number (json reads
    1e999 as infinity), or a string with half of a surrogate pair ("\\ud800"
    decodes to one, and UTF-8 cannot encode it): the proxy answers in strict
    UTF-8 JSON, which cannot carry those, and the app still gets the reply as
    text. The check renders the value the way the response will."""
    body = reply.strip()
    fenced = _FENCE_RE.match(body)
    if fenced:
        body = fenced.group(1)
    try:
        value = json.loads(body)
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, RecursionError):
        return None
    return value


def _reply_result(kind: str, reply: str) -> dict:
    """The result payload for one model reply.

    It always carries the reply as "text", which the app parses itself. For
    the JSON kinds the parsed fields ride along at the top level as well,
    because app releases from before that change read the fields directly
    and never look at "text"; a receipt that comes back as a bare list is
    wrapped as {"items": [...]}, the shape those releases expect."""
    if kind in _JSON_KINDS:
        parsed = _parse_reply(reply)
        if isinstance(parsed, dict):
            return {**parsed, "text": reply}
        if isinstance(parsed, list) and kind == "receipt":
            return {"items": parsed, "text": reply}
    return {"text": reply}


_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


class GeminiForwarder(AIForwarder):
    """Forwards proxy tasks to the Gemini REST API (generateContent).

    Plain httpx against v1beta, no Google SDK. The API key travels only in
    the x-goog-api-key request header, never in the URL or any error body.
    Token counts come from the response's usageMetadata (prompt plus
    candidates), so the ledger records what Google actually charged.

    The result is the reply text as {"text": ...}; for the food, receipt and
    enrich kinds, whose reply Gemini is asked to give as JSON, the parsed
    fields come back beside it (see _reply_result).
    """

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash",
                 timeout: float = 60.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._transport = transport  # tests inject a MockTransport

    async def forward(self, kind: str, image_data: bytes | None,
                      mime_type: str, text: str) -> ForwardResult:
        if not self._api_key:
            raise ForwarderError(503, {
                "error": "upstream_unconfigured",
                "message": "The AI service is not configured yet. "
                           "Try again later.",
            })
        # An unknown kind is treated as enrichment, the same fallback the
        # prompt has always had.
        task = kind if kind in _PROMPTS else "enrich"
        parts: list[dict] = [{"text": self._prompt(task, text)}]
        if image_data is not None:
            parts.append({"inline_data": {
                "mime_type": mime_type or "image/jpeg",
                "data": base64.b64encode(image_data).decode("ascii"),
            }})
        url = f"{_GEMINI_BASE}/models/{self._model}:generateContent"
        try:
            async with httpx.AsyncClient(timeout=self._timeout,
                                         transport=self._transport) as client:
                resp = await client.post(
                    url,
                    headers={"x-goog-api-key": self._api_key},
                    json={"contents": [{"parts": parts}],
                          "generationConfig": _generation_config(task)},
                )
        except httpx.TimeoutException:
            raise ForwarderError(504, {
                "error": "upstream_timeout",
                "message": "The AI service took too long to answer. "
                           "Try again.",
            })
        except httpx.HTTPError:
            raise ForwarderError(502, {
                "error": "upstream_unreachable",
                "message": "The AI service could not be reached. Try again.",
            })

        if resp.status_code == 429:
            raise ForwarderError(429, {
                "error": "upstream_rate_limited",
                "message": "The AI service is busy. Try again in a minute.",
            })
        if resp.status_code != 200:
            # Deliberately no upstream body in the detail: it is not useful
            # to the install and must never echo credentials.
            raise ForwarderError(502, {
                "error": "upstream_error",
                "upstream_status": resp.status_code,
                "message": "The AI service returned an error. Try again.",
            })

        try:
            payload = resp.json()
            candidates = payload.get("candidates") or []
            text_out = "".join(
                p.get("text", "")
                for p in ((candidates[0].get("content") or {}).get("parts") or [])
            ) if candidates else ""
        except (ValueError, AttributeError, IndexError, TypeError):
            raise ForwarderError(502, {
                "error": "upstream_error",
                "message": "The AI service sent an unreadable response. "
                           "Try again.",
            })

        meta = payload.get("usageMetadata") or {}
        tokens = int(meta.get("promptTokenCount") or 0) + \
            int(meta.get("candidatesTokenCount") or 0)
        if not tokens:
            tokens = int(meta.get("totalTokenCount") or 0)
        return ForwardResult(result=_reply_result(task, text_out), tokens=tokens)

    @staticmethod
    def _prompt(kind: str, text: str) -> str:
        """The task prompt carrying the request's text. Enrichment puts the
        product data in the prompt's {info} slot, where the app's own Gemini
        provider puts it; the other kinds get their text appended."""
        if kind not in _PROMPTS or kind == "enrich":
            return _ENRICH_PROMPT.format(info=text)
        prompt = _PROMPTS[kind]
        if text:
            prompt = f"{prompt}\n\n{text}"
        return prompt


_stub = StubForwarder()


def get_forwarder() -> AIForwarder:
    """The forwarder CLOUD_AI_FORWARDER selects: gemini in production,
    stub everywhere else."""
    if settings.ai_forwarder == "gemini":
        return GeminiForwarder(api_key=settings.gemini_api_key,
                               model=settings.gemini_model,
                               timeout=settings.forward_timeout_seconds)
    return _stub
