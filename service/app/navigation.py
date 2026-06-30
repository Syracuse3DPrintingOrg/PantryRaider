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
    {"key": "pending",   "label": "Inbox",     "icon": "bi-inbox",          "href": "ui/pending"},
    {"key": "audit",     "label": "Audit",     "icon": "bi-clipboard-check", "href": "ui/audit"},
    {"key": "recipes",   "label": "Recipes",   "icon": "bi-journal-richtext","href": "ui/recipes",  "requires": "mealie"},
    {"key": "cook",      "label": "Cook",      "icon": "bi-fire",            "href": "ui/cook",     "requires": "mealie"},
    {"key": "current_recipe", "label": "On the Line", "icon": "bi-fire", "href": "ui/current-recipe", "requires": "mealie"},
    {"key": "mealplan",  "label": "Meal Plan", "icon": "bi-calendar-week",   "href": "ui/mealplan", "requires": "mealie"},
    {"key": "shopping",  "label": "Shopping",  "icon": "bi-cart",            "href": "ui/shopping"},
    {"key": "nutrition", "label": "Nutrition", "icon": "bi-heart-pulse",     "href": "ui/nutrition"},
    {"key": "camera",    "label": "Camera",    "icon": "bi-camera-video",    "href": "ui/camera",   "requires": "cameras"},
    {"key": "convert",   "label": "Convert",   "icon": "bi-rulers",          "href": "ui/convert"},
    {"key": "guide",     "label": "Kitchen Guide", "icon": "bi-book",        "href": "ui/kitchen-guide"},
    {"key": "defaults",  "label": "Defaults",  "icon": "bi-table",           "href": "ui/defaults"},
    {"key": "about",     "label": "About",     "icon": "bi-info-circle",     "href": "ui/about"},
]

# Requirements backed by a configurable service that gets its own "unlock" hint
# in the navbar when missing (see auto_hidden_groups). A requirement outside this
# set (for example "cameras", which is configured in Interface, not a service)
# simply hides its tab when unmet, with no lock badge.
_SERVICE_REQUIREMENTS = {"mealie"}


def _requirement_met(tab: dict) -> bool:
    req = tab.get("requires")
    if req == "mealie":
        return settings.mealie_configured()
    if req == "cameras":
        return bool(settings.streamdeck_cameras)
    return True


def visible_tabs() -> list[dict]:
    """Tabs to render, in the user's order, minus hidden + unconfigured."""
    tabs = {t["key"]: t for t in NAV_TABS}
    order = [k for k in settings.nav_order.split(",") if k in tabs]
    # Tabs missing from a saved order (e.g. added in an update) go at the end
    keys = order + [t["key"] for t in NAV_TABS if t["key"] not in order]
    hidden = {k for k in settings.nav_hidden.split(",") if k}
    return [tabs[k] for k in keys if k not in hidden and _requirement_met(tabs[k])]


def auto_hidden_groups() -> list[dict]:
    """Return services whose tabs are auto-hidden (requirement not met, not user-hidden).

    Each entry has: service, label, tab_labels (list), setup_href.
    Used to render "unlock" hints in the navbar when features are unavailable.
    """
    user_hidden = {k for k in settings.nav_hidden.split(",") if k}
    groups: dict[str, dict] = {}
    for t in NAV_TABS:
        req = t.get("requires")
        if req not in _SERVICE_REQUIREMENTS or t["key"] in user_hidden:
            continue
        if not _requirement_met(t):
            if req not in groups:
                _pane = {"mealie": "pane-recipes"}.get(req, req)
                groups[req] = {"service": req, "tab_labels": [], "setup_href": f"setup#{_pane}"}
            groups[req]["tab_labels"].append(t["label"])
    return list(groups.values())


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
