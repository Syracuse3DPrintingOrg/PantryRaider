"""Pantry Raider's own recipe library (FoodAssistant-zwwe).

The native storage behind the recipe features, replacing Mealie's role as the
place recipes live. Recipes are rows in the app's existing SQLite database
(models/db_models.py: Recipe, RecipeIngredient, RecipeStep) and images are
files under data_dir/recipe-images, so everything rides in the app data volume
that is already backed up.

Compatibility is the load-bearing design decision here. Every read shape this
module returns mirrors the Mealie shapes the rest of the app already consumes:

  * ``list_with_ingredients`` returns rows shaped like
    MealieClient.get_recipes_with_ingredients output, so classify_recipes (the
    Cook page tier matcher) works unchanged.
  * ``detail`` returns a Mealie-detail-shaped dict (recipeIngredient,
    recipeInstructions, recipeYield, ...), so current_recipe.from_mealie_detail
    and the recipe preview normalizer both accept it as-is.
  * Rows carry ``source: "mealie"`` on those wire shapes on purpose: the
    browser JS and the Cook page treat that value as "a recipe in my local
    library", and keeping it means every existing flow (Cook this, quick view,
    cook counts keyed by slug) works identically in native mode. The honest
    origin of each recipe lives in the Recipe.source column.

Writes take the same normalized parsed-recipe dict recipes_import.parse_recipe_file
produces ({name, description, servings, total_time, ingredients[str],
instructions[str], source}), which is also what every import path (URL, PDF,
photo, file, external, AI, manual) already builds.

Pure logic (slugs, shape mapping) is kept separate from the database and file
I/O so it unit-tests without fixtures.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from sqlalchemy.orm import Session

from ..models.db_models import Recipe, RecipeIngredient, RecipeStep

# Image files larger than this are refused (a recipe hero image, not a video).
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# Accepted image extensions, keyed by the content types that map to them.
_IMAGE_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}


class RecipeStoreError(Exception):
    """Raised with a user-facing message when a store operation cannot proceed."""


# ── Pure helpers ──────────────────────────────────────────────────────────────

def slugify(name: str) -> str:
    """A URL-safe slug from a recipe name. Pure and total.

    Lowercase ASCII words joined by dashes, matching the style of Mealie slugs
    so a migrated recipe can keep its identity. An empty or symbol-only name
    yields "recipe" so a slug always exists.
    """
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    words = re.findall(r"[a-z0-9]+", text)
    return "-".join(words) or "recipe"


def image_extension(content_type: str | None, url: str = "") -> str:
    """Pick a file extension for an image from its content type, falling back
    to the URL's suffix, then jpg. Pure."""
    ext = _IMAGE_EXTENSIONS.get((content_type or "").split(";")[0].strip().lower())
    if ext:
        return ext
    suffix = Path(url.split("?")[0]).suffix.lower().lstrip(".")
    if suffix in set(_IMAGE_EXTENSIONS.values()):
        return "jpg" if suffix == "jpeg" else suffix
    return "jpg"


def _structured_fields(entry) -> dict:
    """Extract optional parsed fields from one structured-ingredient entry (the
    services.mealie.build_recipe_ingredients intermediate shape). Pure; a plain
    note entry or anything unrecognized yields empty fields."""
    out = {"quantity": None, "unit": None, "food": None, "note": None}
    if not isinstance(entry, dict):
        return out
    food = entry.get("food")
    food_name = (food.get("name") if isinstance(food, dict) else food) or ""
    if not str(food_name).strip():
        return out
    unit = entry.get("unit")
    unit_name = (unit.get("name") if isinstance(unit, dict) else unit) or ""
    qty = entry.get("quantity")
    try:
        qty = float(qty) if qty not in (None, "") else None
    except (TypeError, ValueError):
        qty = None
    out.update({
        "quantity": qty,
        "unit": str(unit_name).strip() or None,
        "food": str(food_name).strip(),
        "note": str(entry.get("note") or "").strip() or None,
    })
    return out


def _ingredient_wire(row: RecipeIngredient) -> dict:
    """One ingredient row rendered as a Mealie recipeIngredient entry.

    ``display`` and ``originalText`` always carry the raw line, so previews and
    stock matching read exactly what was written; the parsed food/unit/quantity
    ride along when present so the Current Recipe page can scale amounts."""
    entry: dict = {
        "display": row.text,
        "originalText": row.text,
        "note": row.note if row.food else row.text,
        "quantity": row.quantity,
        "unit": {"name": row.unit} if row.unit else None,
        "food": {"name": row.food} if row.food else None,
    }
    return entry


