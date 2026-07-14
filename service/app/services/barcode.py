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


class BarcodeStoreLocal(BarcodeNotFound):
    """Barcode is in a store-assigned/random-weight range and cannot be looked up.

    These codes are printed at the deli or meat counter for that store alone
    (a random-weight scale label, for example), so no catalog, OFF included,
    has ever heard of them and never will. Guessing from the digits produced a
    deli-meat scan that got imported as "bananas". Rather than let the LLM
    fallback fabricate a plausible-sounding product, the caller should ask the
    user to take a photo of the item instead.
    """


class BarcodeServiceError(Exception):
    """Open Food Facts is unreachable or returned an error."""


def is_store_local_barcode(barcode: str) -> bool:
    """True when ``barcode`` falls in a GS1 store-assigned/restricted-use range.

    These prefixes are reserved for in-store marking (random-weight scale
    labels, deli-counter tags) and are never assigned to a specific product by
    GS1, so no external catalog can ever resolve them and asking an LLM to
    guess just invites a hallucination:

    - UPC-A (12 digits): leading digit "2" is restricted circulation, random
      weight items, assigned by the store/local retailer.
    - EAN-13 (13 digits): prefixes 020-029 and 200-299 are the equivalent
      in-store/restricted ranges.

    Anything that isn't a plain 12- or 13-digit numeric code (weird lengths,
    non-digits) is left alone here; that's just an unrecognized code, not a
    store-local one.
    """
    barcode = (barcode or "").strip()
    if not barcode.isdigit():
        return False
    if len(barcode) == 12:
        return barcode[0] == "2"
    if len(barcode) == 13:
        prefix3 = barcode[:3]
        return prefix3.startswith("02") or (200 <= int(prefix3) <= 299)
    return False


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
        # A store-assigned/random-weight code can never be resolved, by OFF or
        # by an LLM guessing from the digits alone, so this check runs before
        # the LLM fallback and skips it entirely rather than risk a
        # hallucinated product.
        if is_store_local_barcode(barcode):
            raise BarcodeStoreLocal(
                f"Barcode {barcode} looks like a store-assigned label, not a "
                "product barcode"
            )
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


async def _llm_identify_barcode(barcode: str) -> FoodItem | None:
    """Ask the LLM to identify a barcode not found in Open Food Facts.

    Uses the provider's dedicated identify_barcode path, whose prompt tells the
    model to return null rather than guess: a bare barcode number cannot be
    mapped to a product, and the old path (reusing enrich_product) invented
    plausible brands from the digits (a Stella Artois scan came back
    "Campbell's"). Returns a low-confidence, plainly-flagged FoodItem, or None
    when the model does not recognize the code (or the provider is text-only
    unsupported), so the caller reports the barcode as simply not found.
    """
    try:
        from ..dependencies import get_enrich_provider
        provider = get_enrich_provider()
        result = await provider.identify_barcode(barcode)
    except Exception as e:
        logger.warning("LLM barcode identification failed for %s: %s", barcode, e)
        return None
    if not isinstance(result, dict):
        return None
    name = str(result.get("name") or "").strip()
    # An empty/"unknown" name is the model correctly declining to guess.
    if not name or name.lower().startswith(("unknown", "null", "none")):
        return None
    # Flag it as an unverified guess so it never reads like a confirmed scan.
    item = FoodItem(
        name=f"{name} (unverified guess)",
        quantity=1.0,
        unit="item",
        brand=result.get("brand") or None,
        confidence=0.2,
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
            item.best_by_source = "llm"
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
    if settings.llm_expiry_enabled:
        # Route the shelf-life + storage answer through the shared, tested
        # mapper so a free-form location ("keep refrigerated", "chilled") lands
        # in the right bucket and an absurd day count is clamped.
        from .shelf_life import parse_llm_shelf_life, apply_shelf_life
        if apply_shelf_life(item, parse_llm_shelf_life(result)):
            item.best_by_source = "llm"
    else:
        try:
            item.storage_type = StorageType(result.get("storage_type"))
        except (ValueError, TypeError):
            pass
        try:
            days = int(result.get("shelf_life_days"))
            if 0 < days <= 3650:
                item.best_by_date = date.today() + timedelta(days=days)
                item.best_by_source = "llm"
        except (ValueError, TypeError):
            pass
    return True
