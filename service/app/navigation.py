"""Navigation tab registry.

The navbar is rendered from this list. Users can hide and re-order tabs in
Settings (nav_order / nav_hidden); tabs whose backing service isn't
configured are hidden automatically. Pages stay reachable by direct URL
even when their tab is hidden.
"""
from .config import settings

# hrefs are root-relative (no leading slash) so they resolve against the page's
# <base href>, which carries the HA Ingress prefix when running as an add-on.
NAV_TABS = [
    {"key": "inventory", "label": "Inventory", "icon": "bi-grid",            "href": "ui/"},
    {"key": "expiring",  "label": "Expiring",  "icon": "bi-clock-history",   "href": "ui/expiring"},
    {"key": "add",       "label": "Add Food",  "icon": "bi-plus-circle",     "href": "ui/add"},
    {"key": "pending",   "label": "Pending",   "icon": "bi-hourglass-split", "href": "ui/pending"},
    {"key": "recipes",   "label": "Recipes",   "icon": "bi-journal-richtext","href": "ui/recipes",  "requires": "mealie"},
    {"key": "cook",      "label": "Cook",      "icon": "bi-lightbulb",       "href": "ui/cook",     "requires": "mealie"},
    {"key": "mealplan",  "label": "Meal Plan", "icon": "bi-calendar-week",   "href": "ui/mealplan", "requires": "mealie"},
    {"key": "shopping",  "label": "Shopping",  "icon": "bi-cart",            "href": "ui/shopping", "requires": "mealie"},
    {"key": "defaults",  "label": "Defaults",  "icon": "bi-table",           "href": "ui/defaults"},
    {"key": "about",     "label": "About",     "icon": "bi-info-circle",     "href": "ui/about"},
]


def _requirement_met(tab: dict) -> bool:
    req = tab.get("requires")
    if req == "mealie":
        return settings.mealie_configured()
    return True


def visible_tabs() -> list[dict]:
    """Tabs to render, in the user's order, minus hidden + unconfigured."""
    tabs = {t["key"]: t for t in NAV_TABS}
    order = [k for k in settings.nav_order.split(",") if k in tabs]
    # Tabs missing from a saved order (e.g. added in an update) go at the end
    keys = order + [t["key"] for t in NAV_TABS if t["key"] not in order]
    hidden = {k for k in settings.nav_hidden.split(",") if k}
    return [tabs[k] for k in keys if k not in hidden and _requirement_met(tabs[k])]


def all_tabs() -> list[dict]:
    """Registry with current visibility state, for the Settings editor."""
    visible_keys = {t["key"] for t in visible_tabs()}
    hidden = {k for k in settings.nav_hidden.split(",") if k}
    tabs = {t["key"]: t for t in NAV_TABS}
    order = [k for k in settings.nav_order.split(",") if k in tabs]
    keys = order + [t["key"] for t in NAV_TABS if t["key"] not in order]
    return [{**tabs[k],
             "hidden": k in hidden,
             "available": _requirement_met(tabs[k]),
             "shown": k in visible_keys} for k in keys]
