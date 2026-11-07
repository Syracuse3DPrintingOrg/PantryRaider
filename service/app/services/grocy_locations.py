"""Opt-in tidy-ups of the Grocy locations older installs were left with.

Pantry Raider used to file fridge stock in a "Refrigerator" of its own next to
the "Fridge" a fresh Grocy ships with, and created its "Freezer" without
Grocy's freezer flag. New installs no longer do either (see
GrocyClient.ensure_location). An install that already has them gets one inbox
item for each, and nothing changes in Grocy until its button is pressed:

* Two fridges: move everything in Fridge to Refrigerator, point the products
  that live in Fridge at Refrigerator, then hide Fridge (GrocyClient.
  merge_location). Only Grocy's own name for a fridge counts (the
  _LOCATION_ALIASES of grocy.py), never a place a custom storage category
  owns, so a "Garage Fridge" is left alone.
* Freezer flag: mark Freezer as a freezer in Grocy.

Each item is raised once. Done or archived, it stays closed, and an open one
closes itself once its condition no longer holds (fixed by hand in Grocy, say).
All of it runs on the server that owns the Grocy connection: a Bandit's inbox
is its main server's, and the buttons refuse to run on a Bandit.

Most installs never need either item. Once a read of Grocy finds nothing to
offer, that is noted in a small state file under data_dir and Grocy is asked
again at most once a day, rather than on every inbox refresh.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import settings
from ..models.db_models import ActionItem
from ..storage_categories import custom_categories, location_for
from . import action_items
from .state_lock import state_write_lock
from .state_writer import write_replace

logger = logging.getLogger(__name__)

KIND_TWO_FRIDGES = "grocy_two_fridges"
KIND_FREEZER_FLAG = "grocy_freezer_flag"
DEDUPE_TWO_FRIDGES = "grocy-two-fridges-v1"
DEDUPE_FREEZER_FLAG = "grocy-freezer-flag-v1"

SATELLITE_MSG = "Grocy is managed on the main server."

# After a read that found nothing to offer, Grocy is asked again this long after.
CLEAN_RECHECK_SECS = 24 * 3600

# One merge or flag at a time: a second press (another screen, a double tap)
# waits and then finds the work done.
_lock = asyncio.Lock()


def _flag(row: dict, key: str, default: int = 0) -> bool:
    value = row.get(key)
    return str(default if value is None else value) not in ("0", "", "False", "false")


def _active(row: dict) -> bool:
    return _flag(row, "active", 1)


def _named(locations: list[dict], name: str | None) -> dict | None:
    low = (name or "").strip().lower()
    if not low:
        return None
    return next((r for r in locations or []
                 if str(r.get("name") or "").strip().lower() == low), None)


def two_fridges(locations: list[dict]) -> dict | None:
    """The fridge to merge and where to, or None. Pure.

    Pantry Raider's refrigerated place and Grocy's own fridge (the alias
    ensure_location would have used), both active and neither a freezer in
    Grocy. A place a custom storage category files stock in is never merged,
    and neither is one from a Grocy too old to hide a location (its rows
    carry no ``active`` field), since the merge would end with two fridges
    still showing.
    """
    from .grocy import _LOCATION_ALIASES
    to_name = location_for("refrigerated")
    to = _named(locations, to_name)
    if to is None or not _active(to) or _flag(to, "is_freezer"):
        return None
    owned = {c["location"].strip().lower() for c in custom_categories()}
    for alias in _LOCATION_ALIASES.get((to_name or "").lower(), ()):
        other = _named(locations, alias)
        if (other is None or str(other.get("id")) == str(to.get("id"))
                or "active" not in other
                or not _active(other) or _flag(other, "is_freezer")
                or str(other.get("name")).strip().lower() in owned):
            continue
        return {"from_id": int(other["id"]), "from_name": str(other["name"]),
                "to_id": int(to["id"]), "to_name": str(to["name"])}
    return None


def unflagged_freezer(locations: list[dict]) -> dict | None:
    """Pantry Raider's freezer when Grocy does not know it is one, or None. Pure."""
    row = _named(locations, location_for("frozen"))
    if row is None or not _active(row) or _flag(row, "is_freezer"):
        return None
    return {"id": int(row["id"]), "name": str(row["name"])}


