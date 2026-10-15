"""The two ledger sums every AI call runs are served by an index.

usage.daily_total (the service-wide daily ceiling) filtered on created_at with
no index, so it read the whole ledger on each call, and the ledger only grows.
usage.month_total combined two single-column indexes. These capture the exact
SQL each function runs and check the query plan uses the index meant for it.
"""
from sqlalchemy import event, text

from app import usage
from app.database import SessionLocal, get_engine


def _plan_for(call):
    captured = []

    def listener(conn, cursor, statement, params, context, executemany):
        if "usage_ledger" in statement:
            captured.append((statement, params))

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", listener)
    db = SessionLocal()
    try:
        call(db)
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    assert captured, "the call ran no ledger query"
    statement, params = captured[0]
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute("EXPLAIN QUERY PLAN " + statement, params)
        plan = " | ".join(str(row[-1]) for row in cur.fetchall())
    finally:
        raw.close()
        db.close()
    return plan


def test_the_daily_sum_uses_the_created_at_index():
    plan = _plan_for(lambda db: usage.daily_total(db))
    assert "ix_usage_ledger_created_at" in plan, plan
    assert "SCAN usage_ledger" not in plan.replace("USING", "")


def test_the_month_sum_uses_the_account_month_index():
    plan = _plan_for(lambda db: usage.month_total(db, 1, "2026-10"))
    assert "ix_usage_ledger_account_month" in plan, plan


def test_the_indexes_exist_under_their_names():
    with get_engine().connect() as c:
        names = {row[0] for row in c.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='usage_ledger'"))}
    assert {"ix_usage_ledger_created_at", "ix_usage_ledger_account_month"} <= names
