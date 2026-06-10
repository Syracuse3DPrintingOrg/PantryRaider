import httpx
from io import BytesIO
from PIL import Image
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends
from sqlalchemy.orm import Session
from ..database import get_db
from ..models.food import AnalysisResult, FoodItem, FoodCategory, StorageType
from ..services.defaults import apply_defaults
from ..dependencies import get_vision_provider

router = APIRouter(prefix="/analyze", tags=["analyze"])

_ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "image/heic"}
_OFF_UA = "FoodAssistant/1.0 (github.com/Syracuse3DPrinting/FoodAssistant)"

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
    if category in _REFRIGERATED_CATEGORIES:
        return StorageType.refrigerated
    if category in _DRY_CATEGORIES:
        return StorageType.dry
    return StorageType.room_temp


# Phone photos are 4000px+; vision LLM cost scales with size. Receipts get a
# higher cap so fine print stays legible on tall, narrow images.
_MAX_DIM_FOOD = 1280
_MAX_DIM_RECEIPT = 2048


def _downscale(data: bytes, mime: str, max_dim: int = _MAX_DIM_FOOD) -> tuple[bytes, str]:
    try:
        img = Image.open(BytesIO(data))
        if max(img.size) <= max_dim:
            return data, mime
        img.thumbnail((max_dim, max_dim))
        buf = BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=85)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        # Unreadable by Pillow (e.g. HEIC without plugin) — send as-is
        return data, mime


@router.post("/food", response_model=AnalysisResult)
async def analyze_food(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    provider=Depends(get_vision_provider),
):
    """Analyze a photo of one or more food items."""
    if file.content_type not in _ALLOWED_MIME:
        raise HTTPException(400, f"Unsupported image type: {file.content_type}")
    data, mime = _downscale(await file.read(), file.content_type)
    result = await provider.analyze_food(data, mime)
    result.items = [apply_defaults(item, db) for item in result.items]
    return result


@router.get("/barcode/{barcode}", response_model=AnalysisResult)
async def analyze_barcode(barcode: str, db: Session = Depends(get_db)):
    """Look up a barcode in Open Food Facts and return a food item with defaults applied."""
    async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": _OFF_UA}) as client:
        r = await client.get(
            f"https://world.openfoodfacts.org/api/v0/product/{barcode}.json"
        )
    if r.status_code != 200:
        raise HTTPException(502, "Open Food Facts unavailable")
    data = r.json()
    if data.get("status") != 1:
        raise HTTPException(404, f"Barcode {barcode} not found in Open Food Facts")

    product = data["product"]
    name = (product.get("product_name_en") or product.get("product_name") or "").strip()
    if not name:
        raise HTTPException(404, "Product found but has no name")

    brand = (product.get("brands") or "").split(",")[0].strip() or None
    tags = product.get("categories_tags", [])
    category = _off_category(tags)
    storage = _off_storage(tags, category)

    item = FoodItem(
        name=name,
        quantity=1.0,
        unit="item",
        storage_type=storage,
        category=category,
        brand=brand,
        confidence=0.9,
    )
    item = apply_defaults(item, db)
    return AnalysisResult(items=[item], image_type="barcode")


@router.post("/receipt", response_model=AnalysisResult)
async def analyze_receipt(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    provider=Depends(get_vision_provider),
):
    """Parse a receipt image and return all food items with defaults applied."""
    if file.content_type not in _ALLOWED_MIME:
        raise HTTPException(400, f"Unsupported image type: {file.content_type}")
    data, mime = _downscale(await file.read(), file.content_type, _MAX_DIM_RECEIPT)
    result = await provider.analyze_receipt(data, mime)
    result.items = [apply_defaults(item, db) for item in result.items]
    return result
