from sqlalchemy.orm import Session
from datetime import date, timedelta
from ..models.db_models import ExpiryDefault
from ..models.food import FoodItem, StorageType

_SEED_DEFAULTS = [
    # Poultry
    ("Poultry", "chicken", "refrigerated", 5, "raw"),
    ("Poultry", "chicken", "frozen", 365, None),
    ("Poultry", "turkey", "refrigerated", 4, "raw whole or parts"),
    ("Poultry", "turkey", "frozen", 365, None),
    ("Poultry", "ground turkey", "refrigerated", 2, None),
    ("Poultry", "ground turkey", "frozen", 180, None),
    ("Poultry", "duck", "refrigerated", 4, None),
    # Meat
    ("Meat", "ground beef", "refrigerated", 3, None),
    ("Meat", "ground beef", "frozen", 120, None),
    ("Meat", "steak", "refrigerated", 5, None),
    ("Meat", "steak", "frozen", 365, None),
    ("Meat", "pork chop", "refrigerated", 5, None),
    ("Meat", "pork chop", "frozen", 180, None),
    ("Meat", "sausage", "refrigerated", 7, "unopened"),
    ("Meat", "sausage", "frozen", 180, None),
    ("Meat", "bacon", "refrigerated", 7, "unopened"),
    ("Meat", "bacon", "frozen", 30, None),
    ("Meat", "ham", "refrigerated", 7, "whole or sliced"),
    ("Meat", "ham", "frozen", 60, None),
    ("Meat", "roast", "refrigerated", 5, None),
    ("Meat", "roast", "frozen", 365, None),
    ("Meat", "hot dog", "refrigerated", 14, "unopened"),
    ("Meat", "deli meat", "refrigerated", 5, "opened"),
    # Seafood
    ("Seafood", "fish", "refrigerated", 2, "raw"),
    ("Seafood", "fish", "frozen", 180, None),
    ("Seafood", "shrimp", "refrigerated", 2, "raw"),
    ("Seafood", "shrimp", "frozen", 180, None),
    ("Seafood", "salmon", "refrigerated", 2, "raw"),
    ("Seafood", "salmon", "frozen", 180, None),
    ("Seafood", "crab", "refrigerated", 3, "cooked"),
    ("Seafood", "lobster", "refrigerated", 3, "cooked"),
    # Dairy
    ("Dairy", "milk", "refrigerated", 10, None),
    ("Dairy", "cheese", "refrigerated", 21, "hard, unopened"),
    ("Dairy", "cheese", "frozen", 180, "shredded or sliced"),
    ("Dairy", "cream cheese", "refrigerated", 14, "unopened"),
    ("Dairy", "sour cream", "refrigerated", 21, "unopened"),
    ("Dairy", "yogurt", "refrigerated", 14, "unopened"),
    ("Dairy", "butter", "refrigerated", 30, None),
    ("Dairy", "butter", "frozen", 365, None),
    ("Dairy", "eggs", "refrigerated", 35, "in shell"),
    ("Dairy", "heavy cream", "refrigerated", 14, "unopened"),
    ("Dairy", "cottage cheese", "refrigerated", 10, "unopened"),
    # Produce
    ("Produce", "leafy greens", "refrigerated", 7, "unwashed"),
    ("Produce", "spinach", "refrigerated", 7, None),
    ("Produce", "lettuce", "refrigerated", 10, "whole head"),
    ("Produce", "broccoli", "refrigerated", 7, None),
    ("Produce", "cauliflower", "refrigerated", 7, None),
    ("Produce", "carrots", "refrigerated", 21, "whole"),
    ("Produce", "celery", "refrigerated", 14, None),
    ("Produce", "bell pepper", "refrigerated", 14, None),
    ("Produce", "cucumber", "refrigerated", 7, None),
    ("Produce", "tomato", "room_temp", 7, "ripen on counter"),
    ("Produce", "avocado", "room_temp", 4, "ripe"),
    ("Produce", "banana", "room_temp", 5, None),
    ("Produce", "apple", "refrigerated", 42, None),
    ("Produce", "strawberry", "refrigerated", 5, None),
    ("Produce", "blueberry", "refrigerated", 10, None),
    ("Produce", "grapes", "refrigerated", 14, None),
    ("Produce", "potato", "room_temp", 30, "cool dark place"),
    ("Produce", "onion", "room_temp", 30, "cool dark place"),
    ("Produce", "garlic", "room_temp", 90, "whole head"),
    ("Produce", "mushroom", "refrigerated", 7, None),
    # Grains / Dry
    ("Grains", "bread", "room_temp", 7, "store-bought"),
    ("Grains", "pasta", "dry", 730, "dry uncooked"),
    ("Grains", "rice", "dry", 1825, "dry uncooked"),
    ("Grains", "flour", "dry", 365, None),
    ("Grains", "oats", "dry", 730, None),
    # Condiments
    ("Condiments", "ketchup", "refrigerated", 180, "opened"),
    ("Condiments", "mustard", "refrigerated", 365, "opened"),
    ("Condiments", "mayonnaise", "refrigerated", 60, "opened"),
    ("Condiments", "salsa", "refrigerated", 14, "opened"),
    ("Condiments", "hot sauce", "refrigerated", 180, "opened"),
    ("Condiments", "soy sauce", "dry", 730, "unopened"),
    # Canned / Packaged
    ("Canned", "canned goods", "dry", 1095, "unopened"),
    ("Canned", "canned beans", "dry", 1095, "unopened"),
    ("Canned", "canned soup", "dry", 1095, "unopened"),
    # Beverages
    ("Beverages", "soda", "room_temp", 270, "unopened"),
    ("Beverages", "sparkling water", "room_temp", 365, None),
    ("Beverages", "juice", "refrigerated", 10, "fresh, unopened"),
    ("Beverages", "juice", "room_temp", 270, "shelf-stable, unopened"),
    ("Beverages", "coffee", "dry", 365, "ground or beans"),
    ("Beverages", "tea", "dry", 730, None),
    # Snacks
    ("Snacks", "chips", "room_temp", 75, "unopened"),
    ("Snacks", "crackers", "room_temp", 180, "unopened"),
    ("Snacks", "cookie", "room_temp", 60, "packaged"),
    ("Snacks", "granola bar", "room_temp", 240, None),
    ("Snacks", "popcorn", "room_temp", 240, "unpopped"),
    ("Snacks", "chocolate", "room_temp", 365, None),
    ("Snacks", "nuts", "room_temp", 180, "unopened"),
    # Frozen packaged
    ("Frozen", "ice cream", "frozen", 120, None),
    ("Frozen", "frozen pizza", "frozen", 180, None),
    ("Frozen", "frozen vegetables", "frozen", 240, None),
    ("Frozen", "frozen fruit", "frozen", 240, None),
    ("Frozen", "frozen meal", "frozen", 120, None),
    # More pantry staples
    ("Grains", "cereal", "room_temp", 240, "unopened"),
    ("Grains", "tortilla", "room_temp", 30, None),
    ("Condiments", "peanut butter", "dry", 270, "unopened"),
    ("Condiments", "jam", "refrigerated", 180, "opened"),
    ("Condiments", "honey", "dry", 1095, None),
    ("Condiments", "olive oil", "dry", 540, "unopened"),
]


