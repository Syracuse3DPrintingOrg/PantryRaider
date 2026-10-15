"""Food-intake / nutrition log store (FoodAssistant-e6qt).

Records what was eaten with its nutrition so the Nutrition page can show daily
totals. The totals math is a pure function so it is unit-testable without a DB.
Functions take a SQLAlchemy session, matching the action-items / pending stores.
"""
from __future__ import annotations

from datetime import date as _date, datetime, timezone

from sqlalchemy.orm import Session

from ..models.db_models import IntakeLog

_MACROS = ("calories", "protein", "carbs", "fat")


def _today_str() -> str:
    return _date.today().isoformat()


def _num(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _row_dict(row: IntakeLog) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "servings": row.servings,
        "calories": row.calories,
        "protein": row.protein,
        "carbs": row.carbs,
        "fat": row.fat,
        "date": row.date,
        "source": row.source,
        "created_at": row.created_at,
    }


def day_totals(entries: list[dict]) -> dict:
    """Sum the macros across ``entries`` (each already per its logged servings).

    Missing macro values are treated as 0 so a partially-known entry still adds
    what it knows. Pure: no DB, fully unit-testable. Returns rounded numbers and
    the entry count."""
    totals = {m: 0.0 for m in _MACROS}
    for e in entries or []:
        for m in _MACROS:
            v = e.get(m)
            if isinstance(v, (int, float)):
                totals[m] += v
    return {m: round(totals[m], 1) for m in _MACROS} | {"count": len(entries or [])}


# OFF nutriment key stems, paired with our intake-log field names, for the
# "Log as eaten" offer below. OFF publishes each as "<stem>_serving" and
# "<stem>_100g".
_OFF_MACROS = (("calories", "energy-kcal"), ("protein", "proteins"),
               ("carbs", "carbohydrates"), ("fat", "fat"))


def calorie_offer(name: str, product) -> dict | None:
    """The one-tap "Log as eaten" offer for an Open Food Facts product, or None.

    Built from the product's own nutrition facts, never a guess
    (FoodAssistant-4mi3). Pure truth table:

    - per-serving calories on the label win, offered as one serving;
    - otherwise per-100g calories scaled by the labeled serving weight;
    - otherwise the plain per-100g figures, labeled as such;
    - no calorie figure at all (or no name) means no offer. A genuine 0 kcal
      (a diet soda) is data and is offered; only absence yields nothing.

    Macros ride along only on the same basis as the calories. The returned
    dict matches the /nutrition/log payload plus a human ``basis`` note."""
    name = (name or "").strip()
    if not name or not isinstance(product, dict):
        return None
    nutr = product.get("nutriments")
    if not isinstance(nutr, dict):
        return None
    serving_size = str(product.get("serving_size") or "").strip() or None

    def _values(suffix: str, scale: float = 1.0) -> dict:
        vals = {}
        for field, stem in _OFF_MACROS:
            v = _num(nutr.get(f"{stem}_{suffix}"))
            vals[field] = round(v * scale, 1) if v is not None else None
        return vals

    vals = _values("serving")
    if vals["calories"] is not None:
        basis = f"per serving ({serving_size})" if serving_size else "per serving"
    else:
        per_100 = _values("100g")
        if per_100["calories"] is None:
            return None
        qty = _num(product.get("serving_quantity"))
        if qty and qty > 0:
            vals = _values("100g", scale=qty / 100.0)
            basis = (f"per serving ({serving_size})" if serving_size
                     else f"per {qty:g} g serving")
        else:
            vals, basis = per_100, "per 100 g"
    return {"name": name[:120], **vals, "basis": basis}


def log_intake(db: Session, name: str, servings: float = 1.0, *,
               calories=None, protein=None, carbs=None, fat=None,
               source: str = "manual", date: str | None = None) -> dict:
    """Record one eaten food. Macros are stored as given (already per servings)."""
    row = IntakeLog(
        name=(name or "Food").strip()[:120],
        servings=max(0.0, _num(servings) or 1.0),
        calories=_num(calories), protein=_num(protein),
        carbs=_num(carbs), fat=_num(fat),
        date=(date or _today_str()),
        source=source if source in ("manual", "barcode", "recipe") else "manual",
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _row_dict(row)


def list_for_date(db: Session, date: str | None = None) -> list[dict]:
    """Entries logged for a calendar day (default today), newest first."""
    day = date or _today_str()
    rows = (
        db.query(IntakeLog)
        .filter(IntakeLog.date == day)
        .order_by(IntakeLog.id.desc())
        .all()
    )
    return [_row_dict(r) for r in rows]


def delete(db: Session, item_id: int) -> bool:
    row = db.get(IntakeLog, item_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def recent_days(db: Session, days: int = 7) -> list[dict]:
    """Per-day totals for the most recent ``days`` that have any entries."""
    rows = db.query(IntakeLog).order_by(IntakeLog.date.desc()).all()
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r.date, []).append(_row_dict(r))
    out = []
    for day in sorted(by_day, reverse=True)[:max(1, int(days))]:
        out.append({"date": day, **day_totals(by_day[day])})
    return out
