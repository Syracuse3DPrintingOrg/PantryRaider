"""The few outbound Stripe API calls the portal makes.

A deliberately thin client over httpx rather than the Stripe SDK: three
form-encoded endpoints do not justify a dependency, and every function here
is trivially monkeypatched in tests. Everything is gated on
CLOUD_STRIPE_SECRET_KEY the same all-or-nothing way the other integrations
gate on their credentials; with no key configured every call raises
StripeApiError and the callers fall back or explain themselves.

What lives here and why:

- Customer Portal sessions: the "Manage or cancel subscription" button. The
  hosted portal handles payment methods, receipts, and cancellation, but it
  only works once the portal is configured in the Stripe dashboard, so
  callers must always have a fallback.
- Cancel at period end: the in-app cancel fallback, so cancelling a plan
  never depends on any dashboard configuration.
- Cancel now: account deletion, which must never leave a paying orphan
  subscription behind.
"""
from __future__ import annotations

import logging

import httpx

from .config import settings

logger = logging.getLogger("forager.stripe")

_API_BASE = "https://api.stripe.com/v1"
_TIMEOUT = 15.0


class StripeApiError(Exception):
    """An outbound Stripe call failed (not configured, refused, or down)."""


def configured() -> bool:
    """Whether outbound Stripe calls are possible at all."""
    return bool(settings.stripe_secret_key)


def _request(method: str, path: str, data: dict | None = None) -> dict:
    """One authenticated, form-encoded Stripe API call, decoded to a dict.

    Raises StripeApiError on anything short of a 2xx answer, with a log line
    that names the endpoint but never the payload (no customer data in
    logs)."""
    if not configured():
        raise StripeApiError("Stripe is not configured")
    try:
        resp = httpx.request(
            method, f"{_API_BASE}{path}", data=data,
            auth=(settings.stripe_secret_key, ""), timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        logger.warning("Stripe %s %s failed: %s", method, path, exc)
        raise StripeApiError("Stripe could not be reached") from exc
    if resp.status_code >= 400:
        logger.warning("Stripe %s %s answered %s", method, path,
                       resp.status_code)
        raise StripeApiError(f"Stripe answered {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise StripeApiError("Stripe answered with an unreadable body") from exc


def create_portal_session(customer_id: str, return_url: str) -> str:
    """Open a Stripe Customer Portal session for this customer and return its
    URL. Raises StripeApiError when the key is missing, the customer is
    unknown, or the portal is not configured in the Stripe dashboard (the
    caller falls back to the in-app cancel flow)."""
    if not customer_id:
        raise StripeApiError("No Stripe customer for this account")
    body = _request("POST", "/billing_portal/sessions",
                    {"customer": customer_id, "return_url": return_url})
    url = str(body.get("url") or "")
    if not url:
        raise StripeApiError("Stripe did not return a portal URL")
    return url


def cancel_at_period_end(subscription_id: str) -> dict:
    """Schedule the subscription to end when the paid period does: it stays
    active until current_period_end and then does not renew."""
    if not subscription_id:
        raise StripeApiError("No Stripe subscription to cancel")
    return _request("POST", f"/subscriptions/{subscription_id}",
                    {"cancel_at_period_end": "true"})


def cancel_now(subscription_id: str) -> dict:
    """Cancel the subscription immediately. Used by account deletion, where
    leaving a paying subscription behind would be far worse than cutting the
    remaining period short."""
    if not subscription_id:
        raise StripeApiError("No Stripe subscription to cancel")
    return _request("DELETE", f"/subscriptions/{subscription_id}")
