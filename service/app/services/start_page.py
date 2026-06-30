"""On-screen Start Page: a full-screen launcher that works like an on-screen
Stream Deck (FoodAssistant).

The Start Page editor mirrors the Stream Deck editor exactly: the same key
catalog, the same custom-key library (shared via streamdeck_key_overrides), and
the same key style/icon options. So a Start Page layout is a flat list of the
SAME tokens the deck uses: a built-in action name (see DECK_CATALOG), a custom
key id, or "blank".

This module gives the server the catalog (labels/icons/colours, and the app page
each action opens on-screen) plus a resolver that turns a stored layout into
render-ready keys for the full-screen page. Pure helpers so the template and
tests stay simple; browser behaviour for a key press lives in the template.
"""
from __future__ import annotations

# Grid shapes per key count, mirroring the Stream Deck hardware (cols x rows).
GRID_SHAPES = {6: (3, 2), 15: (5, 3), 32: (8, 4)}
VALID_KEY_COUNTS = tuple(GRID_SHAPES.keys())
_DEFAULT_KEYS = 15

# The built-in key catalog, mirroring the Stream Deck action catalog (the JS
# fallback in setup.html and streamdeck/actions.py). Each entry carries the deck
# face (label/icon/colour, so a key looks identical to the deck) and ``href``:
# the app page the key opens when pressed on-screen, or None for a deck-only
# action (Home Assistant entity, brightness, paging) that has no on-screen page.
DECK_CATALOG: dict[str, dict] = {
    "expiring":  {"label": "Expiring", "icon": "bi-clock-history",   "color": "#b54708", "href": "ui/expiring"},
    "pending":   {"label": "Pending",  "icon": "bi-inbox",           "color": "#1d4ed8", "href": "ui/pending"},
    "commit":    {"label": "Commit",   "icon": "bi-check2-circle",   "color": "#15803d", "href": None},
    "add":       {"label": "Add",      "icon": "bi-plus-circle",     "color": "#b45309", "href": "ui/add"},
    "inventory": {"label": "Stock",    "icon": "bi-grid",            "color": "#0f766e", "href": "ui/inventory"},
    "cook":      {"label": "Cook",     "icon": "bi-fire",            "color": "#7e22ce", "href": "ui/cook"},
    "recipes":   {"label": "Recipes",  "icon": "bi-journal-richtext","color": "#7e22ce", "href": "ui/recipes"},
    "mealplan":  {"label": "Plan",     "icon": "bi-calendar-week",   "color": "#7e22ce", "href": "ui/mealplan"},
    "shopping":  {"label": "Shop",     "icon": "bi-cart",            "color": "#7e22ce", "href": "ui/shopping"},
    "defaults":  {"label": "Defaults", "icon": "bi-table",           "color": "#7e22ce", "href": "ui/defaults"},
    "audit":     {"label": "Audit",    "icon": "bi-clipboard-check", "color": "#0f766e", "href": "ui/audit"},
    "nutrition": {"label": "Nutrition","icon": "bi-heart-pulse",     "color": "#0f766e", "href": "ui/nutrition"},
    "convert":   {"label": "Convert",  "icon": "bi-rulers",          "color": "#0d9488", "href": "ui/convert"},
    "guide":     {"label": "Guide",    "icon": "bi-book",            "color": "#0d9488", "href": "ui/kitchen-guide"},
    "camera":    {"label": "Camera",   "icon": "bi-camera-video",    "color": "#0d9488", "href": "ui/camera"},
    "timer_1":   {"label": "Timer 1",  "icon": "bi-stopwatch",       "color": "#0d9488", "href": "ui/timers"},
    "timer_2":   {"label": "Timer 2",  "icon": "bi-stopwatch",       "color": "#0d9488", "href": "ui/timers"},
    "timer_3":   {"label": "Timer 3",  "icon": "bi-stopwatch",       "color": "#0d9488", "href": "ui/timers"},
    "timers_view": {"label": "Timers", "icon": "bi-stopwatch",       "color": "#0d9488", "href": "ui/timers"},
    "weather":   {"label": "Weather",  "icon": "bi-cloud-sun",       "color": "#1e40af", "href": "ui/weather"},
    "forecast":  {"label": "Forecast", "icon": "bi-cloud-sun",       "color": "#0e7490", "href": "ui/weather"},
    "shop":      {"label": "Shop",     "icon": "bi-bag",             "color": "#7e22ce", "href": "ui/shop"},
    "settings":  {"label": "Settings", "icon": "bi-gear",            "color": "#475569", "href": "setup"},
    "ha_1":      {"label": "HA 1",     "icon": "bi-house",           "color": "#475569", "href": None},
    "ha_2":      {"label": "HA 2",     "icon": "bi-house",           "color": "#475569", "href": None},
    "ha_3":      {"label": "HA 3",     "icon": "bi-house",           "color": "#475569", "href": None},
    "ha_4":      {"label": "HA 4",     "icon": "bi-house",           "color": "#475569", "href": None},
    "ha_5":      {"label": "HA 5",     "icon": "bi-house",           "color": "#475569", "href": None},
    "brightness":{"label": "Bright",   "icon": "bi-brightness-high", "color": "#475569", "href": None},
}


