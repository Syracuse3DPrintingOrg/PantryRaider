"""An instance's last-seen time is written at most once a minute.

It is shown to the day, yet every AI call used to UPDATE the instance row and
commit, which was most of the proxy's write load. A new app version or mode
still lands at once.
"""
from sqlalchemy import event

from app.database import SessionLocal, get_engine
from app.models import Instance


def _updates_during(call):
    seen = []

    def listener(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE INSTANCES"):
            seen.append(statement)

    event.listen(get_engine(), "before_cursor_execute", listener)
    try:
        call()
    finally:
        event.remove(get_engine(), "before_cursor_execute", listener)
    return seen


def _me(client, token, **headers):
    r = client.get("/v1/instance/me", headers={"Authorization": f"Bearer {token}", **headers})
    assert r.status_code == 200
    return r


def test_repeat_requests_do_not_rewrite_last_seen(client, instance_token):
    _me(client, instance_token)                     # first sight writes it
    assert _updates_during(lambda: [_me(client, instance_token) for _ in range(5)]) == []


def test_a_stale_last_seen_is_refreshed(client, instance_token):
    db = SessionLocal()
    try:
        inst = db.query(Instance).first()
        inst.last_seen_at = "2026-01-01T00:00:00+00:00"
        db.commit()
    finally:
        db.close()
    assert len(_updates_during(lambda: _me(client, instance_token))) == 1
    db = SessionLocal()
    try:
        assert db.query(Instance).first().last_seen_at > "2026-01-01T00:00:00+00:00"
    finally:
        db.close()


def test_a_new_version_is_written_at_once(client, instance_token):
    _me(client, instance_token, **{"X-Device-Version": "0.21.0"})
    assert len(_updates_during(
        lambda: _me(client, instance_token, **{"X-Device-Version": "0.21.1"}))) == 1
    assert _updates_during(
        lambda: _me(client, instance_token, **{"X-Device-Version": "0.21.1"})) == []
    db = SessionLocal()
    try:
        assert db.query(Instance).first().app_version == "0.21.1"
    finally:
        db.close()
