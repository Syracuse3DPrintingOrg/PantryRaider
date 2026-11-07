"""Kitchen history (FoodAssistant-i9flm): spoilage and consumption learned
from this install's own Grocy journal.

Covers the pure learners (spoil-age median with its exclusions, consumption
rate with its one-week floor, the run-out forecast), the shorten-only rule,
the cache round trip through a tmp data_dir, and the integration into
apply_defaults: a learned value can shorten a built-in date and is marked
"history", but never lengthens one, never touches freezer storage, and never
overrides the user's own rule.
"""
import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "service"))

from app.config import settings  # noqa: E402
from app.models.food import FoodCategory, FoodItem, StorageType  # noqa: E402
from app.services import community_expiry, kitchen_history as kh  # noqa: E402

TODAY = date(2026, 10, 19)


@pytest.fixture(autouse=True)
def tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "use_kitchen_history", True, raising=False)
    kh.clear_cache()
    community_expiry.clear_cache()
    yield tmp_path
    kh.clear_cache()


def _bought(sid, pid, day, location=1):
    return {"stock_id": sid, "product_id": pid, "transaction_type": "purchase",
            "purchased_date": day.isoformat(), "location_id": location,
            "undone": 0, "amount": 1}


def _spoiled(sid, pid, day):
    return {"stock_id": sid, "product_id": pid, "transaction_type": "consume",
            "spoiled": 1, "undone": 0, "amount": -1,
            "row_created_timestamp": f"{day.isoformat()} 10:00:00"}


def _used(pid, day, amount=1.0, sid="x"):
    return {"stock_id": sid, "product_id": pid, "transaction_type": "consume",
            "spoiled": 0, "undone": 0, "amount": -amount,
            "row_created_timestamp": f"{day.isoformat()} 18:00:00"}


def _spoil_rows(pid, ages, start=date(2026, 9, 1)):
    rows = []
    for i, age in enumerate(ages):
        sid = f"{pid}-{i}"
        bought = start + timedelta(days=i * 10)
        rows += [_bought(sid, pid, bought), _spoiled(sid, pid, bought + timedelta(days=age))]
    return rows


def test_spoil_days_is_the_median_once_there_are_enough_samples():
    out = kh.learn_spoil_days(_spoil_rows(7, [3, 4, 9]))
    assert out == {"7": {"days": 4, "samples": 3}}


def test_too_few_samples_learn_nothing():
    assert kh.learn_spoil_days(_spoil_rows(7, [3, 4])) == {}


def test_freezer_moved_and_undone_entries_are_left_out():
    rows = _spoil_rows(7, [3, 4, 5])
    # A fourth entry bought into the freezer, a fifth moved, a sixth undone.
    d = date(2026, 10, 1)
    rows += [_bought("f", 7, d, location=9), _spoiled("f", 7, d + timedelta(days=60))]
    rows += [_bought("m", 7, d), {"stock_id": "m", "transaction_type": "transfer_to"},
             _spoiled("m", 7, d + timedelta(days=50))]
    undone = _spoiled("u", 7, d + timedelta(days=40))
    undone["undone"] = 1
    rows += [_bought("u", 7, d), undone]
    out = kh.learn_spoil_days(rows, freezer_locations={9})
    assert out["7"] == {"days": 4, "samples": 3}


def test_tosses_after_the_best_by_date_are_not_evidence():
    # Each entry was dated 5 days out and tossed on day 7: the food kept to
    # its date, so nothing is learned (and a shortened date cannot keep
    # shortening itself).
    rows = []
    for i in range(4):
        bought = date(2026, 9, 1) + timedelta(days=i * 10)
        label = (bought + timedelta(days=5)).isoformat()
        toss = _spoiled(f"s{i}", 7, bought + timedelta(days=7))
        toss["best_before_date"] = label
        on_label = dict(_bought(f"s{i}", 7, bought), best_before_date=label)
        rows += [on_label, toss]
    assert kh.learn_spoil_days(rows) == {}
    # Tossed on day 3, before the day-5 label: real early spoilage.
    for row in rows:
        if row["transaction_type"] == "consume":
            bought = date.fromisoformat(row["best_before_date"]) - timedelta(days=5)
            row["row_created_timestamp"] = f"{(bought + timedelta(days=3)).isoformat()} 09:00:00"
    assert kh.learn_spoil_days(rows) == {"7": {"days": 3, "samples": 4}}


