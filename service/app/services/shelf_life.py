"""LLM shelf-life and storage estimate (FoodAssistant-ft92).

When an AI provider is already reading an item on the way in (a barcode scan, a
food photo, or a receipt), it can also estimate how long the item keeps and
where to store it, which is far better than the generic category rule. The
generic rule would, for example, file a refrigerated Japanese cheesecake as a
room-temperature pantry item.

The mapping from a model's free-form answer to a clean {days, location} pair
lives here as a PURE function, deliberately separate from the network call, so
it is trivial to unit-test. Callers fetch the raw JSON from a provider (or the
provider errors and they skip this entirely) and pass it to
``parse_llm_shelf_life``; ``apply_shelf_life`` then writes the result onto a
FoodItem, overriding the generic default.
"""
from datetime import date, timedelta

from ..models.food import FoodItem, StorageType

# Believable shelf-life bounds. A window under a day or past ten years is
# treated as no usable answer, so a garbled number never sets an absurd date.
_MIN_DAYS = 1
_MAX_DAYS = 3650  # ~10 years

# How a model might phrase each storage bucket. Frozen is checked before
# refrigerated so "keep frozen" is not caught by a cold/chill keyword, and the
# buckets map onto the app's real StorageType values (which import_item turns
# into Grocy locations). A present but unrecognized location falls back to the
# pantry bucket, the safest generic home for a shelf-stable item.
_LOCATION_SYNONYMS = [
    (StorageType.frozen, ("freezer", "frozen", "deep freeze")),
    (StorageType.refrigerated, ("refrigerat", "fridge", "chill", "cold", "cooler")),
    (StorageType.room_temp, ("counter", "room temp", "room-temp", "room temperature")),
    (StorageType.dry, ("pantry", "dry", "cupboard", "shelf", "cool dark")),
]


def _coerce_days(value) -> int | None:
    """A believable positive day count, or None. Clamps to the sane range."""
    try:
        days = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    if days < _MIN_DAYS:
        return None
    return min(days, _MAX_DAYS)


def _coerce_location(value) -> str | None:
    """Map a free-form storage phrase to a StorageType value, or None if empty.

    A non-empty phrase that matches no bucket keyword falls back to the pantry
    (dry) bucket rather than None, so an odd answer still lands somewhere sane.
    """
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    for bucket, needles in _LOCATION_SYNONYMS:
        if any(n in text for n in needles):
            return bucket.value
    return StorageType.dry.value


def parse_llm_shelf_life(raw) -> dict:
    """Map an LLM reply to ``{"days": int|None, "location": str|None}``. Pure.

    Tolerant of the several field names a model might use and of missing or
    garbled values: an unusable answer yields None for that field so the caller
    falls back to the generic category default. ``location`` is a StorageType
    value ("refrigerated" | "frozen" | "room_temp" | "dry").
    """
    if not isinstance(raw, dict):
        return {"days": None, "location": None}

    days = None
    for key in ("best_before_days", "shelf_life_days", "days", "shelf_life"):
        if raw.get(key) is not None:
            days = _coerce_days(raw.get(key))
            if days is not None:
                break

    location = None
    for key in ("storage_location", "storage_type", "location", "storage"):
        if raw.get(key) is not None:
            location = _coerce_location(raw.get(key))
            if location is not None:
                break

    return {"days": days, "location": location}


def apply_shelf_life(item: FoodItem, parsed: dict) -> bool:
    """Write a parsed shelf-life/location onto a FoodItem, overriding defaults.

    Sets the storage bucket and the best-before date from the LLM answer when
    each is present. Returns True when a best-before date was set (so the caller
    can skip the generic default), False when the answer had no usable window
    (the caller then falls back to the category rule). Pure aside from mutating
    ``item``.
    """
    location = parsed.get("location")
    if location:
        try:
            item.storage_type = StorageType(location)
        except ValueError:
            pass
    days = parsed.get("days")
    if days:
        item.best_by_date = date.today() + timedelta(days=days)
        return True
    return False
