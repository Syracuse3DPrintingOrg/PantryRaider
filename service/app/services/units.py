"""Grocy quantity-unit conversion resolution for the recipe matcher. Pure.

Grocy stores unit conversions as rows in /objects/quantity_unit_conversions:
{id, product_id (null for a global rule), from_qu_id, to_qu_id, factor},
meaning one from-unit equals ``factor`` to-units. The recipe-vs-stock matcher
uses these to decide whether the stock on hand actually covers a recipe
amount (the "recipe wants 200 g, stock tracks bottles" case). Resolution
never guesses: a factor comes back only when the same unit name appears on
both sides or Grocy has the rows for it (product-specific rows beating
global ones), followed transitively at most one hop (a to b to c). Anything
unresolvable returns None so the caller keeps its name-only matching.
"""


def _norm(name) -> str:
    """Case-insensitive, whitespace-trimmed key for matching a unit name."""
    return str(name or "").strip().lower()


def _unit_ids(units: list[dict]) -> dict[str, int]:
    """Normalized unit name (singular and plural) to Grocy unit id.

    The first unit claiming a name wins, so a duplicate name in the table
    resolves deterministically instead of flapping between ids.
    """
    out: dict[str, int] = {}
    for row in units or []:
        try:
            uid = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        for key in ("name", "name_plural"):
            name = _norm(row.get(key))
            if name and name not in out:
                out[name] = uid
    return out


def _edges(product_id, conversions: list[dict]) -> dict[tuple[int, int], float]:
    """(from_qu_id, to_qu_id) to factor, for this product's usable rows.

    Global rows load first so a product-specific row for the same unit pair
    overwrites (beats) the global one. Rows belonging to other products, rows
    with a zero, negative, or unreadable factor, and rows with malformed ids
    are dropped rather than guessed at.
    """
    try:
        wanted = int(product_id)
    except (TypeError, ValueError):
        wanted = 0
    usable: list[tuple[bool, int, int, float]] = []
    for row in conversions or []:
        if not isinstance(row, dict):
            continue
        pid_raw = row.get("product_id")
        if pid_raw in (None, "", 0, "0"):
            specific = False
        else:
            try:
                pid = int(pid_raw)
            except (TypeError, ValueError):
                continue
            if pid != wanted:
                continue
            specific = True
        try:
            from_id = int(row.get("from_qu_id"))
            to_id = int(row.get("to_qu_id"))
            factor = float(row.get("factor"))
        except (TypeError, ValueError):
            continue
        if factor <= 0:
            continue
        usable.append((specific, from_id, to_id, factor))
    edges: dict[tuple[int, int], float] = {}
    # Global rows first (False sorts before True), so product rows overwrite.
    for specific, from_id, to_id, factor in sorted(usable, key=lambda r: r[0]):
        edges[(from_id, to_id)] = factor
    return edges


def conversion_factor(product_id, from_unit, to_unit,
                      units: list[dict] | None,
                      conversions: list[dict] | None) -> float | None:
    """How many ``to_unit`` one ``from_unit`` of this product equals, or None.

    Unit names match Grocy's quantity_units table case-insensitively, plural
    names included; the same name on both sides is trivially 1.0. A direct
    conversion row wins; failing that, one transitive hop (a to b to c) is
    followed with the factors multiplied, smallest intermediate pair first so
    the answer is deterministic. Product-specific rows always beat global
    rows for the same unit pair. No chain means None, never a guess.
    """
    from_name, to_name = _norm(from_unit), _norm(to_unit)
    if not from_name or not to_name:
        return None
    if from_name == to_name:
        return 1.0
    ids = _unit_ids(units or [])
    from_id, to_id = ids.get(from_name), ids.get(to_name)
    if from_id is None or to_id is None:
        return None
    if from_id == to_id:
        return 1.0
    edges = _edges(product_id, conversions or [])
    direct = edges.get((from_id, to_id))
    if direct is not None:
        return direct
    for (a, mid), first in sorted(edges.items()):
        if a != from_id:
            continue
        second = edges.get((mid, to_id))
        if second is not None:
            return first * second
    return None
