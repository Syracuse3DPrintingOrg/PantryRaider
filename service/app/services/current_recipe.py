"""In-memory holder for the single active ("current") recipe.

The main server keeps ONE active recipe in process memory so that the future
Current Recipe tab, Stream Deck, and satellites can all read the same thing. A
recipe is populated from a Mealie/Grocy recipe, an imported recipe, or an AI
recipe, then normalized into a stable shape (title, source, servings, scaled
servings, ingredients, steps, notes).

This is deliberately process-local and thread-safe via a module lock. There is
no disk persistence: the active recipe is ephemeral session state, and the
later epic beads (Current Recipe tab, satellite wiring) decide how surfaces sync
it. Keep the I/O out so the core normalization/scaling stays pure and testable.
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Ingredient:
    """A single ingredient line. quantity is the BASE (1x servings) amount; the
    scaled amount is derived on read from servings_scale, never stored, so the
    original recipe is never lost when the user scales up and back down."""
    name: str
    quantity: float | None = None
    unit: str | None = None


@dataclass
class ActiveRecipe:
    title: str = ""
    source: str = ""            # e.g. "mealie", "import", "ai", free-form
    id: str | None = None       # upstream id (Mealie slug, etc.) when known
    servings: int = 1           # base servings the quantities are written for
    servings_scale: float = 1.0  # multiplier applied to quantities/servings
    ingredients: list[Ingredient] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    notes: str = ""


_lock = threading.Lock()
_active: ActiveRecipe | None = None
# Whether we have tried to load the persisted recipe yet this process. A loaded
# recipe stays active until the user clears or cooks it, surviving a restart
# (FoodAssistant-yurm), so it is read back from disk once on first access.
_loaded = False


def _recipe_path() -> Path:
    """Path of the persisted current recipe under the app data dir."""
    from ..config import settings
    return Path(settings.data_dir) / "current_recipe.json"


def _from_stored(data: dict) -> ActiveRecipe:
    """Rebuild an ActiveRecipe from its persisted dict (asdict form), preserving
    the servings scale so a restored recipe keeps the user's chosen scale."""
    ings = [
        Ingredient(name=str(i.get("name", "")), quantity=i.get("quantity"),
                   unit=i.get("unit"))
        for i in (data.get("ingredients") or []) if isinstance(i, dict)
    ]
    return ActiveRecipe(
        title=str(data.get("title", "")), source=str(data.get("source", "")),
        id=data.get("id"), servings=int(data.get("servings", 1) or 1),
        servings_scale=float(data.get("servings_scale", 1.0) or 1.0),
        ingredients=ings, steps=list(data.get("steps") or []),
        notes=str(data.get("notes", "")),
    )


def _ensure_loaded_locked() -> None:
    """Load the persisted recipe once, on first access. Caller holds the lock."""
    global _active, _loaded
    if _loaded:
        return
    _loaded = True
    try:
        path = _recipe_path()
        if path.exists():
            data = json.loads(path.read_text())
            if data:
                _active = _from_stored(data)
    except Exception:  # noqa: BLE001 - a bad/old file must not crash the app
        _active = None


def _persist_locked() -> None:
    """Write the active recipe (or clear the file) to disk. Caller holds the
    lock. Best-effort: a write failure leaves the in-memory state authoritative."""
    try:
        path = _recipe_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if _active is None:
            if path.exists():
                path.unlink()
        else:
            path.write_text(json.dumps(asdict(_active)))
    except Exception:  # noqa: BLE001 - persistence is best-effort
        pass