def merge_blocker(locations: list[dict]) -> dict:
    """Why two_fridges() found nothing to merge, for the Merge button. Pure.

    Returns {"ok": True, "message"} when Grocy really has one fridge now, or
    {"ok": False, "error"} when both places are still there but one of them
    can no longer take part, so the button never claims a merge it did not do.
    """
    from .grocy import _LOCATION_ALIASES
    to_name = location_for("refrigerated") or "Refrigerator"
    to = _named(locations, to_name)
    if to is None:
        return {"ok": False, "error": f"Grocy has no location named {to_name} any "
                "more, so nothing was merged."}
    b = str(to["name"])
    if not _active(to):
        return {"ok": False, "error": f"{b} is hidden in Grocy, so nothing was "
                "merged into it. Show it again in Grocy, then press Merge again."}
    if _flag(to, "is_freezer"):
        return {"ok": False, "error": f"{b} is marked as a freezer in Grocy, so "
                "nothing was merged into it."}
    owned = {c["location"].strip().lower() for c in custom_categories()}
    for alias in _LOCATION_ALIASES.get(to_name.lower(), ()):
        other = _named(locations, alias)
        if other is None or str(other.get("id")) == str(to.get("id")):
            continue
        a = str(other["name"])
        if "active" not in other:
            return {"ok": False, "error": "This Grocy is too old to hide a location, "
                    f"so {a} was left as it is."}
        if not _active(other):
            return {"ok": True, "message": f"{a} is already hidden in Grocy."}
        if _flag(other, "is_freezer"):
            return {"ok": False, "error": f"{a} is marked as a freezer in Grocy, so "
                    "it was left as it is."}
        if a.strip().lower() in owned:
            return {"ok": False, "error": "One of your storage categories keeps food "
                    f"in {a}, so it was left as it is."}
    return {"ok": True, "message": "Grocy already has just one fridge."}


def freezer_state(locations: list[dict]) -> dict:
    """Where Pantry Raider's freezer stands in Grocy, for the Mark as freezer
    button. Pure. {"ok": True, "message"} once it is flagged, otherwise
    {"ok": False, "error"} saying what was found."""
    want = location_for("frozen") or "Freezer"
    row = _named(locations, want)
    if row is None:
        return {"ok": False, "error": f"Grocy has no location named {want} any more, "
                "so nothing was changed."}
    name = str(row["name"])
    if not _active(row):
        return {"ok": False, "error": f"{name} is hidden in Grocy, so nothing was "
                "changed. Show it again in Grocy, then press Mark as freezer again."}
    if _flag(row, "is_freezer"):
        return {"ok": True, "message": f"{name} is now a freezer in Grocy."}
    return {"ok": False, "error": f"Grocy answered, but {name} is still not marked "
            "as a freezer there. Try again."}


def merge_text(found: dict) -> tuple[str, str]:
    a, b = found["from_name"], found["to_name"]
    return (f"Your Grocy has two fridges, {a} and {b}. Merge them?",
            f"Pantry Raider keeps fridge food in {b}, and Grocy came with its own "
            f"{a}. Merging moves everything in {a} to {b} with the same amounts, "
            f"best-by dates and opened items, sets products that belong in {a} to "
            f"{b}, then hides {a} in Grocy. Nothing is deleted.")


def freezer_text(found: dict) -> tuple[str, str]:
    name = found["name"]
    return (f"Mark {name} as a freezer in Grocy?",
            f"Grocy does not know {name} is a freezer. Once it does, Grocy sets its "
            f"own best-by date when food goes into or comes out of {name}, but only "
            "for products where you set freezing or thawing days in Grocy. Other "
            "products keep the dates Pantry Raider gives them. Learning from your "
            f"kitchen also leaves out food stocked straight into {name}, since it "
            "keeps longer there.")


