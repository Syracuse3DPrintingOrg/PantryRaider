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
falls back to the default paid plan. Each subscription's status and plan are stored
as their events arrive, in any order, and the entitlement is computed from
those stored rows.
"""
from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import usage
from ..config import DEFAULT_PLAN, PLAN_QUOTAS, PLACEHOLDER_WEBHOOK_SECRET, settings
from ..deps import get_db, utc_now_iso
from ..models import Account, Entitlement, StripeEvent, Subscription
from ..security import verify_stripe_signature
from .tunnel import disable_tunnel_for_account

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/stripe", tags=["stripe"])

# Stripe subscription statuses that count as an active entitlement. Past-due
# stays active through Stripe's retry window; a final failure arrives as a
# status change or deletion event.
_ACTIVE_STATUSES = {"active", "trialing", "past_due"}
# Statuses a Stripe subscription never leaves: a cancelled or expired
# subscription is never reactivated (a new purchase is a new subscription).
_TERMINAL_STATUSES = {"canceled", "incomplete_expired"}


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
    # No commit here: the webhook owns the transaction boundary so the
    # entitlement and the "this event was processed" marker land together.


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
        mapped = settings.stripe_price_to_plan.get(k)
        if mapped in PLAN_QUOTAS:
            return mapped
        if mapped:
            # A mapping to a plan the quota table does not know would grant an
            # active plan with no allowance; the default paid plan is what the
            # buyer most likely bought, and the log says what to fix.
            logger.error("CLOUD_STRIPE_PRICE_TO_PLAN maps %s to unknown plan "
                         "%r; using %s", k, mapped, DEFAULT_PLAN)
    return DEFAULT_PLAN


# The plan an attributed subscription counts as before any event has told us
# its price: the cheapest paid plan, so a buyer has access right away and
# never more than they may have paid for. The subscription's own event then
# sets the real plan, whichever order the two arrive in.
_PROVISIONAL_PLAN = "basic"


def _sub_plan(sub: Subscription) -> str:
    return sub.plan or _PROVISIONAL_PLAN


def _recompute_entitlement(db: Session, account_id: int) -> bool:
    """Set the account's Stripe entitlement from every subscription stored for
    it (FoodAssistant-fa7). The best active one wins (by quota), so cancelling
    an old subscription never takes away a newer one's plan. Returns whether
    any subscription is active. A no-op when the account has none."""
    db.flush()
    subs = db.query(Subscription).filter_by(account_id=account_id).all()
    if not subs:
        return False
    active = [s for s in subs if s.status in _ACTIVE_STATUSES]
    if active:
        best = max(active, key=lambda s: PLAN_QUOTAS.get(_sub_plan(s), 0))
        _set_entitlement(db, account_id, _sub_plan(best), "active")
        return True
    newest = max(subs, key=lambda s: s.id or 0)
    _set_entitlement(db, account_id, _sub_plan(newest), "inactive")
    return False


def _handle_checkout_completed(db: Session, obj: dict) -> None:
    """checkout.session.completed: ties the purchase's subscription to the
    account. The plan comes from the subscription's own event, stored on the
    Subscription row whichever arrives first."""
    try:
        account_id = int(obj.get("client_reference_id") or 0)
    except (TypeError, ValueError):
        account_id = 0
    sub_id = str(obj.get("subscription") or "")
    if not account_id or not db.get(Account, account_id):
        # A purchase we cannot attribute: someone paid and gets nothing until
        # support links it, so say so loudly.
        logger.error("Unattributed Stripe checkout %s (subscription %s, "
                     "customer %s): no matching account for "
                     "client_reference_id %r", obj.get("id") or "?",
                     sub_id or "-", obj.get("customer") or "-",
                     obj.get("client_reference_id"))
        return
    if not sub_id:
        logger.warning("Stripe checkout %s for account %s has no subscription; "
                       "nothing to grant", obj.get("id") or "?", account_id)
        return
    sub = db.query(Subscription).filter_by(stripe_subscription_id=sub_id).first()
    if not sub:
        # The checkout landed first: the subscription is live as of now, and
        # its plan arrives with its own event.
        sub = Subscription(stripe_subscription_id=sub_id, status="active")
        db.add(sub)
    sub.account_id = account_id
    sub.stripe_customer_id = str(obj.get("customer") or sub.stripe_customer_id
                                 or "")
    sub.updated_at = utc_now_iso()
    _recompute_entitlement(db, account_id)


def _event_created(event: dict) -> int:
    """The event's Stripe creation time in unix seconds, or 0 when absent."""
    created = event.get("created")
    if isinstance(created, bool) or not isinstance(created, (int, float)):
        return 0
    return max(0, int(created))


