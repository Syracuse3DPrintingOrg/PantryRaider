"""Open Food Facts read API v3 (FoodAssistant-n25uy).

The lookups moved off the deprecated /api/v0/ route. These tests drive the
real lookup code against recorded v3-shaped replies through an httpx
MockTransport, so no network is touched.
"""
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))

from app.config import APP_VERSION, settings  # noqa: E402
from app.models.food import FoodCategory, StorageType  # noqa: E402
from app.services import barcode  # noqa: E402

# Recorded from world.openfoodfacts.org/api/v3/product/3017620422003 with the
# app's fields= list, trimmed to the fields this app reads.
FOUND = {
    "code": "3017620422003",
    "errors": [],
    "product": {
        "brands": "Nutella, Ferrero",
        "categories_tags": ["en:breakfasts", "en:spreads", "en:sweet-spreads"],
        "generic_name": "Pâte à tartiner aux noisettes et au cacao",
        "generic_name_en": "Hazelnut And Cocoa Spread",
        "labels_tags": ["en:vegetarian", "en:no-gluten"],
        "nutriments": {
            "energy-kcal_100g": 539, "proteins_100g": 6.3,
            "carbohydrates_100g": 57.5, "fat_100g": 30.9,
        },
        "product_name": "Nutella",
        "product_name_en": "Nutella",
        "quantity": "400 g",
        "serving_quantity": 15,
        "serving_size": "15 g",
    },
    "result": {"id": "product_found", "lc_name": "Product found",
               "name": "Product found"},
    "status": "success",
    "warnings": [],
}

# Recorded reply for an unknown code: HTTP 404 with a failure envelope.
NOT_FOUND = {
    "code": "00000017",
    "errors": [{
        "field": {"id": "code", "value": "00000017"},
        "impact": {"id": "failure", "lc_name": "Failure", "name": "Failure"},
        "message": {"id": "product_not_found", "lc_name": "", "name": ""},
    }],
    "result": {"id": "product_not_found", "lc_name": "Product not found",
               "name": "Product not found"},
    "status": "failure",
    "warnings": [],
}


def _patch_client(monkeypatch, status_code, body, seen=None):
    """Route barcode.httpx.AsyncClient through a MockTransport."""
    real_client = httpx.AsyncClient

    def handler(request):
        if seen is not None:
            seen.append(request)
        if isinstance(body, Exception):
            raise body
        if isinstance(body, str):
            return httpx.Response(status_code, text=body)
        return httpx.Response(status_code, json=body)

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(barcode.httpx, "AsyncClient", factory)


@pytest.fixture
def no_llm(monkeypatch):
    monkeypatch.setattr(settings, "barcode_enrichment", "none")
    monkeypatch.setattr(settings, "barcode_llm_fallback", False)
    monkeypatch.setattr(barcode, "apply_defaults", lambda item, db, **kw: item)


# --- pure pieces ----------------------------------------------------------

def test_product_url_is_v3_with_fields():
    url = barcode.off_product_url("3017620422003")
    assert url.startswith(
        "https://world.openfoodfacts.org/api/v3/product/3017620422003?fields=")
    assert "/api/v0/" not in url
    fields = url.split("fields=", 1)[1].split(",")
    for needed in ("product_name", "brands", "categories_tags", "nutriments",
                   "serving_size", "serving_quantity"):
        assert needed in fields


def test_product_url_escapes_the_code():
    assert "/product/a%2Fb?" in barcode.off_product_url("a/b")


def test_user_agent_names_app_version_and_contact():
    assert barcode.OFF_UA.startswith(f"PantryRaider/{APP_VERSION} (")
    assert "github.com/Syracuse3DPrintingOrg/PantryRaider" in barcode.OFF_UA


def test_parse_found():
    assert barcode.parse_off_response(200, FOUND)["product_name"] == "Nutella"


def test_parse_found_with_warnings():
    body = dict(FOUND, status="success_with_warnings")
    assert barcode.parse_off_response(200, body)["brands"] == "Nutella, Ferrero"


def test_parse_not_found_is_none():
    assert barcode.parse_off_response(404, NOT_FOUND) is None