def _ingredients_wire(rows) -> list[dict]:
    """Ordered ingredient rows as Mealie recipeIngredient entries, with section
    grouping (FoodAssistant-zq7k).

    Mirrors Mealie's own mechanism: a ``title`` is set on the FIRST entry of each
    section run and left off the rest, so the same entry both belongs to a section
    and marks its start. A recipe with no sections adds no ``title`` at all, so its
    wire is byte-for-byte identical to before the column existed."""
    out: list[dict] = []
    prev: str | None = None
    for row in rows:
        entry = _ingredient_wire(row)
        section = (row.section or "").strip() or None
        if section and section != prev:
            entry["title"] = section
        prev = section
        out.append(entry)
    return out


def split_ingredient_sections(parsed: dict) -> tuple[list[str], list[str | None]]:
    """Normalize a parsed recipe's ingredients into aligned (lines, sections).

    The stable interchange across the pipeline is a flat list of ingredient
    lines; sections ride alongside as a position-aligned parallel list so every
    producer that only knows the flat form keeps working untouched. This accepts
    either form and always returns the same pair:

      * Flat: ``parsed["ingredients"]`` is a list of strings, optionally with a
        parallel ``parsed["ingredient_sections"]`` naming the heading each line
        sits under (the editor and Mealie imports use this).
      * Grouped: ``parsed["ingredients"]`` is a list of ``{"section", "items"}``
        objects (what a vision model returns for a recipe whose ingredients sit
        under headings). Bare strings mixed in are treated as ungrouped.

    Empty lines are dropped from both lists in lockstep, and an empty or missing
    heading becomes ``None``. A recipe with no sections yields an all-``None``
    sections list, so callers behave exactly as before."""
    raw = parsed.get("ingredients") or []
    grouped = any(isinstance(g, dict) for g in raw)
    lines: list[str] = []
    sections: list[str | None] = []
    if grouped:
        for group in raw:
            if isinstance(group, dict):
                heading = str(group.get("section") or "").strip() or None
                items = group.get("items")
                if items is None:
                    # A single-item object; keep the parse robust.
                    items = [group.get("text") or group.get("name") or ""]
                for item in items or []:
                    text = str(item or "").strip()
                    if text:
                        lines.append(text)
                        sections.append(heading)
            else:
                text = str(group or "").strip()
                if text:
                    lines.append(text)
                    sections.append(None)
        return lines, sections
    parallel = parsed.get("ingredient_sections") or []
    for i, item in enumerate(raw):
        text = str(item or "").strip()
        if not text:
            continue
        raw_section = parallel[i] if i < len(parallel) else ""
        heading = str(raw_section or "").strip() or None
        lines.append(text)
        sections.append(heading)
    return lines, sections


def _loads_list(raw) -> list:
    try:
        value = json.loads(raw or "[]")
        return value if isinstance(value, list) else []
    except (TypeError, ValueError):
        return []


# ── Slug allocation ───────────────────────────────────────────────────────────

def unique_slug(db: Session, name: str, keep: str = "") -> str:
    """A slug for ``name`` not already taken by another recipe. ``keep`` lets an
    update retain its own slug."""
    base = slugify(name)
    slug = base
    n = 2
    while True:
        existing = db.query(Recipe.id).filter(Recipe.slug == slug).first()
        if existing is None or slug == keep:
            return slug
        slug = f"{base}-{n}"
        n += 1


# ── Reads ─────────────────────────────────────────────────────────────────────

def get_by_slug(db: Session, slug: str) -> Recipe | None:
    slug = (slug or "").strip()
    if not slug:
        return None
    return db.query(Recipe).filter(Recipe.slug == slug).one_or_none()


def count(db: Session) -> int:
    return int(db.query(Recipe).count())


