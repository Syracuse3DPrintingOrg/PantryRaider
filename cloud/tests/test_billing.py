"""Subscription self-service: the Stripe Customer Portal door, the in-app
cancel fallback, the account-page button, and the webhook's
cancel-at-period-end mirroring. Outbound Stripe calls are monkeypatched;
nothing here touches the network."""
import hashlib
import hmac
import json
import time

from app import stripe_api
from app.config import settings
from app.database import SessionLocal
from app.models import Account, Subscription


def _portal_login(client, email="dan@example.com", password="hunter2222"):
    resp = client.post("/signup", data={"email": email, "password": password,
                                        "confirm_password": password},
                       follow_redirects=False)
    assert resp.status_code == 303
    return resp


def _give_subscription(email="dan@example.com", status="active",
                       customer="cus_123", sub_id="sub_123",
                       period_end="1786000000", cancelling=0):
    db = SessionLocal()
    try:
        account = db.query(Account).filter_by(email=email).first()
        db.add(Subscription(account_id=account.id,
                            stripe_customer_id=customer,
                            stripe_subscription_id=sub_id,
                            status=status, current_period_end=period_end,
                            cancel_at_period_end=cancelling,
                            updated_at="2026-07-15T00:00:00+00:00"))
        db.commit()
        return account.id
    finally:
        db.close()


def _subscription():
    db = SessionLocal()
    try:
        return db.query(Subscription).first()
    finally:
        db.close()


def _post_event(client, event: dict):
    payload = json.dumps(event).encode()
    ts = int(time.time())
    sig = hmac.new(settings.stripe_webhook_secret.encode(),
                   f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return client.post("/v1/stripe/webhook", content=payload,
                       headers={"Stripe-Signature": f"t={ts},v1={sig}"})


# --- The "Manage or cancel subscription" button -------------------------------

def test_portal_button_opens_a_stripe_portal_session(client, monkeypatch):
    _portal_login(client)
    _give_subscription()
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_x")
    seen = {}

    def fake_session(customer_id, return_url):
        seen["customer"] = customer_id
        seen["return_url"] = return_url
        return "https://billing.stripe.com/session/abc"

    monkeypatch.setattr(stripe_api, "create_portal_session", fake_session)
    resp = client.post("/account/billing/portal", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "https://billing.stripe.com/session/abc"
    assert seen["customer"] == "cus_123"
    assert "/account" in seen["return_url"]


def test_portal_button_falls_back_to_static_link_then_in_app_cancel(
        client, monkeypatch):
    _portal_login(client)
    _give_subscription()

    # Portal session creation fails (dashboard portal not configured): the
    # static portal login link is the next door.
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_x")

    def boom(customer_id, return_url):
        raise stripe_api.StripeApiError("portal not configured")

    monkeypatch.setattr(stripe_api, "create_portal_session", boom)
    monkeypatch.setattr(settings, "stripe_portal_url",
                        "https://billing.stripe.com/p/login/xyz")
    resp = client.post("/account/billing/portal", follow_redirects=False)
    assert resp.headers["location"] == "https://billing.stripe.com/p/login/xyz"

    # With no static link either, the in-app cancel page takes over, so a
    # cancellation path always exists.
    monkeypatch.setattr(settings, "stripe_portal_url", "")
    resp = client.post("/account/billing/portal", follow_redirects=False)
    assert resp.headers["location"] == "/account/cancel"


def test_free_account_has_no_manage_button_and_portal_redirects_home(client):
    _portal_login(client)
    page = client.get("/account")
    assert "Manage or cancel subscription" not in page.text
    resp = client.post("/account/billing/portal", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/account#billing"


def test_account_page_shows_manage_button_and_renewal_terms(client):
    _portal_login(client)
    _give_subscription()
    page = client.get("/account")
    assert "Manage or cancel subscription" in page.text
    assert "until you cancel" in page.text


# --- The in-app cancel fallback ------------------------------------------------

def test_cancel_page_confirms_then_schedules_cancellation(client, monkeypatch):
    _portal_login(client)
    _give_subscription()
    called = {}

    def fake_cancel(sub_id):
        called["sub"] = sub_id
        return {"id": sub_id, "cancel_at_period_end": True,
                "current_period_end": 1786000000}

    monkeypatch.setattr(stripe_api, "cancel_at_period_end", fake_cancel)

    page = client.get("/account/cancel")
    assert page.status_code == 200
    assert "stays active" in page.text

    resp = client.post("/account/cancel", follow_redirects=False)
    assert resp.status_code == 303
    assert "m=cancel-scheduled" in resp.headers["location"]
    assert called["sub"] == "sub_123"
    assert _subscription().cancel_at_period_end == 1

    # The account page now says the plan will not renew, with the date.
    account = client.get("/account")
    assert "will not renew" in account.text


def test_cancel_failure_keeps_the_plan_and_says_so(client, monkeypatch):
    _portal_login(client)
    _give_subscription()

    def boom(sub_id):
        raise stripe_api.StripeApiError("down")

    monkeypatch.setattr(stripe_api, "cancel_at_period_end", boom)
    resp = client.post("/account/cancel")
    assert resp.status_code == 503
    assert "was not changed" in resp.text
    assert _subscription().cancel_at_period_end == 0


def test_cancel_pages_require_a_subscription_and_a_session(client):
    # Signed out: everything bounces to login.
    resp = client.post("/account/cancel", follow_redirects=False)
    assert resp.headers["location"] == "/login"
    # Signed in but free: nothing to cancel.
    _portal_login(client)
    resp = client.get("/account/cancel", follow_redirects=False)
    assert resp.headers["location"] == "/account#billing"


# --- Webhook: mirror Stripe's cancel_at_period_end ------------------------------

def test_webhook_records_and_clears_cancel_at_period_end(client):
    _portal_login(client)
    _give_subscription()
    resp = _post_event(client, {
        "id": "evt_cape_1", "type": "customer.subscription.updated",
        "data": {"object": {"id": "sub_123", "status": "active",
                            "cancel_at_period_end": True,
                            "current_period_end": 1786000000}},
    })
    assert resp.status_code == 200
    sub = _subscription()
    assert sub.cancel_at_period_end == 1
    assert sub.current_period_end == "1786000000"

    # The owner un-cancels from the Stripe portal: the flag clears.
    resp = _post_event(client, {
        "id": "evt_cape_2", "type": "customer.subscription.updated",
        "data": {"object": {"id": "sub_123", "status": "active",
                            "cancel_at_period_end": False}},
    })
    assert resp.status_code == 200
    assert _subscription().cancel_at_period_end == 0


def test_stripe_api_is_dark_without_a_key(monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "")
    assert not stripe_api.configured()
    try:
        stripe_api.cancel_now("sub_x")
        assert False, "expected StripeApiError"
    except stripe_api.StripeApiError:
        pass
