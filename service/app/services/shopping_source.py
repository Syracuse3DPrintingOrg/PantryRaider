"""The shopping-list backend seam (FoodAssistant-g0fd).

The shopping list historically lived in Mealie. Grocy is the inventory
backbone of every install, and it carries a first-class shopping list of its
own, so the list moves there: "things you buy" stay next to "things you
stock", one backup covers both, and the Shopping page works with no Mealie
installed.

``active_backend`` decides which store holds the list:

  * An explicit ``shopping_backend`` setting ("grocy" or "mealie") always wins.
  * Auto (the default, empty): the list follows Grocy, EXCEPT on an install
    whose recipe library still runs in Mealie (recipe_source.active_backend()
    == "mealie"). Those installs have a Mealie shopping list they actively use,
    and yanking it out from under them on upgrade would violate the
    existing-installs-are-production rule. When they migrate their recipes,
    shopping follows automatically.

Every /mealie/shopping* endpoint consults this seam and keeps its existing
wire shapes (the Mealie ones the Shopping page, the Manage Pantry quick-add,
the scanner shopping mode, the Stream Deck quick-check page, and the Home
Assistant sensors already read), so switching backends changes where items
live, never what callers see.

The Grocy half of the seam lives here too: thin async functions over
GrocyClient's shopping endpoints that translate Grocy rows
(shopping_list_items: id/note/amount/done/product_id) into the Mealie
listItems shape (id/note/display/quantity/checked/food). The pure row
translation is kept separate from the HTTP calls so it unit-tests without a
network.
"""
from __future__ import annotations

from ..config import settings
from . import recipe_source

BACKEND_GROCY = "grocy"
BACKEND_MEALIE = "mealie"


def active_backend() -> str:
    """Which store holds the shopping list: "grocy" or "mealie".

    See the module docstring for the rule. Deliberately independent of whether
    the chosen backend is currently reachable: an outage degrades the page
    with an honest message rather than silently swapping the user's list.
    """
    value = (getattr(settings, "shopping_backend", "") or "").strip().lower()
    if value in (BACKEND_GROCY, BACKEND_MEALIE):
        return value
    if (recipe_source.active_backend() == recipe_source.BACKEND_MEALIE
            and settings.mealie_configured()):
        return BACKEND_MEALIE
    return BACKEND_GROCY


def shopping_available() -> bool:
    """True when this install has a shopping list to talk to.

    Grocy-backed shopping needs a configured Grocy (or a satellite's upstream
    link, which proxies to the server's Grocy); Mealie-backed shopping needs
    the Mealie connection.
    """
    if active_backend() == BACKEND_MEALIE:
        return settings.mealie_configured()
    if settings.is_satellite():
        return bool(settings.remote_server_url and settings.upstream_api_key)
    return bool(settings.grocy_base_url and settings.grocy_api_key)


# ── Grocy rows in the Mealie wire shape ──────────────────────────────────────

def grocy_item_wire(row: dict) -> dict:
    """One Grocy shopping_list_items row as a Mealie listItems entry. Pure.

    The Shopping page, the deck quick-check keys, and the HA summary all read
    the Mealie fields (id, note, display, quantity, checked, food.name), so a
    Grocy row is translated once here and every caller stays unchanged. A row
    linked to a Grocy product surfaces the product name as the food, exactly
    like a structured Mealie item.
    """
    note = str(row.get("note") or "").strip()
    product = str(row.get("product_name") or "").strip()
    try:
        quantity = float(row.get("amount") or 1)
    except (TypeError, ValueError):
        quantity = 1.0
    label = note or product
    if quantity and quantity != 1:
        # Mealie's display carries the amount; mirror that so "2 x Milk" reads
        # the same on either backend.
        int_qty = int(quantity) if float(quantity).is_integer() else quantity
        display = f"{int_qty} x {label}" if label else ""
    else:
        display = label
    return {
        "id": row.get("id"),
        "note": note or product,
        "display": display,
        "quantity": quantity,
        "checked": bool(int(row.get("done") or 0)),
        "food": {"name": product} if product else None,
    }


def sort_items_wire(items: list[dict]) -> list[dict]:
    """Unchecked first, then alphabetical: the order the Shopping page shows."""
    return sorted(items, key=lambda i: (bool(i.get("checked")),
                                        str(i.get("note") or "").lower()))


