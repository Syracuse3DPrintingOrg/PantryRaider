"""On-screen Start Page: a full-screen launcher that works like an on-screen
Stream Deck (Pantry Raider).

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

# The on-screen app page each Stream Deck action opens when pressed on the Start
# Page. Any deck action not listed here renders as "deckonly": the ha_1..ha_5
# slot keys still fire server-side via ui/start/fire (see start_actions), while
# the rest (paging, screen power, brightness, deck workflow) note that they need
# a connected deck. Keyed by the deck action name.
ACTION_HREF: dict[str, str] = {
    "expiring": "ui/expiring", "pending": "ui/pending", "add": "ui/add",
    "inventory": "ui/inventory", "cook": "ui/cook", "recipes": "ui/recipes",
    "mealplan": "ui/mealplan", "shopping": "ui/shopping", "shopping_count": "ui/shopping",
    "defaults": "ui/defaults", "convert": "ui/convert", "guide": "ui/kitchen-guide",
    "camera": "ui/camera", "camera_full": "ui/camera", "nutrition": "ui/nutrition",
    "audit": "ui/audit", "shop": "ui/shop", "settings": "setup",
    "weather": "ui/weather", "forecast": "ui/weather", "health": "ui/nutrition",
    "meal_today": "ui/mealplan",
    "timer_1": "ui/timers", "timer_2": "ui/timers", "timer_3": "ui/timers",
    "timer_eggs": "ui/timers", "timer_pasta": "ui/timers", "timer_rice": "ui/timers",
    "timers_view": "ui/timers",
}

# Fallback catalog used off-Pi (or when the host bridge is unreachable), matching
# the JS fallback in setup.html's _sdLoadCatalog so the editor and /ui/start show
# the same keys. On a Pi the real catalog comes from the host bridge.
FALLBACK_CATALOG: list[dict] = [
    {"name": "expiring",  "label": "Expiring", "icon": "clock-history",   "color": "#b54708", "group": "Status"},
    {"name": "pending",   "label": "Pending",  "icon": "inbox",           "color": "#1d4ed8", "group": "Status"},
    {"name": "commit",    "label": "Commit",   "icon": "check2-circle",   "color": "#15803d", "group": "Actions"},
    {"name": "add",       "label": "Add",      "icon": "plus-circle",     "color": "#b45309", "group": "Navigation"},
    {"name": "inventory", "label": "Stock",    "icon": "grid",            "color": "#0f766e", "group": "Navigation"},
    {"name": "cook",      "label": "Cook",     "icon": "fire",            "color": "#7e22ce", "group": "Navigation"},
    {"name": "recipes",   "label": "Recipes",  "icon": "journal-richtext","color": "#7e22ce", "group": "Navigation"},
    {"name": "mealplan",  "label": "Plan",     "icon": "calendar-week",   "color": "#7e22ce", "group": "Navigation"},
    {"name": "shopping",  "label": "Shop",     "icon": "cart",            "color": "#7e22ce", "group": "Navigation"},
    {"name": "defaults",  "label": "Defaults", "icon": "table",           "color": "#7e22ce", "group": "Navigation"},
    {"name": "timer_1",   "label": "Timer 1",  "icon": "stopwatch",       "color": "#0d9488", "group": "Timers"},
    {"name": "timer_2",   "label": "Timer 2",  "icon": "stopwatch",       "color": "#0d9488", "group": "Timers"},
    {"name": "timer_3",   "label": "Timer 3",  "icon": "stopwatch",       "color": "#0d9488", "group": "Timers"},
    {"name": "weather",   "label": "Weather",  "icon": "cloud-sun",       "color": "#1e40af", "group": "Weather"},
    {"name": "forecast",  "label": "Forecast", "icon": "cloud-sun",       "color": "#0e7490", "group": "Weather"},
    {"name": "ha_1",      "label": "HA 1",     "icon": "house",           "color": "#475569", "group": "Home Assistant"},
    {"name": "ha_2",      "label": "HA 2",     "icon": "house",           "color": "#475569", "group": "Home Assistant"},
    {"name": "ha_3",      "label": "HA 3",     "icon": "house",           "color": "#475569", "group": "Home Assistant"},
    {"name": "ha_4",      "label": "HA 4",     "icon": "house",           "color": "#475569", "group": "Home Assistant"},
    {"name": "ha_5",      "label": "HA 5",     "icon": "house",           "color": "#475569", "group": "Home Assistant"},
    {"name": "brightness","label": "Bright",   "icon": "brightness-high", "color": "#475569", "group": "System"},
]


async def fetch_deck_catalog() -> list[dict]:
    """The Stream Deck action catalog: the live one from the host bridge on a Pi
    appliance (identical to what the editor loads), else the static fallback. So
    /ui/start renders every key with the same face as the editor and the deck."""
    from ..hardware import is_raspberry_pi
    if is_raspberry_pi():
        try:
            import httpx
            from ..routers.setup import _HOST_BRIDGE
            async with httpx.AsyncClient(timeout=6.0) as c:
                r = (await c.get(f"{_HOST_BRIDGE}/streamdeck/actions")).json()
            if r.get("ok") and isinstance(r.get("actions"), list):
                return r["actions"]
        except Exception:
            pass
    return FALLBACK_CATALOG


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


def _norm_icon(icon: str) -> str:
    """Normalise a catalog icon to a full Bootstrap Icons class (the deck catalog
    uses bare names like 'grid'; templates expect 'bi-grid')."""
    icon = str(icon or "").strip()
    if not icon:
        return "bi-grid-1x2"
    return icon if icon.startswith("bi-") else "bi-" + icon


def resolve_layout(layout: list | None, key_count: int,
                   overrides: list | None = None,
                   catalog: list | None = None) -> list[dict]:
    """Resolve a stored layout into exactly ``key_count`` render-ready keys.

    ``catalog`` is the deck action catalog (from ``fetch_deck_catalog``); the
    face for a built-in key comes straight from it so it matches the editor and
    the deck. Each result is ``{"kind": "builtin"|"custom"|"deckonly"|"blank",
    ...}``. Tokens are the deck model: an action name, a custom key id, or
    "blank". An action with no on-screen page renders as deck-only."""
    key_count = normalize_key_count(key_count)
    customs = {c["id"]: c for c in custom_buttons(overrides)}
    cat = {a["name"]: a for a in (catalog or FALLBACK_CATALOG)
           if isinstance(a, dict) and a.get("name")}
    slots = list(layout or [])[:key_count]
    out: list[dict] = []
    for tok in slots:
        tok = str(tok or "")
        if not tok or tok == "blank":
            out.append({"kind": "blank"})
        elif tok in customs:
            c = customs[tok]
            out.append({"kind": "custom", "id": tok, "type": c["type"],
                        "label": c["label"], "icon": _norm_icon(c["icon"]), "color": c["color"]})
        elif tok in cat:
            a = cat[tok]
            href = ACTION_HREF.get(tok)
            out.append({"kind": "builtin" if href else "deckonly", "key": tok,
                        "label": a.get("label", tok), "icon": _norm_icon(a.get("icon")),
                        "color": a.get("color", "#374151"), "href": href})
        else:
            out.append({"kind": "blank"})
    while len(out) < key_count:
        out.append({"kind": "blank"})
    return out
