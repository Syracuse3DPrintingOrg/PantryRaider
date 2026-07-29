"""Best-by date provenance (FoodAssistant-cidz).

Grocy stores a best-by date but not where it came from: a date the user typed
themselves, a category-rule estimate, or an AI guess. The label renderer
already knows how to badge a date honestly ("est." / "AI" / nothing, see
services/label_render.py), but it needs to be TOLD the source. This module is
the small local record that remembers it, without touching Grocy's schema
(existing installs are production; see AGENTS.md).

Keyed by Grocy product id, with the item's normalized name kept as a fallback
key for a lookup made before (or without) an id. A record also carries the
best-by date it was made for: a lookup only trusts the stored source when that
date still matches the item's CURRENT best-by. If the date has since changed
(the user edited it directly in Grocy, bypassing the app), the provenance is
stale and the lookup quietly falls back to "manual" (no badge) rather than
badging a guess that no longer applies to today's date.

Persisted the same way as services/timers.py and services/scanner_mode.py: a
small state file under data_dir, atomic writes (temp file + os.replace), mtime
cached reads so a lookup costs one stat call, and a silent in-memory fallback
when data_dir is not writable (tests, a read-only mount).
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Optional

from .state_lock import state_write_lock

# The only sources the label renderer knows how to badge (see
# label_render.DateSource). Anything else is not worth remembering.
_KNOWN_SOURCES = ("manual", "default", "llm", "community")

_lock = threading.Lock()
# In-process view of the state file: {key: {"source": str, "date": str}}.
_records: dict[str, dict] = {}
_mtime: int | None = None


def _state_file() -> Path:
    # Resolved per call (not at import) so tests that repoint data_dir work.
    from ..config import settings
    return Path(settings.data_dir) / "best_by_provenance.json"


def normalize_name(name: str) -> str:
    """Fold a product name down to a stable fallback key: lowercase, collapsed
    whitespace. Pure and fully testable."""
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _id_key(product_id: int) -> str:
    return f"id:{int(product_id)}"


def _name_key(name: str) -> Optional[str]:
    norm = normalize_name(name)
    return f"name:{norm}" if norm else None


def _load_locked() -> None:
    """Refresh the in-process records from the state file if it changed on
    disk. Caller holds the lock."""
    global _records, _mtime
    try:
        sf = _state_file()
        mtime = sf.stat().st_mtime_ns
    except OSError:
        return  # no file yet (fresh install, or unwritable data_dir)
    if mtime == _mtime:
        return
    try:
        data = json.loads(sf.read_text())
        rows = data.get("records", {})
        if not isinstance(rows, dict):
            rows = {}
    except (OSError, ValueError, AttributeError):
        return  # a torn or corrupt file never breaks a lookup; keep what we have
    _mtime = mtime
    _records = rows


def _save_locked() -> None:
    """Write the records to the state file (atomic replace, best effort).
    Caller holds the lock."""
    global _mtime
    sf = _state_file()
    try:
        tmp = sf.with_name(sf.name + ".tmp")
        tmp.write_text(json.dumps({"records": _records}))
        os.replace(tmp, sf)
        _mtime = sf.stat().st_mtime_ns
    except OSError:
        pass  # data_dir not writable: fall back to process-local behavior


def record(product_id: Optional[int], name: str, source: str, date: str) -> None:
    """Remember how ``date`` (YYYY-MM-DD) was worked out for an item.

    Stored under the product id when known, and always under the normalized
    name too as a fallback key, so a lookup before an id exists (or made with
    only a name) still finds it. Unknown sources and blank dates are ignored:
    there is nothing honest to badge without both.
    """
    source = source if source in _KNOWN_SOURCES else "manual"
    date = (date or "").strip()
    if not date or source == "manual":
        # "manual" is the no-badge default anyway; skip the write and let a
        # stale/missing lookup fall back to it for free.
        return
    entry = {"source": source, "date": date}
    # The cross-process lock matters as much as the in-process one: this is a
    # read-modify-write of the WHOLE record set, and a server runs several
    # uvicorn workers. Without it two workers each load, each add their own
    # entry, and the second write silently drops the first (FoodAssistant-dxl0).
    with _lock, state_write_lock(_state_file()):
        _load_locked()
        if product_id is not None:
            _records[_id_key(product_id)] = entry
        nk = _name_key(name)
        if nk is not None:
            _records[nk] = entry
        _save_locked()


def _fresh(entry: Optional[dict], current_date: str) -> Optional[str]:
    if not entry:
        return None
    if (entry.get("date") or "").strip() != (current_date or "").strip():
        return None  # stale: the date on record no longer matches
    source = entry.get("source")
    return source if source in _KNOWN_SOURCES else None


def lookup(product_id: Optional[int], name: str, current_date: str) -> str:
    """The best-by source for an item, or "manual" when nothing fresh is on
    record (never recorded, or the recorded date is stale). ``current_date``
    is the item's best-by date right now (YYYY-MM-DD); a mismatch against the
    date the record was made for is treated as a user edit made outside the
    app, so the badge is dropped rather than shown for a date it no longer
    describes.
    """
    if not (current_date or "").strip():
        return "manual"
    with _lock:
        _load_locked()
        if product_id is not None:
            found = _fresh(_records.get(_id_key(product_id)), current_date)
            if found:
                return found
        nk = _name_key(name)
        if nk is not None:
            found = _fresh(_records.get(nk), current_date)
            if found:
                return found
    return "manual"


def clear_all() -> None:
    """Drop every record (test cleanup)."""
    global _records, _mtime
    with _lock:
        _records = {}
        _mtime = None
        try:
            _state_file().unlink(missing_ok=True)
        except OSError:
            pass
