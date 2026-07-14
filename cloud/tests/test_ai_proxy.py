"""The AI proxy's gates: signup trial, paid quota, expired trial, ledger, rate limit."""
import io

from app.database import SessionLocal
from app.forwarder import StubForwarder
from app.models import UsageLedger
from tests.conftest import activate_entitlement, expire_trial


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _analyze(client, token, kind="food"):
    files = {}
    if kind in ("food", "receipt"):
        files = {"image": ("photo.jpg", io.BytesIO(b"fake-jpeg-bytes"), "image/jpeg")}
    return client.post("/v1/ai/analyze", data={"kind": kind},
                       files=files, headers=_auth(token))


def test_requires_instance_token(client):
    resp = client.post("/v1/ai/analyze", data={"kind": "food"})
    assert resp.status_code == 401


def test_trial_account_gets_premium_quota(client, instance_token):
    # A fresh account is on the 30-day trial, which is the full premium quota.
    from app.config import PLAN_QUOTAS
    resp = _analyze(client, instance_token)
    assert resp.status_code == 200
    q = resp.json()["quota"]
    assert q["quota"] == PLAN_QUOTAS["trial"]
    assert q["plan"] == "trial"
    assert q["trial_days_left"] >= 29


def test_trial_quota_exceeded_is_402(client, instance_token):
    # Past the trial quota in a month, the account gets the 402 gate.
    from app import usage
    from app.config import PLAN_QUOTAS
    from app.models import Account
    db = SessionLocal()
    try:
        account_id = db.query(Account).first().id
        usage.record(db, account_id, 1, PLAN_QUOTAS["trial"], "food",
                     usage.month_key(), "2026-01-01T00:00:00+00:00")
    finally:
        db.close()
    resp = _analyze(client, instance_token)
    assert resp.status_code == 402
    detail = resp.json()["detail"]
    assert detail["error"] == "quota_exceeded"
    assert detail["plan"] == "trial"
    assert detail["quota"] == PLAN_QUOTAS["trial"]


def test_expired_trial_with_no_plan_is_402(client, instance_token):
    # After the trial lapses with nothing paid, the quota is zero and the
    # proxy answers 402 with the expired plan (Forager is trial-then-paid).
    expire_trial()
    resp = _analyze(client, instance_token)
    assert resp.status_code == 402
    detail = resp.json()["detail"]
    # No active entitlement is a distinct signal from a spent quota.
    assert detail["error"] == "no_subscription"
    assert detail["plan"] == "expired"
    assert detail["quota"] == 0


def test_paid_account_gets_premium_quota(client, instance_token):
    # A paid entitlement outranks the trial and grants the premium quota.
    from app.config import PLAN_QUOTAS
    activate_entitlement(plan="premium")
    resp = _analyze(client, instance_token)
    assert resp.status_code == 200
    assert resp.json()["quota"]["quota"] == PLAN_QUOTAS["premium"]
    assert resp.json()["quota"]["plan"] == "premium"


def test_basic_plan_gets_the_small_quota(client, instance_token):
    # Cloud Basic keeps the smaller AI allowance even past the trial.
    from app.config import PLAN_QUOTAS
    expire_trial()
    activate_entitlement(plan="basic")
    resp = _analyze(client, instance_token)
    assert resp.status_code == 200
    assert resp.json()["quota"]["quota"] == PLAN_QUOTAS["basic"]
    assert resp.json()["quota"]["plan"] == "basic"


def test_analyze_records_usage(client, instance_token):
    activate_entitlement()
    resp = _analyze(client, instance_token)
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["stub"] is True
    assert body["tokens"] == StubForwarder.STUB_TOKENS
    assert body["quota"]["used"] == StubForwarder.STUB_TOKENS

    db = SessionLocal()
    try:
        rows = db.query(UsageLedger).all()
        assert len(rows) == 1
        assert rows[0].tokens == StubForwarder.STUB_TOKENS
        assert rows[0].kind == "food"
    finally:
        db.close()


def test_enrich_needs_no_image(client, instance_token):
    activate_entitlement()
    resp = client.post("/v1/ai/analyze",
                       data={"kind": "enrich", "text": "barcode product data"},
                       headers=_auth(instance_token))
    assert resp.status_code == 200
    assert resp.json()["result"]["kind"] == "enrich"


def test_quota_exceeded_is_402(client, instance_token):
    account_id = activate_entitlement()
    # Spend the whole quota directly in the ledger, then make one more call.
    from app import usage
    from app.config import PLAN_QUOTAS
    db = SessionLocal()
    try:
        usage.record(db, account_id, 1, PLAN_QUOTAS["premium"], "food",
                     usage.month_key(), "2026-01-01T00:00:00+00:00")
    finally:
        db.close()
    resp = _analyze(client, instance_token)
    assert resp.status_code == 402
    detail = resp.json()["detail"]
    assert detail["error"] == "quota_exceeded"
    assert detail["used"] >= detail["quota"] > 0
    assert detail["month"]


