"""Community shelf-life overrides from Forager (FoodAssistant-ezkh).

Forager publishes a small aggregated table of shelf lives learned from
kitchens that opted into sharing their expiry corrections (see
services/expiry_learning.py): for a barcode or a normalized product name plus
a storage kind, the median days a real kitchen keeps it. This module fetches
that feed once a day, caches it under data_dir, and folds it into the best-by
suggestion for new items.

Priority when suggesting a date (merge_days, pure and unit-tested):

    the user's own explicit expiry rules  >  a community override
    >  the built-in rules  >  nothing

A community value can only shorten a date or stretch it a little, never
replace the built-in rule outright (FoodAssistant-fa1): for fridge and
counter storage it is capped at 1.5x the built-in rule, and for meat, poultry,
fish and dairy it is capped at the built-in rule itself. A poisoned or simply
wrong feed entry can therefore never tell a kitchen that raw chicken keeps
for weeks.

Reading the feed sends nothing and needs no account; it is gated by the
``use_community_expiry`` setting (on by default). The cache follows the same
state-file pattern as services/best_by_provenance.py: atomic writes, mtime
cached reads, and silent degradation when data_dir is unwritable or the feed
has never been fetched (lookups just return None and the built-in rules
apply as before).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

import httpx

from .best_by_provenance import normalize_name
from .expiry_learning import storage_kind

logger = logging.getLogger(__name__)

# Believable shelf-life window, matching services/shelf_life.py: a feed entry
# outside it is ignored rather than trusted.
_MIN_DAYS = 1
_MAX_DAYS = 3650

# Refresh cadence: the feed changes slowly, one fetch a day is plenty.
_MAX_AGE_SECONDS = 24 * 3600

_lock = threading.Lock()
# In-process view of the cache file: lookup indexes keyed by
# ("barcode", code, storage) and ("name", name_key, storage) -> days.
_index: dict[tuple, int] = {}
_mtime: int | None = None


def _cache_file() -> Path:
    # Resolved per call (not at import) so tests that repoint data_dir work.
    from ..config import settings
    return Path(settings.data_dir) / "community_expiry.json"


def build_index(feed: dict) -> dict[tuple, int]:
    """Lookup index from a raw overrides feed. Pure and defensive: malformed
    entries and out-of-range day counts are skipped."""
    index: dict[tuple, int] = {}
    overrides = feed.get("overrides") if isinstance(feed, dict) else None
    if not isinstance(overrides, list):
        return index
    for entry in overrides:
        if not isinstance(entry, dict):
            continue
        try:
            days = int(entry.get("days_median"))
        except (TypeError, ValueError):
            continue
        if not (_MIN_DAYS <= days <= _MAX_DAYS):
            continue
        storage = str(entry.get("storage") or "").strip()
        if not storage:
            continue
        barcode = str(entry.get("barcode") or "").strip()
        name_key = normalize_name(str(entry.get("name_key") or ""))
        if barcode:
            index[("barcode", barcode, storage)] = days
        elif name_key:
            index[("name", name_key, storage)] = days
    return index


def override_days(index: dict[tuple, int], name: str,
                  barcode: Optional[str], kind: str) -> Optional[int]:
    """The community shelf life for an item, or None. Pure.

    A barcode match is the most specific and wins; otherwise the normalized
    name is tried. ``kind`` is a shared storage kind ("fridge" / "freezer" /
    "pantry" / "other"), usually via storage_kind()."""
    code = (barcode or "").strip()
    if code:
        days = index.get(("barcode", code, kind))
        if days is not None:
            return days
    name_key = normalize_name(name)
    if name_key:
        return index.get(("name", name_key, kind))
    return None


# Storage kinds where a too-long date is a food-safety problem rather than a
# quality one: the fridge and the counter.
_PERISHABLE_KINDS = frozenset({"fridge", "other"})
# How far past the built-in rule a community value may reach for perishable
# storage in general.
_PERISHABLE_FACTOR = 1.5
# Categories where a community value may never exceed the built-in rule.
_STRICT_CATEGORIES = frozenset({"poultry", "meat", "seafood", "fish", "dairy"})
# Name words that mark an item as strict even when it carries no category
# (an uncategorized "chicken breast" is still raw poultry).
_STRICT_NAME_WORDS = frozenset({
    "chicken", "turkey", "duck", "poultry", "beef", "pork", "lamb", "veal",
    "steak", "mince", "ground", "sausage", "bacon", "ham", "meat", "fish",
    "salmon", "tuna", "cod", "tilapia", "shrimp", "prawn", "prawns", "crab",
    "lobster", "scallop", "scallops", "oyster", "oysters", "mussels", "clams",
    "seafood", "milk", "cream", "yogurt", "yoghurt", "cheese", "butter",
    "egg", "eggs",
})


def is_strict_item(category: Optional[str], name: Optional[str]) -> bool:
    """True for meat, poultry, fish, seafood, dairy and eggs. Pure."""
    if (category or "").strip().lower() in _STRICT_CATEGORIES:
        return True
    words = set(normalize_name(name or "").replace(",", " ").split())
    return bool(words & _STRICT_NAME_WORDS)


def community_cap(builtin_days: Optional[int], kind: Optional[str],
                  strict: bool = False) -> Optional[int]:
    """The longest community shelf life allowed for an item, or None for no
    cap. Pure.

    Perishable storage (fridge, counter, or an unknown kind) caps a community
    value at 1.5x the built-in rule, and at the built-in rule itself for
    strict items (meat, fish, dairy). With no built-in rule to compare
    against, perishable storage accepts no community value at all (cap 0
    means reject). Freezer and pantry storage are a matter of quality, not
    safety, and stay uncapped.
    """
    if kind is not None and kind not in _PERISHABLE_KINDS:
        return None
    if builtin_days is None:
        return 0
    if strict:
        return builtin_days
    return int(builtin_days * _PERISHABLE_FACTOR)


def merge_days(user_days: Optional[int], community_days: Optional[int],
               builtin_days: Optional[int], *, kind: Optional[str] = None,
               strict: bool = False) -> tuple[Optional[int], str]:
    """Resolve a suggested shelf life across the three sources. Pure.

    Returns (days, source) where source is "user", "community", "default", or
    "none". The user's own explicit rule always wins; a community override
    beats the built-in rules within the cap from community_cap (a value past
    the cap is clamped to it, and one clamped all the way back to the built-in
    rule reports as "default"); nothing at all yields (None, "none").
    ``kind`` is the shared storage kind; None is treated as perishable."""
    if user_days is not None:
        return user_days, "user"
    if community_days is not None:
        cap = community_cap(builtin_days, kind, strict)
        if cap is not None and community_days > cap:
            community_days = cap if cap > 0 else None
        if community_days is not None:
            if builtin_days is not None and community_days == builtin_days:
                return builtin_days, "default"
            return community_days, "community"
    if builtin_days is not None:
        return builtin_days, "default"
    return None, "none"


def _load_locked() -> None:
    """Refresh the in-process index from the cache file if it changed on disk.
    Caller holds the lock."""
    global _index, _mtime
    try:
        cf = _cache_file()
        mtime = cf.stat().st_mtime_ns
    except OSError:
        return  # never fetched, or unwritable data_dir: no overrides
    if mtime == _mtime:
        return
    try:
        feed = json.loads(cf.read_text())
    except (OSError, ValueError):
        return  # a torn or corrupt cache never breaks a suggestion
    _mtime = mtime
    _index = build_index(feed)


def suggested_days(name: str, barcode: Optional[str],
                   storage_type: str) -> Optional[int]:
    """The community shelf life for an item, or None.

    Gated by the use_community_expiry setting and served entirely from the
    local cache (no network on this path). Never raises."""
    try:
        from ..config import settings
        if not settings.use_community_expiry:
            return None
        with _lock:
            _load_locked()
            index = _index
        return override_days(index, name, barcode, storage_kind(storage_type))
    except Exception:
        return None


def is_stale(max_age_seconds: int = _MAX_AGE_SECONDS,
             now: Optional[float] = None) -> bool:
    """True when the cached feed is missing or older than max_age_seconds."""
    try:
        mtime = _cache_file().stat().st_mtime
    except OSError:
        return True
    return ((time.time() if now is None else now) - mtime) > max_age_seconds


async def refresh() -> bool:
    """Fetch the overrides feed from Forager and cache it. Calm on failure:
    returns False and keeps whatever cache exists. Never raises."""
    try:
        from ..config import settings
        base = (settings.cloud_base_url or "").rstrip("/")
        if not base:
            return False
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{base}/api/learn/expiry/overrides")
        if r.status_code != 200:
            return False
        feed = r.json()
        if not isinstance(feed, dict) or not isinstance(feed.get("overrides"), list):
            return False
        cf = _cache_file()
        tmp = cf.with_name(cf.name + ".tmp")
        tmp.write_text(json.dumps(feed))
        os.replace(tmp, cf)
        return True
    except Exception:
        logger.debug("community expiry refresh failed", exc_info=True)
        return False


def clear_cache() -> None:
    """Drop the cached feed and the in-process index (test cleanup)."""
    global _index, _mtime
    with _lock:
        _index = {}
        _mtime = None
        try:
            _cache_file().unlink(missing_ok=True)
        except OSError:
            pass
