"""Readable plain-text rendering of a saved recipe (FoodAssistant-74c1).

The share hub lets a recipe leave the kitchen as plain text: a downloadable
.txt file and the body of a share email. Both come through here. The input is
the normalized preview shape the recipe detail endpoint serves (name,
description, servings, prep/cook/total times, ingredient display strings with
their position-aligned section headings, and step texts), and the output is
text a person can read anywhere: title and times up top, INGREDIENTS one per
line with the section headings kept, then numbered STEPS, everything wrapped
at 78 columns.

Pure string work only, no I/O and no app state, so the exact output stays
byte-for-byte testable.
"""
from __future__ import annotations

import textwrap

# Wrap width for every paragraph. 78 keeps the text readable in mail clients
# and terminals that still assume 80-column lines.
WRAP_WIDTH = 78


def _wrap(text: str, first_prefix: str = "", rest_prefix: str = "") -> list[str]:
    """One paragraph wrapped at the module width, with a lead-in prefix on the
    first line and a hanging indent on the rest ("- " lists, "3. " steps)."""
    return textwrap.wrap(
        text,
        width=WRAP_WIDTH,
        initial_indent=first_prefix,
        subsequent_indent=rest_prefix,
    )


def _clean_lines(values) -> list[str]:
    """The non-empty entries of a list, as stripped strings."""
    return [str(v).strip() for v in values or [] if str(v).strip()]


def format_recipe_text(recipe: dict) -> str:
    """A saved recipe as readable plain text.

    ``recipe`` uses the detail-endpoint keys: name, description, servings,
    prep_time, cook_time, total_time, ingredients, ingredient_sections (a
    position-aligned heading per ingredient line, "" when a line sits under no
    heading), instructions, and source_url. Every field is optional; missing
    pieces are simply left out rather than rendered as blanks.
    """
    out: list[str] = []
    name = str(recipe.get("name") or "").strip() or "Recipe"
    out.extend(_wrap(name))

    meta = []
    servings = str(recipe.get("servings") or "").strip()
    if servings:
        meta.append(f"Serves: {servings}")
    for key, label in (("prep_time", "Prep time"), ("cook_time", "Cook time"),
                       ("total_time", "Total time")):
        value = str(recipe.get(key) or "").strip()
        if value:
            meta.append(f"{label}: {value}")
    if meta:
        out.append("")
        out.extend(meta)

    description = str(recipe.get("description") or "").strip()
    if description:
        out.append("")
        out.extend(_wrap(description))

    ingredients = _clean_lines(recipe.get("ingredients"))
    sections = recipe.get("ingredient_sections") or []
    if ingredients:
        out.extend(["", "INGREDIENTS", ""])
        current = ""
        for idx, line in enumerate(ingredients):
            heading = str(sections[idx] if idx < len(sections) else "").strip()
            if heading and heading != current:
                if current or idx:
                    out.append("")
                out.append(heading if heading.endswith(":") else f"{heading}:")
                current = heading
            out.extend(_wrap(line, first_prefix="- ", rest_prefix="  "))

    steps = _clean_lines(recipe.get("instructions"))
    if steps:
        out.extend(["", "STEPS", ""])
        for n, step in enumerate(steps, 1):
            prefix = f"{n}. "
            out.extend(_wrap(step, first_prefix=prefix,
                             rest_prefix=" " * len(prefix)))

    source = str(recipe.get("source_url") or "").strip()
    if source:
        out.append("")
        out.append(f"Source: {source}")

    return "\n".join(out) + "\n"
