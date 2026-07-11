"""Navigation tab registry.

The navbar is rendered from this list. Users can hide and re-order tabs in
Settings (nav_order / nav_hidden); tabs whose backing service isn't
configured are hidden automatically. Pages stay reachable by direct URL
even when their tab is hidden.
"""
from .config import settings

# hrefs are root-relative (no leading slash) so they resolve against the page's
# <base href>, which carries the HA Ingress prefix when running as an add-on.
# The nav is two levels (FoodAssistant-gg33): a short list of top-level pages,
# each of which lands on a primary page and shows an in-header sub-pill to switch
# to its sub-pages (rendered from build_nav_tree children in base.html). Some
# top-levels are real pages (Inventory, Cook, Kitchen Guide): their own page is
# the first sub-pill. Others are pure headings (Time & Temp, Home Hub): they have
# no own page and land on their first child; the sub-pill is just the children.
# Registration order is the default nav order (Glance first = home).
NAV_TABS = [
    # Glance: the app home. It is the Start page in "glance" mode (auto grid of
    # top-level buttons + notification pills); the same tab in custom mode is the
    # arrangeable launcher. Gated by start_page (on by default now).
    {"key": "start",     "label": "Glance",    "icon": "bi-grid-3x3-gap",    "href": "ui/start", "requires": "start_page"},
    # Inventory (top) -> Inventory, Expiring. Audit is intentionally not a nav
    # tab: it is reached from Manage. The /ui/audit page still works by URL.
    {"key": "inventory", "label": "Inventory", "icon": "bi-grid",            "href": "ui/"},
    {"key": "expiring",  "label": "Expiring",  "icon": "bi-clock-history",   "href": "ui/expiring"},
    {"key": "add",       "label": "Manage",    "icon": "bi-plus-circle",     "href": "ui/add"},
    {"key": "pending",   "label": "Review",    "icon": "bi-inbox",           "href": "ui/pending"},
    # Cook (top) -> Cook, Recipes, On the Line, Meal Plan. Built in, so no
    # service requirement (works with or without Mealie).
    {"key": "cook",      "label": "Cook",      "icon": "bi-fire",            "href": "ui/cook"},
    {"key": "recipes",   "label": "Recipes",   "icon": "bi-journal-richtext","href": "ui/recipes"},
    {"key": "current_recipe", "label": "On the Line", "icon": "bi-fire",     "href": "ui/current-recipe"},
    {"key": "mealplan",  "label": "Meal Plan", "icon": "bi-calendar-week",   "href": "ui/mealplan"},
    {"key": "shopping",  "label": "Shopping",  "icon": "bi-cart",            "href": "ui/shopping"},
    # Kitchen Guide (top) -> Kitchen Guide, Nutrition, Convert, Shop.
    {"key": "guide",     "label": "Kitchen Guide", "icon": "bi-book",        "href": "ui/kitchen-guide"},
    {"key": "nutrition", "label": "Nutrition", "icon": "bi-heart-pulse",     "href": "ui/nutrition"},
    {"key": "convert",   "label": "Convert",   "icon": "bi-rulers",          "href": "ui/convert"},
    {"key": "shop",      "label": "Shop",      "icon": "bi-bag",             "href": "ui/shop"},
    # Time & Temp (top, heading) -> the /ui/timers page in three views. The
    # heading lands on its first child; the sub-pill switches the view via ?view=.
    {"key": "timetemp",  "label": "Time & Temp", "icon": "bi-thermometer-half", "heading": True},
    {"key": "tt_both",   "label": "Timers and Thermometers", "icon": "bi-stopwatch", "href": "ui/timers"},
    {"key": "tt_timers", "label": "Timers",    "icon": "bi-stopwatch",       "href": "ui/timers?view=timers"},
    {"key": "tt_thermo", "label": "Thermometers", "icon": "bi-thermometer-half", "href": "ui/timers?view=thermometers"},
    # Home Hub (top, heading) -> Weather, Cameras.
    {"key": "homehub",   "label": "Home Hub",  "icon": "bi-house",           "heading": True},
    {"key": "weather",   "label": "Weather",   "icon": "bi-cloud-sun",       "href": "ui/weather"},
    {"key": "camera",    "label": "Cameras",   "icon": "bi-camera-video",    "href": "ui/camera",   "requires": "cameras"},
    # Reference pages: never in the primary row, only in the More menu.
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
    "recipes": "cook",
    "current_recipe": "cook",
    "mealplan": "cook",
    "nutrition": "guide",
    "convert": "guide",
    "shop": "guide",
    "tt_both": "timetemp",
    "tt_timers": "timetemp",
    "tt_thermo": "timetemp",
    "weather": "homehub",
    "camera": "homehub",
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
# simply hides its tab when unmet, with no lock badge. Empty since the recipe
# library moved in-house; the set (and auto_hidden_groups) stays for the next
# service-gated tab.
_SERVICE_REQUIREMENTS: set = set()


def _requirement_met(tab: dict) -> bool:
    req = tab.get("requires")
    if req == "cameras":
        return bool(settings.streamdeck_cameras)
    if req == "start_page":
        # The Start tab appears in the nav only when the Start Page is enabled.
        return bool(getattr(settings, "start_page_enabled", False))
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

    Drops anything without a usable label. A normal tab needs a url; a heading
    (folder) is allowed to have no url and is treated as a pure parent (it gets
    href="" and heading=True). Assigns a stable, prefixed, de-duplicated key
    (from the stored id, else the label), so two custom tabs never share a key.
    No settings access, so this is unit-testable in isolation.
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
        heading = bool(entry.get("heading")) or not url
        if not label:
            continue
        # A non-heading entry must carry a url; a heading is a label-only folder.
        if not heading and not url:
            continue
        key = _custom_key(entry.get("id") or label, seen)
        seen.add(key)
        icon = str(entry.get("icon", "")).strip() or _DEFAULT_CUSTOM_ICON
        parent = str(entry.get("parent", "")).strip()
        out.append({"key": key, "label": label, "icon": icon,
                    "href": "" if heading else url, "parent": parent,
                    "custom": True, "heading": heading})
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


def _start_first(keys: list[str]) -> list[str]:
    """Default the Start tab to the top of the nav when the Start Page is enabled
    and the user has not set their own order (Pantry Raider). Once the user saves
    a nav_order, that wins and this no-ops."""
    if (getattr(settings, "start_page_enabled", False)
            and not (settings.nav_order or "").strip()
            and "start" in keys):
        return ["start"] + [k for k in keys if k != "start"]
    return keys


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
    keys = _start_first(order + tail)
    hidden = {k for k in settings.nav_hidden.split(",") if k}
    return [everything[k] for k in keys
            if k not in hidden and _requirement_met(everything[k])]


def first_visible_href() -> str:
    """Href of the first page in the nav menu (after ordering, nesting, and the
    Start-first default). Used so visiting /ui shows whatever page leads the menu
    rather than a hardcoded one (Pantry Raider)."""
    tree = build_nav_tree()
    for node in tree:
        # A heading/folder has no page of its own; use its first child's href.
        if node.get("href"):
            return node["href"]
        for child in node.get("children", []):
            if child.get("href"):
                return child["href"]
    tabs = _ordered_visible()
    return tabs[0]["href"] if tabs else "ui/"


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
    # A heading (folder) has no page of its own, so a heading with no children is
    # a dead end: drop it from the rendered tree. Its pages are reachable because
    # any orphaned child already fell back to top level above.
    return [n for n in tree if not (n.get("heading") and not n["children"])]


# Keys never shown as a Glance home button (FoodAssistant-gg33): the Glance tab
# itself (you are already on it) and the reference pages (About, Defaults) that
# belong in the More menu, not on the home grid.
_GLANCE_EXCLUDE = {"start", "about", "defaults"}


def glance_pages() -> list[dict]:
    """Top-level nav pages for the Start Page Glance grid, one large square
    button each, in nav order (FoodAssistant-gg33).

    Built from build_nav_tree() so it tracks the user's ordering, hiding, and
    nesting. A heading node (Time & Temp, Home Hub) has no page of its own, so
    it lands on its first child's href, the same rule first_visible_href() uses.
    Excludes the Glance tab itself and the reference pages. Each entry is
    {key, label, icon, href}."""
    out: list[dict] = []
    for node in build_nav_tree():
        if node.get("key") in _GLANCE_EXCLUDE:
            continue
        href = node.get("href")
        if not href:
            for child in node.get("children", []):
                if child.get("href"):
                    href = child["href"]
                    break
        if not href:
            continue
        out.append({"key": node["key"], "label": node["label"],
                    "icon": node["icon"], "href": href})
    return out


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
                _pane = {"mealie": "pane-connections"}.get(req, req)
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


def default_tabs() -> list[dict]:
    """The pristine built-in tab layout, ignoring any saved customization.

    Registry order, the default folder grouping (DEFAULT_NAV_PARENTS), nothing
    hidden, and no user-added custom tabs. Used by the Settings nav editor's
    "Reset to defaults" control so it can rebuild from a known baseline rather
    than the current (possibly broken) saved arrangement.
    """
    rows = []
    for t in NAV_TABS:
        rows.append({**t,
                     "hidden": False,
                     "available": _requirement_met(t),
                     "shown": _requirement_met(t),
                     "custom": False,
                     "heading": bool(t.get("heading")),
                     "parent": DEFAULT_NAV_PARENTS.get(t["key"], "")})
    return rows