@pytest.mark.parametrize("code,body", [
    (500, {"status": "failure"}),
    (503, None),
    (404, None),
    (200, {"status": 1, "product": {}}),   # a v0-shaped body is not trusted
    (200, "not json"),
])
def test_parse_other_replies_are_service_errors(code, body):
    with pytest.raises(barcode.BarcodeServiceError):
        barcode.parse_off_response(code, body)


# --- lookup_barcode -------------------------------------------------------

@pytest.mark.anyio
async def test_lookup_found_builds_same_item(monkeypatch, no_llm):
    seen = []
    _patch_client(monkeypatch, 200, FOUND, seen)
    item = await barcode.lookup_barcode("3017620422003", db=None)
    assert item.name == "Nutella"
    assert item.brand == "Nutella"
    assert item.confidence == 0.9
    assert item.category == FoodCategory.other
    assert item.storage_type == StorageType.room_temp
    req = seen[0]
    assert req.url.path == "/api/v3/product/3017620422003"
    assert req.headers["User-Agent"] == barcode.OFF_UA


@pytest.mark.anyio
async def test_lookup_not_found_raises_not_found(monkeypatch, no_llm):
    _patch_client(monkeypatch, 404, NOT_FOUND)
    with pytest.raises(barcode.BarcodeNotFound) as exc:
        await barcode.lookup_barcode("0000000000017", db=None)
    assert not isinstance(exc.value, barcode.BarcodeStoreLocal)


@pytest.mark.anyio
async def test_lookup_store_local_not_found(monkeypatch, no_llm):
    _patch_client(monkeypatch, 404, NOT_FOUND)
    with pytest.raises(barcode.BarcodeStoreLocal):
        await barcode.lookup_barcode("212345678905", db=None)


@pytest.mark.anyio
async def test_lookup_not_found_uses_llm_fallback(monkeypatch, no_llm):
    from app.models.food import FoodItem

    monkeypatch.setattr(settings, "barcode_llm_fallback", True)

    async def _guess(code):
        return FoodItem(name="Thing (unverified guess)", confidence=0.2)

    monkeypatch.setattr(barcode, "_llm_identify_barcode", _guess)
    _patch_client(monkeypatch, 404, NOT_FOUND)
    item = await barcode.lookup_barcode("0000000000017", db=None)
    assert item.name == "Thing (unverified guess)"


@pytest.mark.anyio
async def test_lookup_nameless_product_is_not_found(monkeypatch, no_llm):
    body = dict(FOUND, product={"brands": "Ferrero"})
    _patch_client(monkeypatch, 200, body)
    with pytest.raises(barcode.BarcodeNotFound):
        await barcode.lookup_barcode("3017620422003", db=None)


@pytest.mark.anyio
async def test_lookup_server_error_is_service_error(monkeypatch, no_llm):
    _patch_client(monkeypatch, 502, "Bad Gateway")
    with pytest.raises(barcode.BarcodeServiceError):
        await barcode.lookup_barcode("3017620422003", db=None)


@pytest.mark.anyio
async def test_lookup_unreachable_is_service_error(monkeypatch, no_llm):
    _patch_client(monkeypatch, 200, httpx.ConnectError("down"))
    with pytest.raises(barcode.BarcodeServiceError):
        await barcode.lookup_barcode("3017620422003", db=None)


# --- fetch_off_product (post-consume nutrition) ---------------------------

@pytest.mark.anyio
async def test_fetch_product_returns_nutrition(monkeypatch):
    _patch_client(monkeypatch, 200, FOUND)
    product = await barcode.fetch_off_product("3017620422003")
    assert product["nutriments"]["energy-kcal_100g"] == 539
    assert product["serving_quantity"] == 15


@pytest.mark.anyio
@pytest.mark.parametrize("code,body", [
    (404, NOT_FOUND), (500, "oops"), (200, httpx.ConnectError("down")),
])
async def test_fetch_product_never_raises(monkeypatch, code, body):
    _patch_client(monkeypatch, code, body)
    assert await barcode.fetch_off_product("3017620422003") is None
