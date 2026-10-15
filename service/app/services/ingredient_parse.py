"""Turn recipe ingredient lines into structured quantity/unit/food entries.

Shared by the native recipe store and the Mealie backend: the save and edit
paths and the "Parse ingredients" action all build their ingredient lists
here, so neither needs the Mealie client loaded. Pure apart from the optional
AI parse in build_recipe_ingredients.
"""
from ..config import settings
from .recipe_match import _to_quantity


# ── Structured ingredient normalization (FoodAssistant-au59) ─────────────────
# Turn an AI provider's parsed {quantity, unit, food, note} objects into the
# recipeIngredient shape Mealie stores. Pure and shared so the save path and the
# "Parse ingredients" action agree, and so it is cheap to unit-test. The guiding
# rule is that an ingredient is NEVER lost: a line the model could not parse (no
# food) falls back to the plain note entry Mealie has always accepted, exactly
# like the pre-au59 behavior. Amounts go through the shared recipe_match
# quantity parser.


def structured_ingredient(line: str, parsed) -> dict | None:
    """One Mealie recipeIngredient entry from an original line and its parse.

    ``line`` is always the user's original text and becomes ``originalText`` and
    the fallback note, so it is never dropped or altered. When ``parsed`` has no
    usable food, the entry is a plain ``{"note": line}`` (today's free-text
    shape), which is what keeps an unparseable ingredient from being lost.
    """
    line = (line or "").strip()
    if not line:
        return None
    food = ""
    unit = ""
    note = ""
    quantity = None
    if isinstance(parsed, dict):
        food = str(parsed.get("food") or "").strip()
        unit = str(parsed.get("unit") or "").strip()
        note = str(parsed.get("note") or "").strip()
        quantity = _to_quantity(parsed.get("quantity"))
    if not food:
        return {"note": line}
    return {
        "quantity": quantity,
        # This is the intermediate {name} shape. Mealie 3.19 will not accept a
        # food or unit named this way, so the async resolver on MealieClient
        # (resolve_structured_ids) turns each {"name": ...} into {"id", "name"}
        # by finding or creating the food/unit before the recipe is saved. null
        # means "no unit" (fine for "2 eggs" or "salt to taste").
        "unit": {"name": unit} if unit else None,
        "food": {"name": food},
        "note": note,
        "originalText": line,
        # Show the amount: false keeps quantity/unit visible. A null quantity
        # simply renders as the food alone, so "salt to taste" reads correctly.
        "disableAmount": False,
    }


def structured_recipe_ingredients(lines: list[str], parsed_list) -> list[dict]:
    """Normalize whole ingredient lists, one entry per non-empty line, in order.

    Aligns the model's objects to the original lines by position. A short,
    over-long, or missing result never drops a line: any line without a matching
    parse falls back to a plain note entry.
    """
    parsed_list = parsed_list if isinstance(parsed_list, list) else []
    out: list[dict] = []
    for i, line in enumerate(lines or []):
        if not (line or "").strip():
            continue
        parsed = parsed_list[i] if i < len(parsed_list) else None
        entry = structured_ingredient(line, parsed)
        if entry:
            out.append(entry)
    return out


async def build_recipe_ingredients(lines: list[str]) -> list[dict]:
    """Intermediate recipeIngredient list for a create/patch, AI-parsed when able.

    When an AI provider is configured, the lines are parsed into
    quantity/unit/food so the recipe can land parsed in Mealie. This returns the
    {name} intermediate shape; MealieClient.resolve_structured_ids then swaps
    each food/unit name for its Mealie id before the save. Resilient by design:
    no provider, a provider that does not support parsing (returns None), or any
    error falls back to the plain note entries Mealie has always taken, so a save
    never breaks and no ingredient is lost.
    """
    lines = [line.strip() for line in (lines or []) if line and line.strip()]
    if not lines:
        return []
    from . import usage
    # Once the monthly AI budget is spent the lines are saved as written, the
    # same as with no provider at all.
    if settings.ai_configured() and not usage.over_budget():
        try:
            from ..dependencies import get_enrich_provider
            parsed = await get_enrich_provider().parse_ingredients(lines)
            if parsed:
                return structured_recipe_ingredients(lines, parsed)
        except Exception:
            # A bad reply, a provider outage, or an unsupported provider all fall
            # through to the free-text shape rather than failing the save.
            pass
    return [{"note": line} for line in lines]
