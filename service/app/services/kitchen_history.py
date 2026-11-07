"""What this kitchen's own Grocy history says (FoodAssistant-i9flm).

Two small learners over Grocy's stock journal, no model and no network beyond
the Grocy call that fetches the rows:

* Spoilage. A stock entry bought on day 0 and later consumed as spoiled on
  day N is one real observation that the product lasted about N days here.
  With enough of them, the median becomes this kitchen's shelf life for that
  product, and a new item's suggested best-by is shortened to it. It only ever
  shortens: a kitchen that throws strawberries out on day 3 should see day 3,
  but a long history of eating things early says nothing about how long they
  would have kept, so it never stretches a date.
* Consumption. How fast a product gets used (amount consumed per day over a
  recent window) turns current stock into "runs out in about N days", shown
  next to the restock suggestions on the Shopping page.

The learned tables are cached in a small state file under data_dir, written
atomically and read with an mtime check (the community_expiry.py pattern), so
a lookup never touches Grocy and a missing or corrupt cache just means no
learned values. The learners themselves are pure functions over plain dicts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import statistics
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from .best_by_provenance import normalize_name

logger = logging.getLogger(__name__)

# A product needs this many spoiled stock entries before its median counts.
# Fewer is an anecdote (one forgotten bag of spinach), not a habit.
MIN_SPOIL_SAMPLES = 3
# How far back the journal is read, and the longest spoil age believed.
_LOOKBACK_DAYS = 365
# Consumption rates look at the recent past only, so a habit that changed
# last month is not averaged away by last year.
RATE_WINDOW_DAYS = 60
MIN_CONSUME_EVENTS = 3
# A rate is never spread over less than a week, so three yogurts on Monday do
# not read as a three-a-day habit.
_MIN_RATE_SPAN_DAYS = 7
# The run-out forecast shows items expected to run out within this many days.
RUNOUT_HORIZON_DAYS = 7

_JOURNAL_LIMIT = 5000
_JOURNAL_MAX_PAGES = 20
_MAX_AGE_SECONDS = 6 * 3600

_lock = threading.Lock()
_cache: dict = {}
_mtime: int | None = None


def _cache_file() -> Path:
    from ..config import settings
    # Resolved per call (not at import) so tests that repoint data_dir work.
    return Path(settings.data_dir) / "kitchen_history.json"


def _day(value) -> Optional[date]:
    """The date part of a Grocy timestamp or date string, or None."""
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _flag(value) -> bool:
    return str(value).strip().lower() in ("1", "true")


def _amount(value) -> float:
    try:
        return abs(float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def _event_key(row, day: date) -> str:
    """One user action in the journal. Grocy gives every row written by the
    same action one transaction_id (a toss of three stock entries is three
    rows, one action); without it the day stands in."""
    tid = str(row.get("transaction_id") or "").strip()
    return f"t:{tid}" if tid else f"d:{day.isoformat()}"


def learn_spoil_days(rows, *, freezer_locations=frozenset(),
                     min_samples: int = MIN_SPOIL_SAMPLES) -> dict[str, dict]:
    """Per-product days until spoiled, from Grocy stock_log rows. Pure.

    Pairs each stock entry's purchase with its first spoiled consume (by
    stock_id) and takes the days between them. Entries bought into a freezer
    location or moved between locations are left out: their clock does not
    run at fridge speed, so they would only blur the number. Undone rows are
    ignored, and so is a toss on or after the entry's own best-by date: food
    thrown out once its date has passed says nothing about it spoiling early,
    and counting it would let a shortened date keep shortening itself. A
    same-day toss (age 0) is a mistake fixed or a bad purchase rather than a
    shelf life, so it is left out too.

    One toss that clears several entries is a single observation, not
    several: entries are grouped by the toss's transaction_id (by purchase
    day when the journal has none) and each group adds one sample, its
    median age. Returns {product_id: {"days": int, "samples": int}} for
    products with at least ``min_samples`` observations; days is the median.
    """
    purchases: dict[str, tuple[str, date, Optional[date]]] = {}
    moved: set[str] = set()
    spoiled: dict[str, tuple[date, Optional[date], dict]] = {}
    freezers = {str(x) for x in freezer_locations}
    for row in rows or []:
        if not isinstance(row, dict) or _flag(row.get("undone")):
            continue
        sid = str(row.get("stock_id") or "")
        if not sid:
            continue
        kind = row.get("transaction_type")
        if kind == "purchase":
            if str(row.get("location_id") or "") in freezers:
                moved.add(sid)
                continue
            day = _day(row.get("purchased_date")) or _day(row.get("row_created_timestamp"))
            pid = str(row.get("product_id") or "")
            if day and pid:
                purchases[sid] = (pid, day, _day(row.get("best_before_date")))
        elif kind in ("transfer_from", "transfer_to"):
            moved.add(sid)
        elif kind == "consume" and _flag(row.get("spoiled")):
            day = _day(row.get("row_created_timestamp"))
            if day and (sid not in spoiled or day < spoiled[sid][0]):
                spoiled[sid] = (day, _day(row.get("best_before_date")), row)

    groups: dict[tuple[str, str], list[int]] = {}
    for sid, (gone, label, row) in spoiled.items():
        if sid in moved or sid not in purchases:
            continue
        pid, bought, bought_label = purchases[sid]
        label = label or bought_label
        if label is not None and gone >= label:
            continue
        age = (gone - bought).days
        if not 1 <= age <= _LOOKBACK_DAYS:
            continue
        tid = str(row.get("transaction_id") or "").strip()
        key = f"t:{tid}" if tid else f"d:{bought.isoformat()}"
        groups.setdefault((pid, key), []).append(age)

    ages: dict[str, list[int]] = {}
    for (pid, _key), values in groups.items():
        ages.setdefault(pid, []).append(statistics.median_low(values))

    out = {}
    for pid, values in ages.items():
        if len(values) >= min_samples:
            out[pid] = {"days": max(1, int(statistics.median_low(values))),
                        "samples": len(values)}
    return out


def learn_consumption(rows, *, today: date,
                      window_days: int = RATE_WINDOW_DAYS,
                      min_events: int = MIN_CONSUME_EVENTS) -> dict[str, dict]:
    """Per-product amount used per day over the recent window. Pure.

    Counts ordinary (not spoiled, not undone) consumes from the last
    ``window_days`` days. The rate is the total amount over the days since the
    first of those consumes, never less than a week. One consume that draws
    from several stock entries writes several journal rows, so events are
    counted per action (transaction_id, or the day when there is none) while
    every row's amount adds up. Returns
    {product_id: {"per_day": float, "events": int}} for products with at least
    ``min_events`` consumes.
    """
    cutoff = today - timedelta(days=window_days)
    totals: dict[str, float] = {}
    events: dict[str, set[str]] = {}
    first: dict[str, date] = {}
    for row in rows or []:
        if not isinstance(row, dict) or _flag(row.get("undone")):
            continue
        if row.get("transaction_type") != "consume" or _flag(row.get("spoiled")):
            continue
        day = _day(row.get("row_created_timestamp"))
        pid = str(row.get("product_id") or "")
        amount = _amount(row.get("amount"))
        if not day or not pid or amount <= 0 or day < cutoff or day > today:
            continue
        totals[pid] = totals.get(pid, 0.0) + amount
        events.setdefault(pid, set()).add(_event_key(row, day))
        if pid not in first or day < first[pid]:
            first[pid] = day

    out = {}
    for pid, total in totals.items():
        count = len(events[pid])
        if count < min_events:
            continue
        span = max((today - first[pid]).days, _MIN_RATE_SPAN_DAYS)
        out[pid] = {"per_day": round(total / span, 4), "events": count}
    return out


def forecast_runout(rates: dict[str, dict], stock: dict[str, float],
                    names: dict[str, str] | None = None,
                    horizon_days: int = RUNOUT_HORIZON_DAYS) -> list[dict]:
    """Products in stock that should run out within ``horizon_days``. Pure.

    ``stock`` maps product id to the amount on hand. Out-of-stock products are
    left to the restock suggestions. Sorted soonest first.
    """
    names = names or {}
    out = []
    for pid, info in (rates or {}).items():
        try:
            per_day = float(info.get("per_day") or 0)
            on_hand = float(stock.get(pid) or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if per_day <= 0 or on_hand <= 0:
            continue
        days_left = on_hand / per_day
        if days_left > horizon_days:
            continue
        out.append({
            "product_id": int(pid) if str(pid).isdigit() else pid,
            "product_name": names.get(pid) or f"Product {pid}",
            "amount": on_hand,
            "per_day": per_day,
            "days_left": max(0, int(days_left)),
        })
    out.sort(key=lambda r: (r["days_left"], r["product_name"].lower()))
    return out


def shorten_days(days: Optional[int], learned: Optional[int],
                 kind: Optional[str]) -> tuple[Optional[int], bool]:
    """Apply a learned spoil age to a suggested shelf life. Pure.

    Returns (days, used_history). The learned value wins only when it is
    shorter than the suggestion, and never for freezer storage (the spoilage
    data comes from items that were not frozen).
    """
    if learned is None or days is None or kind == "freezer":
        return days, False
    if 1 <= learned < days:
        return learned, True
    return days, False


# The first few times a learned date shortens a new item, the inbox says so
# and offers to turn the feature off. After that the "est." badge is enough.
NOTICE_KIND = "kitchen_history"
MAX_NOTICES = 3


def _plural_days(n: int) -> str:
    return "1 day" if n == 1 else f"{n} days"


def notice_text(name: str, days: int, standard_days: int) -> tuple[str, str]:
    """Title and body for the inbox notice. Pure."""
    name = (name or "this product").strip()
    title = f"Best-by dates for {name} now follow your kitchen"
    body = (f"The {name} you marked as spoiled usually went bad about "
            f"{_plural_days(days)} after purchase, so new items are dated "
            f"{_plural_days(days)} out instead of {_plural_days(standard_days)}. "
            f"Turn this off if you would rather keep the standard dates.")
    return title, body


def maybe_notify(db, name: str, days: int, standard_days: int) -> bool:
    """Raise the inbox notice for a shortened date, at most MAX_NOTICES times
    over the life of the install (one per product). Never raises."""
    try:
        from ..models.db_models import ActionItem
        from . import action_items
        key = f"{NOTICE_KIND}:{normalize_name(name)}"
        seen = db.query(ActionItem).filter(ActionItem.kind == NOTICE_KIND)
        if seen.filter(ActionItem.dedupe_key == key).first() is not None:
            return False
        if seen.count() >= MAX_NOTICES:
            return False
        title, body = notice_text(name, days, standard_days)
        action_items.create(db, NOTICE_KIND, title, body=body, dedupe_key=key,
                            payload={"setting": "use_kitchen_history"})
        return True
    except Exception:
        logger.debug("kitchen history notice failed", exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return False


def _is_freezer(loc: dict) -> bool:
    """A Grocy location that freezes: flagged as a freezer in Grocy, or the
    one Pantry Raider files frozen food under (matched by name, since a
    Freezer location made by hand in Grocy may not carry the flag)."""
    if _flag(loc.get("is_freezer")):
        return True
    try:
        from ..storage_categories import classify_location
        return classify_location(str(loc.get("name") or "")) == "frozen"
    except Exception:
        return False


def build_cache(rows, products, locations, *, today: date,
                grocy_url: str = "") -> dict:
    """The state-file document from raw Grocy rows. Pure apart from reading
    the storage categories to recognize the freezer by name."""
    freezers = {str(loc.get("id")) for loc in locations or []
                if isinstance(loc, dict) and _is_freezer(loc)}
    names = {str(p.get("id")): str(p.get("name") or "")
             for p in products or [] if isinstance(p, dict)}
    spoil = learn_spoil_days(rows, freezer_locations=freezers)
    by_name = {}
    for pid, info in spoil.items():
        key = normalize_name(names.get(pid, ""))
        if key:
            by_name[key] = info["days"]
    return {
        "version": 1,
        "built": today.isoformat(),
        # Which Grocy the history came from: pointing the app at another
        # Grocy makes this cache someone else's kitchen (see _load).
        "grocy_url": _url_key(grocy_url),
        "spoil_by_product": spoil,
        "spoil_by_name": by_name,
        "consumption": learn_consumption(rows, today=today),
    }


def _load() -> dict:
    """The cached document, re-read only when the file changed on disk."""
    global _cache, _mtime
    with _lock:
        try:
            cf = _cache_file()
            mtime = cf.stat().st_mtime_ns
        except OSError:
            return {}
        if mtime != _mtime:
            try:
                doc = json.loads(cf.read_text())
            except (OSError, ValueError):
                return {}
            _cache = doc if isinstance(doc, dict) else {}
            _mtime = mtime
        if not _same_grocy(_cache):
            return {}
        return _cache


def _url_key(url) -> str:
    return str(url or "").strip().rstrip("/").lower()


def _same_grocy(doc: dict) -> bool:
    """False when the cache was built from a different Grocy than the one
    configured now. A cache with no recorded Grocy (older builds) is kept."""
    built_from = doc.get("grocy_url") if isinstance(doc, dict) else None
    if not built_from:
        return True
    from ..config import settings
    return _url_key(built_from) == _url_key(settings.grocy_base_url)


def learned_spoil_days(name: str, product_id=None) -> Optional[int]:
    """This kitchen's learned days-until-spoiled for a product, or None.

    Looked up by Grocy product id when one is known (two products can share a
    name, and a renamed product keeps its id), then by name. Gated by the
    use_kitchen_history setting and served from the local cache only. Never
    raises."""
    try:
        from ..config import settings
        if not settings.use_kitchen_history:
            return None
        doc = _load()
        if product_id not in (None, ""):
            info = (doc.get("spoil_by_product") or {}).get(str(product_id))
            if isinstance(info, dict) and info.get("days"):
                return int(info["days"])
        value = (doc.get("spoil_by_name") or {}).get(normalize_name(name))
        return int(value) if value else None
    except Exception:
        return None


def consumption_rates() -> dict[str, dict]:
    """Cached per-product consumption rates, or {} (never raises)."""
    try:
        rates = _load().get("consumption")
        return rates if isinstance(rates, dict) else {}
    except Exception:
        return {}


def is_stale(max_age_seconds: int = _MAX_AGE_SECONDS,
             now: Optional[float] = None) -> bool:
    try:
        mtime = _cache_file().stat().st_mtime
    except OSError:
        return True
    try:
        if not _load():
            return True  # unreadable, or built from a different Grocy
    except Exception:
        return True
    return ((time.time() if now is None else now) - mtime) > max_age_seconds


async def _read_journal(grocy, since: str) -> list:
    """Every stock_log row since ``since``, a page at a time, newest first.
    Stops at _JOURNAL_MAX_PAGES pages so a huge journal cannot run away."""
    query = quote(f"row_created_timestamp>={since}", safe="")
    rows: list = []
    for page in range(_JOURNAL_MAX_PAGES):
        batch = await grocy._get(
            f"/objects/stock_log?query%5B%5D={query}"
            f"&order=row_created_timestamp%3Adesc"
            f"&limit={_JOURNAL_LIMIT}&offset={page * _JOURNAL_LIMIT}")
        if not isinstance(batch, list):
            break
        rows.extend(batch)
        if len(batch) < _JOURNAL_LIMIT:
            break
    return rows


async def refresh(grocy=None) -> bool:
    """Read the journal from Grocy, relearn, and cache. Returns False on any
    failure and keeps the old cache. Never raises."""
    try:
        if grocy is None:
            from .grocy import GrocyClient
            grocy = GrocyClient()
        from ..config import settings
        # Local date: Grocy writes the journal in the kitchen's local time.
        today = date.today()
        since = (today - timedelta(days=_LOOKBACK_DAYS)).isoformat()
        rows = await _read_journal(grocy, since)
        products = await grocy.get_products()
        locations = await grocy._get("/objects/locations")
        doc = build_cache(rows, products, locations, today=today,
                          grocy_url=settings.grocy_base_url)
        from .. import durable
        await asyncio.to_thread(durable.write_json, _cache_file(), doc,
                                keep_last_good=False)
        return True
    except Exception:
        logger.debug("kitchen history refresh failed", exc_info=True)
        return False


def clear_cache() -> None:
    """Drop the cache file and the in-process copy (test cleanup)."""
    global _cache, _mtime
    with _lock:
        _cache, _mtime = {}, None
        try:
            _cache_file().unlink(missing_ok=True)
        except OSError:
            pass
