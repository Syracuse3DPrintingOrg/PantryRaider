"""AI on a satellite, run by its main server (0.21).

A satellite whose server relays AI (settings.fleet_relay_active('ai')) holds
no provider key. Every provider call becomes one POST to the server's
/api/fleet/ai, which runs it with the server's own provider and answers the
result. The server counts the tokens against its own budget, so nothing is
recorded here, and a budget refusal (429) reaches the satellite's page as it
would on the server.
"""
from __future__ import annotations

import base64

import httpx
from fastapi import HTTPException

from ..models.food import AnalysisResult
from ..services.ttl_cache import TTLCache
from .base import VisionProvider

UNREACHABLE_MSG = "The main server is not reachable. This will return when it is."
FAILED_MSG = "The main server could not run that AI action."

_client: httpx.AsyncClient | None = None

# health_check answers per role, for a minute: /health is public and polled,
# and each check would otherwise be one request to the main server (and up to
# a connect timeout when the server is away).
_health_vision = TTLCache(60.0)
_health_enrich = TTLCache(60.0)


def _texts(values, max_items: int, max_len: int) -> list[str]:
    """A list of strings in the shape the server accepts."""
    return [str(v or "")[:max_len] for v in list(values or [])[:max_items]]


# Picture types the server takes, and the other names browsers and phones use
# for the same files.
_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif",
                "image/heic", "image/heif"}
_MIME_ALIASES = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg",
                 "image/x-png": "image/png"}


def _mime(value) -> str:
    mime = str(value or "").split(";")[0].strip().lower()
    mime = _MIME_ALIASES.get(mime, mime)
    return mime if mime in _IMAGE_TYPES else "image/jpeg"


def _bounded_int(value, lo: int, hi: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=5.0))
    return _client


def _url() -> str:
    from ..config import settings
    return (settings.remote_server_url or "").rstrip("/") + "/api/fleet/ai"


def _detail(resp) -> str:
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 - a non-JSON error page
        return FAILED_MSG
    detail = body.get("detail") if isinstance(body, dict) else None
    return detail if isinstance(detail, str) and detail else FAILED_MSG


class FleetProvider(VisionProvider):
    """A VisionProvider whose every call runs on the main server."""

    def __init__(self, role: str) -> None:
        self.role = "enrich" if role == "enrich" else "vision"

    async def _call(self, op: str, args: dict | None = None, *,
                    image: bytes | None = None, mime_type: str | None = None):
        from ..services import upstream
        body: dict = {"role": self.role, "op": op, "args": args or {}}
        if image is not None:
            body["image_b64"] = base64.b64encode(image).decode("ascii")
            body["mime_type"] = _mime(mime_type)
        try:
            resp = await _http().post(_url(), json=body, **upstream.request_kwargs())
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail=UNREACHABLE_MSG) from None
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=_detail(resp))
        try:
            data = resp.json()
        except ValueError:
            raise HTTPException(status_code=502, detail=FAILED_MSG) from None
        if not isinstance(data, dict) or not data.get("ok"):
            raise HTTPException(status_code=502, detail=FAILED_MSG)
        return data.get("result")

    async def _analysis(self, op: str, image_data: bytes, mime_type: str) -> AnalysisResult:
        result = await self._call(op, image=image_data, mime_type=mime_type)
        try:
            return AnalysisResult.model_validate(result)
        except Exception:  # noqa: BLE001 - a reply this device cannot read
            raise HTTPException(status_code=502, detail=FAILED_MSG) from None

    async def analyze_food(self, image_data: bytes, mime_type: str) -> AnalysisResult:
        return await self._analysis("analyze_food", image_data, mime_type)

    async def analyze_receipt(self, image_data: bytes, mime_type: str) -> AnalysisResult:
        return await self._analysis("analyze_receipt", image_data, mime_type)

    async def extract_receipt_prices(self, image_data: bytes, mime_type: str) -> str | None:
        return await self._call("extract_receipt_prices", image=image_data,
                                mime_type=mime_type)

    async def health_check(self) -> bool:
        cache = _health_enrich if self.role == "enrich" else _health_vision
        cached = cache.get()
        if cached is not None:
            return cached
        try:
            ok = bool(await self._call("health_check"))
        except Exception:  # noqa: BLE001 - a health check never raises
            ok = False
        cache.set(ok)
        return ok

    async def enrich_product(self, info: dict) -> dict | None:
        return await self._call("enrich_product", {"info": info})

    async def estimate_shelf_lives(self, items: list[dict]) -> list[dict | None] | None:
        """The main server's batch estimate, so a long receipt reaches the
        server's Forager as a few requests rather than one per line. None when
        the server has no batch path (its provider has none, or it is a
        release from before this op, which refuses the op with 400), and the
        caller then asks line by line as before."""
        try:
            result = await self._call("estimate_shelf_lives", {"items": items})
        except HTTPException as exc:
            if exc.status_code == 400:
                return None
            raise
        if not isinstance(result, list) or len(result) != len(items):
            return None
        return [r if isinstance(r, dict) else None for r in result]

    async def generate_recipe(self, name: str, extra_instructions: str = "") -> dict | None:
        return await self._call("generate_recipe", {
            "name": str(name or "")[:200],
            "extra_instructions": str(extra_instructions or "")[:2000]})

    async def suggest_from_inventory(self, items: list[str], limit: int = 8,
                                     preferences: str = "") -> list[dict] | None:
        return await self._call("suggest_from_inventory", {
            "items": _texts(items, 500, 200), "limit": _bounded_int(limit, 1, 50, 8),
            "preferences": str(preferences or "")[:2000]})

    async def estimate_nutrition(self, name: str, servings: float = 1.0) -> dict | None:
        return await self._call("estimate_nutrition", {"name": str(name or "")[:200],
                                                       "servings": servings})

    async def extract_recipe(self, image_data: bytes | None = None,
                             mime_type: str | None = None,
                             page_text: str | None = None) -> dict | None:
        if image_data is not None:
            return await self._call("extract_recipe", image=image_data, mime_type=mime_type)
        return await self._call("extract_recipe", {"page_text": str(page_text or "")[:200000]})

    async def optimize_recipe(self, recipe: dict) -> dict | None:
        return await self._call("optimize_recipe", {"recipe": recipe})

    async def parse_ingredients(self, lines: list[str]) -> list[dict] | None:
        return await self._call("parse_ingredients", {"lines": _texts(lines, 200, 500)})

    async def identify_barcode(self, barcode: str) -> dict | None:
        return await self._call("identify_barcode", {"barcode": barcode})
