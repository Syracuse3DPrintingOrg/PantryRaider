"""Parse an uploaded recipe file into the normalized dict MealieClient.create_recipe expects.

Supported sources (detected by content, with the filename used only as a hint):
  - Generic recipe JSON   : a plain object with name/title, ingredients/steps,
                            instructions/steps, optional description/servings/source.
  - schema.org Recipe JSON-LD : a JSON object (or a file containing a
                            <script type="application/ld+json"> block, or an
                            array / @graph) carrying "@type": "Recipe".
  - Mealie export JSON    : Mealie's own recipe JSON (recipeIngredient /
                            recipeInstructions, or ingredients / instructions).

Every format is reduced to the same shape create_recipe consumes:
    {name, description, servings, total_time, ingredients[str], instructions[str], source}
Ingredients normalize to a list of plain strings; instructions normalize to a
list of plain step strings. Anything unparseable, empty, or clearly not a
recipe raises ValueError with a message suitable for showing to the user.
"""
import json
import re


def parse_recipe_file(filename: str, raw: bytes) -> dict:
    """Convert a supported recipe file into a normalized recipe dict.

    Raises ValueError (with a user-facing message) when the file is empty,
    is not valid JSON / JSON-LD, or does not look like a recipe.
    """
    if not raw or not raw.strip():
        raise ValueError("The file is empty.")

    text = raw.decode("utf-8", errors="replace").strip()

    # A page or fragment may embed the recipe in a <script type="ld+json"> block.
    embedded = _extract_ld_json_block(text)
    payload_text = embedded if embedded is not None else text

    try:
        data = json.loads(payload_text)
    except json.JSONDecodeError as e:
        raise ValueError(f"This file is not valid recipe JSON: {e.msg}.")

    recipe = _find_recipe_object(data)
    if recipe is None:
        raise ValueError("Could not find a recipe in this file. Supported formats: "
                         "generic recipe JSON, schema.org Recipe JSON-LD, and Mealie export JSON.")

    name = _first_str(recipe.get("name"), recipe.get("title"))
    if not name:
        raise ValueError("This recipe has no name. A name is required to import it.")

    ingredients = _normalize_ingredients(
        recipe.get("recipeIngredient")
        if recipe.get("recipeIngredient") is not None
        else recipe.get("ingredients"))
    instructions = _normalize_instructions(
        recipe.get("recipeInstructions")
        if recipe.get("recipeInstructions") is not None
        else (recipe.get("instructions")
              if recipe.get("instructions") is not None
              else recipe.get("steps")))

    return {
        "name": name,
        "description": _first_str(recipe.get("description")),
        "servings": _yield_text(recipe.get("recipeYield"), recipe.get("servings"),
                                recipe.get("yield")),
        "total_time": _first_str(recipe.get("total_time"), recipe.get("totalTime")),
        "ingredients": ingredients,
        "instructions": instructions,
        "source": _first_str(recipe.get("source"), recipe.get("source_url"),
                             recipe.get("sourceUrl"), recipe.get("url"),
                             recipe.get("orgURL")),
    }


# ── Detection helpers ─────────────────────────────────────────────────────────

_LD_JSON_RE = re.compile(
    r"<script[^>]*type\s*=\s*['\"]application/ld\+json['\"][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)


def _extract_ld_json_block(text: str) -> str | None:
    """Return the JSON inside the first <script type="application/ld+json"> tag, if any."""
    if "<script" not in text.lower():
        return None
    m = _LD_JSON_RE.search(text)
    return m.group(1).strip() if m else None


def _find_recipe_object(data) -> dict | None:
    """Locate the recipe object within parsed JSON / JSON-LD.

    Handles a bare recipe object, a JSON-LD ``@graph`` wrapper, and a top-level
    array of nodes (common in scraped JSON-LD). When nothing is explicitly
    typed as a Recipe, fall back to any object that looks like a recipe (carries
    ingredients or instructions under a known key).
    """
    if isinstance(data, dict):
        if _is_recipe_type(data):
            return data
        graph = data.get("@graph")
        if isinstance(graph, list):
            found = _find_in_list(graph)
            if found is not None:
                return found
        if _looks_like_recipe(data):
            return data
        return None
    if isinstance(data, list):
        return _find_in_list(data)
    return None


def _find_in_list(nodes: list) -> dict | None:
    for node in nodes:
        if isinstance(node, dict) and _is_recipe_type(node):
            return node
    for node in nodes:
        if isinstance(node, dict) and _looks_like_recipe(node):
            return node
    return None


def _is_recipe_type(obj: dict) -> bool:
    t = obj.get("@type")
    if isinstance(t, str):
        return t.lower() == "recipe"
    if isinstance(t, list):
        return any(isinstance(x, str) and x.lower() == "recipe" for x in t)
    return False


def _looks_like_recipe(obj: dict) -> bool:
    """True when an untyped object still carries recipe-shaped fields."""
    has_name = bool(obj.get("name") or obj.get("title"))
    has_body = any(obj.get(k) for k in
                   ("recipeIngredient", "ingredients", "recipeInstructions",
                    "instructions", "steps"))
    return has_name and has_body


# ── Field normalization ───────────────────────────────────────────────────────

def _normalize_ingredients(value) -> list[str]:
    """Reduce ingredients (strings or objects) to a list of plain strings."""
    out: list[str] = []
    for item in value or []:
        text = ""
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            # Mealie uses {note} or {food:{name}}; generic JSON may use {name}/{text}.
            food = item.get("food") or {}
            text = _first_str(item.get("note"), item.get("display"),
                              food.get("name") if isinstance(food, dict) else None,
                              item.get("name"), item.get("text"), item.get("ingredient"))
        text = text.strip()
        if text:
            out.append(text)
    return out


def _normalize_instructions(value) -> list[str]:
    """Reduce instructions (strings or HowToStep/Mealie step objects) to step strings.

    A single instruction string with newlines or numbered lines is split into
    steps. schema.org HowToSection objects (with nested itemListElement) are
    flattened.
    """
    if isinstance(value, str):
        return _split_instruction_text(value)

    out: list[str] = []
    for item in value or []:
        if isinstance(item, str):
            out.extend(_split_instruction_text(item))
        elif isinstance(item, dict):
            # HowToSection: recurse into its steps.
            if isinstance(item.get("itemListElement"), list):
                out.extend(_normalize_instructions(item["itemListElement"]))
                continue
            text = _first_str(item.get("text"), item.get("instruction"),
                              item.get("name")).strip()
            if text:
                out.append(text)
    return out


def _split_instruction_text(text: str) -> list[str]:
    """Split a blob of instruction text into individual steps."""
    parts = [p.strip() for p in re.split(r"[\r\n]+", text or "") if p.strip()]
    # Drop a leading step number like "1." or "1)" left over from numbered lists.
    return [re.sub(r"^\s*\d+[.)]\s*", "", p) for p in parts]


def _yield_text(*values) -> str:
    """Coerce a recipeYield / servings value (str, number, or list) to text."""
    for v in values:
        if v is None or v == "":
            continue
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, str):
            return v.strip()
        if isinstance(v, list):
            for part in v:
                if isinstance(part, str) and part.strip():
                    return part.strip()
                if isinstance(part, (int, float)):
                    return str(part)
    return ""


def _first_str(*values) -> str:
    """Return the first non-empty string among the values (coercing numbers)."""
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)):
            return str(v)
    return ""
