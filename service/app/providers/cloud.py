"""Forager provider (docs/design/cloud-platform.md).

A VisionProvider that sends photo, receipt, and barcode-enrichment work to
the managed AI proxy (POST {cloud_base_url}/v1/ai/analyze) instead of a
provider API, authenticated with the instance token issued when the install
was paired. The cloud holds the real provider key, meters tokens against the
account's monthly quota, and returns the provider's JSON result.

Error surfacing: a 402 quota reply from the proxy is raised as the same
HTTPException shape as the local token-budget gate in routers/analyze.py
(status 429, plain-text detail), so the pending page and every other caller
shows it the same way. An unreachable cloud raises 502 with an honest
message rather than a bare traceback.

The proxy covers the food / receipt / enrich task kinds only, so recipe
extraction, recipe generation, nutrition estimates, and cook suggestions
return None here (the base-class "unsupported" contract) until the cloud
grows those endpoints.
"""
from __future__ import annotations

import json
import time
from datetime import date

import httpx
from fastapi import HTTPException

from .base import VisionProvider
from ..models.food import AnalysisResult, FoodItem, StorageType, FoodCategory

# The analyze call carries an image and waits on a real LLM upstream, so it
# gets a generous read timeout; the health check is a cheap status lookup and
# must never hold up a settings page.
_ANALYZE_TIMEOUT = httpx.Timeout(60.0, connect=6.0)
_HEALTH_TIMEOUT = httpx.Timeout(6.0, connect=4.0)
_HEALTH_CACHE_TTL = 300  # seconds; keeps /health polls off the cloud

_UNREACHABLE_MSG = ("Forager could not be reached. Check the "
                    "internet connection and try again; your inventory and "
                    "manual entry keep working in the meantime.")


def _quota_message(body: dict) -> str:
    """User-forward text for the proxy's 402 body, mirroring the local
    budget gate's message shape (routers/analyze.py _BUDGET_MSG)."""
    err = body.get("error", "")
    if err == "no_subscription":
        return ("This install is linked to Forager, but the "
                "account has no active subscription. Renew it on the cloud "
                "portal, or switch to your own API key in Settings, AI.")
    used, quota, month = body.get("used"), body.get("quota"), body.get("month")
    msg = "Forager AI quota reached for this month"
    if used is not None and quota:
        msg += f" ({used:,} of {quota:,} tokens used for {month})"
    return msg + ". It resets at the start of next month."


def raise_for_cloud_error(resp: httpx.Response) -> None:
    """Map a non-2xx proxy reply to the user-facing HTTPException shape.

    402 (quota / no subscription) surfaces as 429 with a plain message,
    exactly like the local token-budget gate; 401 means the pairing was
    revoked; anything else passes the cloud's detail through honestly.
    """
    if resp.status_code < 400:
        return
    try:
        detail = resp.json().get("detail", {})
    except ValueError:
        detail = {}
    if resp.status_code == 402 and isinstance(detail, dict):
        raise HTTPException(429, detail=_quota_message(detail))
    if resp.status_code == 401:
        raise HTTPException(502, detail=(
            "Forager no longer accepts this install's link "
            "(it may have been removed from the account). Re-link with a "
            "new pairing code in Settings, AI."))
    text = detail if isinstance(detail, str) else resp.text[:200]
    raise HTTPException(502, detail=f"Forager error: {text}")


