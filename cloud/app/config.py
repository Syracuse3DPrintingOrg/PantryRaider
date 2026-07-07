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
    # Stripe Customer Portal login link (billing.stripe.com/p/login/...),
    # where a subscriber manages or cancels their plan.
    stripe_portal_url: str = ""
    stripe_checkout_url_basic_year: str = ""
    stripe_checkout_url_premium_month: str = ""
    stripe_checkout_url_premium_year: str = ""

    # Extra price-id-to-plan mappings, for future tiers.
    stripe_price_to_plan: dict[str, str] = {}

    # Google sign-in ("Continue with Google"). Fully gated: the portal
    # buttons and the /auth/google routes only exist when both values are
    # set. Credentials come from a Google Cloud OAuth client.
    # Cloudflare Turnstile signup CAPTCHA. Both must be set for the widget
    # to render and the server to verify; dark otherwise.
    turnstile_site_key: str = ""
    turnstile_secret: str = ""
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
    # The pending token cost reserved in the ledger before an AI proxy call
    # is forwarded, reconciled to the real count once the provider answers.
    # It only has to be greater than zero to close the concurrent-burst gap
    # (usage.gate_and_reserve); a modest value keeps a legitimate final call
    # near the limit from being blocked by its own reservation.
    proxy_reservation_tokens: int = 1000
    # Password-reset requests and verification re-sends both put mail on the
    # wire for an unauthenticated (or barely authenticated) caller, so they
    # get their own tighter windows.
    forgot_rate_per_minute: int = 5
    resend_verification_rate_per_minute: int = 5
    # Sharing a community recipe both writes to the shared library and is a
    # spammer's favourite surface, so submissions get their own tight window,
    # applied per account and per IP.
    recipe_submit_rate_per_minute: int = 5
    # The portal "share a recipe" upload path spends Forager's own AI key to
    # format each draft, so it gets a tighter window still, applied per account
    # and per IP. Covers both the draft (AI) step and the confirm (save) step.
    recipe_upload_rate_per_minute: int = 3
    # How recently a kitchen must have checked in for the account to count as
    # "actively using Pantry Raider" and be allowed to upload recipes from the
    # portal. An account can also be authorized by hand from the admin panel,
    # which bypasses this check.
    recipe_active_kitchen_days: int = 30
    # The largest recipe PDF or photo the portal upload accepts, in bytes.
    # Anything larger gets a clear "that file is too large" message rather than
    # being sent upstream.
    recipe_upload_max_bytes: int = 8_000_000
    # How many separate members must flag a shared recipe before it drops out
    # of the community browser on its own, waiting on a human review. 0
    # disables the auto-hide.
    recipe_report_hide_threshold: int = 5
    # Whether a newly shared recipe must be approved by a moderator before the
    # community can see it. Off by default: at launch the shared library is
    # small and the friendlier experience is that a recipe appears the moment
    # it is shared, with moderation handled reactively (member flags auto-hide
    # trouble, and the admin panel can hide or reject anything after the fact).
    # Flip it on (CLOUD_RECIPE_REQUIRE_APPROVAL=1) once volume grows and a
    # moderator is watching the pending queue; new recipes then land "pending"
    # and stay out of the browser until an admin approves them.
    recipe_require_approval: bool = False

    # --- Outgoing email (password reset, verification) ---
    # The whole feature is dark until a host and a from address are set:
    # email_configured() gates the forgot-password link, verification sends,
    # and the verify banner, exactly like Google sign-in is gated on its
    # credentials. Production uses Resend over SMTP (smtp.resend.com).
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    # The from address subscribers see, e.g. noreply@forager.pantryraider.app.
    smtp_from: str = ""
    # STARTTLS on the submission port (587, the default). Set false to dial a
    # TLS-from-the-start port instead (SMTP_SSL, typically 465).
    smtp_starttls: bool = True
    # How long to wait on the mail server before giving up. Kept short: a slow
    # mail host must never hold a web request open.
    smtp_timeout_seconds: float = 10.0
    # How long a password-reset link stays good.
    password_reset_ttl_hours: int = 1
    # How long an email-verification link stays good. Longer than a reset:
    # confirming an address is not urgent, and a person may open the mail days
    # later.
    email_verify_ttl_days: int = 7

    # Per-account failed-login lockout. The per-IP rate limit above does not
    # stop an attacker who rotates IPs against one email, so an account locks
    # itself for a short while after too many consecutive wrong passwords.
    # Purely time-based (locked_until compared to now), no cron. A correct
    # login clears the counter.
    account_lockout_threshold: int = 8
    account_lockout_minutes: int = 15

    # --- Remote-access tunnel (WireGuard + Caddy on the VPS) ---
    # The public WireGuard endpoint kitchens dial out to (host:port). The
    # app hands this to WireGuard as the peer Endpoint.
    tunnel_endpoint: str = "forager.pantryraider.app:51820"
    # The VPS WireGuard server's public key (base64). Filled into the VPS
    # .env after generating the server keypair; the app pins its peer to it.
    tunnel_server_public_key: str = ""
    # The tunnel network kitchens are allocated from. Kept in sync with
    # tunnel.TUNNEL_CIDR; env-overridable for a future re-addressing.
    tunnel_cidr: str = "10.99.0.0/16"
    # The local tunnel agent that programs wg0 and Caddy on the VPS. In the
    # compose stack it is a sidecar (http://tunnel-agent:9300); on a single
    # host it is http://127.0.0.1:9300.
    tunnel_agent_url: str = "http://tunnel-agent:9300"
    # Shared secret the cloud app presents to the agent (X-Tunnel-Token).
    # The agent reads the same value from /etc/forager/tunnel-token.
    tunnel_agent_token: str = ""
    # How long the app waits on an agent call before treating it as down.
    tunnel_agent_timeout_seconds: float = 10.0

    model_config = {"env_prefix": "CLOUD_"}


settings = CloudSettings()
