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

The proxy covers the food / receipt / enrich task kinds, plus enrich_batch
(a receipt's shelf-life estimates in a few requests, see
estimate_shelf_lives), so recipe extraction, recipe generation, nutrition
estimates, and cook suggestions return None here (the base-class
"unsupported" contract) until the cloud grows those endpoints.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx
from fastapi import HTTPException

from .base import (VisionProvider, clean_text, parse_json_response, _first_dict,
                   _number, _parse_item, _parse_receipt)
from ..models.food import AnalysisResult

logger = logging.getLogger(__name__)

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

# A receipt's shelf-life estimates go to Forager as batches (the enrich_batch
# kind) rather than one enrich call per line: the proxy allows 30 requests a
# minute per install, so a long receipt used to leave its last lines with no
# estimate. Forager takes at most 30 items and 8,000 characters of text per
# request, so the lines are split on both, with room to spare on the text.
_BATCH_KIND = "enrich_batch"
BATCH_MAX_ITEMS = 30
_BATCH_MAX_CHARS = 6000
# A line's fields, each cut to the receipt parser's own length, so no single
# line can come near a request's text limit.
_BATCH_FIELDS = (("name", "product_name", 120), ("brand", "brand", 80),
                 ("category", "category", 40))
# Every Forager answers a task kind it does not know with 400 "Unknown task
# kind: <kind>". With a release from before enrich_batch the estimates go one
# call per line, as they always did. That answer is remembered for an hour, so
# the scans in between skip the batch request, and an updated Forager is
# picked up within the hour.
_UNKNOWN_KIND = "Unknown task kind"
_BATCH_RETRY_SECONDS = 3600


class _UnknownKind(Exception):
    """This Forager does not know the task kind (a release from before it)."""


def _unknown_kind(resp: httpx.Response) -> bool:
    """Whether the proxy turned the request down for its task kind."""
    if resp.status_code != 400:
        return False
    try:
        body = resp.json()
    except ValueError:
        return False
    detail = body.get("detail") if isinstance(body, dict) else None
    return isinstance(detail, str) and detail.startswith(_UNKNOWN_KIND)


def _batch_line(index: int, info: dict) -> dict:
    """One item as a batch line: its 1-based index and whichever of name,
    brand and category it has."""
    line: dict = {"index": index}
    for key, source, limit in _BATCH_FIELDS:
        value = clean_text(info.get(source), limit)
        if value:
            line[key] = value
    return line


def _batch_text(batch: list[dict]) -> str:
    return json.dumps([_batch_line(n, info) for n, info in enumerate(batch, 1)],
                      ensure_ascii=False)


def batch_requests(items: list[dict]) -> list[list[dict]]:
    """The items split into the requests Forager accepts: at most
    BATCH_MAX_ITEMS lines and _BATCH_MAX_CHARS characters of text each, in
    order."""
    batches: list[list[dict]] = []
    batch: list[dict] = []
    size = 0
    for info in items:
        # The line's own text plus the ", " (or the brackets) around it.
        line_size = len(json.dumps(_batch_line(len(batch) + 1, info),
                                   ensure_ascii=False)) + 2
        if batch and (len(batch) >= BATCH_MAX_ITEMS
                      or size + line_size > _BATCH_MAX_CHARS):
            batches.append(batch)
            batch, size = [], 0
        batch.append(info)
        size += line_size
    if batch:
        batches.append(batch)
    return batches


def _plain_fields(entry: dict) -> dict:
    """An answer's text and finite-number fields. Anything else is dropped: a
    nested value, or a NaN or infinity, which JSON cannot carry back out (a
    satellite's estimates travel on from here)."""
    out: dict = {}
    for key, value in entry.items():
        if isinstance(value, str):
            out[key] = value[:200]
        elif _number(value) is not None:
            out[key] = value
    return out


def batch_answers(data, count: int) -> list[dict | None]:
    """The model's batch reply as one answer per line, in line order.

    The reply is {"items": [...]} (a bare list is read too), and each entry
    names the 1-based index of the line it answers. An entry without a usable
    index, with one out of range, or for a line already answered is dropped,
    so a reply that skips, repeats or reorders lines costs only those lines.
    An answer is never matched to a line by its position alone: a wrong
    estimate is worse than the category default.
    """
    entries = data.get("items") if isinstance(data, dict) else data
    answers: list[dict | None] = [None] * count
    if not isinstance(entries, list):
        return answers
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        index = _number(entry.get("index"))
        if index is None or not index.is_integer():
            continue
        slot = int(index) - 1
        if 0 <= slot < count and answers[slot] is None:
            answers[slot] = _plain_fields(entry)
    return answers


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
        # When to try enrich_batch again after a Forager said it has no such
        # kind (see _BATCH_RETRY_SECONDS).
        self._batch_retry_at: float = 0.0

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
                       mime_type: str = "", text: str = "", *,
                       newer_kind: bool = False):
        """One POST /v1/ai/analyze round trip; returns the model's answer.

        The forwarder asks the model for the same JSON shapes the local
        providers ask for, and the proxy hands the reply back as text
        ({"result": {"text": "<reply>"}}). That text is parsed here the way a
        direct provider's reply is (code fences stripped), so the answer is a
        dict or a list. A Forager that sends the fields already parsed (older
        releases, and the stub forwarder) keeps working: a result with no
        text is used as it is.

        newer_kind marks a kind an older Forager may not have; its refusal
        then raises _UnknownKind, so the caller can use the kinds every
        Forager has instead.
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
        if newer_kind and _unknown_kind(resp):
            raise _UnknownKind(kind)
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

    async def _estimate_batch(self, batch: list[dict]) -> list[dict | None] | None:
        """One enrich_batch request: an answer per line, or None when this
        Forager does not know the kind. Any other failure (the quota, the
        network, a reply that cannot be read) costs these lines their
        estimate and nothing more, the way a failed per-line call does."""
        try:
            data = await self._analyze(_BATCH_KIND, text=_batch_text(batch),
                                       newer_kind=True)
        except _UnknownKind:
            return None
        except Exception as exc:  # noqa: BLE001 - an estimate never breaks intake
            logger.warning("Forager shelf-life batch failed, using defaults: %s", exc)
            return [None] * len(batch)
        return batch_answers(data, len(batch))

    async def estimate_shelf_lives(self, items: list[dict]) -> list[dict | None] | None:
        """Estimates for many lines in a few enrich_batch requests sent
        together (see batch_requests), or None when this Forager has no batch
        kind, and the caller then asks once per line as before. An older
        Forager turns the kind down before its rate limit and quota, so
        finding out costs nothing there."""
        if not items:
            return []
        if time.monotonic() < self._batch_retry_at:
            return None
        batches = batch_requests(items)
        results = await asyncio.gather(*(self._estimate_batch(b) for b in batches))
        if all(got is None for got in results):
            self._batch_retry_at = time.monotonic() + _BATCH_RETRY_SECONDS
            return None
        answers: list[dict | None] = []
        for batch, got in zip(batches, results):
            answers.extend(got if got is not None else [None] * len(batch))
        return answers

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