def _rows(db: Session, key: str) -> list[ActionItem]:
    return db.query(ActionItem).filter(ActionItem.dedupe_key == key).all()


def settled(db: Session) -> bool:
    """True once both items were raised and closed: Grocy need not be asked again."""
    for key in (DEDUPE_TWO_FRIDGES, DEDUPE_FREEZER_FLAG):
        rows = _rows(db, key)
        if not rows or any(r.status in ("open", "snoozed") for r in rows):
            return False
    return True


def _close(db: Session, key: str) -> int:
    """Mark the item done. The dedupe key stays, so it never comes back."""
    n = 0
    for row in _rows(db, key):
        if row.status in ("open", "snoozed"):
            row.status = "done"
            row.updated_at = action_items._iso(action_items._now())
            n += 1
    if n:
        db.commit()
    return n


def _raise(db: Session, kind: str, key: str, title: str, body: str,
           payload: dict) -> None:
    """Raise the item, or refresh its text. An item that already says the same
    is left alone, so a refresh every minute does not rewrite it."""
    rows = _rows(db, key)
    latest = max(rows, key=lambda r: r.id) if rows else None
    if (latest is not None and latest.kind == kind and latest.title == title
            and (latest.body or "") == body
            and latest.payload == json.dumps(payload)):
        return
    action_items.create(db, kind, title, body=body, dedupe_key=key, level="info",
                        payload=payload)


def sync(db: Session, locations: list[dict]) -> int:
    """Raise, refresh or close the two items for Grocy's ``locations``.
    Returns how many apply now. The caller does the fetch."""
    count = 0
    fridges = two_fridges(locations)
    if fridges:
        title, body = merge_text(fridges)
        _raise(db, KIND_TWO_FRIDGES, DEDUPE_TWO_FRIDGES, title, body, fridges)
        count += 1
    else:
        _close(db, DEDUPE_TWO_FRIDGES)
    freezer = unflagged_freezer(locations)
    if freezer:
        title, body = freezer_text(freezer)
        _raise(db, KIND_FREEZER_FLAG, DEDUPE_FREEZER_FLAG, title, body, freezer)
        count += 1
    else:
        _close(db, DEDUPE_FREEZER_FLAG)
    return count


def _state_file() -> Path:
    # Resolved per call so tests that repoint data_dir work.
    return Path(settings.data_dir) / "grocy_locations.json"


def _checked_clean_at() -> float | None:
    """When a read of Grocy last found nothing to offer, or None."""
    try:
        data = json.loads(_state_file().read_text())
        return float(data["clean_at"])
    except (OSError, ValueError, TypeError, KeyError):
        return None  # no file yet, or a torn one: ask Grocy


def _note_clean(when: float | None) -> None:
    """Record (or with None, forget) a clean read. Best effort: an unwritable
    data_dir only means Grocy is asked on every refresh, as before."""
    sf = _state_file()
    with state_write_lock(sf):
        if when is None:
            if _checked_clean_at() is not None:
                write_replace(sf, None)
            return
        write_replace(sf, json.dumps({"clean_at": when}).encode("utf-8"))


def _due(db: Session, now: float) -> bool:
    """True when Grocy should be read: an item can still change, and no clean
    read happened within the last day."""
    if settled(db):
        return False
    clean = _checked_clean_at()
    return clean is None or not (0 <= now - clean < CLEAN_RECHECK_SECS)


def _sync_and_note(db: Session, locations: list[dict], now: float) -> int:
    count = sync(db, locations)
    _note_clean(now if count == 0 else None)
    return count


