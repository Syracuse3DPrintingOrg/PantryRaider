"""Open Food Facts barcode lookup, shared by /analyze/barcode and /pending/scan."""
import asyncio
import logging
import re
from datetime import date, timedelta
from urllib.parse import quote

import httpx
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session
from ..config import APP_VERSION, settings
from ..models.food import FoodItem, FoodCategory, StorageType
from ..providers.base import clean_text
from . import usage
from .defaults import apply_defaults
from .package_size import apply_package_size, parse_size
from .tls import default_context
from ..plain_digits import is_plain_digits

logger = logging.getLogger(__name__)

# Open Food Facts asks every API client to identify itself as
# AppName/Version (contact), so a misbehaving client can be reached rather
# than blocked.
OFF_UA = (f"PantryRaider/{APP_VERSION} "
          "(https://github.com/Syracuse3DPrintingOrg/PantryRaider)")

# The read API v3 product endpoint. The legacy /api/v0/ route is deprecated.
OFF_PRODUCT_URL = "https://world.openfoodfacts.org/api/v3/product/{code}"

# Only the product fields this app reads, so a lookup downloads a few hundred
# bytes instead of the full product record: the name and brand for display,
# the tags and generic name for category, storage and defaults matching, the
# quantity for the LLM enrichment, and the nutrition facts for the
# post-consume calorie offer (services/nutrition.calorie_offer).
OFF_FIELDS = (
    "product_name", "product_name_en", "generic_name", "generic_name_en",
    "brands", "categories_tags", "labels_tags", "quantity",
    "nutriments", "serving_size", "serving_quantity",
)

# v3 answers "success_with_warnings" when the product was found but some
# part of the request (an unknown field, say) drew a warning.
_OFF_FOUND = ("success", "success_with_warnings")

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

# Longest product name or brand an AI answer may set, so a rambling reply
# never becomes a paragraph-long Grocy product name.
_AI_NAME_MAX = 120
_AI_BRAND_MAX = 80


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


def off_product_url(barcode: str) -> str:
    """The v3 product URL for ``barcode``, limited to OFF_FIELDS."""
    return (OFF_PRODUCT_URL.format(code=quote(barcode, safe=""))
            + "?fields=" + ",".join(OFF_FIELDS))


def parse_off_response(status_code: int, data) -> dict | None:
    """Pure reading of a v3 product reply.

    Returns the product dict when Open Food Facts found the barcode, None
    when it answered that the product does not exist, and raises
    BarcodeServiceError for anything else (a server error, an unexpected
    body). Unlike v0, which answered 200 with ``status: 0``, v3 reports a
    missing product as HTTP 404 with ``status: "failure"``, so that pairing
    is the only 404 read as "not found"."""
    if not isinstance(data, dict):
        raise BarcodeServiceError("Open Food Facts unavailable")
    status = data.get("status")
    if status_code == 200 and status in _OFF_FOUND:
        product = data.get("product")
        if isinstance(product, dict):
            return product
        return None
    if status_code in (200, 404) and status == "failure":
        return None
    raise BarcodeServiceError("Open Food Facts unavailable")


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
    if not is_plain_digits(barcode):
        return False
    if len(barcode) == 12:
        return barcode[0] == "2"
    if len(barcode) == 13:
        prefix3 = barcode[:3]
        return prefix3.startswith("02") or (200 <= int(prefix3) <= 299)
    return False


def off_display_name(product: dict) -> str:
    """Human display name for an OFF product dict, or "" when it has none.

    OFF names are contributor-entered: often SHOUTING or missing the brand
    entirely (Dr Pepper Zero's name is just "zero sugar"), so the brand is
    prefixed when the name does not already carry it. Pure and shared by the
    full lookup below and the post-consume nutrition fetch."""
    name = (product.get("product_name_en") or product.get("product_name") or "").strip()
    if not name:
        return ""
    brand = (product.get("brands") or "").split(",")[0].strip()
    if name.isupper():
        name = name.title()
    if brand and brand.lower() not in name.lower():
        name = f"{brand} {name}"
    return name