def list_recipes(db: Session, search: str = "", limit: int = 50) -> list[dict]:
    """Recipe summaries, newest first, optionally filtered by a name or tag
    substring (case-insensitive). Shaped like the Mealie recipe summary the
    Recipes page consumes."""
    q = db.query(Recipe).order_by(Recipe.created_at.desc(), Recipe.id.desc())
    needle = (search or "").strip().lower()
    rows = q.all()
    out: list[dict] = []
    for r in rows:
        if needle:
            hay = (r.name or "").lower()
            tags = " ".join(str(t) for t in _loads_list(r.tags)
                            + _loads_list(r.categories)).lower()
            if needle not in hay and needle not in tags:
                continue
        out.append({
            "id": r.id,
            "name": r.name,
            "slug": r.slug,
            "description": r.description or "",
            "totalTime": r.total_time or "",
            "orgURL": r.source_url,
            "origin": r.source,
            "image": f"/recipes/images/{r.id}" if r.image_path else None,
            "rating": None,
        })
        if len(out) >= max(1, limit):
            break
    return out


def _detail_dict(db: Session, r: Recipe) -> dict:
    ings = (db.query(RecipeIngredient)
            .filter(RecipeIngredient.recipe_id == r.id)
            .order_by(RecipeIngredient.position, RecipeIngredient.id)
            .all())
    steps = (db.query(RecipeStep)
             .filter(RecipeStep.recipe_id == r.id)
             .order_by(RecipeStep.position, RecipeStep.id)
             .all())
    return {
        # No Mealie media id: the native image is served by the app itself and
        # carried in "image", so callers must not build a Mealie media URL.
        "id": None,
        "native_id": r.id,
        "slug": r.slug,
        "name": r.name,
        "description": r.description or "",
        "recipeYield": r.servings or "",
        "totalTime": r.total_time or "",
        "prepTime": r.prep_time or "",
        # current_recipe.from_mealie_detail and the print quick-facts header both
        # read this key for the native store, per its own comment (FoodAssistant-v7gj).
        "cookTime": r.cook_time or "",
        "orgURL": r.source_url,
        "origin": r.source,
        "tags": _loads_list(r.tags),
        "categories": _loads_list(r.categories),
        "image": f"/recipes/images/{r.id}" if r.image_path else None,
        "recipeIngredient": _ingredients_wire(ings),
        "recipeInstructions": [{"text": s.text, "ingredientReferences": []}
                               for s in steps],
    }


def detail(db: Session, slug: str) -> dict | None:
    """Full recipe detail in the Mealie detail shape, or None when unknown.

    current_recipe.from_mealie_detail and the preview normalizer both accept
    this dict unchanged, which is what lets the Cook and Current Recipe flows
    work identically in native mode."""
    r = get_by_slug(db, slug)
    return _detail_dict(db, r) if r else None


def list_with_ingredients(db: Session, limit: int = 200) -> list[dict]:
    """Every recipe with its ingredient list, shaped like
    MealieClient.get_recipes_with_ingredients output so classify_recipes (the
    /suggest tier matcher) consumes it unchanged.

    ``source`` is "mealie" by design: the browse and Cook page JS treat that
    value as "a recipe in my local library" (quick view by slug, Cook this,
    add-missing), and cook counts stay keyed the same way, so made-before
    tallies survive a migration. orgURL is deliberately left off so the Cook
    tier badges read "My recipes" rather than a Mealie label."""
    rows = (db.query(Recipe)
            .order_by(Recipe.created_at.desc(), Recipe.id.desc())
            .limit(max(1, limit))
            .all())
    out: list[dict] = []
    for r in rows:
        d = _detail_dict(db, r)
        d["source"] = "mealie"
        # The listing's id is the native store id (the same id list_recipes
        # returns), so the Cook page's "Plan" button can reference the recipe
        # in the meal plan. Only the detail shape keeps id None (no Mealie
        # media id to build image URLs from).
        d["id"] = r.id
        d.pop("orgURL", None)
        out.append(d)
    return out


# ── Writes ────────────────────────────────────────────────────────────────────