async def refresh(db: Session) -> int:
    """Read Grocy's locations and sync the items. Best-effort: on a Bandit,
    once both items are closed, within a day of a read that found nothing to
    offer, or when Grocy does not answer, nothing changes (returns -1). The
    state file and the database are only touched from the thread pool."""
    from fastapi.concurrency import run_in_threadpool
    from .grocy import GrocyClient
    if settings.is_satellite():
        return -1
    now = time.time()
    if not await run_in_threadpool(_due, db, now):
        return -1
    try:
        locations = await GrocyClient().get_locations()
    except Exception:  # noqa: BLE001 - an outage leaves the inbox as it is
        return -1
    return await run_in_threadpool(_sync_and_note, db, locations, now)


def _summary(result: dict, found: dict) -> str:
    a, b = found["from_name"], found["to_name"]
    rows = result.get("moved_rows", 0)
    moved = "Nothing needed moving" if not rows else (
        f"Moved {rows} stock {'entry' if rows == 1 else 'entries'} to {b}")
    text = f"{moved}, and {a} is now hidden in Grocy."
    if result.get("errors"):
        text += " Some products gave trouble on the way. Grocy said: " + " ".join(
            result["errors"])
    return text


def _left_text(result: dict, found: dict) -> str:
    a = found["from_name"]
    left = result.get("left") or []
    parts = []
    if left:
        parts.append(f"{a} was not hidden. Still in {a}: {', '.join(left)}.")
    else:
        parts.append(f"{a} was not hidden: some products still belong in {a} in "
                     "Grocy.")
    if result.get("weighed"):
        parts.append("Grocy cannot transfer a product weighed with its container ("
                     f"{', '.join(result['weighed'])}), so change the location on "
                     "its stock entries in Grocy.")
    if result.get("errors"):
        parts.append("Grocy said: " + " ".join(result["errors"]))
    parts.append("Then press Merge again.")
    return " ".join(parts)


async def merge_fridges(db: Session) -> dict:
    """The Merge button. Returns {"ok", "message"}, or {"ok": False, "error",
    "status"} with the HTTP status the route answers with."""
    from fastapi.concurrency import run_in_threadpool
    from .grocy import GrocyClient, GrocyError, grocy_said
    if settings.is_satellite():
        return {"ok": False, "error": SATELLITE_MSG, "status": 403}
    async with _lock:
        client = GrocyClient()
        try:
            locations = await client.get_locations()
            found = two_fridges(locations)
            if found is None:
                answer = merge_blocker(locations)
                if not answer["ok"]:
                    return {**answer, "status": 409}
                await run_in_threadpool(_close, db, DEDUPE_TWO_FRIDGES)
                return answer
            result = await client.merge_location(found["from_id"], found["to_id"])
        except GrocyError as e:
            return {"ok": False, "status": 502,
                    "error": f"The merge stopped: {grocy_said(e)} Press Merge again "
                             "to carry on."}
        if not result.get("hidden"):
            return {"ok": False, "error": _left_text(result, found), "status": 409,
                    "result": result}
        await run_in_threadpool(_close, db, DEDUPE_TWO_FRIDGES)
        return {"ok": True, "message": _summary(result, found), "result": result}


async def mark_freezer(db: Session) -> dict:
    """The Mark as freezer button. Answers as merge_fridges does."""
    from fastapi.concurrency import run_in_threadpool
    from .grocy import GrocyClient, GrocyError, grocy_said
    if settings.is_satellite():
        return {"ok": False, "error": SATELLITE_MSG, "status": 403}
    async with _lock:
        client = GrocyClient()
        try:
            locations = await client.get_locations()
            found = unflagged_freezer(locations)
            if found is not None:
                await client.mark_location_freezer(found["id"])
                # Say it is done only once Grocy shows the flag.
                locations = await client.get_locations()
        except GrocyError as e:
            return {"ok": False, "status": 502,
                    "error": f"Grocy did not take the change: {grocy_said(e)}"}
        answer = freezer_state(locations)
        if not answer["ok"]:
            return {**answer, "status": 409}
        await run_in_threadpool(_close, db, DEDUPE_FREEZER_FLAG)
        return answer