async def fetch_off_product(barcode: str) -> dict | None:
    """Fetch the raw OFF product dict for ``barcode``, or None.

    A slimmer sibling of lookup_barcode used after a consume to read the
    product's nutrition facts (FoodAssistant-4mi3): short timeout, no LLM
    fallback, and it never raises, so a consume reply can only ever be
    enriched by it, never delayed by an error."""
    barcode = (barcode or "").strip()
    if not barcode or is_store_local_barcode(barcode):
        return None
    try:
        async with httpx.AsyncClient(timeout=4.0, headers={"User-Agent": OFF_UA},
                                     verify=default_context()) as client:
            r = await client.get(off_product_url(barcode))
        return parse_off_response(r.status_code, r.json())
    except Exception:  # noqa: BLE001 - best-effort by design
        return None


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


def _community_fix(barcode: str) -> dict | None:
    """The community fix for a barcode (FoodAssistant-mypf0), or None.
    A store-local code means a different product in every store, so it is
    never taken from the list even if one slipped in."""
    if is_store_local_barcode(barcode):
        return None
    from . import community_barcodes
    return community_barcodes.lookup(barcode)


def apply_community_fix(item: FoodItem, fix: dict) -> None:
    """Put a community fix's name, brand and category on ``item``. Pure.

    The fix is what several trusted kitchens agreed the product is, so it
    beats both the Open Food Facts entry and an AI rename."""
    item.name = fix["name"]
    if fix.get("brand"):
        item.brand = fix["brand"]
    if fix.get("category"):
        try:
            item.category = FoodCategory(fix["category"])
        except ValueError:
            pass
    item.confidence = max(item.confidence or 0.0, 0.9)


async def _community_item(fix: dict, db: Session) -> FoodItem:
    item = FoodItem(name=fix["name"], quantity=1.0, unit="item", confidence=0.9)
    apply_community_fix(item, fix)
    return await run_in_threadpool(apply_defaults, item, db)