def create_from_parsed(db: Session, parsed: dict, *, source: str = "manual",
                       source_url: str | None = None,
                       structured: list | None = None,
                       slug: str | None = None) -> dict:
    """Save a normalized parsed recipe (the recipes_import.parse_recipe_file
    shape) as a new native recipe and return its detail dict.

    ``structured`` optionally carries AI-parsed ingredient entries aligned by
    position with the parsed dict's ingredient lines (the
    services.mealie.build_recipe_ingredients shape); lines without a matching
    parse keep only their raw text, so nothing is ever lost or invented.
    """
    parsed = parsed or {}
    name = str(parsed.get("name") or "").strip()
    if not name:
        raise RecipeStoreError("This recipe has no name. A name is required to save it.")
    lines, sections = split_ingredient_sections(parsed)
    steps = [str(s).strip() for s in (parsed.get("instructions") or [])
             if str(s or "").strip()]
    # The parsed shape sometimes carries the original webpage in "source"
    # (recipes_import maps sourceUrl/orgURL there); accept it only when it is
    # actually a URL, never a source label like "themealdb".
    src_url = source_url or parsed.get("source_url") or parsed.get("orgURL")
    if not src_url and _looks_like_url(parsed.get("source")):
        src_url = parsed.get("source")
    recipe = Recipe(
        slug=unique_slug(db, name) if slug is None else slug,
        name=name,
        description=str(parsed.get("description") or "").strip(),
        source=(source or "manual").strip().lower(),
        source_url=(str(src_url).strip() or None) if src_url else None,
        servings=str(parsed.get("servings") or "").strip(),
        total_time=str(parsed.get("total_time") or "").strip(),
        prep_time=str(parsed.get("prep_time") or "").strip(),
        cook_time=str(parsed.get("cook_time") or "").strip(),
        tags=json.dumps([str(t) for t in parsed.get("tags") or []]),
        categories=json.dumps([str(c) for c in parsed.get("categories") or []]),
    )
    db.add(recipe)
    db.flush()
    structured = structured if isinstance(structured, list) else []
    for pos, line in enumerate(lines):
        fields = _structured_fields(structured[pos] if pos < len(structured) else None)
        section = sections[pos] if pos < len(sections) else None
        db.add(RecipeIngredient(recipe_id=recipe.id, position=pos, text=line,
                                section=section, **fields))
    for pos, text in enumerate(steps):
        db.add(RecipeStep(recipe_id=recipe.id, position=pos, text=text))
    db.commit()
    db.refresh(recipe)
    return _detail_dict(db, recipe)


def update_from_parsed(db: Session, slug: str, parsed: dict, *,
                       structured: list | None = None) -> dict:
    """Rewrite an existing native recipe's editable fields from a parsed dict
    (the same shape create_from_parsed takes) and return its detail dict.

    Backs the comprehensive editor (FoodAssistant-83jo). The recipe's identity
    (its slug) and origin (source, source_url, image) are preserved on purpose:
    the slug is what cook counts, the meal plan, and the Current Recipe key on,
    so a rename must not orphan them. Name, description, servings,
    prep/cook/total time, tags, and the ingredient and step lists are replaced from the parsed
    dict. ``structured`` optionally carries parsed ingredient entries aligned by
    position, exactly as create_from_parsed accepts them, so a line the parser
    could not read keeps only its raw text and nothing is lost or invented.

    Raises RecipeStoreError when the slug is unknown or the name is blank.
    """
    r = get_by_slug(db, slug)
    if r is None:
        raise RecipeStoreError("That recipe could not be found in your library.")
    parsed = parsed or {}
    name = str(parsed.get("name") or "").strip()
    if not name:
        raise RecipeStoreError("This recipe has no name. A name is required to save it.")
    lines, sections = split_ingredient_sections(parsed)
    steps = [str(s).strip() for s in (parsed.get("instructions") or [])
             if str(s or "").strip()]
    r.name = name
    r.description = str(parsed.get("description") or "").strip()
    r.servings = str(parsed.get("servings") or "").strip()
    r.total_time = str(parsed.get("total_time") or "").strip()
    r.prep_time = str(parsed.get("prep_time") or "").strip()
    r.cook_time = str(parsed.get("cook_time") or "").strip()
    # Tags and categories are only touched when the caller sends them, so an
    # editor that does not expose them leaves the existing ones intact.
    if "tags" in parsed:
        r.tags = json.dumps([str(t) for t in parsed.get("tags") or []])
    if "categories" in parsed:
        r.categories = json.dumps([str(c) for c in parsed.get("categories") or []])
    # Replace the ingredient and step rows wholesale: the editor posts the full
    # lists, so a rewrite keeps ordering and positions correct without diffing.
    (db.query(RecipeIngredient)
     .filter(RecipeIngredient.recipe_id == r.id).delete())
    (db.query(RecipeStep)
     .filter(RecipeStep.recipe_id == r.id).delete())
    db.flush()
    structured = structured if isinstance(structured, list) else []
    for pos, line in enumerate(lines):
        fields = _structured_fields(structured[pos] if pos < len(structured) else None)
        section = sections[pos] if pos < len(sections) else None
        db.add(RecipeIngredient(recipe_id=r.id, position=pos, text=line,
                                section=section, **fields))
    for pos, text in enumerate(steps):
        db.add(RecipeStep(recipe_id=r.id, position=pos, text=text))
    _touch(r)
    db.commit()
    db.refresh(r)
    return _detail_dict(db, r)


