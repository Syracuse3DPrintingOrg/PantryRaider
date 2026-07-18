"""Native recipe store endpoints (FoodAssistant-zwwe).

The recipe browse/save/suggest API stays under /mealie for wire compatibility
with the existing pages (routers/mealie.py consults the backend seam). This
router carries the pieces that only exist for the native store: serving recipe
images from data_dir, deleting a native recipe, and the one-click migration
that copies a Mealie library into Pantry Raider.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models.db_models import Recipe
from ..services import recipe_store

router = APIRouter(prefix="/recipes", tags=["recipes"])

logger = logging.getLogger("foodassistant.recipes")

_MEDIA_TYPES = {
    "jpg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
}


@router.get("/images/{recipe_id}")
def recipe_image(recipe_id: int, db: Session = Depends(get_db)):
    """Serve a native recipe's image from data_dir/recipe-images."""
    r = db.query(Recipe).filter(Recipe.id == recipe_id).one_or_none()
    path = recipe_store.image_file(r) if r else None
    if path is None or not path.is_file():
        raise HTTPException(404, "This recipe has no image.")
    media = _MEDIA_TYPES.get(path.suffix.lstrip(".").lower(), "application/octet-stream")
    return FileResponse(path, media_type=media,
                        headers={"Cache-Control": "public, max-age=86400"})


@router.delete("/{slug}")
def delete_recipe(slug: str, db: Session = Depends(get_db)):
    """Remove a recipe from the native library (its image file included)."""
    if not recipe_store.delete_recipe(db, slug):
        raise HTTPException(404, "That recipe could not be found in your library.")
    return {"ok": True}


@router.post("/{slug}/image")
async def upload_recipe_image(slug: str, file: UploadFile = File(...),
                              db: Session = Depends(get_db)):
    """Upload or replace a native recipe's hero image (FoodAssistant-v7gj).

    Backs the editor's image field: reuses recipe_store.attach_image, which
    already handles a previous image with a different extension. Refuses when
    the recipe is unknown, so a stray upload never lands orphaned."""
    slug = (slug or "").strip()
    if recipe_store.get_by_slug(db, slug) is None:
        raise HTTPException(404, "That recipe could not be found in your library.")
    data = await file.read()
    served = recipe_store.attach_image(db, slug, data, file.content_type,
                                       file.filename or "")
    if not served:
        raise HTTPException(
            400, "That image could not be saved. Try a smaller file (under 8 MB) "
                 "or a JPEG, PNG, WebP, or GIF.")
    return {"ok": True, "image": served}


@router.delete("/{slug}/image")
def remove_recipe_image(slug: str, db: Session = Depends(get_db)):
    """Remove a native recipe's hero image, leaving the recipe itself intact."""
    if not recipe_store.remove_image(db, slug):
        raise HTTPException(404, "This recipe has no image to remove.")
    return {"ok": True}


def _mealie_parsed(detail: dict) -> tuple[dict, list]:
    """Reduce one Mealie recipe detail to (parsed dict, structured entries) for
    recipe_store.create_from_parsed. Pure. The raw display line of every
    ingredient is preserved; Mealie's parsed quantity/unit/food ride along."""
    from .mealie import _mealie_ingredient_line
    entries = [i for i in (detail.get("recipeIngredient") or []) if i]
    lines: list[str] = []
    structured: list = []
    for entry in entries:
        line = _mealie_ingredient_line(entry)
        if not line:
            continue
        lines.append(line)
        structured.append(entry if isinstance(entry, dict) else None)
    steps = []
    for s in detail.get("recipeInstructions") or []:
        text = (s.get("text") if isinstance(s, dict) else str(s)) or ""
        if text.strip():
            steps.append(text.strip())
    tags = [t.get("name") for t in detail.get("tags") or []
            if isinstance(t, dict) and t.get("name")]
    parsed = {
        "name": detail.get("name") or "",
        "description": detail.get("description") or "",
        "servings": str(detail.get("recipeYield") or "").strip(),
        "total_time": str(detail.get("totalTime") or "").strip(),
        "ingredients": lines,
        "instructions": steps,
        "tags": tags,
        "source_url": detail.get("orgURL") or None,
    }
    return parsed, structured


