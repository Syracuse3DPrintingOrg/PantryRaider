"""Stripe webhook: signed events become entitlements, idempotently."""
import hashlib
import hmac
import json
import time

from app.config import PLAN_QUOTAS, settings
from app.database import SessionLocal
from app.models import Account, Entitlement, Subscription


def _post_event(client, event: dict, secret: str | None = None):
    payload = json.dumps(event).encode()
    ts = int(time.time())
    sig = hmac.new((secret or settings.stripe_webhook_secret).encode(),
                   f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return client.post("/v1/stripe/webhook", content=payload,
                       headers={"Stripe-Signature": f"t={ts},v1={sig}"})


def _account_id(client, session_token):
    db = SessionLocal()
    try:
        return db.query(Account).first().id
    finally:
        db.close()


def _checkout_event(account_id, event_id="evt_1"):
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {"object": {
            "client_reference_id": str(account_id),
            "customer": "cus_123",
            "subscription": "sub_123",
        }},
    }


def test_rejects_bad_signature(client):
    resp = _post_event(client, {"id": "evt_x", "type": "x"}, secret="whsec_wrong")
    assert resp.status_code == 400
    unsigned = client.post("/v1/stripe/webhook", content=b"{}")
    assert unsigned.status_code == 400


def test_placeholder_webhook_secret_is_rejected(client, session_token, monkeypatch):
    # FoodAssistant-spg1: the shipped placeholder is public in this repo, so a
    # deploy that forgets to override it must not verify against it. With the
    # placeholder (or an empty secret) configured, the webhook refuses to
    # process, so a forged, perfectly-signed event cannot grant premium.
    from app.config import PLACEHOLDER_WEBHOOK_SECRET
    account_id = _account_id(client, session_token)
    monkeypatch.setattr(settings, "stripe_webhook_secret", PLACEHOLDER_WEBHOOK_SECRET)
    forged = _post_event(client, _checkout_event(account_id, event_id="evt_forged"),
                         secret=PLACEHOLDER_WEBHOOK_SECRET)
    assert forged.status_code == 503
    monkeypatch.setattr(settings, "stripe_webhook_secret", "")
    empty = _post_event(client, _checkout_event(account_id, event_id="evt_forged2"),
                        secret="")
    assert empty.status_code == 503
    # No Stripe entitlement was written by the forged deliveries (the signup
    # trial is the only entitlement the account has).
    db = SessionLocal()
    try:
        assert db.query(Entitlement).filter_by(
            account_id=account_id, source="stripe").count() == 0
    finally:
        db.close()


def test_checkout_completed_activates_entitlement(client, session_token):
    account_id = _account_id(client, session_token)
    resp = _post_event(client, _checkout_event(account_id))
    assert resp.status_code == 200

    db = SessionLocal()
    try:
        ent = db.query(Entitlement).filter_by(account_id=account_id, source="stripe").first()
        assert ent.status == "active"
        assert ent.monthly_token_quota == PLAN_QUOTAS["premium"]
        sub = db.query(Subscription).first()
        assert sub.stripe_subscription_id == "sub_123"
        assert sub.stripe_customer_id == "cus_123"
    finally:
        db.close()


def test_events_are_idempotent(client, session_token):
    account_id = _account_id(client, session_token)
    assert _post_event(client, _checkout_event(account_id)).status_code == 200
    dup = _post_event(client, _checkout_event(account_id))
    assert dup.status_code == 200
    assert dup.json().get("duplicate") is True
    db = SessionLocal()
    try:
        assert db.query(Subscription).count() == 1
    finally:
        db.close()


def test_subscription_deleted_deactivates(client, session_token):
    account_id = _account_id(client, session_token)
    _post_event(client, _checkout_event(account_id))
    resp = _post_event(client, {
        "id": "evt_2",
        "type": "customer.subscription.deleted",
        "data": {"object": {"id": "sub_123", "status": "canceled"}},
    })
    assert resp.status_code == 200
    db = SessionLocal()
    try:
        ent = db.query(Entitlement).filter_by(account_id=account_id, source="stripe").first()
        assert ent.status == "inactive"
        assert db.query(Subscription).first().status == "canceled"
    finally:
        db.close()


def test_subscription_updated_past_due_stays_active(client, session_token):
    account_id = _account_id(client, session_token)
    _post_event(client, _checkout_event(account_id))
    resp = _post_event(client, {
        "id": "evt_3",
        "type": "customer.subscription.updated",
        "data": {"object": {"id": "sub_123", "status": "past_due",
                            "current_period_end": 1780000000}},
    })
    assert resp.status_code == 200
    db = SessionLocal()
    try:
        ent = db.query(Entitlement).filter_by(account_id=account_id, source="stripe").first()
        assert ent.status == "active"
    finally:
        db.close()