def _looks_like_url(value) -> bool:
    return isinstance(value, str) and value.strip().startswith(("http://", "https://"))


def set_parsed_ingredients(db: Session, slug: str, structured: list) -> int:
    """Write AI-parsed quantity/unit/food onto a saved recipe's existing lines.

    Backs the "Parse ingredients" action in native mode: the raw text of every
    line stays exactly as written; only the parsed fields are filled in, aligned
    by position. Returns how many lines gained a parsed food."""
    r = get_by_slug(db, slug)
    if r is None:
        raise RecipeStoreError("That recipe could not be found in your library.")
    rows = (db.query(RecipeIngredient)
            .filter(RecipeIngredient.recipe_id == r.id)
            .order_by(RecipeIngredient.position, RecipeIngredient.id)
            .all())
    structured = structured if isinstance(structured, list) else []
    parsed_count = 0
    for pos, row in enumerate(rows):
        fields = _structured_fields(structured[pos] if pos < len(structured) else None)
        if not fields["food"]:
            continue
        row.quantity = fields["quantity"]
        row.unit = fields["unit"]
        row.food = fields["food"]
        row.note = fields["note"]
        parsed_count += 1
    _touch(r)
    db.commit()
    return parsed_count


def delete_recipe(db: Session, slug: str) -> bool:
    """Remove a recipe, its lines, and its image file. True when it existed."""
    r = get_by_slug(db, slug)
    if r is None:
        return False
    (db.query(RecipeIngredient)
     .filter(RecipeIngredient.recipe_id == r.id).delete())
    (db.query(RecipeStep)
     .filter(RecipeStep.recipe_id == r.id).delete())
    image = image_file(r)
    db.delete(r)
    db.commit()
    if image is not None:
        try:
            image.unlink(missing_ok=True)
        except OSError:
            pass
    return True


def _touch(r: Recipe) -> None:
    from datetime import datetime, timezone
    r.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Images ────────────────────────────────────────────────────────────────────

def images_dir() -> Path:
    from ..config import settings
    return Path(settings.data_dir) / "recipe-images"


def image_file(r: Recipe) -> Path | None:
    """The on-disk image path for a recipe, or None. Refuses a stored filename
    that would escape the images directory."""
    name = (r.image_path or "").strip()
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    return images_dir() / name


def attach_image(db: Session, slug: str, data: bytes,
                 content_type: str | None = None, url: str = "") -> str | None:
    """Store image bytes for a recipe under data_dir/recipe-images and record
    the filename. Returns the served image URL, or None when the data is empty
    or oversized (never raises: an image is a nice-to-have, not the recipe)."""
    if not data or len(data) > MAX_IMAGE_BYTES:
        return None
    r = get_by_slug(db, slug)
    if r is None:
        return None
    ext = image_extension(content_type, url)
    filename = f"{r.id}.{ext}"
    try:
        directory = images_dir()
        directory.mkdir(parents=True, exist_ok=True)
        # Drop a previous image with a different extension before writing.
        old = image_file(r)
        (directory / filename).write_bytes(data)
        if old is not None and old.name != filename:
            old.unlink(missing_ok=True)
    except OSError:
        return None
    r.image_path = filename
    _touch(r)
    db.commit()
    return f"/recipes/images/{r.id}"


def remove_image(db: Session, slug: str) -> bool:
    """Drop a recipe's image (file and record), leaving the recipe itself
    untouched. True when there was an image to remove."""
    r = get_by_slug(db, slug)
    if r is None or not (r.image_path or "").strip():
        return False
    image = image_file(r)
    r.image_path = None
    _touch(r)
    db.commit()
    if image is not None:
        try:
            image.unlink(missing_ok=True)
        except OSError:
            pass
    return True


async def fetch_image(url: str, headers: dict | None = None) -> tuple[bytes, str] | None:
    """Download an image for attach_image. Returns (bytes, content_type) or
    None on any failure; never raises, an image download must not fail a save."""
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers or {})
            resp.raise_for_status()
            if len(resp.content) > MAX_IMAGE_BYTES:
                return None
            return resp.content, resp.headers.get("content-type", "")
    except Exception:  # noqa: BLE001 - best effort by contract
        return None