async def lookup_barcode(barcode: str, db: Session, guess: bool = True) -> FoodItem:
    """Look up a barcode and return a FoodItem with defaults applied.

    Open Food Facts supplies the product, and a community fix (the names
    other kitchens corrected, services/community_barcodes.py) replaces its
    name, brand and category. A barcode Open Food Facts does not know, or a
    lookup while it is unreachable, is answered from the fix alone, before
    any AI guess. ``guess`` False skips that guess, for digits that may be
    misread (a receipt's item numbers). Raises BarcodeNotFound /
    BarcodeServiceError.
    """
    fix = _community_fix(barcode)
    async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": OFF_UA},
                                 verify=default_context()) as client:
        try:
            r = await client.get(off_product_url(barcode))
        except httpx.HTTPError as e:
            if fix:
                return await _community_item(fix, db)
            raise BarcodeServiceError(f"Open Food Facts unreachable: {e}")
    try:
        data = r.json()
    except ValueError:
        data = None
    try:
        product = parse_off_response(r.status_code, data)
    except BarcodeServiceError:
        if fix:
            return await _community_item(fix, db)
        raise
    if product is None and fix:
        return await _community_item(fix, db)
    if product is None:
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
        if guess and settings.barcode_llm_fallback:
            item = await _llm_identify_barcode(barcode)
            if item:
                return await run_in_threadpool(
                    apply_defaults, item, db, infer_storage=False)
        raise BarcodeNotFound(f"Barcode {barcode} not found in Open Food Facts")

    name = off_display_name(product)
    if not name and fix:
        name = fix["name"]
    if not name:
        raise BarcodeNotFound("Product found but has no name")

    brand = (product.get("brands") or "").split(",")[0].strip() or None
    tags = product.get("categories_tags", []) + product.get("labels_tags", [])
    category = _off_category(tags)
    storage = _off_storage(tags, category)
    generic = (product.get("generic_name_en") or product.get("generic_name") or "")

    # What the package holds, from Open Food Facts' own "quantity" text
    # ("1.89 l", "16.9 fl oz (500 ml)", "6 x 330 ml"). None when it is
    # missing or names no weight or volume.
    size = parse_size(product.get("quantity"))
    item = FoodItem(
        name=name,
        quantity=1.0,
        unit="item",
        package_size=size[0] if size else None,
        package_unit=size[1] if size else None,
        storage_type=storage,
        category=category,
        brand=brand,
        confidence=0.9,
    )

    enriched = await _llm_enrich(item, product, generic, tags)
    if fix:
        apply_community_fix(item, fix)
    # A cleaned-up name can carry the size as well ("Whole Milk 64 oz"); it
    # comes out of the name when it is the same size, or fills a missing one.
    item = apply_package_size(item)

    # OFF tags ("en:yogurts", "en:potato-chips") let branded names match
    # generic defaults rules like "yogurt" or "chips". When the LLM didn't
    # answer, also let rules correct the tag-based storage guess.
    tag_text = " ".join(tags).replace("-", " ")
    return await run_in_threadpool(
        apply_defaults, item, db, extra_match_text=f"{generic} {tag_text}",
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
    Once the monthly AI budget is reached it makes no call at all.
    """
    if usage.over_budget():
        return None
    try:
        from ..dependencies import get_enrich_provider
        provider = get_enrich_provider()
        result = await provider.identify_barcode(barcode)
    except Exception as e:
        logger.warning("LLM barcode identification failed for %s: %s", barcode, e)
        return None
    if not isinstance(result, dict):
        return None
    name = clean_text(result.get("name"), _AI_NAME_MAX)
    # An empty/"unknown" name is the model correctly declining to guess.
    if not name or name.lower().startswith(("unknown", "null", "none")):
        return None
    # Flag it as an unverified guess so it never reads like a confirmed scan.
    item = FoodItem(
        name=f"{name} (unverified guess)",
        quantity=1.0,
        unit="item",
        brand=clean_text(result.get("brand"), _AI_BRAND_MAX),
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
    except (ValueError, TypeError, OverflowError):
        pass
    return item


async def _llm_enrich(item: FoodItem, product: dict, generic: str, tags: list[str]) -> bool:
    """Refine name/category/storage/best-by via the LLM. Returns True on success.

    Once the monthly AI budget is reached it makes no call and returns False,
    so the scan keeps the Open Food Facts data and the storage heuristics.
    """
    if settings.barcode_enrichment != "llm":
        return False
    if usage.over_budget():
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

    name = clean_text(result.get("name"), _AI_NAME_MAX)
    if name:
        item.name = name
    brand = clean_text(result.get("brand"), _AI_BRAND_MAX)
    if brand:
        item.brand = brand
    try:
        item.category = FoodCategory(result.get("category"))
    except (ValueError, TypeError):
        pass
    if settings.llm_expiry_effective():
        # Route the shelf-life + storage answer through the shared, tested
        # mapper so a free-form location ("keep refrigerated", "chilled") lands
        # in the right bucket and an absurd day count is clamped. A day count
        # it cannot read (an infinite one) just leaves the heuristics' answer.
        from .shelf_life import parse_llm_shelf_life, apply_shelf_life
        try:
            if apply_shelf_life(item, parse_llm_shelf_life(result)):
                item.best_by_source = "llm"
        except (ValueError, TypeError, OverflowError):
            pass
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
        except (ValueError, TypeError, OverflowError):
            pass
    return True


# --- Item numbers printed on a receipt -------------------------------------------

def _gs1_check_digit(body: str) -> str:
    """The GS1 check digit for ``body`` (UPC, EAN): weights 3 and 1 from the
    right. Pure."""
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return str((10 - total % 10) % 10)


def receipt_code_barcodes(code: str | None) -> list[str]:
    """The barcodes a receipt's item number may stand for, likeliest first.
    Pure.

    Most US receipts print a product's UPC without its check digit and
    without leading zeros: Kings prints 89520300101 for the 895203001011 on a
    tub of Organic Girl spinach, and 7989340064 for 079893400648. A number
    that is already a whole UPC or EAN, check digit and all, is tried as it
    is first. Store codes too short to be a product barcode (a produce PLU
    like 4011) give nothing.
    """
    digits = re.sub(r"\D", "", code or "")
    if not 6 <= len(digits) <= 14:
        return []
    out = []
    if len(digits) in (8, 12, 13, 14) and _gs1_check_digit(digits[:-1]) == digits[-1]:
        out.append(digits)
    if len(digits) <= 11:
        body = digits.zfill(11)
        out.append(body + _gs1_check_digit(body))
    elif len(digits) == 12 and not out:
        out.append(digits + _gs1_check_digit(digits))
    return list(dict.fromkeys(out))


# Words a receipt abbreviates or shares with almost anything, so a match on
# them says nothing about the product.
_RECEIPT_NOISE = {"org", "organic", "organics", "the", "and", "with", "fresh",
                  "natural", "brand", "pack", "whl", "whole", "free", "large"}


def _line_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", (text or "").lower())
            if len(w) >= 3 and w not in _RECEIPT_NOISE}


def same_product(line: str, found: str) -> bool:
    """True when the product a code found shares a word with the receipt
    line, a receipt's abbreviation counting ("ARUGU" for arugula). Pure.

    A misread digit can land on a real but different product; this keeps
    that from replacing the line."""
    for a in _line_words(line):
        for b in _line_words(found):
            if a == b or (min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a))):
                return True
    return False


_RECEIPT_LOOKUPS = 4


async def resolve_receipt_codes(items: list[FoodItem], db: Session) -> None:
    """Fill in each receipt line from the product its printed item number
    names: the full name, the brand, what one package holds, and the barcode,
    which Grocy then keeps so a later scan of the package finds the product.

    A product already in Grocy under that barcode comes in as the user left
    it, with their name for it and the size they gave it, so a size fixed
    once on the review screen fills every later purchase. Otherwise Open Food
    Facts (or a kitchen's fix) is asked.

    A receipt prints "ORG BBY SPINACH"; the code says it is a 5 oz tub of
    Organic Girl baby spinach. The line keeps its own quantity and the AI's
    storage and category, and a size printed on the line wins over the
    catalog's. No AI ever guesses a product from these digits, and a product
    that shares no word with the line is passed over (same_product). Any
    failure leaves the line as the receipt printed it.
    """
    from .grocy import GrocyClient  # grocy imports this module's neighbours
    gate = asyncio.Semaphore(_RECEIPT_LOOKUPS)
    grocy = GrocyClient()

    async def one(item: FoodItem) -> None:
        code, item.receipt_code = item.receipt_code, None
        for barcode in receipt_code_barcodes(code):
            if is_store_local_barcode(barcode):
                continue
            try:
                async with gate:
                    known = await grocy.known_barcode(barcode)
            except Exception as e:  # noqa: BLE001 - Open Food Facts can still answer
                logger.warning("Grocy barcode check failed for %s: %s", barcode, e)
                known = None
            if known:
                # The user's own product. A misread digit landing exactly on
                # another barcode they keep is far less likely than a name
                # they changed sharing no word with the receipt, so no
                # same_product check here.
                item.name = known["name"]
                item.barcode = barcode
                if known["size"]:
                    item.package_size, item.package_unit = known["size"]
                return
            try:
                async with gate:
                    found = await lookup_barcode(barcode, db, guess=False)
            except BarcodeNotFound:
                continue
            except Exception as e:  # noqa: BLE001 - the line stays as printed
                logger.warning("Receipt code %s lookup failed: %s", barcode, e)
                return
            if not same_product(item.name, f"{found.brand or ''} {found.name}"):
                continue
            item.name = found.name
            item.brand = found.brand or item.brand
            item.barcode = barcode
            if item.package_size is None and found.package_size:
                item.package_size = found.package_size
                item.package_unit = found.package_unit
            return

    await asyncio.gather(*(one(item) for item in items))
