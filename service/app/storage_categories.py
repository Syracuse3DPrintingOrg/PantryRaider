"""Storage-category registry.

A single source of truth for the inventory dashboard buckets, the move-item
targets, and the Grocy-location classifier. There are four built-in categories
plus any number of user-defined custom ones (e.g. "Wine Cellar", "Garage
Fridge") stored in settings.json under `custom_storage_categories`.

Each category is a dict with:
  key       dashboard bucket id (slug; stable identifier)
  label     display name shown on the panel header / move button
  icon      Bootstrap-icon class, e.g. "bi-snow"
  color     accent/text colour (hex)
  bg        panel-header background colour (hex)
  location  Grocy location name items in this bucket live in
  match     case-insensitive substrings used to classify a Grocy location
            name into this bucket
  custom    False for built-ins, True for user-defined
"""
import re

from .config import settings

# Built-in categories. Order is the dashboard display order. `match` keywords
# are mutually exclusive across the built-ins, so their relative order is safe.
BUILTIN_CATEGORIES = [
    {"key": "refrigerated", "label": "Refrigerated", "icon": "bi-thermometer-low",
     "color": "#74c0fc", "bg": "#0d3b5e", "location": "Refrigerator",
     "match": ["refriger", "fridge"]},
    {"key": "frozen", "label": "Frozen", "icon": "bi-snow",
     "color": "#a5b4fc", "bg": "#1a1a4d", "location": "Freezer",
     "match": ["freezer", "frozen"]},
    {"key": "room_temp", "label": "Room Temp / Counter", "icon": "bi-sun",
     "color": "#ffa94d", "bg": "#3b2100", "location": "Counter / Room Temp",
     "match": ["counter", "room"]},
    {"key": "pantry", "label": "Pantry / Dry Storage", "icon": "bi-box-seam",
     "color": "#69db7c", "bg": "#1a2e10", "location": "Pantry / Dry Storage",
     "match": ["pantry", "dry"]},
]

# Catch-all bucket for stock in Grocy locations that match no category.
OTHER = {"key": "other", "label": "Other / Unsorted", "icon": "bi-box",
         "color": "#adb5bd", "bg": "#2a2a33", "location": "", "match": []}

_BUILTIN_KEYS = {c["key"] for c in BUILTIN_CATEGORIES} | {"other"}
_DEFAULT_COLOR = "#adb5bd"
_DEFAULT_BG = "#2a2a33"
_DEFAULT_ICON = "bi-box"
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(name: str) -> str:
    return _SLUG_RE.sub("_", name.strip().lower()).strip("_")


def _normalize_custom(raw) -> list[dict]:
    """Coerce stored custom-category entries into full category dicts.

    Tolerant of partial/hand-edited input: a bare {"label": "Wine Cellar"}
    becomes a usable category. Entries with no label, a blank key, or a key
    that collides with a built-in or an earlier custom one are skipped so a
    bad row can never shadow a built-in or crash the dashboard.
    """
    out: list[dict] = []
    seen = set(_BUILTIN_KEYS)
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or entry.get("name") or "").strip()
        if not label:
            continue
        key = str(entry.get("key") or _slug(label)).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        location = str(entry.get("location") or label).strip()
        match = entry.get("match")
        if isinstance(match, str):
            match = [m.strip() for m in match.split(",")]
        match = [str(m).strip().lower() for m in (match or []) if str(m).strip()]
        if not match:
            match = [location.lower()]
        out.append({
            "key": key,
            "label": label,
            "icon": str(entry.get("icon") or _DEFAULT_ICON).strip(),
            "color": str(entry.get("color") or _DEFAULT_COLOR).strip(),
            "bg": str(entry.get("bg") or _DEFAULT_BG).strip(),
            "location": location,
            "match": match,
            "custom": True,
        })
    return out


def custom_categories() -> list[dict]:
    """User-defined categories from settings (validated/normalized)."""
    return _normalize_custom(getattr(settings, "custom_storage_categories", None))


def all_categories() -> list[dict]:
    """Built-ins (display order) followed by valid custom categories."""
    builtins = [{**c, "custom": False} for c in BUILTIN_CATEGORIES]
    return builtins + custom_categories()


def category_keys() -> list[str]:
    """Bucket keys for the dashboard, in display order (no 'other')."""
    return [c["key"] for c in all_categories()]


def storable(cat: dict) -> dict:
    """The user-facing subset of a category to persist in settings.json."""
    return {k: cat[k] for k in ("key", "label", "icon", "color", "bg", "location", "match")}


def location_for(key: str) -> str | None:
    """Grocy location name for a bucket key, or None if unknown."""
    for c in all_categories():
        if c["key"] == key:
            return c["location"]
    return None


def classify_location(loc_name: str) -> str:
    """Map a Grocy location name to a bucket key, falling back to 'other'.

    An exact location-name match wins first (so each category reliably owns
    its own location). Otherwise keyword substrings decide, with custom
    categories checked before built-ins so a custom "Garage Fridge" can claim
    a location the built-in "refrigerated" keywords would otherwise grab.
    """
    low = (loc_name or "").strip().lower()
    if not low:
        return "other"
    cats = all_categories()
    for c in cats:
        if c["location"].lower() == low:
            return c["key"]
    for c in sorted(cats, key=lambda c: not c["custom"]):
        if any(kw in low for kw in c["match"]):
            return c["key"]
    return "other"
