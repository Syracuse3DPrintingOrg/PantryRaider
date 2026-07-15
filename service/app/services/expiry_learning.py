"""Anonymous expiry-correction capture for community shelf life (FoodAssistant-ezkh).

When the user corrects or sets a best-by date on the Pending review screen and
then commits the item, that correction is a learning signal: the app suggested
one shelf life and a real kitchen chose another. With the OPT-IN
``share_expiry_learning`` setting on (off by default), each such commit adds
one anonymous data point to a small local queue, and a background task uploads
batches to Forager, which aggregates them into the community shelf-life
dataset every install can benefit from (see services/community_expiry.py).

Privacy rules, enforced here rather than promised elsewhere:

- A point carries ONLY: the product barcode (when the item has a real one),
  the normalized product name, the storage kind (fridge / freezer / pantry /
  other), the chosen shelf life in days, and what the app would have suggested.
- No timestamps beyond day granularity (day counts only), no install or device
  id, no user identifiers, no account link (the upload is unauthenticated and
  works with or without a Forager account), no free text beyond the product
  name.
- When sharing is OFF nothing is captured at all, not even queued locally; a
  queue left over from before the toggle was switched off is discarded on the
  next flush pass instead of being sent.

The queue is a small JSON state file under data_dir with the same atomic-write
pattern as services/timers.py and services/best_by_provenance.py, so multiple
workers agree and a failed upload just waits for the next pass. Uploads are
fire-and-forget with retry-later semantics and never block a user path.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date
from pathlib import Path
from typing import Optional

import httpx

from .best_by_provenance import normalize_name

logger = logging.getLogger(__name__)

# Storage kinds the shared dataset speaks. The app's own storage types map
# onto them; anything unknown (custom storage categories) becomes "other".
_STORAGE_KIND_MAP = {
    "refrigerated": "fridge",
    "frozen": "freezer",
    "dry": "pantry",
    "room_temp": "other",
}
STORAGE_KINDS = ("fridge", "freezer", "pantry", "other")

# Where a suggestion can come from. "none" = the app had no date to offer.
SUGGESTION_SOURCES = ("default", "llm", "community", "none")

# Believable shelf-life window, matching services/shelf_life.py.
_MIN_DAYS = 1
_MAX_DAYS = 3650

# Longest name key we keep; anything longer is not a product name.
_MAX_NAME_LEN = 120

# Queue cap: oldest points are dropped first so the file can never grow
# without bound if the upload endpoint is unreachable for a long time.
_MAX_QUEUE = 500

# Upload batch size per request (the server caps the batch it accepts too).
_BATCH_SIZE = 100

_lock = threading.Lock()


def _queue_file() -> Path:
    # Resolved per call (not at import) so tests that repoint data_dir work.
    from ..config import settings
    return Path(settings.data_dir) / "expiry_learning_queue.json"


def storage_kind(storage_type: str) -> str:
    """Map an app storage type to a shared storage kind. Pure."""
    return _STORAGE_KIND_MAP.get((storage_type or "").strip(), "other")


def _clean_barcode(barcode: Optional[str]) -> Optional[str]:
    """A shareable product barcode, or None.

    Only plain numeric codes of a plausible product length are kept, and
    store-assigned/random-weight ranges are dropped: those codes mean nothing
    outside the store that printed them, so they carry no community value.
    """
    code = (barcode or "").strip()
    if not code.isdigit() or not (6 <= len(code) <= 24):
        return None
    from .barcode import is_store_local_barcode
    if is_store_local_barcode(code):
        return None
    return code


def build_point(name: str, barcode: Optional[str], storage_type: str,
                chosen_date: Optional[date], suggested_date: Optional[date],
                suggestion_source: Optional[str],
                base_date: date) -> Optional[dict]:
    """One anonymous data point from a committed correction, or None. Pure.

    ``base_date`` is the day the item entered the pantry (the pending row's
    created day), so the shared value is a true shelf-life-in-days rather than
    something that shrinks while the item waits in the review queue. Returns
    None when there is nothing honest to share: no usable name, a chosen date
    outside the believable window, or a "correction" that did not actually
    change the suggested date.
    """
    name_key = normalize_name(name)[:_MAX_NAME_LEN]
    if not name_key or chosen_date is None:
        return None
    days = (chosen_date - base_date).days
    if not (_MIN_DAYS <= days <= _MAX_DAYS):
        return None
    if suggested_date is not None and chosen_date == suggested_date:
        return None  # the user confirmed the suggestion; not a correction
    suggested_days = None
    if suggested_date is not None:
        sd = (suggested_date - base_date).days
        if 0 <= sd <= _MAX_DAYS:
            suggested_days = sd
    source = suggestion_source if suggestion_source in SUGGESTION_SOURCES else "none"
    return {
        "barcode": _clean_barcode(barcode),
        "name_key": name_key,
        "storage": storage_kind(storage_type),
        "shelf_life_days": days,
        "suggested_days": suggested_days,
        "suggestion_source": source,
    }


def _load_locked() -> list[dict]:
    try:
        data = json.loads(_queue_file().read_text())
        points = data.get("points", [])
        return points if isinstance(points, list) else []
    except (OSError, ValueError, AttributeError):
        return []


def _save_locked(points: list[dict]) -> None:
    qf = _queue_file()
    try:
        tmp = qf.with_name(qf.name + ".tmp")
        tmp.write_text(json.dumps({"points": points[-_MAX_QUEUE:]}))
        os.replace(tmp, qf)
    except OSError:
        pass  # data_dir not writable: the point is simply not queued


def queued_points() -> list[dict]:
    """The points currently waiting to upload (for the UI and tests)."""
    with _lock:
        return list(_load_locked())


def clear_queue() -> None:
    """Drop every queued point (sharing turned off, or test cleanup)."""
    with _lock:
        try:
            _queue_file().unlink(missing_ok=True)
        except OSError:
            pass


def record(name: str, barcode: Optional[str], storage_type: str,
           chosen_date: Optional[date], suggested_date: Optional[date],
           suggestion_source: Optional[str],
           base_date: Optional[date] = None) -> bool:
    """Queue one correction for upload, if sharing is enabled and the point is
    valid. Returns True when a point was queued. Never raises."""
    try:
        from ..config import settings
        if not settings.share_expiry_learning:
            return False  # sharing is off: capture nothing, not even locally
        point = build_point(name, barcode, storage_type, chosen_date,
                            suggested_date, suggestion_source,
                            base_date or date.today())
        if point is None:
            return False
        with _lock:
            points = _load_locked()
            points.append(point)
            _save_locked(points)
        return True
    except Exception:
        logger.debug("expiry learning capture skipped", exc_info=True)
        return False


async def flush() -> dict:
    """Upload queued points to Forager, best effort.

    Sends one batch (up to _BATCH_SIZE points) to POST
    {cloud_base_url}/api/learn/expiry with no authentication and no
    identifying headers. On success the sent points are removed from the
    queue; on any failure they stay put and the next pass retries. If sharing
    has been turned off, the queue is discarded instead of sent (the user
    withdrew consent). Never raises.
    """
    from ..config import settings
    if not settings.share_expiry_learning:
        clear_queue()
        return {"sent": 0}
    base = (settings.cloud_base_url or "").rstrip("/")
    if not base:
        return {"sent": 0}
    with _lock:
        points = _load_locked()
    if not points:
        return {"sent": 0}
    batch = points[:_BATCH_SIZE]
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(f"{base}/api/learn/expiry",
                                  json={"points": batch})
    except Exception:
        return {"sent": 0}  # unreachable: retry on a later pass
    if r.status_code >= 300:
        # A validation rejection (4xx) will never succeed on retry, so those
        # points are dropped; a server-side hiccup (5xx) is retried later.
        if 400 <= r.status_code < 500 and r.status_code != 429:
            _remove_points(batch)
        return {"sent": 0}
    _remove_points(batch)
    return {"sent": len(batch)}


def _remove_points(sent: list[dict]) -> None:
    """Drop the first occurrence of each sent point from the queue."""
    with _lock:
        points = _load_locked()
        for p in sent:
            try:
                points.remove(p)
            except ValueError:
                pass
        _save_locked(points)
