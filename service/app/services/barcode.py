"""Open Food Facts barcode lookup, shared by /analyze/barcode and /pending/scan."""
import logging
from datetime import date, timedelta

import httpx
from sqlalchemy.orm import Session
from ..config import settings
from ..models.food import FoodItem, FoodCategory, StorageType
from .defaults import apply_defaults

logger = logging.getLogger(__name__)

OFF_UA = "PantryRaider/1.0 (github.com/Syracuse3DPrintingOrg/PantryRaider)"

_OFF_CATEGORY_MAP = [
    # (substring to match in categories_tags, our FoodCategory)
    ("poultry", FoodCategory.poultry),
    ("chicken", FoodCategory.poultry),
    ("turkey", FoodCategory.poultry),
    ("beef",   FoodCategory.meat),
    ("pork",   FoodCategory.meat),
    ("meat",   FoodCategory.meat),
    ("sausage",FoodCategory.meat),
    ("fish",   FoodCategory.seafood),
    ("seafood",FoodCategory.seafood),
    ("shrimp", FoodCategory.seafood),
    ("dairy",  FoodCategory.dairy),
    ("cheese", FoodCategory.dairy),
    ("milk",   FoodCategory.dairy),
    ("yogurt", FoodCategory.dairy),
    ("egg",    FoodCategory.dairy),
    ("butter", FoodCategory.dairy),
    ("cream",  FoodCategory.dairy),
    ("fruit",  FoodCategory.produce),
    ("vegetable", FoodCategory.produce),
    ("salad",  FoodCategory.produce),
    ("bread",  FoodCategory.grains),
    ("cereal", FoodCategory.grains),
    ("pasta",  FoodCategory.grains),
    ("rice",   FoodCategory.grains),
    ("grain",  FoodCategory.grains),
    ("flour",  FoodCategory.grains),
    ("sauce",  FoodCategory.condiments),
    ("condiment", FoodCategory.condiments),
    ("dressing", FoodCategory.condiments),
    ("beverage", FoodCategory.beverages),
    ("drink",  FoodCategory.beverages),
    ("juice",  FoodCategory.beverages),
    ("water",  FoodCategory.beverages),
    ("snack",  FoodCategory.snacks),
    ("chips",  FoodCategory.snacks),
    ("cookie", FoodCategory.snacks),
    ("frozen", FoodCategory.frozen),
    ("canned", FoodCategory.canned),
    ("tinned", FoodCategory.canned),
]

_REFRIGERATED_CATEGORIES = {FoodCategory.dairy, FoodCategory.poultry, FoodCategory.meat,
                             FoodCategory.seafood, FoodCategory.produce}
_DRY_CATEGORIES = {FoodCategory.grains, FoodCategory.canned, FoodCategory.condiments}


class BarcodeNotFound(Exception):
    """Barcode missing from Open Food Facts, or the product has no name."""


class BarcodeServiceError(Exception):
    """Open Food Facts is unreachable or returned an error."""


def _off_category(tags: list[str]) -> FoodCategory:
    joined = " ".join(tags).lower()
    for keyword, cat in _OFF_CATEGORY_MAP:
        if keyword in joined:
            return cat
    return FoodCategory.other


def _off_storage(tags: list[str], category: FoodCategory) -> StorageType:
    joined = " ".join(tags).lower()
    if "frozen" in joined:
        return StorageType.frozen
    if "refrigerated" in joined or "fresh" in joined:
        return StorageType.refrigerated
    if category in _REFRIGERATED_CATEGORIES:
        return StorageType.refrigerated
    if category in _DRY_CATEGORIES:
        return StorageType.dry
    return StorageType.room_temp