async def grocy_get_shopping(list_id: str = "") -> dict:
    """The GET /mealie/shopping payload from Grocy's list.

    Same shape as the Mealie branch: {lists, list, items}, items in the Mealie
    listItems form. A missing default list is created (Grocy installs start
    with one, but an empty database should still get a working page).
    """
    from .grocy import GrocyClient
    g = GrocyClient()
    default_id = await g.ensure_shopping_list()
    lists = await g.get_shopping_lists()
    wire_lists = [{"id": str(l["id"]), "name": l.get("name") or "Shopping list"}
                  for l in lists]
    selected = next((l for l in wire_lists if l["id"] == str(list_id)),
                    None) or next((l for l in wire_lists
                                   if l["id"] == str(default_id)), wire_lists[0])
    rows = await g.get_shopping_items(int(selected["id"]))
    items = sort_items_wire([grocy_item_wire(r) for r in rows])
    return {"lists": wire_lists, "list": selected, "items": items}


async def grocy_add_item(list_id: str, note: str, quantity: float = 1.0) -> dict:
    """Add one line to the Grocy list, product-linked when the text names a
    product the inventory already knows (a nicety: Grocy then shows it as the
    real product). Returns {"id": ...} like the Mealie branch."""
    from .grocy import GrocyClient
    g = GrocyClient()
    lid = int(list_id) if str(list_id).strip() else await g.ensure_shopping_list()
    product_id = None
    try:
        product_id = await g.product_id_by_name(note)
    except Exception:  # noqa: BLE001 - linking is optional, adding is not
        product_id = None
    result = await g.add_shopping_item(lid, note, quantity, product_id=product_id)
    return {"id": result.get("created_object_id")}


async def grocy_unchecked_items() -> tuple[list[dict], str]:
    """(unchecked items in wire shape, list name) for the default Grocy list.

    Backs the HA summary and the deck count; raises GrocyError upward so each
    caller keeps its own degrade behavior.
    """
    from .grocy import GrocyClient
    g = GrocyClient()
    lists = await g.get_shopping_lists()
    if not lists:
        return [], ""
    rows = await g.get_shopping_items(int(lists[0]["id"]))
    items = [grocy_item_wire(r) for r in rows]
    return ([i for i in items if not i["checked"]],
            lists[0].get("name") or "Shopping list")


async def grocy_suggest_products(prefix: str, limit: int = 8) -> list[str]:
    """Product names for the quick-add typeahead, from Grocy's catalog.

    Prefix hits first, then names that merely contain the text, each group
    alphabetical: the same ordering the Mealie foods typeahead uses.
    """
    key = (prefix or "").strip().lower()
    if not key:
        return []
    from .grocy import GrocyClient
    products = await GrocyClient().get_products()
    names = sorted({str(p.get("name") or "").strip()
                    for p in products if p.get("name")}, key=str.lower)
    starts = [n for n in names if n.lower().startswith(key)]
    contains = [n for n in names if key in n.lower()
                and not n.lower().startswith(key)]
    return (starts + contains)[: max(1, limit)]


# ── Backend-agnostic helpers shared by the quick-add surfaces ────────────────

async def quick_add(item: str, quantity: float = 1.0) -> str:
    """Add one item to the active shopping list, whichever backend holds it.

    Used by the scanner shopping mode and the Start Page / deck quick-add key.
    Returns the name of the list the item landed on. Raises the backend's own
    error (GrocyError / MealieError / ValueError) for the caller to report.
    """
    if active_backend() == BACKEND_MEALIE:
        from .mealie import MealieClient
        m = MealieClient()
        lists = await m.get_shopping_lists()
        if not lists:
            raise ValueError("There is no shopping list yet.")
        await m.add_shopping_item(lists[0]["id"], item, quantity)
        return lists[0].get("name") or "Shopping List"
    from .grocy import GrocyClient
    g = GrocyClient()
    lid = await g.ensure_shopping_list()
    await grocy_add_item(str(lid), item, quantity)
    lists = await g.get_shopping_lists()
    name = next((l.get("name") for l in lists if int(l["id"]) == lid), None)
    return name or "Shopping list"


async def autocheck(item_name: str) -> None:
    """Check off shopping items that token-match a just-stocked item name.

    Backs the barcode_autocheck_shopping setting: adding an item to inventory
    ticks it off the list, on whichever backend holds the list.
    """
    from .mealie import _tokens
    item_toks = _tokens(item_name)
    if not item_toks:
        return
    if active_backend() == BACKEND_MEALIE:
        from .mealie import MealieClient
        m = MealieClient()
        for lst in await m.get_shopping_lists():
            detail = await m.get_shopping_list(lst["id"])
            for si in detail.get("listItems", []):
                if si.get("checked"):
                    continue
                if item_toks & _tokens(si.get("note") or ""):
                    await m.update_shopping_item(si["id"], {**si, "checked": True})
        return
    from .grocy import GrocyClient
    g = GrocyClient()
    for lst in await g.get_shopping_lists():
        for row in await g.get_shopping_items(int(lst["id"])):
            if int(row.get("done") or 0):
                continue
            text = row.get("note") or row.get("product_name") or ""
            if item_toks & _tokens(text):
                await g.toggle_shopping_item(int(row["id"]), True)
