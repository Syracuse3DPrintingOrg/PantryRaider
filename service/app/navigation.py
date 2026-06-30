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

# Default submenu nesting for a fresh install (FoodAssistant-dprh). This is the
# baseline child-key to parent-key map: a logical grouping so a new menu is tidy
# instead of one long flat row. The primary daily-use tabs (Inventory, Add,
# Inbox, Shopping, Recipes) stay at the top level; secondary views nest under a
# visible top-level parent whose own page is still reachable as the first item of
# its dropdown.
#
#   Inventory  -> Expiring, Audit        (stock views)
#   Recipes    -> Cook, On the Line, Meal Plan  (cooking workflow)
#   Kitchen Guide -> Convert, Nutrition, Camera (reference and tools)
#
# A saved nav_parents map (set the moment the user touches the nav editor) wins
# wholesale over this baseline, so a user's arrangement is never overridden. See
# effective_nav_parents().
DEFAULT_NAV_PARENTS = {
    "expiring": "inventory",
    "audit": "inventory",
    "cook": "recipes",
    "current_recipe": "recipes",
    "mealplan": "recipes",
    "convert": "guide",
    "nutrition": "guide",
    "camera": "guide",
}


def effective_nav_parents() -> dict:
    """The parent map to apply: the user's saved nav_parents if they have set
    any, otherwise the DEFAULT_NAV_PARENTS baseline.

    A fresh install (and any install where the nav editor was never saved) has an
    empty nav_parents, so it inherits the default grouping. As soon as the user
    saves nesting choices, that non-empty map replaces the default entirely, so a
    deliberate flat arrangement or a custom grouping is preserved.
    """
    saved = getattr(settings, "nav_parents", {}) or {}
    if not isinstance(saved, dict):
        saved = {}
    return saved if saved else dict(DEFAULT_NAV_PARENTS)


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


# Custom-tab id prefix. User-created tabs always carry this so their ids can
# never collide with a built-in key, which keeps nav_order / nav_hidden /
# nav_parents lookups unambiguous.
CUSTOM_PREFIX = "custom_"

# Bootstrap icon used when a custom tab has no icon set, so the row always
# renders something rather than an empty glyph slot.
_DEFAULT_CUSTOM_ICON = "bi-link-45deg"


def custom_nav_tabs() -> list[dict]:
    """Validated, normalized custom tabs from settings.

    Each returned tab is {key, label, icon, href, parent, custom: True}. Invalid
    entries (missing label or url, non-dict) are dropped. Pure apart from reading
    settings; the heavy lifting is in normalize_custom_tabs so it stays testable.
    """
    return normalize_custom_tabs(getattr(settings, "custom_nav_tabs", []) or [])


def normalize_custom_tabs(raw) -> list[dict]:
    """Coerce stored custom-tab dicts into render-ready tabs.

    Drops anything without a usable label and url. Assigns a stable, prefixed,
    de-duplicated key (from the stored id, else the label), so two custom tabs
    never share a key. No settings access, so this is unit-testable in isolation.
    """
    out: list[dict] = []
    seen: set[str] = set()
    if not isinstance(raw, list):
        return out
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label", "")).strip()
        url = str(entry.get("url", "")).strip()
        if not label or not url:
            continue
        key = _custom_key(entry.get("id") or label, seen)
        seen.add(key)
        icon = str(entry.get("icon", "")).strip() or _DEFAULT_CUSTOM_ICON
        parent = str(entry.get("parent", "")).strip()
        out.append({"key": key, "label": label, "icon": icon,
                    "href": url, "parent": parent, "custom": True})
    return out


def _custom_key(raw_id: str, seen: set[str]) -> str:
    base = "".join(c if c.isalnum() else "_" for c in str(raw_id).lower()).strip("_")
    if not base:
        base = "tab"
    if not base.startswith(CUSTOM_PREFIX):
        base = CUSTOM_PREFIX + base
    key = base
    n = 2
    while key in seen:
        key = f"{base}_{n}"
        n += 1
    return key


