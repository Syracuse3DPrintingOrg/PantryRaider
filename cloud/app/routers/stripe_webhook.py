"""Stripe webhook: Checkout and subscription events become entitlements.

No Stripe SDK and no outbound Stripe calls in the scaffold; the endpoint
verifies the signature over the raw body (security.verify_stripe_signature)
and reads the documented event shapes. Event ids are recorded so Stripe's
retried deliveries process once.

Wiring expectations for the live Stripe account: the Checkout Session is
created with client_reference_id set to the cloud account id, and the live
price ids are set in CLOUD_STRIPE_PRICE_BASIC_YEAR,
CLOUD_STRIPE_PRICE_PREMIUM_MONTH, and CLOUD_STRIPE_PRICE_PREMIUM_YEAR
(extra tiers go in CLOUD_STRIPE_PRICE_TO_PLAN). An unrecognised price
falls back to the default paid plan.
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import DEFAULT_PLAN, PLAN_QUOTAS, settings
from ..deps import get_db, utc_now_iso
from ..models import Account, Entitlement, StripeEvent, Subscription
from ..security import verify_stripe_signature

router = APIRouter(prefix="/v1/stripe", tags=["stripe"])

# Stripe subscription statuses that count as an active entitlement. Past-due
# stays active through Stripe's retry window; a final failure arrives as a
# status change or deletion event.
_ACTIVE_STATUSES = {"active", "trialing", "past_due"}


def _set_entitlement(db: Session, account_id: int, plan: str, status: str) -> None:
    # The webhook owns the account's Stripe-sourced row and never touches
    # the signup trial or an admin comp; usage.resolve_entitlement ranks a
    # paid row above both. Rows written before the source column ("") are
    # Stripe rows from the same flow.
    ent = (db.query(Entitlement)
           .filter(Entitlement.account_id == account_id,
                   Entitlement.source.in_(["stripe", ""]))
           .first())
    if not ent:
        ent = Entitlement(account_id=account_id)
        db.add(ent)
    ent.plan = plan
    ent.status = status
    ent.monthly_token_quota = PLAN_QUOTAS.get(plan, 0)
    # Stripe rows never carry the trial/comp-style hard expiry: their
    # lifecycle arrives as webhook status events.
    ent.source = "stripe"
    ent.expires_at = ""
    ent.updated_at = utc_now_iso()
    db.commit()


def _plan_for_price(price_id: str, lookup_key: str = "") -> str:
    """Map a Stripe price to its plan. Matches on either the price id
    (price_...) or the price's lookup_key, so the CLOUD_STRIPE_PRICE_* env
    values can be set to friendly lookup keys (foragerbasic) instead of the
    generated ids. Cloud Basic has one yearly price; Premium has a monthly and
    a yearly one. The old CLOUD_STRIPE_PRICE_STARTER value is a premium alias
    (deprecated); price_to_plan covers any future tiers."""
    keys = {k for k in (price_id, lookup_key) if k}
    if keys:
        if settings.stripe_price_basic_year in keys:
            return "basic"
        if keys & {settings.stripe_price_premium_month,
                   settings.stripe_price_premium_year,
                   settings.stripe_price_starter}:
            return "premium"
    for k in keys:
        if k in settings.stripe_price_to_plan:
            return settings.stripe_price_to_plan[k]
    return DEFAULT_PLAN


def _handle_checkout_completed(db: Session, obj: dict) -> None:
    """checkout.session.completed: the purchase that creates the entitlement."""
    try:
        account_id = int(obj.get("client_reference_id") or 0)
    except (TypeError, ValueError):
        account_id = 0
    if not account_id or not db.get(Account, account_id):
        return  # a purchase we cannot attribute; Stripe's dashboard still has it
    sub_id = str(obj.get("subscription") or "")
    if sub_id:
        sub = db.query(Subscription).filter_by(stripe_subscription_id=sub_id).first()
        if not sub:
            sub = Subscription(account_id=account_id, stripe_subscription_id=sub_id)
            db.add(sub)
        sub.stripe_customer_id = str(obj.get("customer") or "")
        sub.status = "active"
        sub.updated_at = utc_now_iso()
    # The checkout payload does not carry the price without expansion; the
    # subscription event that accompanies the purchase does, and corrects
    # the plan if this default guessed wrong.
    _set_entitlement(db, account_id, DEFAULT_PLAN, "active")


def _handle_subscription_event(db: Session, obj: dict, deleted: bool) -> None:
    """customer.subscription.updated / .deleted: status changes after purchase."""
    sub_id = str(obj.get("id") or "")
    sub = db.query(Subscription).filter_by(stripe_subscription_id=sub_id).first()
    if not sub:
        return  # a subscription we never attributed to an account
    status = "canceled" if deleted else str(obj.get("status") or "")
    sub.status = status
    period_end = obj.get("current_period_end")
    if isinstance(period_end, (int, float)):
        sub.current_period_end = str(int(period_end))
    # A cancel-at-period-end (set in the Customer Portal or by the in-app
    # cancel flow) arrives as an update with the flag set; undoing the
    # cancellation arrives the same way with it cleared. Mirrored here so the
    # account page can say "cancels on <date>". A deleted subscription has
    # nothing left to cancel.
    if deleted:
        sub.cancel_at_period_end = 0
    elif "cancel_at_period_end" in obj:
        sub.cancel_at_period_end = 1 if obj.get("cancel_at_period_end") else 0
    sub.updated_at = utc_now_iso()

    plan = DEFAULT_PLAN
    items = (obj.get("items") or {}).get("data") or []
    if items:
        price = (items[0] or {}).get("price") or {}
        price_id = str(price.get("id") or "")
        lookup_key = str(price.get("lookup_key") or "")
        if price_id or lookup_key:
            plan = _plan_for_price(price_id, lookup_key)
    active = not deleted and status in _ACTIVE_STATUSES
    _set_entitlement(db, sub.account_id, plan, "active" if active else "inactive")
    if not active:
        # The paid plan lapsed. Remote access is a paid or trial feature, so
        # tear down any tunnel this account holds. If the account still has an
        # active trial the app can re-enable on its next check; a periodic
        # sweep for trials that expire on their own is a follow-up.
        from .tunnel import disable_tunnel_for_account
        disable_tunnel_for_account(db, sub.account_id)


@router.post("/webhook")
async def webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    header = request.headers.get("Stripe-Signature", "")
    if not verify_stripe_signature(payload, header,
                                   settings.stripe_webhook_secret,
                                   now=int(time.time())):
        raise HTTPException(400, detail="Invalid Stripe signature")

    try:
        event = json.loads(payload)
    except ValueError:
        raise HTTPException(400, detail="Invalid payload")

    event_id = str(event.get("id") or "")
    event_type = str(event.get("type") or "")
    if event_id:
        if db.query(StripeEvent).filter_by(event_id=event_id).first():
            return {"ok": True, "duplicate": True}
        # event_id is UNIQUE, so two simultaneous deliveries of the same event
        # both pass the check above; the loser's insert then violates the
        # constraint. Treat that IntegrityError as the dedup it is (roll back
        # and return duplicate) rather than surfacing a 500.
        db.add(StripeEvent(event_id=event_id, event_type=event_type,
                           processed_at=utc_now_iso()))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return {"ok": True, "duplicate": True}

    obj = (event.get("data") or {}).get("object") or {}
    if event_type == "checkout.session.completed":
        _handle_checkout_completed(db, obj)
    elif event_type in ("customer.subscription.created",
                        "customer.subscription.updated"):
        # created carries the purchased price, so a Cloud Basic purchase
        # lands on the right plan even before any later update event.
        _handle_subscription_event(db, obj, deleted=False)
    elif event_type == "customer.subscription.deleted":
        _handle_subscription_event(db, obj, deleted=True)
    # Unrecognised event types are acknowledged so Stripe stops retrying them.
    return {"ok": True}
