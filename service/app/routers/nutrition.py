"""Food-intake / nutrition tracking API (FoodAssistant-e6qt).

Log what you ate (manually, from a barcode lookup, or a cooked recipe) and read
back the day's running totals. The optional /estimate endpoint asks the AI
provider to fill in calories and macros for a food name so logging is a couple
of taps rather than manual data entry.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..services import nutrition

router = APIRouter(prefix="/nutrition", tags=["nutrition"])


class LogIn(BaseModel):
    name: str
    servings: float = 1.0
    calories: float | None = None
    protein: float | None = None
    carbs: float | None = None
    fat: float | None = None
    source: str = "manual"
    date: str | None = None


@router.post("/log")
def log_food(payload: LogIn, db: Session = Depends(get_db)):
    if not payload.name.strip():
        raise HTTPException(400, "A food name is required.")
    return {"entry": nutrition.log_intake(
        db, payload.name, payload.servings,
        calories=payload.calories, protein=payload.protein,
        carbs=payload.carbs, fat=payload.fat,
        source=payload.source, date=payload.date)}


@router.get("/today")
def today(db: Session = Depends(get_db)):
    entries = nutrition.list_for_date(db)
    from datetime import date as _date
    return {"date": _date.today().isoformat(), "entries": entries,
            "totals": nutrition.day_totals(entries)}


@router.get("/recent")
def recent(days: int = 7, db: Session = Depends(get_db)):
    return {"days": nutrition.recent_days(db, days)}


@router.delete("/{item_id}")
def delete_entry(item_id: int, db: Session = Depends(get_db)):
    return {"ok": nutrition.delete(db, item_id)}


class EstimateIn(BaseModel):
    name: str
    servings: float = 1.0


@router.post("/estimate")
async def estimate(payload: EstimateIn):
    """Ask the AI provider for calories + macros for a food, scaled to servings.

    Returns {ok, estimate:{calories,protein,carbs,fat}} or {ok: false} when no
    provider is configured or it could not produce numbers. The page uses it to
    pre-fill the log form; the user can still edit before saving."""
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "A food name is required.")
    from ..dependencies import get_enrich_provider
    try:
        provider = get_enrich_provider()
        est = await provider.estimate_nutrition(name, payload.servings)
    except NotImplementedError:
        return {"ok": False, "error": "AI provider not configured"}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"AI error: {e}"}, status_code=200)
    if not est:
        return {"ok": False, "error": "no estimate returned"}
    return {"ok": True, "estimate": est}
