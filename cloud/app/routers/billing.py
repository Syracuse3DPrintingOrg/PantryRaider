"""Subscription self-service and account deletion for the portal.

Two things a member must always be able to do without emailing anyone:

- Cancel (or otherwise manage) their subscription. The primary door is the
  Stripe Customer Portal, opened fresh per click; when that cannot be opened
  (no API key, portal not configured in the Stripe dashboard, Stripe down)
  the in-app cancel page takes over and schedules the cancellation directly,
  so cancelling never depends on any external configuration. The path is one
  click to the cancel page and one confirmation, the same effort as signing
  up, with no retention hoops.

- Delete their account. Guarded by real re-authentication (password, or the
  typed account email for a Google-only account, plus the authenticator code
  when two-factor is on) and a typed "delete", then the whole cascade in
  app/deletion.py runs: billing stops first, and the deletion aborts rather
  than ever leaving a paying subscription behind.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .. import deletion, ratelimit, stripe_api
from ..config import settings
from ..deps import SESSION_COOKIE, cookie_account, get_db
from ..email import base_url, send_email
from ..models import Account, Subscription
from ..security import verify_password
from .accounts import consume_totp

router = APIRouter(include_in_schema=False)

templates = Jinja2Templates(
    directory=str(Path(__file__).parent.parent / "templates"))

# Stripe statuses with nothing left to manage or cancel.
_DEAD_SUB_STATUSES = {"canceled", "incomplete_expired", ""}

CANCEL_UNAVAILABLE_MESSAGE = (
    "The billing service could not be reached, so your plan was not "
    "changed. Please try again in a few minutes, or email "
    "support@pantryraider.app and we will cancel it for you.")


def live_subscription(db: Session, account_id: int) -> Subscription | None:
    """The account's current Stripe subscription, or None for a free
    account. Newest first, skipping rows that are already dead."""
    rows = (db.query(Subscription).filter_by(account_id=account_id)
            .order_by(Subscription.id.desc()).all())
    for row in rows:
        if row.stripe_subscription_id and row.status not in _DEAD_SUB_STATUSES:
            return row
    return None


def period_end_label(sub: Subscription | None) -> str:
    """current_period_end (a unix timestamp string from the webhook) as a
    date a person reads, e.g. "August 14, 2026". Empty when unknown."""
    if not sub or not sub.current_period_end:
        return ""
    try:
        moment = datetime.fromtimestamp(int(sub.current_period_end),
                                        tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return ""
    return f"{moment.strftime('%B')} {moment.day}, {moment.year}"


def _login_redirect() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


# --- Manage or cancel a subscription -----------------------------------------

@router.post("/account/billing/portal")
def open_billing_portal(request: Request,
                        account: Account | None = Depends(cookie_account),
                        db: Session = Depends(get_db)):
    """The "Manage or cancel subscription" button: open a fresh Stripe
    Customer Portal session for this customer and send the browser there.
    Every fallback still lands somewhere the plan can actually be cancelled."""
    if not account:
        return _login_redirect()
    sub = live_subscription(db, account.id)
    if not sub:
        return RedirectResponse("/account#billing", status_code=303)
    if sub.stripe_customer_id and stripe_api.configured():
        try:
            url = stripe_api.create_portal_session(
                sub.stripe_customer_id, f"{base_url()}/account#billing")
            return RedirectResponse(url, status_code=303)
        except stripe_api.StripeApiError:
            pass  # fall through to a door that still works
    if settings.stripe_portal_url:
        # The dashboard's static portal login link (emails the customer a
        # sign-in code); works with no API key at all.
        return RedirectResponse(settings.stripe_portal_url, status_code=303)
    return RedirectResponse("/account/cancel", status_code=303)


@router.get("/account/cancel")
def cancel_page(request: Request,
                account: Account | None = Depends(cookie_account),
                db: Session = Depends(get_db)):
    """The in-app cancel page: one plain confirmation, nothing else."""
    if not account:
        return _login_redirect()
    sub = live_subscription(db, account.id)
    if not sub:
        return RedirectResponse("/account#billing", status_code=303)
    return templates.TemplateResponse(request, "cancel.html", {
        "signed_in": True,
        "period_end": period_end_label(sub),
        "already_cancelling": bool(sub.cancel_at_period_end),
    })


@router.post("/account/cancel")
def cancel_submit(request: Request,
                  account: Account | None = Depends(cookie_account),
                  db: Session = Depends(get_db)):
    """Schedule the cancellation with Stripe directly: the plan stays active
    until the end of the period already paid for and then does not renew."""
    if not account:
        return _login_redirect()
    sub = live_subscription(db, account.id)
    if not sub:
        return RedirectResponse("/account#billing", status_code=303)
    if not sub.cancel_at_period_end:
        try:
            body = stripe_api.cancel_at_period_end(sub.stripe_subscription_id)
        except stripe_api.StripeApiError:
            return templates.TemplateResponse(request, "cancel.html", {
                "signed_in": True,
                "period_end": period_end_label(sub),
                "already_cancelling": False,
                "error": CANCEL_UNAVAILABLE_MESSAGE,
            }, status_code=503)
        sub.cancel_at_period_end = 1
        period_end = body.get("current_period_end")
        if isinstance(period_end, (int, float)):
            sub.current_period_end = str(int(period_end))
        sub.updated_at = datetime.now(timezone.utc).isoformat(
            timespec="seconds")
        db.commit()
    return RedirectResponse("/account?m=cancel-scheduled#billing",
                            status_code=303)


# --- Delete the account -------------------------------------------------------

def _delete_page(request: Request, account: Account, error: str = "",
                 status: int = 200):
    return templates.TemplateResponse(request, "delete_account.html", {
        "signed_in": True,
        "email": account.email,
        "has_password": bool(account.password_hash),
        "totp_enabled": bool(account.totp_enabled),
        "error": error,
    }, status_code=status)


@router.get("/account/delete")
def delete_account_page(request: Request,
                        account: Account | None = Depends(cookie_account)):
    if not account:
        return _login_redirect()
    return _delete_page(request, account)


@router.post("/account/delete")
def delete_account_submit(request: Request,
                          credential: str = Form(""),
                          totp: str = Form(""),
                          confirm_text: str = Form(""),
                          account: Account | None = Depends(cookie_account),
                          db: Session = Depends(get_db)):
    if not account:
        return _login_redirect()
    # The form re-checks the password, so it gets the login limiter: a live
    # session must not become a password-guessing oracle.
    if not ratelimit.allow(f"delete-acct:{account.id}",
                           settings.login_rate_per_minute):
        return _delete_page(request, account,
                            "Too many attempts. Wait a minute and try again.",
                            429)
    if confirm_text.strip().lower() != "delete":
        return _delete_page(request, account,
                            'Type "delete" to confirm.', 400)
    # Re-authentication: the account password, or for a Google account with
    # no password, the account's own email typed in full.
    credential = credential.strip()
    if account.password_hash:
        ok = verify_password(credential, account.password_hash)
    else:
        ok = credential.lower() == account.email.lower()
    if not ok:
        return _delete_page(
            request, account,
            "That did not match. Your account was not deleted.", 401)
    if account.totp_enabled and not consume_totp(db, account, totp):
        return _delete_page(
            request, account,
            "That two-factor code did not match. Your account was not "
            "deleted.", 401)

    goodbye_to = account.email
    try:
        deletion.delete_account(db, account)
    except deletion.DeletionBlocked as exc:
        return _delete_page(request, account, str(exc), 503)
    # A best-effort goodbye so the owner has written confirmation; the
    # address only lives in this request now, nowhere in the database.
    send_email(
        goodbye_to, "Your Forager account has been deleted",
        "Your Forager account and its data have been deleted.\n\n"
        "Your kitchens keep working on their own; they just no longer have "
        "a Forager account behind them. Community recipes you shared stay "
        "available without your name.\n\n"
        "Thanks for cooking with us. You are welcome back anytime.")
    resp = RedirectResponse("/?deleted=1", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp
