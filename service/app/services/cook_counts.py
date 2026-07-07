"""Per-recipe cook counts (FoodAssistant-bjps).

A tiny durable "made-before" tally. Every time a recipe is cooked (the Recipes
page Cook button, the Current Recipe "Mark cooked", cooking a course) the count
for that recipe's identity goes up by one and its last-cooked time is refreshed.
The browse and detail surfaces then show a quiet "Made N times" note so a user
can tell a tried-and-true recipe from one they have never made.

Identity is the load-bearing part: the SAME recipe from the SAME source must map
to the SAME counter no matter which surface cooked it. ``cook_identity`` keys off
the source plus the recipe's upstream id (a Mealie slug, or a TheMealDB /
Spoonacular / Forager external id); when there is no id it falls back to a
normalized title. The source is part of the key, so the same title from two
different sources stays two separate counters. The helper is pure so it unit
tests without a database.

The store takes a SQLAlchemy session so it composes with the request-scoped
``get_db`` dependency, exactly like the action-items and pending stores. Every
read and write is wrapped by callers (or degrades here) so a bookkeeping failure
never blocks a cook or breaks the recipe list.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..models.db_models import RecipeCookCount

logger = logging.getLogger("foodassistant.cook_counts")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm_title(title) -> str:
    """Lowercase, drop punctuation, collapse whitespace. Pure. So 'Chicken
    Soup!' and 'chicken  soup' resolve to the same fallback identity."""
    t = str(title or "").strip().lower()
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def cook_identity(source, external_id=None, slug=None, title=None) -> str:
    """Stable identity for a recipe. Pure and total.

    ``"{source}:{id}"`` when an upstream id is known (slug preferred for Mealie,
    else external id), otherwise ``"{source}:t:{normalized-title}"``. Returns ""
    only when there is neither an id nor a usable title, which the store treats
    as "nothing to record"."""
    src = (str(source or "").strip().lower()) or "unknown"
    ident = ""
    for candidate in (slug, external_id):
        c = str(candidate).strip() if candidate is not None else ""
        if c:
            ident = c
            break
    if ident:
        return f"{src}:{ident}"
    t = _norm_title(title)
    return f"{src}:t:{t}" if t else ""


def key_for_recipe(recipe: dict) -> str:
    """Identity for a browse/suggestion recipe dict (has source + slug/external_id
    + name), or a Current Recipe dict (has source + id + title)."""
    return cook_identity(
        recipe.get("source"),
        external_id=recipe.get("external_id"),
        slug=recipe.get("slug") or recipe.get("id"),
        title=recipe.get("name") or recipe.get("title"),
    )


def record_cook(db: Session, source, *, external_id=None, slug=None,
                title=None) -> dict | None:
    """Bump the cook count for a recipe and stamp last_cooked_at. Returns the
    row dict, or None when there is nothing to key on. Fails soft: any database
    error is swallowed so cooking is never blocked by this tally."""
    key = cook_identity(source, external_id=external_id, slug=slug, title=title)
    if not key:
        return None
    try:
        row = (db.query(RecipeCookCount)
               .filter(RecipeCookCount.recipe_key == key)
               .one_or_none())
        now = _now_iso()
        clean_title = (str(title).strip()[:200] if title else None)
        if row is None:
            row = RecipeCookCount(
                recipe_key=key, title=clean_title,
                source=(str(source or "").strip().lower() or None),
                count=1, last_cooked_at=now)
            db.add(row)
        else:
            row.count = (row.count or 0) + 1
            row.last_cooked_at = now
            if clean_title and not row.title:
                row.title = clean_title
        db.commit()
        db.refresh(row)
        return {"recipe_key": row.recipe_key, "count": row.count or 0,
                "last_cooked_at": row.last_cooked_at}
    except Exception as exc:  # noqa: BLE001
        logger.info("cook count: record failed for %s: %s", key, exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def counts_for(db: Session, keys: list[str]) -> dict[str, dict]:
    """Batch lookup: one query for a set of recipe keys, so the recipe list
    never does N+1. Returns ``{key: {"count", "last_cooked_at"}}`` for the keys
    that have been cooked; missing keys are simply absent. Fails soft to {}."""
    wanted = sorted({k for k in keys if k})
    if not wanted:
        return {}
    try:
        rows = (db.query(RecipeCookCount)
                .filter(RecipeCookCount.recipe_key.in_(wanted))
                .all())
    except Exception as exc:  # noqa: BLE001
        logger.info("cook count: batch lookup failed: %s", exc)
        return {}
    return {r.recipe_key: {"count": r.count or 0, "last_cooked_at": r.last_cooked_at}
            for r in rows}


def annotate(db: Session, recipes: list[dict]) -> list[dict]:
    """Attach ``cook_count`` (and ``last_cooked_at`` when cooked) to each recipe
    dict in a list, using a single batch query. Mutates and returns the list.
    Fails soft: on any error the recipes are returned untouched, so a missing
    store or lookup failure degrades to no count rather than an error."""
    try:
        keys = [key_for_recipe(r) for r in recipes]
        counts = counts_for(db, keys)
    except Exception as exc:  # noqa: BLE001
        logger.info("cook count: annotate failed: %s", exc)
        return recipes
    for r, key in zip(recipes, keys):
        info = counts.get(key)
        r["cook_count"] = info["count"] if info else 0
        if info and info.get("last_cooked_at"):
            r["last_cooked_at"] = info["last_cooked_at"]
    return recipes
