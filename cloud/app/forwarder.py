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

        kind is 'food', 'receipt', or 'enrich'; image tasks carry the bytes
        and mime type, enrichment carries text. Implementations must not
        persist the image anywhere."""


class StubForwarder(AIForwarder):
    """Deterministic forwarder for tests and local development."""

    # A nominal charge so the ledger, quota gate, and 402 path exercise end
    # to end without an upstream call.
    STUB_TOKENS = 1000

    async def forward(self, kind: str, image_data: bytes | None,
                      mime_type: str, text: str) -> ForwardResult:
        return ForwardResult(
            result={
                "stub": True,
                "kind": kind,
                "items": [],
                "note": "AI forwarding is not wired up yet.",
            },
            tokens=self.STUB_TOKENS,
        )


# Task prompts mirror the intent of the app's providers (see
# service/app/providers/); the app-side cloud provider parses the returned
# text the same way it parses a direct provider response.
_PROMPTS = {
    "food": (
        "Identify the food items in this photo. Reply with JSON: a list of "
        "objects with name, quantity, unit, and estimated shelf life in days."
    ),
    "receipt": (
        "Read this grocery receipt image. Reply with JSON: a list of "
        "purchased items with name, quantity, and price."
    ),
    "enrich": (
        "Given this product data, reply with JSON describing the product: "
        "name, category, typical storage location, and shelf life in days."
    ),
}

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


class GeminiForwarder(AIForwarder):
    """Forwards proxy tasks to the Gemini REST API (generateContent).

    Plain httpx against v1beta, no Google SDK. The API key travels only in
    the x-goog-api-key request header, never in the URL or any error body.
    Token counts come from the response's usageMetadata (prompt plus
    candidates), so the ledger records what Google actually charged.
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
        parts: list[dict] = [{"text": self._prompt(kind, text)}]
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
                    json={"contents": [{"parts": parts}]},
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
        return ForwardResult(result={"text": text_out}, tokens=tokens)

    @staticmethod
    def _prompt(kind: str, text: str) -> str:
        prompt = _PROMPTS.get(kind, _PROMPTS["enrich"])
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