def seed_defaults(db: Session) -> None:
    """Insert any seed rules not already present (keyed by category+pattern+storage).

    Top-up rather than all-or-nothing so new seed rules reach existing
    installs without clobbering user edits to existing rules.
    """
    existing = {
        (d.category, d.name_pattern, d.storage_type)
        for d in db.query(ExpiryDefault).all()
    }
    added = False
    for category, pattern, storage, days, notes in _SEED_DEFAULTS:
        if (category, pattern, storage) in existing:
            continue
        db.add(ExpiryDefault(
            category=category,
            name_pattern=pattern,
            storage_type=storage,
            default_days=days,
            notes=notes,
            priority=1,
        ))
        added = True
    if added:
        db.commit()


def apply_defaults(item: FoodItem, db: Session, extra_match_text: str = "",
                   infer_storage: bool = False) -> FoodItem:
    """Fill in best_by_date if not already set, using the defaults table.

    extra_match_text lets callers supply additional keywords to match patterns
    against: e.g. Open Food Facts category tags, so a branded product like
    "Chobani Vanilla Greek" still hits the "yogurt" rule.

    infer_storage lets a matching rule override the item's storage_type when
    no rule exists for the guessed storage: e.g. mayonnaise guessed "dry"
    from OFF tags adopts the "mayonnaise → refrigerated" rule's storage.
    """
    if item.best_by_date is not None:
        return item

    haystack = f"{item.name} {extra_match_text}".lower()

    # name_pattern is a substring match; rules whose category matches the
    # item's category win over name-only matches so e.g. "Chicken of the Sea"
    # canned tuna doesn't inherit the fresh-chicken expiry.
    rows = db.query(ExpiryDefault).all()
    matches = [r for r in rows if r.name_pattern.lower() in haystack]
    category_matches = [r for r in matches if r.category.lower() == item.category.value.lower()]
    pool = category_matches or matches

    if infer_storage and pool:
        same_storage = [r for r in pool if r.storage_type == item.storage_type.value]
        if not same_storage:
            best = max(pool, key=lambda r: (len(r.name_pattern), r.priority))
            try:
                item.storage_type = StorageType(best.storage_type)
            except ValueError:
                pass

    storage = item.storage_type.value
    pool = [r for r in pool if r.storage_type == storage]

    if pool:
        best_match = max(pool, key=lambda r: (len(r.name_pattern), r.priority))
        item.best_by_date = date.today() + timedelta(days=best_match.default_days)
    else:
        # Generic fallback by category
        fallback = _CATEGORY_FALLBACKS.get(item.category.value, {}).get(storage, 7)
        item.best_by_date = date.today() + timedelta(days=fallback)

    return item


_CATEGORY_FALLBACKS: dict[str, dict[str, int]] = {
    "Poultry":     {"refrigerated": 4,  "frozen": 270, "room_temp": 0,  "dry": 0},
    "Meat":        {"refrigerated": 4,  "frozen": 180, "room_temp": 0,  "dry": 0},
    "Seafood":     {"refrigerated": 2,  "frozen": 180, "room_temp": 0,  "dry": 0},
    "Dairy":       {"refrigerated": 14, "frozen": 180, "room_temp": 0,  "dry": 0},
    "Produce":     {"refrigerated": 7,  "frozen": 365, "room_temp": 5,  "dry": 0},
    "Grains":      {"refrigerated": 14, "frozen": 365, "room_temp": 14, "dry": 730},
    "Condiments":  {"refrigerated": 90, "frozen": 0,   "room_temp": 30, "dry": 365},
    "Canned":      {"refrigerated": 0,  "frozen": 0,   "room_temp": 0,  "dry": 1095},
    "Beverages":   {"refrigerated": 10, "frozen": 365, "room_temp": 270, "dry": 365},
    "Snacks":      {"refrigerated": 30, "frozen": 180, "room_temp": 90,  "dry": 180},
    "Frozen":      {"refrigerated": 3,  "frozen": 180, "room_temp": 0,   "dry": 0},
    "Other":       {"refrigerated": 7,  "frozen": 180, "room_temp": 7,  "dry": 365},
}