@router.post("/migrate-from-mealie")
async def migrate_from_mealie(db: Session = Depends(get_db)):
    """Copy the whole Mealie recipe library into Pantry Raider's own store.

    Read-only toward Mealie: nothing there is ever written, changed, or
    deleted. Idempotent: a recipe whose slug or name is already in the native
    library is skipped, so re-running never duplicates. Each recipe keeps its
    Mealie slug when free, so made-before cook counts carry over. On success
    the install switches its recipe library to Pantry Raider's own store.
    """
    if settings.is_satellite():
        raise HTTPException(400, "Run the migration on your main server; "
                                 "this device follows it automatically.")
    if not settings.mealie_configured():
        raise HTTPException(400, "Mealie is not connected, so there is nothing "
                                 "to copy. Your recipes already live here.")

    # Imported here, not at module top, so the Mealie backend only loads
    # when the migration actually reads from it (FoodAssistant-pjtq).
    from ..services.mealie import MealieClient, MealieError
    m = MealieClient()
    try:
        details = await m.get_recipes_with_ingredients(limit=1000)
    except MealieError as e:
        raise HTTPException(502, str(e))

    existing_slugs = {row.slug for row in db.query(Recipe.slug).all()}
    existing_names = {(row.name or "").strip().lower()
                      for row in db.query(Recipe.name).all()}

    imported = 0
    skipped = 0
    errors: list[dict] = []
    for detail in details:
        name = str(detail.get("name") or "").strip()
        slug = str(detail.get("slug") or "").strip() or recipe_store.slugify(name)
        if slug in existing_slugs or (name and name.lower() in existing_names):
            skipped += 1
            continue
        try:
            parsed, structured = _mealie_parsed(detail)
            saved = recipe_store.create_from_parsed(
                db, parsed, source="mealie",
                source_url=detail.get("orgURL"),
                structured=structured,
                # Keep the Mealie slug so cook counts keyed by it carry over.
                # unique_slug guards the rare case where two different names
                # reduce to the same slug.
                slug=recipe_store.unique_slug(db, name)
                if recipe_store.get_by_slug(db, slug) else slug)
            existing_slugs.add(saved["slug"])
            if name:
                existing_names.add(name.lower())
            imported += 1
        except Exception as exc:  # noqa: BLE001 - one bad recipe never aborts the rest
            db.rollback()
            errors.append({"name": name or slug, "error": str(exc)[:200]})
            logger.info("mealie migration: %s failed: %s", name or slug, exc)
            continue
        # Best-effort image copy from Mealie's media path; a missing image
        # never fails the recipe.
        mealie_id = detail.get("id")
        if mealie_id:
            image_url = f"{m.base}/api/media/recipes/{mealie_id}/images/original.webp"
            fetched = await recipe_store.fetch_image(image_url, headers=m.headers)
            if fetched:
                recipe_store.attach_image(db, saved["slug"], fetched[0], fetched[1],
                                          image_url)

    # Switch this install's recipe library to the native store. Mealie itself
    # is untouched and stays available as an import source.
    if imported or skipped:
        try:
            settings.save({"recipes_backend": "native"})
        except OSError:
            settings.apply({"recipes_backend": "native"})

    total = imported + skipped
    message = (f"Copied {imported} recipe{'s' if imported != 1 else ''} into "
               f"Pantry Raider" + (f", skipped {skipped} already here" if skipped else ""))
    if errors:
        message += f", {len(errors)} could not be copied"
    message += ". Your recipes now live in Pantry Raider; Mealie was left untouched."
    if not total and not errors:
        message = "Your Mealie library is empty, so there was nothing to copy."
    return {"ok": True, "imported": imported, "skipped": skipped,
            "errors": errors, "message": message}