def _handle_subscription_event(db: Session, obj: dict, deleted: bool,
                               event_created: int = 0) -> None:
    """customer.subscription.created / .updated / .deleted: store the
    subscription's status and plan, then recompute the owner's entitlement.
    A subscription not yet tied to an account is stored anyway, so a later
    checkout event finds its plan.

    Stripe does not deliver events in order, so a late event must not undo a
    newer one. An event older than the newest one already applied to the row
    is ignored, and a subscription that has ended (cancelled or expired) never
    comes back from an update, whatever its timestamp says: Stripe never
    reactivates one, so an "active" arriving after the cancellation can only
    be stale."""
    sub_id = str(obj.get("id") or "")
    if not sub_id:
        return
    sub = db.query(Subscription).filter_by(stripe_subscription_id=sub_id).first()
    customer = str(obj.get("customer") or "")
    if sub is not None:
        stale = bool(event_created and sub.last_event_created
                     and event_created < sub.last_event_created)
        ended = sub.status in _TERMINAL_STATUSES and not deleted
        if stale or ended:
            logger.info("Ignoring an out-of-order Stripe event for "
                        "subscription %s (status %s)", sub_id, sub.status)
            if customer and not sub.stripe_customer_id:
                sub.stripe_customer_id = customer
            return
    if not sub:
        sub = Subscription(stripe_subscription_id=sub_id)
        # A second subscription for a customer we already know (an upgrade)
        # belongs to the same account.
        if customer:
            known = (db.query(Subscription)
                     .filter(Subscription.stripe_customer_id == customer,
                             Subscription.account_id.isnot(None))
                     .first())
            if known:
                sub.account_id = known.account_id
        db.add(sub)
    if customer:
        sub.stripe_customer_id = customer
    status = "canceled" if deleted else str(obj.get("status") or "")
    sub.status = status
    if event_created:
        sub.last_event_created = max(sub.last_event_created or 0,
                                     event_created)
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

    items = (obj.get("items") or {}).get("data") or []
    if items:
        price = (items[0] or {}).get("price") or {}
        price_id = str(price.get("id") or "")
        lookup_key = str(price.get("lookup_key") or "")
        if price_id or lookup_key:
            sub.plan = _plan_for_price(price_id, lookup_key)

    if sub.account_id is None:
        return  # stored; the checkout event attributes it
    if not _recompute_entitlement(db, sub.account_id):
        # No paid plan left. Remote access is a paid, comped or trial feature,
        # so tear down the account's tunnels only when nothing else still
        # grants it; a running trial or an admin comp keeps them up, and the
        # periodic sweep takes them down when that ends.
        db.flush()
        state = usage.quota_state(db, sub.account_id, usage.month_key())
        if not usage.has_active_access(state):
            disable_tunnel_for_account(db, sub.account_id)


def _configured_livemode() -> bool | None:
    """Whether the configured Stripe secret key is a live one (True), a test
    one (False), or unknown (None, no key set)."""
    key = settings.stripe_secret_key or ""
    if key.startswith(("sk_live_", "rk_live_")):
        return True
    if key.startswith(("sk_test_", "rk_test_")):
        return False
    return None