def test_starter_price_id_maps_to_premium_alias(client, session_token,
                                               monkeypatch):
    # CLOUD_STRIPE_PRICE_STARTER is a deprecated alias that still maps the live
    # Stripe price to a working premium plan.
    monkeypatch.setattr(settings, "stripe_price_starter", "price_live_starter")
    account_id = _account_id(client, session_token)
    _post_event(client, _checkout_event(account_id))
    resp = _post_event(client, {
        "id": "evt_price",
        "type": "customer.subscription.updated",
        "data": {"object": {
            "id": "sub_123", "status": "active",
            "items": {"data": [{"price": {"id": "price_live_starter"}}]},
        }},
    })
    assert resp.status_code == 200
    db = SessionLocal()
    try:
        ent = db.query(Entitlement).filter_by(account_id=account_id, source="stripe").first()
        assert ent.plan == "premium"
        assert ent.monthly_token_quota == PLAN_QUOTAS["premium"]
    finally:
        db.close()


def test_unattributed_events_are_acknowledged(client):
    # A checkout with no matching account, and an unknown event type: both
    # return 200 so Stripe stops retrying, and change nothing.
    assert _post_event(client, _checkout_event(999999)).status_code == 200
    assert _post_event(client, {"id": "evt_9", "type": "invoice.paid",
                                "data": {"object": {}}}).status_code == 200
    db = SessionLocal()
    try:
        assert db.query(Entitlement).count() == 0
    finally:
        db.close()


def test_racing_insert_returns_duplicate_not_500(client, session_token, monkeypatch):
    """The TOCTOU race (security report): the event id passes the existence
    check, but a concurrent delivery has already committed the row by the time
    this one inserts, so the UNIQUE constraint fires. The webhook must handle
    that IntegrityError as a graceful duplicate, not a 500. Simulated by making
    the existence check miss a row that is in fact present, forcing the insert
    down the except branch."""
    from app.database import SessionLocal
    from app.models import StripeEvent
    account_id = _account_id(client, session_token)

    # Pre-insert the row so the commit will violate the UNIQUE constraint.
    seed = SessionLocal()
    seed.add(StripeEvent(event_id="evt_race", event_type="checkout.session.completed",
                         processed_at="2026-01-01T00:00:00+00:00"))
    seed.commit()
    seed.close()

    # Make the handler's own existence check miss it (as it would mid-race).
    class _Blind(StripeEvent):
        pass
    # Patch the query path: filter_by(...).first() returns None for this call.
    from sqlalchemy.orm import Query
    real_first = Query.first
    calls = {"n": 0}
    def fake_first(self):
        calls["n"] += 1
        if calls["n"] == 1:
            return None   # the racing miss
        return real_first(self)
    monkeypatch.setattr(Query, "first", fake_first)

    resp = _post_event(client, _checkout_event(account_id, event_id="evt_race"))
    assert resp.status_code == 200 and resp.json().get("duplicate") is True

    check = SessionLocal()
    assert check.query(StripeEvent).filter_by(event_id="evt_race").count() == 1
    check.close()


def test_checkout_links_carry_the_account_reference(monkeypatch):
    """The webhook attributes a payment by client_reference_id, so each
    checkout link must append it for the account (FoodAssistant billing)."""
    from app.config import settings
    from app.routers.portal import checkout_options
    monkeypatch.setattr(settings, "stripe_checkout_url_basic_year",
                        "https://buy.stripe.com/basic", raising=False)
    monkeypatch.setattr(settings, "stripe_checkout_url_premium_month",
                        "https://buy.stripe.com/prem?foo=1", raising=False)
    opts = checkout_options(account_id=42)
    urls = [o["url"] for o in opts]
    assert "https://buy.stripe.com/basic?client_reference_id=42" in urls
    # A link that already has a query gets & not ?.
    assert "https://buy.stripe.com/prem?foo=1&client_reference_id=42" in urls
    # No account id: link is untouched (should not happen on the account page).
    assert checkout_options(0)[0]["url"] == "https://buy.stripe.com/basic"


def test_plan_maps_by_lookup_key_not_just_price_id(monkeypatch):
    """The CLOUD_STRIPE_PRICE_* values can be friendly lookup keys; the webhook
    matches the event price's lookup_key or id against them."""
    from app.config import settings
    from app.routers.stripe_webhook import _plan_for_price
    monkeypatch.setattr(settings, "stripe_price_basic_year", "foragerbasic", raising=False)
    monkeypatch.setattr(settings, "stripe_price_premium_month", "foragerpremmonthly", raising=False)
    monkeypatch.setattr(settings, "stripe_price_premium_year", "foragerpremyearly", raising=False)
    # Stripe sends the generated id in .id and the friendly key in .lookup_key.
    assert _plan_for_price("price_1AbC", "foragerbasic") == "basic"
    assert _plan_for_price("price_9XyZ", "foragerpremmonthly") == "premium"
    assert _plan_for_price("price_2Def", "foragerpremyearly") == "premium"
    # Matching directly on the id still works when the env holds an id.
    monkeypatch.setattr(settings, "stripe_price_basic_year", "price_realbasic", raising=False)
    assert _plan_for_price("price_realbasic", "") == "basic"
