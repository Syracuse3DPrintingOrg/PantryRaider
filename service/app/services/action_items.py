"""Persistent Action Items (notifications) store (FoodAssistant-iut3).

Action items are durable, user-actionable notifications: "X expired, archive or
snooze?", "save tonight's dinner to leftovers?". Unlike the transient on-screen
HA event toasts (services/ha_events.py), these live in the database so they
survive a restart and can be worked through from the Pending page inbox.

Each item has a ``kind``, a ``status`` (open / snoozed / archived / done), an
optional ``dedupe_key`` so a regenerated item (the same expired product on the
next poll) updates its row instead of piling up, and a JSON ``payload`` carrying
the context a quick action needs (the Grocy item id, a recipe title, ...).

The functions take a SQLAlchemy session so they compose with the request-scoped
``get_db`` dependency, exactly like the pending-items store.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..models.db_models import ActionItem

# Item kinds. Kept as plain strings (not an enum) so a newer generator can add a
# kind without a migration; the UI falls back to a generic look for unknown ones.
KIND_FOOD_EXPIRED = "food_expired"
KIND_LEFTOVER_PROMPT = "leftover_prompt"
KIND_GENERIC = "generic"

_VALID_LEVELS = ("info", "success", "warning", "error")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _row_dict(row: ActionItem) -> dict:
    payload = {}
    if row.payload:
        try:
            payload = json.loads(row.payload)
        except (ValueError, TypeError):
            payload = {}
    return {
        "id": row.id,
        "kind": row.kind,
        "title": row.title,
        "body": row.body or "",
        "status": row.status,
        "snooze_until": row.snooze_until,
        "dedupe_key": row.dedupe_key,
        "level": row.level or "info",
        "payload": payload,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def create(db: Session, kind: str, title: str, *, body: str = "",
           dedupe_key: str | None = None, level: str = "info",
           payload: dict | None = None) -> dict:
    """Create an action item, or refresh an existing one with the same
    ``dedupe_key``.

    Dedupe is what keeps the inbox sane: the expired-food generator can run every
    poll and call create() for each expiring product, but a product that already
    has an item only refreshes that row's content. The row's STATUS is never
    touched here: a snooze stays snoozed until it is due (list_active reveals it
    then), and an archive or done sticks for as long as the same occurrence
    persists. Reviving here undid every snooze and archive on the very next
    inbox poll, because an expired product stays expired (FoodAssistant-wf62).
    A resolved occurrence retires its dedupe key (see sync_food_expired), so a
    later recurrence creates a fresh open item. Returns the row dict.
    """
    lvl = level if level in _VALID_LEVELS else "info"
    body = body or ""
    payload_json = json.dumps(payload) if payload else None
    now = _iso(_now())

    existing = None
    if dedupe_key:
        existing = (
            db.query(ActionItem)
            .filter(ActionItem.dedupe_key == dedupe_key)
            .order_by(ActionItem.id.desc())
            .first()
        )
    if existing is not None:
        existing.title = title
        existing.body = body
        existing.level = lvl
        existing.kind = kind
        existing.payload = payload_json
        existing.updated_at = now
        db.commit()
        db.refresh(existing)
        return _row_dict(existing)

    row = ActionItem(
        kind=kind, title=title, body=body, status="open", level=lvl,
        dedupe_key=dedupe_key, payload=payload_json, created_at=now, updated_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _row_dict(row)


def list_active(db: Session) -> list[dict]:
    """Open items plus any snoozed item whose snooze has elapsed, newest first.

    Archived/done items and not-yet-due snoozed items are excluded, so the inbox
    shows exactly what needs attention now.
    """
    now = _iso(_now())
    rows = (
        db.query(ActionItem)
        .filter(ActionItem.status.in_(("open", "snoozed")))
        .order_by(ActionItem.created_at.desc(), ActionItem.id.desc())
        .all()
    )
    out = []
    for r in rows:
        if r.status == "snoozed" and (r.snooze_until or "") > now:
            continue  # still snoozed: hide until it is due
        out.append(_row_dict(r))
    return out


def count_active(db: Session) -> int:
    """How many items need attention now (drives the inbox badge)."""
    return len(list_active(db))


def _set_status(db: Session, item_id: int, status: str,
                snooze_until: str | None = None) -> dict | None:
    row = db.get(ActionItem, item_id)
    if row is None:
        return None
    row.status = status
    row.snooze_until = snooze_until
    row.updated_at = _iso(_now())
    db.commit()
    db.refresh(row)
    return _row_dict(row)


def archive(db: Session, item_id: int) -> dict | None:
    """Dismiss an item (it stays in history but leaves the inbox)."""
    return _set_status(db, item_id, "archived")


def resolve(db: Session, item_id: int) -> dict | None:
    """Mark an item handled (a quick action was taken)."""
    return _set_status(db, item_id, "done")


def snooze(db: Session, item_id: int, hours: float = 24.0) -> dict | None:
    """Hide an item until ``hours`` from now, then it returns to the inbox."""
    hours = max(0.0, float(hours))
    until = _iso(_now() + timedelta(hours=hours))
    return _set_status(db, item_id, "snoozed", snooze_until=until)


def archive_all(db: Session) -> int:
    """Archive every currently active item at once. Returns how many were
    archived. A food-expired item that is still expiring will reappear on the
    next sweep (its dedupe key revives it), which is intended."""
    now = _iso(_now())
    rows = (
        db.query(ActionItem)
        .filter(ActionItem.status.in_(("open", "snoozed")))
        .all()
    )
    n = 0
    for r in rows:
        # Skip snoozed-not-yet-due rows so "Archive all" only clears the visible
        # inbox, matching list_active().
        if r.status == "snoozed" and (r.snooze_until or "") > now:
            continue
        r.status = "archived"
        r.updated_at = now
        n += 1
    if n:
        db.commit()
    return n


def get(db: Session, item_id: int) -> dict | None:
    row = db.get(ActionItem, item_id)
    return _row_dict(row) if row is not None else None


def expired_dedupe_key(product_id) -> str:
    """Stable dedupe key for a food-expired item, keyed by the Grocy product."""
    return f"{KIND_FOOD_EXPIRED}:{product_id}"


def sync_food_expired(db: Session, expiring_items: list[dict]) -> int:
    """Raise/refresh a food-expired action item per expired-or-today product, and
    auto-archive items whose product is no longer expired (FoodAssistant-7zzv).

    ``expiring_items`` is the Grocy expiring list filtered to ``days_remaining
    <= 0`` (the caller does the fetch so this stays pure of network and testable).
    Returns the number of active expired items. A snoozed or archived item for a
    still-expired product keeps its status (create only refreshes content); a
    snooze that elapses reappears via list_active. When a product stops being
    expired, its items are auto-archived AND their dedupe key is retired, so a
    genuinely new expiry later raises a fresh alert even if the old item was
    archived or resolved (FoodAssistant-wf62).
    """
    seen: set[str] = set()
    for it in expiring_items or []:
        prod = it.get("product") or {}
        pid = it.get("product_id") or prod.get("id")
        if pid is None:
            continue
        days = int(it.get("days_remaining", 0) or 0)
        if days > 0:
            continue
        name = prod.get("name") or it.get("name") or "An item"
        key = expired_dedupe_key(pid)
        seen.add(key)
        title = f"{name} has expired" if days < 0 else f"{name} expires today"
        level = "error" if days < 0 else "warning"
        bb = it.get("best_before_date", "")
        amt = it.get("amount")
        body = f"Best-before {bb}." + (f" Quantity {amt}." if amt is not None else "")
        create(db, KIND_FOOD_EXPIRED, title, body=body.strip(), dedupe_key=key,
               level=level, payload={"product_id": pid, "name": name,
                                     "days_remaining": days, "best_before_date": bb})
    # The occurrence resolved (consumed, removed, or its date pushed out):
    # auto-archive still-visible items so the inbox drops them, and retire the
    # dedupe key on EVERY row for that product (whatever its status) so the
    # next expiry starts a fresh item instead of being swallowed by an old
    # archived or resolved row.
    stale = (
        db.query(ActionItem)
        .filter(ActionItem.kind == KIND_FOOD_EXPIRED,
                ActionItem.dedupe_key.isnot(None))
        .all()
    )
    changed = False
    for row in stale:
        if row.dedupe_key in seen:
            continue
        if row.status in ("open", "snoozed"):
            row.status = "archived"
        row.dedupe_key = None
        row.updated_at = _iso(_now())
        changed = True
    if changed:
        db.commit()
    return len(seen)


async def refresh_food_expired(db: Session) -> int:
    """Fetch the Grocy expiring list and sync food-expired action items.

    Best-effort: a Grocy error leaves the inbox untouched (returns -1) rather
    than wiping items on a transient outage."""
    from .grocy import GrocyClient
    try:
        items = await GrocyClient().get_expiring(days=0)
    except Exception:
        return -1
    return sync_food_expired(db, items)