def _to_float(value) -> float | None:
    """Best-effort numeric coercion for an ingredient quantity. Blank/None and
    unparseable strings (e.g. "to taste") become None so they render as-is."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_ingredient(raw) -> Ingredient:
    """Accept a dict {name, quantity, unit} or a bare string and return an
    Ingredient. Unknown keys are ignored."""
    if isinstance(raw, str):
        return Ingredient(name=raw.strip())
    name = str(raw.get("name", "")).strip()
    return Ingredient(
        name=name,
        quantity=_to_float(raw.get("quantity")),
        unit=(str(raw.get("unit")).strip() or None) if raw.get("unit") else None,
    )


def _normalize(recipe_dict: dict) -> ActiveRecipe:
    """Coerce an arbitrary recipe dict into a clean ActiveRecipe. Tolerant of
    missing keys so callers (Mealie, import, AI) need not all agree on shape."""
    d = recipe_dict or {}
    try:
        servings = int(d.get("servings") or 1)
    except (TypeError, ValueError):
        servings = 1
    if servings < 1:
        servings = 1
    try:
        scale = float(d.get("servings_scale") or 1.0)
    except (TypeError, ValueError):
        scale = 1.0
    if scale <= 0:
        scale = 1.0

    raw_ings = d.get("ingredients") or []
    ingredients = [_normalize_ingredient(i) for i in raw_ings]
    ingredients = [i for i in ingredients if i.name]

    raw_steps = d.get("steps") or []
    steps = [str(s).strip() for s in raw_steps if str(s).strip()]

    rid = d.get("id")
    return ActiveRecipe(
        title=str(d.get("title", "")).strip(),
        source=str(d.get("source", "")).strip(),
        id=str(rid) if rid not in (None, "") else None,
        servings=servings,
        servings_scale=scale,
        ingredients=ingredients,
        steps=steps,
        notes=str(d.get("notes", "")).strip(),
    )


def _serialize(recipe: ActiveRecipe) -> dict:
    """Render an ActiveRecipe to a JSON-friendly dict. Ingredient quantities are
    returned at their BASE value plus a derived scaled_quantity, and a derived
    scaled_servings, so a surface can show either without redoing the math."""
    out = asdict(recipe)
    scale = recipe.servings_scale or 1.0
    for raw, ing in zip(out["ingredients"], recipe.ingredients):
        qty = ing.quantity
        raw["scaled_quantity"] = round(qty * scale, 3) if qty is not None else None
    out["scaled_servings"] = round(recipe.servings * scale, 3)
    return out


def set_active(recipe_dict: dict) -> dict:
    """Replace the active recipe with a normalized copy of recipe_dict and
    return the serialized form."""
    global _active, _loaded
    normalized = _normalize(recipe_dict)
    with _lock:
        _loaded = True  # an explicit set supersedes anything on disk
        _active = normalized
        _persist_locked()
        return _serialize(_active)


def get_active() -> dict | None:
    """Return the serialized active recipe, or None when nothing is loaded."""
    with _lock:
        _ensure_loaded_locked()
        if _active is None:
            return None
        return _serialize(_active)


def clear_active() -> None:
    """Forget the active recipe (and remove the persisted copy)."""
    global _active, _loaded
    with _lock:
        _loaded = True
        _active = None
        _persist_locked()


def _mealie_ingredient(raw: dict) -> dict:
    """Map one Mealie recipeIngredient entry to the {name, quantity, unit} shape.

    A structured entry has food.name plus quantity and unit.name. An
    unstructured one only carries a free-text note/display, which becomes the
    whole name with no quantity."""
    food = raw.get("food") or {}
    unit = raw.get("unit") or {}
    name = str(food.get("name") or "").strip()
    if not name:
        name = str(raw.get("note") or raw.get("display") or "").strip()
        return {"name": name}
    return {
        "name": name,
        "quantity": raw.get("quantity"),
        "unit": (str(unit.get("name")).strip() or None) if unit.get("name") else None,
    }


def _mealie_servings(recipe_yield) -> int:
    """Parse a Mealie recipeYield ('4 servings', '4', 4) into an int >= 1."""
    if isinstance(recipe_yield, (int, float)):
        n = int(recipe_yield)
    else:
        import re as _re
        m = _re.search(r"\d+", str(recipe_yield or ""))
        n = int(m.group()) if m else 1
    return n if n >= 1 else 1


def from_mealie_detail(detail: dict, slug: str = "") -> dict:
    """Convert a Mealie recipe detail object into the set_active() input shape.

    Tolerant of Mealie's nested ingredient/instruction objects so a recipe can
    be made the Current Recipe straight from its Mealie slug (FoodAssistant-1g4l)."""
    d = detail or {}
    ings = [_mealie_ingredient(i) for i in (d.get("recipeIngredient") or []) if isinstance(i, dict)]
    ings = [i for i in ings if i.get("name")]
    steps = []
    for s in d.get("recipeInstructions") or []:
        text = (s.get("text") if isinstance(s, dict) else str(s)) or ""
        text = text.strip()
        if text:
            steps.append(text)
    return {
        "title": str(d.get("name") or "").strip(),
        "source": "mealie",
        "id": slug or d.get("slug") or "",
        "servings": _mealie_servings(d.get("recipeYield")),
        "ingredients": ings,
        "steps": steps,
        "notes": str(d.get("description") or "").strip(),
    }


def scale_servings(factor: float) -> dict | None:
    """Set the servings-scale multiplier on the active recipe and return the
    serialized form. Returns None when no recipe is loaded. A non-positive or
    unparseable factor is ignored (kept at the current scale)."""
    with _lock:
        _ensure_loaded_locked()
        if _active is None:
            return None
        try:
            f = float(factor)
        except (TypeError, ValueError):
            f = _active.servings_scale
        if f > 0:
            _active.servings_scale = f
        _persist_locked()
        return _serialize(_active)
