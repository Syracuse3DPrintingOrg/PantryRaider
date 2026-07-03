"""Pantry audit session (FoodAssistant-ugku).

A location-scoped, read-only stock count. The user locks audit mode to one
storage location, then scans the items physically there. Each scan is recorded
as "seen" against the active session; nothing is written to Grocy. A kiosk page
compares what has been scanned to the location's expected Grocy stock so
discrepancies (missing items that were not scanned, unexpected items that do not
belong here) are obvious.

The session is persisted to a small state file under data_dir, the same way the
scanner mode is (FoodAssistant-60hl): a main server running multiple uvicorn
workers must see the same session from every worker, or a forwarded audit scan
handled by one worker lands in a session another worker started. Reads check
the file's mtime and only re-parse when it changed, so the per-scan cost is one
stat call. A side effect worth keeping: the session now survives an app
restart, which is friendlier than losing a half-finished count. If data_dir is
not writable (tests, a read-only mount) the module quietly degrades to the old
process-local in-memory behavior.

The matching logic is kept pure and unit-testable (no network), and the
expected stock is passed in by the caller so this module never talks to Grocy
itself.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path


def normalize(name: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation for loose name matching.

    A scanned barcode resolves to a product name (from the barcode lookup), and
    the Grocy stock list carries product names too, but the two can differ in
    case, brand prefix, or punctuation. Normalizing both sides lets a scan match
    an expected item without an exact string compare."""
    if not name:
        return ""
    n = name.casefold()
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


# A session holds: the locked location, the expected items (a snapshot of the
# location's Grocy stock taken at start), and the scans recorded so far. Each
# scanned entry keeps its raw name plus a normalized key and a count, so a
# repeated scan bumps the count rather than duplicating. _state is the
# in-process view of the shared state file; _mtime is the file mtime it
# corresponds to (None until the file has been seen) so a read can skip
# re-parsing an unchanged file.
_state: dict = {"active": False}
_mtime: int | None = None


def _state_file() -> Path:
    # Resolved per call (not at import) so tests that repoint data_dir work.
    from ..config import settings
    return Path(settings.data_dir) / "audit_session.json"


def _load() -> None:
    """Refresh the in-process session from the state file if it changed on disk."""
    global _mtime
    try:
        sf = _state_file()
        mtime = sf.stat().st_mtime_ns
    except OSError:
        return  # no file yet (fresh install, or unwritable data_dir)
    if mtime == _mtime:
        return
    try:
        data = json.loads(sf.read_text())
    except (OSError, ValueError):
        return  # a torn or corrupt file never breaks a scan; keep what we have
    _mtime = mtime
    if isinstance(data, dict) and "active" in data:
        _state.clear()
        _state.update(data)


def _save() -> None:
    """Write the current session to the state file (atomic replace, best effort)."""
    global _mtime
    sf = _state_file()
    try:
        tmp = sf.with_name(sf.name + ".tmp")
        tmp.write_text(json.dumps(_state))
        os.replace(tmp, sf)
        _mtime = sf.stat().st_mtime_ns
    except OSError:
        pass  # data_dir not writable: fall back to process-local behavior


def is_active() -> bool:
    _load()
    return bool(_state.get("active"))


def get_location() -> str | None:
    return _state.get("location") if is_active() else None


def start(location: str, expected: list[dict] | None = None) -> dict:
    """Begin an audit session locked to a storage location.

    `expected` is the location's current Grocy stock (list of entries with at
    least a "name"); the caller fetches it so this stays network-free. Starting a
    new session replaces any session already in progress."""
    exp = []
    for e in (expected or []):
        nm = (e.get("name") or "").strip()
        if not nm:
            continue
        exp.append({
            "name": nm,
            "key": normalize(nm),
            "amount": e.get("amount"),
            "days_remaining": e.get("days_remaining"),
        })
    _state.clear()
    _state.update({
        "active": True,
        "location": location,
        "expected": exp,
        "scanned": {},   # key -> {"name", "key", "count", "barcode"}
        "started_at": datetime.now(timezone.utc).isoformat(),
    })
    _save()
    return status()


def record_scan(name: str, barcode: str | None = None) -> dict:
    """Record one scanned item against the active session, by resolved name.

    Returns a small result describing the match: "matched" when the name lines up
    with an expected item, "unexpected" when it does not belong at this location.
    A repeat scan of the same item bumps its count. Raises if no session is
    active so the caller can surface that the user must start audit mode first."""
    if not is_active():
        raise RuntimeError("no audit session is active")
    nm = (name or "").strip()
    key = normalize(nm)
    if not key:
        return {"status": "ignored", "reason": "empty name"}
    scanned = _state["scanned"]
    entry = scanned.get(key)
    if entry:
        entry["count"] += 1
        if barcode and not entry.get("barcode"):
            entry["barcode"] = barcode
    else:
        entry = {"name": nm, "key": key, "count": 1, "barcode": barcode}
        scanned[key] = entry
    _save()
    expected_keys = {e["key"] for e in _state["expected"]}
    match = "matched" if key in expected_keys else "unexpected"
    return {"status": match, "name": nm, "count": entry["count"], "location": _state["location"]}


def status() -> dict:
    """The current audit picture: expected vs scanned, with derived lists.

    - expected: the location's stock snapshot, each flagged seen/not seen
    - scanned: what has been scanned, each flagged expected/unexpected
    - missing: expected items not yet scanned (likely gone, or moved)
    - unexpected: scanned items that do not belong at this location
    """
    if not is_active():
        return {"active": False, "location": None, "expected": [], "scanned": [],
                "missing": [], "unexpected": []}
    expected = _state["expected"]
    scanned = _state["scanned"]
    scanned_keys = set(scanned.keys())
    expected_keys = {e["key"] for e in expected}

    expected_out = [{
        "name": e["name"],
        "amount": e.get("amount"),
        "days_remaining": e.get("days_remaining"),
        "seen": e["key"] in scanned_keys,
        "scanned_count": scanned.get(e["key"], {}).get("count", 0),
    } for e in expected]

    scanned_out = [{
        "name": s["name"],
        "count": s["count"],
        "expected": s["key"] in expected_keys,
    } for s in scanned.values()]

    missing = [e["name"] for e in expected if e["key"] not in scanned_keys]
    unexpected = [s["name"] for s in scanned.values() if s["key"] not in expected_keys]

    return {
        "active": True,
        "location": _state["location"],
        "started_at": _state.get("started_at"),
        "expected": expected_out,
        "scanned": scanned_out,
        "missing": missing,
        "unexpected": unexpected,
        "counts": {
            "expected": len(expected),
            "seen": len([e for e in expected_out if e["seen"]]),
            "missing": len(missing),
            "unexpected": len(unexpected),
        },
    }


def stop() -> dict:
    """End the audit session. Returns a final status snapshot, then clears."""
    final = status()
    _state.clear()
    _state["active"] = False
    _save()
    return final


def reset() -> None:
    """Clear any session and drop the state file (used by tests)."""
    global _mtime
    _state.clear()
    _state["active"] = False
    _mtime = None
    try:
        _state_file().unlink(missing_ok=True)
    except OSError:
        pass