async def lookup_barcode(barcode: str, db: Session) -> FoodItem:
    """Look up a barcode in Open Food Facts and return a FoodItem with defaults applied.

    Raises BarcodeNotFound / BarcodeServiceError.
    """
    async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": OFF_UA}) as client:
        try:
            r = await client.get(
                f"https://world.openfoodfacts.org/api/v0/product/{barcode}.json"
            )
        except httpx.HTTPError as e:
            raise BarcodeServiceError(f"Open Food Facts unreachable: {e}")
    if r.status_code != 200:
        raise BarcodeServiceError("Open Food Facts unavailable")
    data = r.json()
    if data.get("status") != 1:
        # OFF didn't recognise this barcode: optionally try the LLM
        if settings.barcode_llm_fallback:
            item = await _llm_identify_barcode(barcode)
            if item:
                return apply_defaults(item, db, infer_storage=False)
        raise BarcodeNotFound(f"Barcode {barcode} not found in Open Food Facts")

    product = data["product"]
    name = (product.get("product_name_en") or product.get("product_name") or "").strip()
    if not name:
        raise BarcodeNotFound("Product found but has no name")

    brand = (product.get("brands") or "").split(",")[0].strip() or None
    tags = product.get("categories_tags", []) + product.get("labels_tags", [])
    category = _off_category(tags)
    storage = _off_storage(tags, category)
    generic = (product.get("generic_name_en") or product.get("generic_name") or "")

    # OFF names are contributor-entered: often SHOUTING or missing the brand
    # entirely (Dr Pepper Zero's name is just "zero sugar").
    if name.isupper():
        name = name.title()
    if brand and brand.lower() not in name.lower():
        name = f"{brand} {name}"

    item = FoodItem(
        name=name,
        quantity=1.0,
        unit="item",
        storage_type=storage,
        category=category,
        brand=brand,
        confidence=0.9,
    )

    enriched = await _llm_enrich(item, product, generic, tags)

    # OFF tags ("en:yogurts", "en:potato-chips") let branded names match
    # generic defaults rules like "yogurt" or "chips". When the LLM didn't
    # answer, also let rules correct the tag-based storage guess.
    tag_text = " ".join(tags).replace("-", " ")
    return apply_defaults(item, db, extra_match_text=f"{generic} {tag_text}",
                          infer_storage=not enriched)


_BARCODE_IDENTIFY_PROMPT = """
A product with barcode/UPC "{barcode}" was not found in the Open Food Facts database.
Based on your training knowledge, what product might have this barcode?
Return a JSON object:
{{
  "name": "specific product name with brand, e.g. 'Heinz Tomato Ketchup', or null if unknown",
  "brand": "brand name or null",
  "category": "Poultry | Meat | Seafood | Dairy | Produce | Grains | Condiments | Beverages | Snacks | Frozen | Canned | Other",
  "storage_type": "refrigerated | frozen | room_temp | dry",
  "shelf_life_days": 365
}}
If you don't recognise this barcode, set "name" to null.
Return ONLY valid JSON. No markdown, no explanation.
""".strip()


async def _llm_identify_barcode(barcode: str) -> FoodItem | None:
    """Ask the LLM to identify a barcode not found in Open Food Facts.

    Passes the barcode as product data so the existing enrich_product path
    handles it. Returns a low-confidence FoodItem or None if unrecognised.
    """
    try:
        from ..dependencies import get_enrich_provider
        provider = get_enrich_provider()
        result = await provider.enrich_product({
            "barcode": barcode,
            "product_name": f"UNKNOWN: barcode {barcode} not in Open Food Facts database",
            "note": "Identify this product by its UPC/EAN barcode if you recognise it. "
                    "Return your best guess; leave name as-is if completely unknown.",
        })
    except Exception as e:
        logger.warning("LLM barcode identification failed for %s: %s", barcode, e)
        return None
    if not isinstance(result, dict) or not result.get("name"):
        return None
    item = FoodItem(
        name=str(result["name"]).strip(),
        quantity=1.0,
        unit="item",
        brand=result.get("brand") or None,
        confidence=0.35,
    )
    try:
        item.category = FoodCategory(result.get("category"))
    except (ValueError, TypeError):
        pass
    try:
        item.storage_type = StorageType(result.get("storage_type"))
    except (ValueError, TypeError):
        pass
    try:
        days = int(result.get("shelf_life_days"))
        if 0 < days <= 3650:
            item.best_by_date = date.today() + timedelta(days=days)
    except (ValueError, TypeError):
        pass
    return item


async def _llm_enrich(item: FoodItem, product: dict, generic: str, tags: list[str]) -> bool:
    """Refine name/category/storage/best-by via the LLM. Returns True on success."""
    if settings.barcode_enrichment != "llm":
        return False
    try:
        from ..dependencies import get_enrich_provider
        result = await get_enrich_provider().enrich_product({
            "product_name": product.get("product_name_en") or product.get("product_name"),
            "generic_name": generic or None,
            "brands": product.get("brands"),
            "categories_tags": tags[:20],
            "quantity": product.get("quantity"),
        })
    except Exception as e:
        logger.warning("Barcode LLM enrichment failed, using heuristics: %s", e)
        return False
    if not isinstance(result, dict):
        return False

    if result.get("name"):
        item.name = str(result["name"]).strip()
    if result.get("brand"):
        item.brand = str(result["brand"]).strip()
    try:
        item.category = FoodCategory(result.get("category"))
    except (ValueError, TypeError):
        pass
    try:
        item.storage_type = StorageType(result.get("storage_type"))
    except (ValueError, TypeError):
        pass
    try:
        days = int(result.get("shelf_life_days"))
        if 0 < days <= 3650:
            item.best_by_date = date.today() + timedelta(days=days)
    except (ValueError, TypeError):
        pass
    return True