class CloudProvider(VisionProvider):
    """Vision provider backed by the Forager AI proxy."""

    def __init__(self, base_url: str, instance_token: str,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = (base_url or "").rstrip("/")
        self.token = instance_token
        # Injectable transport so tests exercise the real request/parse path
        # against httpx.MockTransport with no network.
        self._transport = transport
        self._health_ok: bool | None = None
        self._health_ts: float = 0.0

    def _headers(self) -> dict:
        from ..config import settings, APP_VERSION
        return {
            "Authorization": f"Bearer {self.token}",
            # Last-seen metadata for the account's instance list.
            "X-Device-Version": APP_VERSION,
            "X-Device-Mode": settings.deployment_mode or "server",
        }

    def _client(self, timeout: httpx.Timeout) -> httpx.AsyncClient:
        kwargs: dict = {"timeout": timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    async def _analyze(self, kind: str, image_data: bytes | None = None,
                       mime_type: str = "", text: str = "") -> dict:
        """One POST /v1/ai/analyze round trip; returns the proxy's JSON."""
        data = {"kind": kind, "text": text}
        files = None
        if image_data is not None:
            files = {"image": ("upload", image_data, mime_type)}
        try:
            async with self._client(_ANALYZE_TIMEOUT) as client:
                resp = await client.post(f"{self.base_url}/v1/ai/analyze",
                                         data=data, files=files,
                                         headers=self._headers())
        except httpx.HTTPError:
            raise HTTPException(502, detail=_UNREACHABLE_MSG)
        raise_for_cloud_error(resp)
        payload = resp.json()
        # The cloud already metered these tokens against the account; record
        # them locally too so the usage card's history includes cloud work.
        try:
            from ..services import usage
            usage.record("cloud", int(payload.get("tokens") or 0))
        except Exception:
            pass
        result = payload.get("result")
        return result if isinstance(result, dict) else {}

    async def analyze_food(self, image_data: bytes, mime_type: str) -> AnalysisResult:
        data = await self._analyze("food", image_data, mime_type)
        item = _parse_item(data, default_confidence=0.8)
        return AnalysisResult(items=[item], image_type="food",
                              raw_response=json.dumps(data))

    async def analyze_receipt(self, image_data: bytes, mime_type: str) -> AnalysisResult:
        data = await self._analyze("receipt", image_data, mime_type)
        return _parse_receipt(data, default_confidence=0.8,
                              raw=json.dumps(data))

    async def enrich_product(self, info: dict) -> dict | None:
        data = await self._analyze(
            "enrich", text=json.dumps(info, ensure_ascii=False))
        return data or None

    async def health_check(self) -> bool:
        """Reachable-and-linked check via GET /v1/instance/me, cached."""
        now = time.monotonic()
        if self._health_ok is not None and now - self._health_ts < _HEALTH_CACHE_TTL:
            return self._health_ok
        try:
            async with self._client(_HEALTH_TIMEOUT) as client:
                resp = await client.get(f"{self.base_url}/v1/instance/me",
                                        headers=self._headers())
            self._health_ok = resp.status_code == 200
        except httpx.HTTPError:
            self._health_ok = False
        self._health_ts = now
        return self._health_ok


# Result parsing mirrors providers/gemini.py: the cloud forwarder sends the
# same prompts the local providers use, so the JSON shapes match. Duplicated
# rather than imported because gemini.py imports its SDK at module scope.

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


def _safe_date(value) -> date | None:
    if isinstance(value, date):
        return value
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _parse_receipt(data, default_confidence: float, raw: str) -> AnalysisResult:
    store = None
    purchased_on = None
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        store = data.get("store") or None
        purchased_on = _safe_date(data.get("purchase_date"))
        rows = data["items"]
    elif isinstance(data, dict):
        rows = [data]
    else:
        rows = data or []

    items = []
    for d in rows:
        if not isinstance(d, dict):
            continue
        item = _parse_item(d, default_confidence=default_confidence)
        item.purchased_on = purchased_on
        items.append(item)
    return AnalysisResult(items=items, image_type="receipt",
                          purchased_on=purchased_on, store=store,
                          raw_response=raw)


def _safe_storage(value) -> StorageType:
    try:
        return StorageType(value)
    except (ValueError, TypeError):
        return StorageType.refrigerated


def _safe_category(value) -> FoodCategory:
    try:
        return FoodCategory(value)
    except (ValueError, TypeError):
        return FoodCategory.other
