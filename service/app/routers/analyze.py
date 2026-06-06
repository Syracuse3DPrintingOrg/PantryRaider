from fastapi import APIRouter, UploadFile, File, HTTPException, Depends
from sqlalchemy.orm import Session
from ..database import get_db
from ..models.food import AnalysisResult
from ..services.defaults import apply_defaults
from ..dependencies import get_vision_provider

router = APIRouter(prefix="/analyze", tags=["analyze"])

_ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "image/heic"}


@router.post("/food", response_model=AnalysisResult)
async def analyze_food(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    provider=Depends(get_vision_provider),
):
    """Analyze a photo of one or more food items."""
    if file.content_type not in _ALLOWED_MIME:
        raise HTTPException(400, f"Unsupported image type: {file.content_type}")
    data = await file.read()
    result = await provider.analyze_food(data, file.content_type)
    result.items = [apply_defaults(item, db) for item in result.items]
    return result


@router.post("/receipt", response_model=AnalysisResult)
async def analyze_receipt(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    provider=Depends(get_vision_provider),
):
    """Parse a receipt image and return all food items with defaults applied."""
    if file.content_type not in _ALLOWED_MIME:
        raise HTTPException(400, f"Unsupported image type: {file.content_type}")
    data = await file.read()
    result = await provider.analyze_receipt(data, file.content_type)
    result.items = [apply_defaults(item, db) for item in result.items]
    return result