def test_the_purchase_label_is_used_when_the_toss_has_none():
    rows = _spoil_rows(7, [6, 6, 6])
    for row in rows:
        if row["transaction_type"] == "purchase":
            day = date.fromisoformat(row["purchased_date"])
            row["best_before_date"] = (day + timedelta(days=4)).isoformat()
    assert kh.learn_spoil_days(rows) == {}


def test_same_day_tosses_are_not_a_shelf_life():
    assert kh.learn_spoil_days(_spoil_rows(7, [0, 0, 0])) == {}
    assert kh.learn_spoil_days(_spoil_rows(7, [0, 2, 2, 2]))["7"] == {
        "days": 2, "samples": 3}


def test_one_toss_of_several_entries_is_one_sample():
    # Three entries from one shopping trip, thrown out together: one sample.
    d = date(2026, 9, 1)
    rows = []
    for i in range(3):
        toss = _spoiled(f"s{i}", 7, d + timedelta(days=2))
        toss["transaction_id"] = "toss-1"
        rows += [_bought(f"s{i}", 7, d), toss]
    assert kh.learn_spoil_days(rows) == {}
    assert kh.learn_spoil_days(rows, min_samples=1)["7"]["samples"] == 1
    # Without transaction ids, entries bought the same day count once.
    for row in rows:
        row.pop("transaction_id", None)
    assert kh.learn_spoil_days(rows, min_samples=1)["7"]["samples"] == 1


def test_the_apps_freezer_counts_as_a_freezer_by_name():
    rows = _spoil_rows(7, [3, 4, 5])
    d = date(2026, 10, 1)
    for i in range(3):
        rows += [_bought(f"f{i}", 7, d + timedelta(days=i), location=9),
                 _spoiled(f"f{i}", 7, d + timedelta(days=i + 1))]
    locations = [{"id": 1, "name": "Refrigerator", "is_freezer": 0},
                 {"id": 9, "name": "Freezer", "is_freezer": 0}]
    doc = kh.build_cache(rows, [{"id": 7, "name": "Peas"}], locations, today=TODAY)
    assert doc["spoil_by_product"]["7"] == {"days": 4, "samples": 3}


def test_consumption_rate_and_its_one_week_floor():
    rows = [_used(5, TODAY - timedelta(days=d)) for d in (20, 10, 0)]
    out = kh.learn_consumption(rows, today=TODAY)
    assert out["5"] == {"per_day": round(3 / 20, 4), "events": 3}
    burst = [dict(_used(6, TODAY), transaction_id=f"t{i}") for i in range(3)]
    assert kh.learn_consumption(burst, today=TODAY)["6"]["per_day"] == round(3 / 7, 4)


def test_consumption_counts_actions_not_journal_rows():
    # One consume that drew from three stock entries writes three rows with
    # one transaction_id: one event, though every amount counts.
    rows = [dict(_used(5, TODAY - timedelta(days=10), sid=f"s{i}"),
                 transaction_id="one") for i in range(3)]
    out = kh.learn_consumption(rows, today=TODAY, min_events=1)
    assert out["5"] == {"per_day": round(3 / 10, 4), "events": 1}
    assert kh.learn_consumption(rows, today=TODAY) == {}
    # Without transaction ids, rows on the same day are one event.
    same_day = [_used(5, TODAY - timedelta(days=2), sid=f"s{i}") for i in range(4)]
    assert kh.learn_consumption(same_day, today=TODAY) == {}


def test_consumption_ignores_spoiled_old_and_sparse_rows():
    rows = [_used(5, TODAY - timedelta(days=90)) for _ in range(5)]
    rows += [_used(8, TODAY), _used(8, TODAY)]
    rows += _spoil_rows(9, [1, 1, 1], start=TODAY - timedelta(days=40))
    assert kh.learn_consumption(rows, today=TODAY) == {}


