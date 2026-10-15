"""Pi system warnings: action-item sync/retire and the timer-chips gate
(FoodAssistant-y06w, FoodAssistant-kfda)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.database import SessionLocal  # noqa: E402
from app.models.db_models import ActionItem  # noqa: E402
from app.services import action_items as ai  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_table():
    db = SessionLocal()
    db.query(ActionItem).delete()
    db.commit()
    db.close()
    yield
    db = SessionLocal()
    db.query(ActionItem).delete()
    db.commit()
    db.close()


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _warn(key, live=True, message="msg"):
    return {"key": key, "message": message, "live": live}


# -- sync_system_warnings: raise, dedupe, retire -----------------------------

def test_sync_raises_one_item_per_condition(db):
    n = ai.sync_system_warnings(db, [_warn("undervoltage"), _warn("disk")])
    assert n == 2
    items = ai.list_active(db)
    assert len(items) == 2
    by_key = {i["dedupe_key"]: i for i in items}
    uv = by_key[ai.system_warning_dedupe_key("undervoltage")]
    assert uv["title"] == "Undervoltage detected"
    assert uv["level"] == "error"
    # The power guidance link rides in the payload for the inbox to render.
    assert uv["payload"]["url"] == ai.POWER_GUIDE_URL
    assert "power supply" in uv["body"].lower() or "power" in uv["body"].lower()
    dk = by_key[ai.system_warning_dedupe_key("disk")]
    assert dk["level"] == "warning"
    assert "url" not in dk["payload"]


def test_sync_dedupes_across_polls(db):
    ai.sync_system_warnings(db, [_warn("undervoltage")])
    ai.sync_system_warnings(db, [_warn("undervoltage")])
    ai.sync_system_warnings(db, [_warn("undervoltage")])
    assert len(ai.list_active(db)) == 1


def test_sync_sticky_wording_differs_from_live(db):
    ai.sync_system_warnings(db, [_warn("undervoltage", live=False)])
    item = ai.list_active(db)[0]
    assert "since" in item["body"].lower()
    # The same condition turning live refreshes the same row's copy.
    ai.sync_system_warnings(db, [_warn("undervoltage", live=True)])
    items = ai.list_active(db)
    assert len(items) == 1
    assert items[0]["payload"]["live"] is True


def test_sync_retires_cleared_condition(db):
    ai.sync_system_warnings(db, [_warn("temperature")])
    assert len(ai.list_active(db)) == 1
    # Condition cleared: item auto-archives and its dedupe key is retired.
    n = ai.sync_system_warnings(db, [])
    assert n == 0
    assert ai.list_active(db) == []
    rows = db.query(ActionItem).all()
    assert rows[0].status == "archived"
    assert rows[0].dedupe_key is None
    # A later recurrence starts a fresh open item, not a revived archive.
    ai.sync_system_warnings(db, [_warn("temperature")])
    active = ai.list_active(db)
    assert len(active) == 1
    assert active[0]["id"] != rows[0].id


def test_sync_archived_by_user_stays_archived_while_active(db):
    ai.sync_system_warnings(db, [_warn("throttled")])
    item = ai.list_active(db)[0]
    ai.archive(db, item["id"])
    # Still throttling on the next poll: the archive sticks (create never
    # touches status), so the user is not re-nagged for the same occurrence.
    ai.sync_system_warnings(db, [_warn("throttled")])
    assert ai.list_active(db) == []


def test_sync_unknown_condition_still_surfaces(db):
    ai.sync_system_warnings(db, [_warn("mystery", message="Something odd")])
    item = ai.list_active(db)[0]
    assert item["title"] == "Device warning"
    assert item["body"] == "Something odd"
    assert item["level"] == "warning"


def test_sync_does_not_touch_other_kinds(db):
    ai.create(db, ai.KIND_FOOD_EXPIRED, "Milk has expired",
              dedupe_key=ai.expired_dedupe_key(7))
    ai.sync_system_warnings(db, [])
    items = ai.list_active(db)
    assert len(items) == 1
    assert items[0]["kind"] == ai.KIND_FOOD_EXPIRED


# -- timer_chips_hidden: the chips setting gate (FoodAssistant-kfda) ---------

def test_timer_chips_auto_hides_only_at_large_scales():
    from app.config import timer_chips_hidden
    assert timer_chips_hidden("auto", "large") is True
    assert timer_chips_hidden("auto", "xlarge") is True
    assert timer_chips_hidden("auto", "normal") is False
    assert timer_chips_hidden("auto", "small") is False
    # Unlike nav_visibility's auto, no Stream Deck is involved: scale alone.
    assert timer_chips_hidden("auto", "") is False


def test_timer_chips_explicit_overrides_win():
    from app.config import timer_chips_hidden
    assert timer_chips_hidden("off", "normal") is True
    assert timer_chips_hidden("on", "xlarge") is False
