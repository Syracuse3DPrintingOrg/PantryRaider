"""Community shelf-life learning: anonymous submissions and the public feed.

Two endpoints, both deliberately account-free:

POST /api/learn/expiry takes a small batch of anonymous expiry corrections
from a Pantry Raider install whose owner opted into sharing. There is no
authentication (sharing works with or without a Forager account) and nothing
about the caller is stored: no account, no instance, no address, and the
received date is day-granular. The only throttle is a per-IP rate-limit
window, which lives in memory and is never persisted. Validation is strict:
a batch with anything out of shape or out of range is rejected whole.

GET /api/learn/expiry/overrides publishes the aggregated dataset the apps
consume: for each (barcode or product name, storage kind) with at least
``learn_k_threshold`` observations and sane agreement, the median shelf life
in days. The k-threshold means no single kitchen's data is ever visible on
its own, and the median plus a spread check keep one absurd value from
steering anyone's fridge. Aggregation runs on request behind a short
in-process cache; the feed changes slowly, so that is plenty.
"""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timezone
from statistics import median

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import ratelimit
from ..config import settings
from ..deps import client_ip, get_db
from ..models import ExpiryObservation

router = APIRouter(prefix="/api/learn", tags=["learn"])

RATE_LIMITED = "Too many submissions from this address. Try again in a minute."
INVALID_BATCH = "The submitted batch is not valid."

# Hard caps and vocabularies, mirrored from the app's expiry_learning module
# (duplicated on purpose: the two apps share nothing at import time).
MAX_POINTS = 100
MAX_NAME_LEN = 120
MIN_DAYS = 1
MAX_DAYS = 3650
STORAGE_KINDS = ("fridge", "freezer", "pantry", "other")
SUGGESTION_SOURCES = ("default", "llm", "community", "none")

# How large the published feed may grow, largest sample counts first.
MAX_FEED_ENTRIES = 5000

_WS = re.compile(r"\s+")


class ExpiryPoint(BaseModel):
    barcode: str | None = None
    name_key: str = ""
    storage: str = ""
    shelf_life_days: int = 0
    suggested_days: int | None = None
    suggestion_source: str = "none"


class ExpiryBatch(BaseModel):
    points: list[ExpiryPoint] = []


# --- Pure helpers (validation and aggregation), unit-tested directly --------

def normalize_name_key(value: str) -> str:
    """The same product-name folding the app applies: lowercase, collapsed
    whitespace, trimmed. Applied again here so a misbehaving client cannot
    smuggle in a differently cased duplicate."""
    return _WS.sub(" ", (value or "").strip().lower())


def clean_point(point: ExpiryPoint) -> dict | None:
    """A validated, normalized point ready to store, or None when invalid.

    Everything is checked hard: the name key must be a non-empty string within
    length, the storage kind and suggestion source must come from the known
    vocabularies, the day counts must be sane, and a barcode (optional) must be
    a plain numeric code of plausible product length."""
    name_key = normalize_name_key(point.name_key)
    if not name_key or len(name_key) > MAX_NAME_LEN:
        return None
    if point.storage not in STORAGE_KINDS:
        return None
    if point.suggestion_source not in SUGGESTION_SOURCES:
        return None
    if not (MIN_DAYS <= point.shelf_life_days <= MAX_DAYS):
        return None
    if point.suggested_days is not None and not (0 <= point.suggested_days <= MAX_DAYS):
        return None
    barcode = (point.barcode or "").strip() or None
    if barcode is not None and (not barcode.isdigit() or not (6 <= len(barcode) <= 24)):
        return None
    return {
        "barcode": barcode,
        "name_key": name_key,
        "storage": point.storage,
        "shelf_life_days": point.shelf_life_days,
        "suggested_days": point.suggested_days,
        "suggestion_source": point.suggestion_source,
    }


def sane_agreement(days: list[int]) -> bool:
    """Whether a group of observations agrees well enough to publish. Pure.

    Uses the median absolute deviation: a group whose typical member sits more
    than half the median (or more than 3 days, whichever is larger) away from
    the median is arguing with itself, so it is held back rather than
    published as a confident number."""
    if not days:
        return False
    mid = median(days)
    mad = median(abs(d - mid) for d in days)
    return mad <= max(3, 0.5 * mid)


