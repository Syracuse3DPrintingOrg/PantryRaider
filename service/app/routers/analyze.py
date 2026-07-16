import logging
from io import BytesIO
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends
from sqlalchemy.orm import Session
from ..config import settings
from ..database import get_db
from ..models.food import AnalysisResult
from ..services.defaults import apply_defaults
from ..services.barcode import lookup_barcode, BarcodeNotFound, BarcodeServiceError, BarcodeStoreLocal
from ..services.shelf_life import parse_llm_shelf_life, apply_shelf_life
from ..services import usage
from ..dependencies import get_vision_provider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analyze", tags=["analyze"])


async def _llm_shelf_life(item) -> None:
    """When enabled, ask the AI provider to estimate this item's shelf life and
    storage, overriding the generic category default (FoodAssistant-ft92).

    Skipped for an item that already carries a best-before date read off the
    packaging: real printed data wins over an estimate. Any provider error is
    swallowed so intake never breaks; the caller then falls back to defaults.
    """
    if item.best_by_date is not None:
        return
    try:
        from ..dependencies import get_enrich_provider
        raw = await get_enrich_provider().enrich_product({
            "product_name": item.name,
            "brand": item.brand,
            "category": item.category.value,
            "note": "Estimate the typical home shelf life in days from purchase "
                    "and the best storage location for this item.",
        })
    except Exception as e:
        logger.warning("LLM shelf-life estimate failed, using defaults: %s", e)
        return
    apply_shelf_life(item, parse_llm_shelf_life(raw))

_ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "image/heic"}
_BUDGET_MSG = ("AI token budget reached for this month. Raise it in "
               "Settings, AI, or wait for the next month.")


def _check_budget():
    if usage.over_budget():
        raise HTTPException(429, detail=_BUDGET_MSG)


# Phone photos are 4000px+; vision LLM cost scales with size. Receipts get a
# higher cap so fine print stays legible on tall, narrow images.
_MAX_DIM_FOOD = 1280
_MAX_DIM_RECEIPT = 2048


def _downscale(data: bytes, mime: str, max_dim: int = _MAX_DIM_FOOD) -> tuple[bytes, str]:
    # PIL imports lazily: it costs real time on a Pi boot and only photo
    # analysis needs it here (FoodAssistant-7dt9).
    from PIL import Image
    try:
        img = Image.open(BytesIO(data))
        if max(img.size) <= max_dim:
            return data, mime
        img.thumbnail((max_dim, max_dim))
        buf = BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=85)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        # Unreadable by Pillow (e.g. HEIC without plugin): send as-is
        return data, mime


_NO_AI = {"detail": "AI provider not configured", "setup_url": "/setup"}


@router.post("/food", response_model=AnalysisResult)
async def analyze_food(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    provider=Depends(get_vision_provider),
):
    """Analyze a photo of one or more food items."""
    if file.content_type not in _ALLOWED_MIME:
        raise HTTPException(400, f"Unsupported image type: {file.content_type}")
    _check_budget()
    data, mime = _downscale(await file.read(), file.content_type)
    try:
        result = await provider.analyze_food(data, mime)
    except NotImplementedError:
        raise HTTPException(503, detail=_NO_AI)
    if settings.llm_expiry_effective() and settings.ai_configured():
        for item in result.items:
            await _llm_shelf_life(item)
    result.items = [apply_defaults(item, db) for item in result.items]
    return result


@router.get("/barcode/{barcode}", response_model=AnalysisResult)
async def analyze_barcode(barcode: str, db: Session = Depends(get_db)):
    """Look up a barcode in Open Food Facts and return a food item with defaults applied."""
    try:
        item = await lookup_barcode(barcode, db)
    except BarcodeStoreLocal as e:
        raise HTTPException(422, str(e) + ". Take a photo of the item instead.")
    except BarcodeNotFound as e:
        raise HTTPException(404, str(e))
    except BarcodeServiceError as e:
        raise HTTPException(502, str(e))
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
    _check_budget()
    data, mime = _downscale(await file.read(), file.content_type, _MAX_DIM_RECEIPT)
    try:
        result = await provider.analyze_receipt(data, mime)
    except NotImplementedError:
        raise HTTPException(503, detail=_NO_AI)
    if settings.llm_expiry_effective() and settings.ai_configured():
        for item in result.items:
            await _llm_shelf_life(item)
    result.items = [apply_defaults(item, db) for item in result.items]
    return result