def test_forecast_lists_only_in_stock_items_inside_the_horizon():
    rates = {"1": {"per_day": 0.5}, "2": {"per_day": 0.1}, "3": {"per_day": 1.0}}
    stock = {"1": 2.0, "2": 4.0, "3": 0.0}
    out = kh.forecast_runout(rates, stock, {"1": "Milk", "2": "Rice"})
    assert [(r["product_name"], r["days_left"]) for r in out] == [("Milk", 4)]


def test_shorten_days_only_ever_shortens():
    assert kh.shorten_days(7, 4, "fridge") == (4, True)
    assert kh.shorten_days(7, 12, "fridge") == (7, False)
    assert kh.shorten_days(90, 4, "freezer") == (90, False)
    assert kh.shorten_days(7, None, "fridge") == (7, False)


def _write_cache(by_name):
    from app import durable
    durable.write_json(kh._cache_file(), {"spoil_by_name": by_name,
                                          "consumption": {}},
                       keep_last_good=False)


def test_lookup_reads_the_cache_and_respects_the_setting(monkeypatch):
    _write_cache({"strawberries": 3})
    assert kh.learned_spoil_days("  Strawberries ") == 3
    assert kh.learned_spoil_days("Blueberries") is None
    monkeypatch.setattr(settings, "use_kitchen_history", False, raising=False)
    assert kh.learned_spoil_days("Strawberries") is None


def test_lookup_prefers_the_product_id():
    from app import durable
    durable.write_json(kh._cache_file(), {
        "spoil_by_product": {"42": {"days": 2, "samples": 3}},
        "spoil_by_name": {"strawberries": 5}}, keep_last_good=False)
    assert kh.learned_spoil_days("Strawberries", 42) == 2
    assert kh.learned_spoil_days("Strawberries", 43) == 5
    assert kh.learned_spoil_days("Strawberries") == 5


def test_a_cache_from_another_grocy_is_ignored(monkeypatch):
    from app import durable
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy-a:9383", raising=False)
    durable.write_json(kh._cache_file(), {
        "grocy_url": "http://grocy-a:9383/", "spoil_by_name": {"strawberries": 3}},
        keep_last_good=False)
    assert kh.learned_spoil_days("Strawberries") == 3
    assert not kh.is_stale()
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy-b:9383", raising=False)
    assert kh.learned_spoil_days("Strawberries") is None
    assert kh.is_stale()


def test_refresh_reads_the_journal_a_page_at_a_time(monkeypatch):
    monkeypatch.setattr(kh, "_JOURNAL_LIMIT", 2)
    rows = _spoil_rows(7, [2, 3, 3], start=TODAY - timedelta(days=60))
    paths = []

    class _Grocy:
        async def _get(self, path):
            if "locations" in path:
                return []
            paths.append(path)
            offset = int(path.rsplit("offset=", 1)[1])
            return rows[offset:offset + 2]

        async def get_products(self):
            return [{"id": 7, "name": "Strawberries"}]

    assert asyncio.run(kh.refresh(_Grocy())) is True
    assert len(paths) == 4  # 2 + 2 + 2 rows, then an empty page
    assert kh.learned_spoil_days("strawberries") == 3


def test_refresh_builds_the_cache_from_grocy():
    rows = _spoil_rows(7, [2, 3, 3], start=TODAY - timedelta(days=60))

    class _Grocy:
        async def _get(self, path):
            return [{"id": 1, "is_freezer": 0}] if "locations" in path else rows

        async def get_products(self):
            return [{"id": 7, "name": "Strawberries"}]

    assert asyncio.run(kh.refresh(_Grocy())) is True
    assert kh.learned_spoil_days("strawberries") == 3