def _ordered_visible() -> list[dict]:
    """Built-in + custom tabs the user should see, in saved order, minus hidden
    and unconfigured. Flat: nesting is applied later by build_nav_tree."""
    builtins = {t["key"]: t for t in NAV_TABS}
    customs = {t["key"]: t for t in custom_nav_tabs()}
    everything = {**builtins, **customs}
    order = [k for k in settings.nav_order.split(",") if k in everything]
    # Tabs missing from a saved order (added in an update, or freshly created)
    # go at the end in their registration order: built-ins first, then customs.
    tail = [k for k in builtins if k not in order] + [k for k in customs if k not in order]
    keys = order + tail
    hidden = {k for k in settings.nav_hidden.split(",") if k}
    return [everything[k] for k in keys
            if k not in hidden and _requirement_met(everything[k])]


def visible_tabs() -> list[dict]:
    """Flat list of tabs to render (built-in + custom), in the user's order,
    minus hidden + unconfigured. Kept flat for callers that want every reachable
    tab (the floating nav, the overflow menu); build_nav_tree adds nesting."""
    return _ordered_visible()


def _parent_for(tab: dict, parents: dict) -> str:
    """Resolve a tab's parent key. Custom tabs carry their own parent field;
    built-ins look theirs up in the nav_parents map."""
    if tab.get("custom"):
        return tab.get("parent", "")
    return str(parents.get(tab["key"], "")).strip()


def build_nav_tree(tabs: list[dict] | None = None, parents: dict | None = None) -> list[dict]:
    """Resolve a flat tab list into a nested nav tree.

    Each top-level entry gains a "children" list (empty for a plain link). A tab
    whose parent points at another VISIBLE top-level tab is nested under it; a
    parent reference that is missing, hidden, or itself nested is ignored so the
    child falls back to a top-level link rather than vanishing. Order follows the
    incoming flat order at each level. Pure: pass tabs/parents to test it.
    """
    if tabs is None:
        tabs = _ordered_visible()
    if parents is None:
        parents = effective_nav_parents()
    if not isinstance(parents, dict):
        parents = {}
    by_key = {t["key"]: t for t in tabs}

    def resolved_parent(tab: dict) -> str:
        p = _parent_for(tab, parents)
        # Only nest under a parent that is itself visible and top-level (one
        # level deep); otherwise treat the tab as top-level.
        if p and p in by_key and not _parent_for(by_key[p], parents):
            return p
        return ""

    nodes = {t["key"]: {**t, "children": []} for t in tabs}
    tree: list[dict] = []
    for t in tabs:
        p = resolved_parent(t)
        if p:
            nodes[p]["children"].append(nodes[t["key"]])
        else:
            tree.append(nodes[t["key"]])
    return tree


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
    """Registry with current visibility + nesting state, for the Settings editor.

    Lists every built-in tab plus the user's custom tabs in the saved order, each
    flagged with hidden / available / shown / custom and its resolved parent key
    so the editor can render the reorder, hide, and submenu controls.
    """
    visible_keys = {t["key"] for t in visible_tabs()}
    hidden = {k for k in settings.nav_hidden.split(",") if k}
    parents = effective_nav_parents()
    builtins = {t["key"]: t for t in NAV_TABS}
    customs = {t["key"]: t for t in custom_nav_tabs()}
    everything = {**builtins, **customs}
    order = [k for k in settings.nav_order.split(",") if k in everything]
    tail = [k for k in builtins if k not in order] + [k for k in customs if k not in order]
    keys = order + tail
    rows = []
    for k in keys:
        t = everything[k]
        rows.append({**t,
                     "hidden": k in hidden,
                     "available": _requirement_met(t),
                     "shown": k in visible_keys,
                     "custom": bool(t.get("custom")),
                     "parent": _parent_for(t, parents)})
    return rows