def test_rejects_bad_inputs(client, instance_token):
    activate_entitlement()
    no_image = client.post("/v1/ai/analyze", data={"kind": "food"},
                           headers=_auth(instance_token))
    assert no_image.status_code == 400
    bad_kind = client.post("/v1/ai/analyze", data={"kind": "poetry"},
                           headers=_auth(instance_token))
    assert bad_kind.status_code == 400
    bad_mime = client.post(
        "/v1/ai/analyze", data={"kind": "food"},
        files={"image": ("x.gif", io.BytesIO(b"gif"), "image/gif")},
        headers=_auth(instance_token))
    assert bad_mime.status_code == 400


def test_proxy_rate_limit(client, instance_token, monkeypatch):
    activate_entitlement()
    from app.config import settings
    monkeypatch.setattr(settings, "proxy_rate_per_minute", 2)
    assert _analyze(client, instance_token).status_code == 200
    assert _analyze(client, instance_token).status_code == 200
    assert _analyze(client, instance_token).status_code == 429


def test_reservation_blocks_concurrent_burst(client, instance_token):
    # Regression for the check-to-record gap: a reservation from an in-flight
    # request must make the next gate see "no room" before the first request
    # has recorded its real usage. Interleave the gate/reserve calls directly
    # (deterministic; the Postgres advisory lock only adds cross-process
    # serialization on top of this same ledger-visible reservation).
    from app import usage
    from app.config import PLAN_QUOTAS
    from app.models import Instance
    account_id = activate_entitlement(plan="premium")
    estimate = PLAN_QUOTAS["premium"]  # leave room for exactly one reservation
    mk = usage.month_key()
    db = SessionLocal()
    try:
        inst_id = db.query(Instance).filter_by(account_id=account_id).first().id
        # Spend everything but one reservation's worth of the monthly quota.
        usage.record(db, account_id, inst_id,
                     PLAN_QUOTAS["premium"] - estimate, "food", mk,
                     "2026-01-01T00:00:00+00:00")

        # First request reserves the last of the room and has NOT reconciled.
        state1, res1 = usage.gate_and_reserve(
            db, account_id, inst_id, mk, estimate, "2026-01-01T00:00:01+00:00")
        assert res1 is not None
        assert not state1["over_quota"]

        # Second concurrent request now sees the reservation and is refused,
        # even though the first has not yet recorded its real token cost.
        state2, res2 = usage.gate_and_reserve(
            db, account_id, inst_id, mk, estimate, "2026-01-01T00:00:02+00:00")
        assert res2 is None
        assert state2["over_quota"]

        # The ledger never exceeds the quota across the burst.
        assert usage.month_total(db, account_id, mk) <= PLAN_QUOTAS["premium"]
    finally:
        db.close()


def test_reservation_released_on_upstream_error(client, instance_token, monkeypatch):
    # An upstream failure must give the reserved tokens back: no phantom row is
    # left counting against the quota.
    from app import usage
    from app.forwarder import ForwarderError
    from app.models import Account
    activate_entitlement()

    class _BoomForwarder:
        async def forward(self, kind, image_data, mime_type, text):
            raise ForwarderError(502, {"error": "upstream_error",
                                       "message": "boom"})

    monkeypatch.setattr("app.routers.ai.get_forwarder", lambda: _BoomForwarder())
    resp = _analyze(client, instance_token)
    assert resp.status_code == 502

    db = SessionLocal()
    try:
        account_id = db.query(Account).first().id
        # Reservation released: the month total is back to zero, no phantom.
        assert usage.month_total(db, account_id, usage.month_key()) == 0
        assert db.query(UsageLedger).count() == 0
    finally:
        db.close()

    # And once the provider recovers, a follow-up call succeeds and records
    # only its real usage (no lingering reservation from the failed attempt).
    monkeypatch.undo()
    ok = _analyze(client, instance_token)
    assert ok.status_code == 200
    assert ok.json()["quota"]["used"] == StubForwarder.STUB_TOKENS


def test_reservation_reconciled_to_real_tokens(client, instance_token):
    # The happy path leaves exactly one ledger row at the real token count,
    # not the reservation estimate (reconcile overwrites the reserved row).
    from app.config import settings
    activate_entitlement()
    resp = _analyze(client, instance_token)
    assert resp.status_code == 200
    db = SessionLocal()
    try:
        rows = db.query(UsageLedger).all()
        assert len(rows) == 1
        assert rows[0].tokens == StubForwarder.STUB_TOKENS
        assert rows[0].kind == "food"  # not the transient "reserve" marker
        # The estimate is just a placeholder; the ledger reflects reality.
        assert rows[0].tokens != settings.proxy_reservation_tokens or \
            StubForwarder.STUB_TOKENS == settings.proxy_reservation_tokens
    finally:
        db.close()