def normalize_key_count(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_KEYS
    return n if n in VALID_KEY_COUNTS else _DEFAULT_KEYS


_TYPE_ICONS = {
    "ha_action": "bi-toggle-on", "timer": "bi-stopwatch", "weather": "bi-cloud-sun",
    "shopping_add": "bi-cart-plus", "macro": "bi-collection-play",
    "camera": "bi-camera-video", "media": "bi-music-note-beamed",
}
_TYPE_COLORS = {
    "ha_action": "#475569", "timer": "#0d9488", "weather": "#1e40af",
    "shopping_add": "#15803d", "macro": "#6d28d9", "camera": "#0e7490",
    "media": "#7e22ce",
}


def _custom_meta(ov: dict) -> dict:
    """Face (label/icon/colour) for a custom key, matching the deck preview."""
    t = ov.get("type", "")
    label = ov.get("label")
    if not label:
        if t == "timer":
            label = f"{ov.get('minutes', 0)} min"
        elif t == "shopping_add":
            label = ov.get("item", "Add")
        else:
            label = t.replace("_", " ").title() or "Custom"
    icon = ov.get("icon") or _TYPE_ICONS.get(t, "bi-grid-1x2")
    return {"label": label, "icon": icon, "color": _TYPE_COLORS.get(t, "#374151")}


def custom_buttons(overrides: list | None = None) -> list[dict]:
    """Shared custom keys (from streamdeck_key_overrides) as render dicts."""
    from ..config import settings
    raw = overrides if overrides is not None else (settings.streamdeck_key_overrides or [])
    out, seen = [], set()
    for ov in raw:
        if not isinstance(ov, dict):
            continue
        cid = str(ov.get("id") or "").strip()
        if not cid or cid in seen:
            continue
        seen.add(cid)
        m = _custom_meta(ov)
        out.append({"id": cid, "type": ov.get("type", ""), **m})
    return out


def resolve_layout(layout: list | None, key_count: int,
                   overrides: list | None = None) -> list[dict]:
    """Resolve a stored layout into exactly ``key_count`` render-ready keys.

    Each result is ``{"kind": "builtin"|"custom"|"deckonly"|"blank", ...}`` with
    the face fields. Tokens are the same as the deck: an action name, a custom
    key id, or "blank". A deck-only action (no on-screen page) renders but is
    flagged so the page can note it needs a connected deck."""
    key_count = normalize_key_count(key_count)
    customs = {c["id"]: c for c in custom_buttons(overrides)}
    slots = list(layout or [])[:key_count]
    out: list[dict] = []
    for tok in slots:
        tok = str(tok or "")
        if not tok or tok == "blank":
            out.append({"kind": "blank"})
        elif tok in customs:
            c = customs[tok]
            out.append({"kind": "custom", "id": tok, "type": c["type"],
                        "label": c["label"], "icon": c["icon"], "color": c["color"]})
        elif tok in DECK_CATALOG:
            a = DECK_CATALOG[tok]
            kind = "builtin" if a["href"] else "deckonly"
            out.append({"kind": kind, "key": tok, "label": a["label"],
                        "icon": a["icon"], "color": a["color"], "href": a["href"]})
        else:
            out.append({"kind": "blank"})
    while len(out) < key_count:
        out.append({"kind": "blank"})
    return out


def catalog_for_editor() -> list[dict]:
    """The built-in catalog as a list (name + face + group) for the editor."""
    groups = {
        "expiring": "Status", "pending": "Status", "commit": "Actions",
        "add": "Pages", "inventory": "Pages", "cook": "Pages", "recipes": "Pages",
        "mealplan": "Pages", "shopping": "Pages", "defaults": "Pages",
        "audit": "Pages", "nutrition": "Pages", "convert": "Tools",
        "guide": "Tools", "camera": "Tools", "shop": "Tools", "settings": "Tools",
        "weather": "Weather", "forecast": "Weather",
        "timer_1": "Timers", "timer_2": "Timers", "timer_3": "Timers", "timers_view": "Timers",
        "ha_1": "Home Assistant", "ha_2": "Home Assistant", "ha_3": "Home Assistant",
        "ha_4": "Home Assistant", "ha_5": "Home Assistant", "brightness": "System",
    }
    return [{"name": k, "label": v["label"], "icon": v["icon"],
             "color": v["color"], "group": groups.get(k, "Other")}
            for k, v in DECK_CATALOG.items()]