def _apply(monkeypatch, name, storage, rule_days, user_rule=False):
    from app.services import defaults as defaults_mod

    class _Rule:
        name_pattern = name.lower()
        category = "Produce"
        storage_type = storage.value
        default_days = rule_days
        priority = 0

    class _Query:
        def all(self):
            return [_Rule()]

    class _Db:
        def query(self, _model):
            return _Query()

    monkeypatch.setattr(defaults_mod, "is_seed_rule", lambda r: not user_rule)
    item = FoodItem(name=name, category=FoodCategory.produce, storage_type=storage)
    return defaults_mod.apply_defaults(item, _Db())


def test_apply_defaults_shortens_with_history(monkeypatch):
    _write_cache({"strawberries": 3})
    item = _apply(monkeypatch, "Strawberries", StorageType.refrigerated, 7)
    assert item.best_by_date == date.today() + timedelta(days=3)
    assert item.best_by_source == "history"


def test_apply_defaults_keeps_a_shorter_rule_and_the_users_rule(monkeypatch):
    _write_cache({"strawberries": 10})
    item = _apply(monkeypatch, "Strawberries", StorageType.refrigerated, 7)
    assert item.best_by_date == date.today() + timedelta(days=7)
    assert item.best_by_source == "default"
    _write_cache({"strawberries": 2})
    item = _apply(monkeypatch, "Strawberries", StorageType.refrigerated, 7, user_rule=True)
    assert item.best_by_date == date.today() + timedelta(days=7)


def test_apply_defaults_leaves_frozen_items_alone(monkeypatch):
    _write_cache({"strawberries": 3})
    item = _apply(monkeypatch, "Strawberries", StorageType.frozen, 180)
    assert item.best_by_date == date.today() + timedelta(days=180)


# ── Inbox notice (FoodAssistant-p3ind) ──────────────────────────────────────

@pytest.fixture
def mem_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.database import Base
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _notices(db):
    from app.models.db_models import ActionItem
    return db.query(ActionItem).filter(ActionItem.kind == kh.NOTICE_KIND).all()


def test_notice_explains_the_shorter_date():
    title, body = kh.notice_text("Strawberries", 3, 7)
    assert "Strawberries" in title
    assert "about 3 days after purchase" in body
    assert "3 days out instead of 7 days" in body
    assert "—" not in title + body
    assert "1 day out instead of 1 day" in kh.notice_text("Milk", 1, 1)[1]


def test_notices_stop_after_the_first_few_products(mem_db):
    names = ["Strawberries", "Spinach", "Cilantro", "Raspberries"]
    shown = [kh.maybe_notify(mem_db, n, 3, 7) for n in names]
    assert shown == [True, True, True, False]
    assert len(_notices(mem_db)) == kh.MAX_NOTICES


def test_one_notice_per_product(mem_db):
    assert kh.maybe_notify(mem_db, "Strawberries", 3, 7) is True
    assert kh.maybe_notify(mem_db, "  strawberries ", 3, 7) is False
    assert len(_notices(mem_db)) == 1
    assert _notices(mem_db)[0].status == "open"


def test_apply_defaults_raises_the_notice(monkeypatch, mem_db):
    from app.services import defaults as defaults_mod
    _write_cache({"strawberries": 3})
    monkeypatch.setattr(defaults_mod, "is_seed_rule", lambda rule: True)
    from app.models.db_models import ExpiryDefault
    mem_db.add(ExpiryDefault(name_pattern="strawberr", category="Produce",
                             storage_type="refrigerated", default_days=7,
                             priority=0))
    mem_db.commit()
    item = FoodItem(name="Strawberries", category=FoodCategory.produce,
                    storage_type=StorageType.refrigerated)
    defaults_mod.apply_defaults(item, mem_db)
    assert item.best_by_source == "history"
    assert [n.title for n in _notices(mem_db)] == [
        "Best-by dates for Strawberries now follow your kitchen"]


def test_a_history_date_is_not_reported_as_a_rule_miss():
    from app.services.expiry_learning import build_point
    base = date(2026, 10, 1)
    point = build_point("Strawberries", None, "refrigerated",
                        base + timedelta(days=5), base + timedelta(days=3),
                        "history", base)
    assert point["suggestion_source"] == "none"
    assert point["suggested_days"] is None
    assert point["shelf_life_days"] == 5
