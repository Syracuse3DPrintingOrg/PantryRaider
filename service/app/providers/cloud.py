"""Forager provider (docs/design/cloud-platform.md).

A VisionProvider that sends photo, receipt, and barcode-enrichment work to
the managed AI proxy (POST {cloud_base_url}/v1/ai/analyze) instead of a
provider API, authenticated with the instance token issued when the install
was paired. The cloud holds the real provider key, meters tokens against the
account's monthly quota, and returns the model's reply as text, which is
parsed here exactly like a direct provider's reply.

Error surfacing: a 402 quota reply from the proxy is raised as the same
HTTPException shape as the local token-budget gate in routers/analyze.py
(status 429, plain-text detail), so the pending page and every other caller
shows it the same way. An unreachable cloud raises 502 with an honest
message rather than a bare traceback, and so does a reply that cannot be
read. A disabled account raises 403 with the cloud's own explanation.

The proxy covers the food / receipt / enrich task kinds only, so recipe
extraction, recipe generation, nutrition estimates, and cook suggestions
return None here (the base-class "unsupported" contract) until the cloud
grows those endpoints.
"""
from __future__ import annotations

import json
import time

import httpx
from fastapi import HTTPException

from .base import (VisionProvider, parse_json_response, _first_dict, _parse_item,
                   _parse_receipt)
from ..models.food import AnalysisResult

# The analyze call carries an image and waits on a real LLM upstream, so it
# gets a generous read timeout; the health check is a cheap status lookup and
# must never hold up a settings page.
_ANALYZE_TIMEOUT = httpx.Timeout(60.0, connect=6.0)
_HEALTH_TIMEOUT = httpx.Timeout(6.0, connect=4.0)
_HEALTH_CACHE_TTL = 300  # seconds; keeps /health polls off the cloud

_UNREACHABLE_MSG = ("Forager could not be reached. Check the "
                    "internet connection and try again; your inventory and "
                    "manual entry keep working in the meantime.")
_UNREADABLE_MSG = ("Forager sent back an answer Pantry Raider could not read. "
                   "Try again in a moment.")
_DISABLED_MSG = ("This Forager account has been disabled. Contact support to "
                 "restore access.")


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
    revoked; 403 account_disabled carries the cloud's own explanation;
    anything else passes the cloud's detail through honestly.
    """
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
    except ValueError:
        body = {}
    detail = body.get("detail", {}) if isinstance(body, dict) else {}
    if resp.status_code == 402 and isinstance(detail, dict):
        raise HTTPException(429, detail=_quota_message(detail))
    if (resp.status_code == 403 and isinstance(detail, dict)
            and detail.get("error") == "account_disabled"):
        message = detail.get("message")
        if not isinstance(message, str) or not message.strip():
            message = _DISABLED_MSG
        raise HTTPException(403, detail=message)
    if resp.status_code == 401:
        raise HTTPException(502, detail=(
            "Forager no longer recognizes this device (it may have been "
            "removed from the account). Sign in again under Settings, AI."))
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
                       mime_type: str = "", text: str = ""):
        """One POST /v1/ai/analyze round trip; returns the model's answer.

        The forwarder asks the model for the same JSON shapes the local
        providers ask for, and the proxy hands the reply back as text
        ({"result": {"text": "<reply>"}}). That text is parsed here the way a
        direct provider's reply is (code fences stripped), so the answer is a
        dict or a list. A Forager that sends the fields already parsed (older
        releases, and the stub forwarder) keeps working: a result with no
        text is used as it is.
        """
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
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            raise HTTPException(502, detail=_UNREADABLE_MSG)
        # The cloud already metered these tokens against the account; record
        # them locally too so the usage card's history includes cloud work.
        try:
            from ..services import usage
            usage.record("cloud", int(payload.get("tokens") or 0))
        except Exception:
            pass
        result = payload.get("result")
        if isinstance(result, dict) and isinstance(result.get("text"), str):
            try:
                parsed = parse_json_response(result["text"])
            except ValueError:
                raise HTTPException(502, detail=_UNREADABLE_MSG)
            return parsed if isinstance(parsed, (dict, list)) else {}
        return result if isinstance(result, dict) else {}

    async def analyze_food(self, image_data: bytes, mime_type: str) -> AnalysisResult:
        data = await self._analyze("food", image_data, mime_type)
        # One item per photo, like every provider: a list reply gives its
        # first object.
        data = _first_dict(data)
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
        return _first_dict(data) or None

    async def health_check(self) -> bool:
        """Reachable-and-linked check via GET /v1/instance/me, cached.

        A disabled account still answers /v1/instance/me (so the settings
        page can say why scans stopped) with account_disabled set; that
        counts as not usable."""
        now = time.monotonic()
        if self._health_ok is not None and now - self._health_ts < _HEALTH_CACHE_TTL:
            return self._health_ok
        try:
            async with self._client(_HEALTH_TIMEOUT) as client:
                resp = await client.get(f"{self.base_url}/v1/instance/me",
                                        headers=self._headers())
            ok = resp.status_code == 200
            if ok:
                try:
                    body = resp.json()
                except ValueError:
                    body = None
                if isinstance(body, dict) and body.get("account_disabled"):
                    ok = False
            self._health_ok = ok
        except httpx.HTTPError:
            self._health_ok = False
        self._health_ts = now
        return self._health_ok