@router.post("/webhook")
async def webhook(request: Request, db: Session = Depends(get_db)):
    # Async only to read the raw body. Everything after it (the queries, the
    # entitlement writes, a tunnel teardown) runs in the threadpool so a slow
    # database never stalls the event loop for every other request.
    payload = await request.body()
    header = request.headers.get("Stripe-Signature", "")
    return await run_in_threadpool(_process_webhook, db, payload, header)


def _process_webhook(db: Session, payload: bytes, header: str) -> dict:
    secret = settings.stripe_webhook_secret
    # Fail loudly on a missing or placeholder signing secret instead of
    # verifying against a value published in this repo. The placeholder ships in
    # config.py and .env.example so the signature path stays real in tests and
    # local dev; a deploy that forgets to override it would otherwise let anyone
    # forge a checkout.session.completed and comp themselves premium, or a
    # subscription.deleted to tear down another account's tunnel
    # (FoodAssistant-spg1). Refuse to process rather than trust a known secret.
    if not secret or secret == PLACEHOLDER_WEBHOOK_SECRET:
        logger.warning(
            "Refusing Stripe webhook: CLOUD_STRIPE_WEBHOOK_SECRET is unset or "
            "still the placeholder. Set the real signing secret from the Stripe "
            "dashboard before billing goes live.")
        raise HTTPException(503, detail="Stripe webhook is not configured")

    if not verify_stripe_signature(payload, header, secret,
                                   now=int(time.time())):
        raise HTTPException(400, detail="Invalid Stripe signature")

    try:
        event = json.loads(payload)
    except ValueError:
        raise HTTPException(400, detail="Invalid payload")

    event_id = str(event.get("id") or "")
    event_type = str(event.get("type") or "")
    # A test-mode event reaching a live deployment (or the reverse) is a
    # misconfigured endpoint; acting on it could hand out real plans for fake
    # payments (FoodAssistant-fa7). Refused, not recorded, so it is retried
    # and shows up in Stripe's dashboard as failing.
    expected = _configured_livemode()
    if (expected is not None and "livemode" in event
            and bool(event.get("livemode")) != expected):
        logger.error("Refusing Stripe event %s (%s): livemode=%s but the "
                     "configured key is %s", event_id or "?", event_type,
                     event.get("livemode"), "live" if expected else "test")
        raise HTTPException(400, detail="Event mode does not match this server")
    if event_id and db.query(StripeEvent).filter_by(event_id=event_id).first():
        return {"ok": True, "duplicate": True}

    # Apply the event BEFORE the "already processed" marker is written, and let
    # the marker be the last thing committed. Recording the marker first meant a
    # crash, restart, or database error in between left a durable marker and no
    # entitlement: Stripe's redelivery hits the check above, gets a 200, and
    # stops retrying, so a customer who paid is silently never upgraded. This
    # way an interrupted delivery leaves no marker and the retry does the work,
    # which is safe because every handler here is idempotent.
    obj = (event.get("data") or {}).get("object") or {}
    if event_type == "checkout.session.completed":
        _handle_checkout_completed(db, obj)
    elif event_type in ("customer.subscription.created",
                        "customer.subscription.updated"):
        # created carries the purchased price, so a Cloud Basic purchase
        # lands on the right plan even before any later update event.
        _handle_subscription_event(db, obj, deleted=False,
                                   event_created=_event_created(event))
    elif event_type == "customer.subscription.deleted":
        _handle_subscription_event(db, obj, deleted=True,
                                   event_created=_event_created(event))
    # Unrecognised event types fall through and are acknowledged below, so
    # Stripe stops retrying them.

    if event_id:
        db.add(StripeEvent(event_id=event_id, event_type=event_type,
                           processed_at=utc_now_iso()))
    # event_id is UNIQUE, so two simultaneous deliveries of the same event both
    # pass the check above; the loser's insert then violates the constraint.
    # Treat that IntegrityError as the dedup it is (roll back this delivery's
    # work, which the winner has already committed, and answer duplicate)
    # rather than surfacing a 500.
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"ok": True, "duplicate": True}
    return {"ok": True}
