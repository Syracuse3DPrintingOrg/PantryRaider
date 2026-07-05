"""Forager settings (Pantry Raider's hosted cloud service).

Environment-driven (CLOUD_ prefix), no settings.json: the cloud runs on one
VPS with an env file, not on appliances with a setup wizard. This service
shares nothing at import time with service/ (see docs/design/cloud-platform.md);
where behaviour matches the app, the logic is duplicated on purpose.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings

CLOUD_VERSION = "0.1.0"

# The plan table. Quotas are AI tokens per calendar month, the same unit
# service/app/services/usage.py meters locally. Every new account gets a
# 30-day trial entitlement at signup (the full premium quota, so the trial
# is the real experience); after it expires the account needs a paid plan.
# Prices live in Stripe, never in code.
PLAN_QUOTAS: dict[str, int] = {
    # 30 days of the full premium experience, granted automatically at signup.
    "trial": 2_000_000,
    # Cloud Basic ($10/year): remote access plus a small AI allowance, for
    # people who bring their own AI key or scan lightly.
    "basic": 100_000,
    # Premium ($3/month or $30/year): remote access plus the full AI allowance.
    "premium": 2_000_000,
}
TRIAL_PLAN = "trial"
TRIAL_DAYS = 30
# What quota_state reports when no entitlement is active: the trial ran out
# and nothing paid replaced it. Not a plan with a quota of its own; the AI
# proxy answers 402 until the account subscribes.
EXPIRED_PLAN = "expired"
# The plan a paid Stripe purchase maps to when the price id is unrecognised.
DEFAULT_PLAN = "premium"


class CloudSettings(BaseSettings):
    # Prod is Postgres (multi-tenant, concurrent webhook + proxy writers).
    # Tests override this with SQLite so the suite runs without Docker.
    database_url: str = "postgresql+psycopg2://pantry:pantry@db:5432/pantrycloud"

    # Stripe webhook endpoint secret ("whsec_..."). The placeholder keeps the
    # signature check real in tests; the VPS env file supplies the live value.
    stripe_webhook_secret: str = "whsec_placeholder"

    # The Stripe price ids (price_...) that map purchases to plans. One
    # per way to pay: Cloud Basic is yearly only, Premium comes monthly
    # or yearly. Set each from the Stripe dashboard when billing goes live.
    stripe_price_basic_year: str = ""
    stripe_price_premium_month: str = ""
    stripe_price_premium_year: str = ""

    # Deprecated: the pre-pricing-rework starter price id. Kept working as
    # a premium alias so an env file that still sets it maps purchases
    # correctly; use CLOUD_STRIPE_PRICE_PREMIUM_* for new setups.
    stripe_price_starter: str = ""

    # The Stripe Checkout links the portal's plan buttons point at, one per
    # price. Empty links hide their button; if only the plain
    # CLOUD_STRIPE_CHECKOUT_URL is set it becomes a single Subscribe
    # button; when none are set the account page says billing is not live
    # yet instead of showing dead buttons.
    stripe_checkout_url: str = ""
    stripe_checkout_url_basic_year: str = ""
    stripe_checkout_url_premium_month: str = ""
    stripe_checkout_url_premium_year: str = ""

    # Extra price-id-to-plan mappings, for future tiers.
    stripe_price_to_plan: dict[str, str] = {}

    # Google sign-in ("Continue with Google"). Fully gated: the portal
    # buttons and the /auth/google routes only exist when both values are
    # set. Credentials come from a Google Cloud OAuth client.
    google_client_id: str = ""
    google_client_secret: str = ""

    # The public origin this service is reached at. Google redirects back
    # to {public_base_url}/auth/google/callback, which must match the
    # redirect URI registered with the OAuth client.
    public_base_url: str = "https://forager.pantryraider.app"

    # Admin panel access: comma-separated account emails allowed into
    # /admin. Empty means nobody. Anyone not on the list gets a 404 there,
    # the same answer as a route that does not exist.
    admin_emails: str = ""

    # Blended Gemini 2.5 Flash cost per million tokens, used only for the
    # admin panel's month-to-date spend estimate. A rough weighting of the
    # $0.30/M input and $2.50/M output list prices for the proxy's
    # image-heavy, short-answer workload; tune it as real bills arrive.
    gemini_cost_per_million_tokens: float = 0.60

    # Which AIForwarder backs the proxy: "stub" (tests, local dev) or
    # "gemini" (production).
    ai_forwarder: str = "stub"

    # Gemini upstream for the AI proxy (used when ai_forwarder is "gemini").
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    forward_timeout_seconds: float = 60.0

    # Portal session lifetime.
    session_ttl_hours: int = 24 * 14

    # The portal session cookie is HttpOnly and SameSite=Lax always; Secure
    # is on by default (production sits behind Caddy TLS) and switched off
    # only for tests and plain-HTTP local dev.
    cookie_secure: bool = True

    # Pairing codes are a short-lived credential typed by hand; keep the
    # window tight.
    pairing_code_ttl_minutes: int = 15

    # Fixed-window rate limits (requests per minute) for the abuse-prone
    # unauthenticated/spendy endpoints. 0 disables (used by most tests).
    signup_rate_per_minute: int = 10
    login_rate_per_minute: int = 10
    proxy_rate_per_minute: int = 30

    # Per-account failed-login lockout. The per-IP rate limit above does not
    # stop an attacker who rotates IPs against one email, so an account locks
    # itself for a short while after too many consecutive wrong passwords.
    # Purely time-based (locked_until compared to now), no cron. A correct
    # login clears the counter.
    account_lockout_threshold: int = 8
    account_lockout_minutes: int = 15

    model_config = {"env_prefix": "CLOUD_"}


settings = CloudSettings()
