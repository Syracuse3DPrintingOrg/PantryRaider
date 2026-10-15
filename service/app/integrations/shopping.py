"""ShoppingBackend inhabitants: Grocy and Mealie (FoodAssistant-pjtq).

The shopping-list seam (services.shopping_source) already routed every
surface's quick-add and stock-and-tick-off through one pair of functions;
those two operations now live on the backends themselves, and the seam
module delegates to whichever one the registry resolves as active. The
Grocy wire-shape helpers (grocy_item_wire, grocy_get_shopping, ...) stay in
services.shopping_source, where the /mealie/shopping* endpoints and their
tests already use them: they are the Grocy backend's implementation detail,
not part of the cross-backend contract.

Like the recipe seam, Mealie is imported only inside the Mealie backend's
methods, never at module load.
"""
from __future__ import annotations

from .interfaces import KIND_SHOPPING, ShoppingBackend

# The two backend names, the exact strings the shopping_backend setting
# stores. services.shopping_source re-exports them as BACKEND_GROCY /
# BACKEND_MEALIE.
NAME_GROCY = "grocy"
NAME_MEALIE = "mealie"


def stocked_item_covers(item_name: str, row_text: str) -> bool:
    """True when stocking ``item_name`` should tick the list row ``row_text``.

    Every significant word of the row has to appear in the item: stocking
    "Great Value Whole Milk" ticks "Milk" and "whole milk" but not "Whole
    wheat bread", and "Ground Coffee" leaves "Ground beef" alone. A row with
    no significant words is never ticked. Pure, and shared by both backends.
    """
    from ..services.recipe_match import _tokens
    row_toks = _tokens(row_text)
    return bool(row_toks) and row_toks <= _tokens(item_name)


class GrocyShoppingBackend(ShoppingBackend):
    """The shopping list kept in Grocy, next to the inventory."""

    name = NAME_GROCY
    label = "Grocy shopping list"

    def enabled(self) -> bool:
        return self.configured()

    def configured(self) -> bool:
        """Grocy-backed shopping needs a configured Grocy, or a satellite's
        upstream link (which proxies to the server's Grocy)."""
        from ..config import settings
        if settings.is_satellite():
            return settings.upstream_linked()
        return bool(settings.grocy_base_url and settings.grocy_api_key)

    async def quick_add(self, item: str, quantity: float = 1.0) -> str:
        from ..services import shopping_source
        from ..services.grocy import GrocyClient
        g = GrocyClient()
        lid = await g.ensure_shopping_list()
        await shopping_source.grocy_add_item(str(lid), item, quantity)
        lists = await g.get_shopping_lists()
        name = next((l.get("name") for l in lists if int(l["id"]) == lid), None)
        return name or "Shopping list"

    async def autocheck(self, item_name: str) -> None:
        from ..services.recipe_match import _tokens
        if not _tokens(item_name):
            return
        from ..services.grocy import GrocyClient
        g = GrocyClient()
        for lst in await g.get_shopping_lists():
            for row in await g.get_shopping_items(int(lst["id"])):
                if int(row.get("done") or 0):
                    continue
                text = row.get("note") or row.get("product_name") or ""
                if stocked_item_covers(item_name, text):
                    await g.toggle_shopping_item(int(row["id"]), True)


class MealieShoppingBackend(ShoppingBackend):
    """The shopping list kept in Mealie (installs that still run their
    recipe library there keep the Mealie list they actively use)."""

    name = NAME_MEALIE
    label = "Mealie shopping list"

    def enabled(self) -> bool:
        return self.configured()

    def configured(self) -> bool:
        from ..config import settings
        return bool(settings.mealie_configured())

    async def quick_add(self, item: str, quantity: float = 1.0) -> str:
        from ..services.mealie import MealieClient
        m = MealieClient()
        lists = await m.get_shopping_lists()
        if not lists:
            raise ValueError("There is no shopping list yet.")
        await m.add_shopping_item(lists[0]["id"], item, quantity)
        return lists[0].get("name") or "Shopping List"

    async def autocheck(self, item_name: str) -> None:
        from ..services.mealie import MealieClient
        from ..services.recipe_match import _tokens
        if not _tokens(item_name):
            return
        m = MealieClient()
        for lst in await m.get_shopping_lists():
            detail = await m.get_shopping_list(lst["id"])
            for si in detail.get("listItems", []):
                if si.get("checked"):
                    continue
                if stocked_item_covers(item_name, si.get("note") or ""):
                    await m.update_shopping_item(si["id"], {**si, "checked": True})


def resolve_active() -> str:
    """Which store holds the shopping list: "grocy" or "mealie".

    The seam's long-standing rule, unchanged: an explicit shopping_backend
    setting wins (any registered backend name counts). Auto (the default,
    empty): the list follows Grocy, EXCEPT on an install whose recipe
    library still runs in Mealie, which keeps the Mealie shopping list it is
    already using (existing installs are production). Deliberately
    independent of whether the chosen backend is currently reachable: an
    outage degrades the page with an honest message rather than silently
    swapping the user's list.
    """
    from ..config import settings
    from . import registry
    value = (getattr(settings, "shopping_backend", "") or "").strip().lower()
    if value in registry.names(KIND_SHOPPING):
        return value
    if (registry.active_name(registry.KIND_RECIPES) == NAME_MEALIE
            and settings.mealie_configured()):
        return NAME_MEALIE
    return NAME_GROCY


def register_builtins() -> None:
    """Register both backends and the resolver (called by the registry)."""
    from . import registry
    registry.register(GrocyShoppingBackend(), replace=True)
    registry.register(MealieShoppingBackend(), replace=True)
    registry.set_resolver(KIND_SHOPPING, resolve_active)
