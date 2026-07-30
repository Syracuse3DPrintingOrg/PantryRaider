"""Pantry Raider's own meal plan (FoodAssistant-g0fd).

The native store behind the /mealie/mealplan endpoints when the recipe
library is Pantry Raider's own: rows in the app's SQLite database
(models/db_models.MealPlanEntry) instead of Mealie's mealplans API. Every
read shape mirrors what the Meal Plan page, the Stream Deck today-meal key,
and the Home Assistant summary already consume, so the surfaces work
identically over either backend.

An entry references a saved recipe by slug (its title denormalized so the
plan renders without joins) or is a plain free-text line. The pure wire
mapping is separate from the database calls so it unit-tests without
fixtures.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models.db_models import MealPlanEntry, Recipe


def entry_wire(row: MealPlanEntry) -> dict:
    """One entry in the shape the Meal Plan page reads. Pure."""
    return {
        "id": row.id,
        "entry_type": row.entry_type or "dinner",
        "title": row.title or "",
        "recipe_slug": row.recipe_slug,
    }


def list_range(db: Session, start: str, end: str) -> list[dict]:
    """Wire entries for every plan day in [start, end], date-ascending.

    Each entry also carries its ISO ``date`` so the caller can bucket by day
    (the same field Mealie entries carry).
    """
    rows = (db.query(MealPlanEntry)
            .filter(MealPlanEntry.date >= start, MealPlanEntry.date <= end)
            .order_by(MealPlanEntry.date, MealPlanEntry.id)
            .all())
    return [{**entry_wire(r), "date": r.date} for r in rows]


def _resolve_recipe(db: Session, recipe_id: str | None) -> Recipe | None:
    """The native recipe a picked ``recipe_id`` names, or None.

    The Meal Plan page's recipe search returns the ids the /mealie/recipes
    listing carries, which in native mode are the store's own integer ids; a
    slug is also accepted so API callers can plan by slug directly.
    """
    value = str(recipe_id or "").strip()
    if not value:
        return None
    if value.isdigit():
        return db.query(Recipe).filter(Recipe.id == int(value)).one_or_none()
    return db.query(Recipe).filter(Recipe.slug == value).one_or_none()


def add_entry(db: Session, date: str, entry_type: str,
              recipe_id: str | None = None, title: str = "") -> dict:
    """Plan one meal and return its wire entry.

    Raises ValueError with a user-facing message when neither a known recipe
    nor a title is given, so the endpoint can answer 400 the same way the
    Mealie branch does.
    """
    recipe = _resolve_recipe(db, recipe_id)
    if recipe is None and recipe_id and not (title or "").strip():
        raise ValueError("That recipe could not be found in your library.")
    if recipe is None and not (title or "").strip():
        raise ValueError("Provide a recipe or a free-text title.")
    row = MealPlanEntry(
        date=(date or "").strip(),
        entry_type=(entry_type or "dinner").strip().lower() or "dinner",
        title=(recipe.name if recipe is not None else title).strip(),
        recipe_slug=recipe.slug if recipe is not None else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return entry_wire(row)


def delete_entry(db: Session, entry_id: int) -> bool:
    """Remove one planned meal. True when it existed."""
    row = (db.query(MealPlanEntry)
           .filter(MealPlanEntry.id == int(entry_id)).one_or_none())
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def summary(db: Session, today: str, tomorrow: str) -> dict:
    """The lean today/tomorrow view the Home Assistant sensor reads.

    Same shape as the Mealie branch: {count, today: [{type, name}], tomorrow:
    [...]}, count being today's entries.
    """
    def lean(e: dict) -> dict:
        return {"type": e.get("entry_type", ""), "name": e.get("title") or "?"}

    entries = list_range(db, today, tomorrow)
    by_day = {"today": [lean(e) for e in entries if e["date"] == today],
              "tomorrow": [lean(e) for e in entries if e["date"] == tomorrow]}
    return {"count": len(by_day["today"]),
            "today": by_day["today"], "tomorrow": by_day["tomorrow"]}