def aggregate(rows: list[dict], k_threshold: int) -> list[dict]:
    """The published override entries from raw observations. Pure.

    Groups by (barcode, storage) for points that carry a barcode and by
    (name_key, storage) for every point, publishes only groups with at least
    ``k_threshold`` observations and sane agreement, and reports the median
    days plus the sample count. Ordered by samples (largest first) and capped
    at MAX_FEED_ENTRIES."""
    by_barcode: dict[tuple[str, str], list[int]] = {}
    by_name: dict[tuple[str, str], list[int]] = {}
    for row in rows:
        days = row["shelf_life_days"]
        if row.get("barcode"):
            by_barcode.setdefault((row["barcode"], row["storage"]), []).append(days)
        by_name.setdefault((row["name_key"], row["storage"]), []).append(days)

    entries: list[dict] = []
    for (barcode, storage), days in by_barcode.items():
        if len(days) >= k_threshold and sane_agreement(days):
            entries.append({"barcode": barcode, "storage": storage,
                            "days_median": int(round(median(days))),
                            "samples": len(days)})
    for (name_key, storage), days in by_name.items():
        if len(days) >= k_threshold and sane_agreement(days):
            entries.append({"name_key": name_key, "storage": storage,
                            "days_median": int(round(median(days))),
                            "samples": len(days)})
    entries.sort(key=lambda e: (-e["samples"],
                                e.get("barcode") or "", e.get("name_key") or "",
                                e["storage"]))
    return entries[:MAX_FEED_ENTRIES]


# --- Feed cache --------------------------------------------------------------

_FEED_TTL_SECONDS = 900
_cache_lock = threading.Lock()
_cached_feed: dict | None = None
_cached_at: float = 0.0


def reset_feed_cache() -> None:
    """Forget the cached feed (tests, and after bulk admin cleanup)."""
    global _cached_feed, _cached_at
    with _cache_lock:
        _cached_feed = None
        _cached_at = 0.0


# --- Routes ------------------------------------------------------------------

@router.post("/expiry")
def submit_expiry_points(batch: ExpiryBatch, request: Request,
                         db: Session = Depends(get_db)):
    """Accept a batch of anonymous shelf-life corrections. No account needed."""
    if not ratelimit.allow(f"learn-ip:{client_ip(request)}",
                           settings.learn_rate_per_minute):
        raise HTTPException(429, detail=RATE_LIMITED)
    if not batch.points or len(batch.points) > MAX_POINTS:
        raise HTTPException(422, detail=INVALID_BATCH)

    cleaned: list[dict] = []
    seen: set[tuple] = set()
    for point in batch.points:
        row = clean_point(point)
        if row is None:
            # Reject the whole batch: a well-behaved app never produces an
            # invalid point, so a bad one signals a bad or hostile client.
            raise HTTPException(422, detail=INVALID_BATCH)
        # Duplicate points within one batch count once: cheap padding defense.
        key = tuple(sorted(row.items(), key=lambda kv: kv[0]))
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(row)

    today = datetime.now(timezone.utc).date().isoformat()
    for row in cleaned:
        db.add(ExpiryObservation(received_date=today, **row))
    db.commit()
    return {"accepted": len(cleaned)}


@router.get("/expiry/overrides")
def expiry_overrides(db: Session = Depends(get_db)):
    """The aggregated community shelf-life feed the apps download daily."""
    global _cached_feed, _cached_at
    with _cache_lock:
        if _cached_feed is not None and time.time() - _cached_at < _FEED_TTL_SECONDS:
            return _cached_feed

        rows = [{"barcode": o.barcode, "name_key": o.name_key,
                 "storage": o.storage, "shelf_life_days": o.shelf_life_days}
                for o in db.query(ExpiryObservation).all()]
        feed = {
            "version": 1,
            "generated_date": datetime.now(timezone.utc).date().isoformat(),
            "overrides": aggregate(rows, settings.learn_k_threshold),
        }
        _cached_feed = feed
        _cached_at = time.time()
        return feed
