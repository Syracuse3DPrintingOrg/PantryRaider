"""Database models for the cloud platform.

Timestamps are stored as UTC ISO-8601 strings, matching the convention the
app uses in its device registry. Bearer tokens (sessions and instances) are
stored only as SHA-256 hashes, so the database never holds a usable
credential.
"""
from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    # Empty for accounts created via Google sign-in until the owner sets a
    # password on the account page; verify_password rejects an empty hash,
    # so password login simply fails for them.
    password_hash: Mapped[str] = mapped_column(String(512))
    # How the account was created: "password" or "google". Informational;
    # login ability is governed by password_hash above.
    auth_provider: Mapped[str] = mapped_column(String(20), default="password")
    # Whether the owner has confirmed this email address by following a
    # verification link. Advisory only: an unverified account still works
    # everywhere, so a misconfigured mail server can never brick signups.
    # Google-created accounts start verified (Google already confirmed the
    # address). Existing rows default to 0 (unverified) and stay usable.
    email_verified: Mapped[int] = mapped_column(Integer, default=0)
    # Admin kill switch. A disabled account cannot log in, provision, or use
    # the AI proxy; every seam answers with a clear message.
    disabled: Mapped[int] = mapped_column(Integer, default=0)
    # Per-account failed-login lockout, enforced in accounts.authenticate.
    # failed_logins counts consecutive wrong passwords; once it crosses the
    # configured threshold, locked_until holds an ISO timestamp until which
    # even the right password is refused. A successful login resets both.
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[str] = mapped_column(String(40), default="")
    # Two-factor sign-in (TOTP). totp_secret is the base32 authenticator
    # secret, empty until the owner turns 2FA on and confirms a code; it is
    # never shown again after enrollment. totp_enabled is the switch every
    # sign-in seam checks. Existing rows default to off, so nothing changes
    # for accounts that never enroll.
    totp_secret: Mapped[str] = mapped_column(String(64), default="")
    totp_enabled: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(40))


class AuthSession(Base):
    """A portal login session, keyed by the hash of its bearer token."""

    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    expires_at: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[str] = mapped_column(String(40))


class EmailToken(Base):
    """A single-use, expiring token emailed to an account, for password
    resets and email verification.

    One table serves both jobs, told apart by ``purpose`` ("reset" or
    "verify"): same shape, same lifecycle, and one place to reason about
    expiry and single use. Stored hashed like every other credential, so a
    database peek never yields a usable link.
    """

    __tablename__ = "email_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    # "reset" (forgotten password) or "verify" (confirm email address).
    purpose: Mapped[str] = mapped_column(String(20), index=True)
    expires_at: Mapped[str] = mapped_column(String(40))
    # Flipped to 1 the moment the token is redeemed, so a reset link or a
    # verification link works exactly once.
    used: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(40))


class RecoveryCode(Base):
    """A single-use two-factor recovery code, stored only as a hash.

    Handed to the owner in a set when 2FA is turned on (or regenerated), so a
    lost authenticator app is not a lockout. Burned the moment it is used, the
    same single-use lifecycle as EmailToken. Regenerating deletes the old set.
    """

    __tablename__ = "recovery_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # Flipped to 1 the moment the code is redeemed, so each works exactly once.
    used: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(40))


class TotpChallenge(Base):
    """A half-finished sign-in waiting on a two-factor code.

    Minted after a correct password (or a Google login) when the account has
    2FA on, and carried to the browser in a short-lived cookie. It is NOT a
    session: it only names the account and expires fast, so an abandoned
    challenge never logs anyone in. Redeeming a correct code deletes the row
    and issues the real session.
    """

    __tablename__ = "totp_challenges"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    expires_at: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[str] = mapped_column(String(40))
    # App-return context, set only when the challenge was raised by the Google
    # app-return flow (flow=app) for a 2FA account. Empty for a portal login.
    # When present, redeeming the code mints a provision code and sends the
    # browser back to the app instead of starting a portal session.
    return_url: Mapped[str] = mapped_column(String(2048), default="")
    device_name: Mapped[str] = mapped_column(String(120), default="")


class Instance(Base):
    """A paired install. Created by redeeming a pairing code; authenticates
    the AI proxy with its (hashed) instance token."""

    __tablename__ = "instances"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    app_version: Mapped[str] = mapped_column(String(40), default="")
    deployment_mode: Mapped[str] = mapped_column(String(40), default="")
    last_seen_at: Mapped[str] = mapped_column(String(40), default="")
    # The install's suggested public URL, set when a remote-access tunnel is
    # enabled and cleared when it is disabled. Surfaced by /v1/instance/me and
    # provision so the app can show and link its own remote address. Empty
    # means no tunnel; the app then falls back to its LAN address.
    public_url: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[str] = mapped_column(String(40))


class PairingCode(Base):
    """A short-lived, single-use code minted in the portal and typed into an
    install's settings. Stored hashed like every other credential."""

    __tablename__ = "pairing_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    expires_at: Mapped[str] = mapped_column(String(40))
    redeemed: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(40))


class Subscription(Base):
    """Mirror of the Stripe subscription object, updated by the webhook. The
    entitlement row, not this, is what requests check."""

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    stripe_customer_id: Mapped[str] = mapped_column(String(120), default="")
    stripe_subscription_id: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(40), default="")
    current_period_end: Mapped[str] = mapped_column(String(40), default="")
    updated_at: Mapped[str] = mapped_column(String(40), default="")


class Entitlement(Base):
    """What the account is allowed right now: at most one row per source
    (trial, stripe, comp). usage.resolve_entitlement picks the governing
    row, so a paid plan can sit alongside the signup trial."""

    __tablename__ = "entitlements"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    plan: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(20), default="inactive")  # active | inactive
    monthly_token_quota: Mapped[int] = mapped_column(Integer, default=0)
    # Where the entitlement came from: "trial" (granted at signup),
    # "stripe" (webhook), or "comp" (granted from the admin panel). Empty
    # on rows written before this column existed (treated as Stripe).
    source: Mapped[str] = mapped_column(String(20), default="")
    # Optional hard expiry (ISO timestamp), used by trials and comped
    # plans. An active row past this moment no longer counts; Stripe rows
    # leave it empty and expire via webhook status changes instead.
    expires_at: Mapped[str] = mapped_column(String(40), default="")
    updated_at: Mapped[str] = mapped_column(String(40), default="")


class TrialClaim(Base):
    """One free trial per install.

    Records that a given install has already consumed its signup trial, so the
    same physical copy of the app cannot mint a fresh 30-day trial by creating
    account after account. The install key is an opaque, random per-install id
    the app generates and keeps (its device_id); it identifies a copy of the
    software, not a person, and is not personal data.

    The unique constraint is the enforcement point: two concurrent first-claims
    for the same key resolve to exactly one winner (the loser hits an
    IntegrityError and is refused a trial), the same idempotency pattern the
    Stripe event table uses.
    """

    __tablename__ = "trial_claims"

    id: Mapped[int] = mapped_column(primary_key=True)
    install_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # The account that first claimed this install's trial. Kept for support and
    # auditing; the gate itself only cares that the key exists.
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    created_at: Mapped[str] = mapped_column(String(40))


class UsageLedger(Base):
    """Append-only token usage. Monthly totals are sums over (account, month);
    the per-account counterpart of the app's local ai_usage.json."""

    __tablename__ = "usage_ledger"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    # Nullable with SET NULL: revoking an instance deletes its row but must
    # not erase the month's usage (otherwise unlink-and-relink would reset
    # the quota). Account totals sum by account_id and are unaffected.
    instance_id: Mapped[int | None] = mapped_column(
        ForeignKey("instances.id", ondelete="SET NULL"), nullable=True, index=True)
    month_key: Mapped[str] = mapped_column(String(7), index=True)  # YYYY-MM
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(20), default="")  # food | receipt | enrich
    created_at: Mapped[str] = mapped_column(String(40))


class AdminAction(Base):
    """Audit trail for the admin panel: one row per admin mutation."""

    __tablename__ = "admin_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    admin_email: Mapped[str] = mapped_column(String(255), default="")
    action: Mapped[str] = mapped_column(String(40))  # disable, enable, comp, ...
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    detail: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[str] = mapped_column(String(40))


class StripeEvent(Base):
    """Processed Stripe event ids, so retried deliveries are idempotent."""

    __tablename__ = "stripe_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(80), default="")
    processed_at: Mapped[str] = mapped_column(String(40))


class TunnelPeer(Base):
    """One WireGuard remote-access tunnel per kitchen.

    The kitchen dials out to the VPS as a WireGuard peer and Caddy
    reverse-proxies its subdomain to the tunnel IP. The database holds only
    the kitchen's public key (the private key never leaves the device), the
    allocated tunnel IP, and the subdomain. One row per instance (a kitchen
    has at most one tunnel); disabling remote access deletes the row.
    """

    __tablename__ = "tunnel_peers"

    id: Mapped[int] = mapped_column(primary_key=True)
    # One tunnel per kitchen: the instance id is unique.
    instance_id: Mapped[int] = mapped_column(
        ForeignKey("instances.id", ondelete="CASCADE"), unique=True, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    # The kitchen's WireGuard public key (base64). The private key stays on
    # the device; the VPS only ever sees this public half.
    public_key: Mapped[str] = mapped_column(String(64), default="")
    # The stable /32 assigned inside 10.99.0.0/16, e.g. "10.99.4.7".
    tunnel_ip: Mapped[str] = mapped_column(String(40), index=True)
    # The port the kitchen's app listens on behind the tunnel. A Pi appliance
    # publishes on the host at 9284 (the default); a plain server runs
    # WireGuard inside the app container and is reached on its internal 8000.
    # Caddy reverse-proxies to tunnel_ip:app_port, so the port rides along to
    # the VPS agent. Existing peers default to 9284.
    app_port: Mapped[int] = mapped_column(Integer, default=9284)
    # The public subdomain, sanitized from the hostname hint and made unique,
    # e.g. "kitchen-pi" for kitchen-pi.forager.pantryraider.app.
    subdomain: Mapped[str] = mapped_column(String(63), unique=True, index=True)
    # Last WireGuard handshake seen for this peer (ISO-8601), updated
    # best-effort. Empty until the tunnel first connects.
    last_handshake: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[str] = mapped_column(String(40))


class CommunityRecipe(Base):
    """A recipe a member shared with the Forager community.

    Ingredients and steps are stored as JSON-encoded lists of plain strings,
    the same shape the app sends and reads back. The rating totals are kept
    denormalized (rating_count and rating_sum) so a listing can show an
    average without scanning the ratings table on every card. report_count is
    a running tally used to surface trouble for review and to auto-hide a
    recipe that too many people have flagged.
    """

    __tablename__ = "community_recipes"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    # JSON-encoded list[str].
    ingredients: Mapped[str] = mapped_column(Text, default="[]")
    steps: Mapped[str] = mapped_column(Text, default="[]")
    image_url: Mapped[str] = mapped_column(String(1024), default="")
    # Required free text: who to credit or where the recipe came from. The
    # submitter must fill it, so shared recipes always carry their source.
    attribution: Mapped[str] = mapped_column(String(500), default="")
    submitter_account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id"), index=True)
    # "approved" (visible in the community browser), "pending" (waiting on a
    # review), "hidden" (pulled from view), or "rejected". Stage 1 ships with
    # no moderation panel, so a new recipe lands "approved" and is browsable
    # right away; the review workflow that mints "pending" arrives in a later
    # stage without any schema change.
    status: Mapped[str] = mapped_column(String(20), default="approved", index=True)
    rating_count: Mapped[int] = mapped_column(Integer, default=0)
    rating_sum: Mapped[int] = mapped_column(Integer, default=0)
    report_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[str] = mapped_column(String(40), default="")


class RecipeRating(Base):
    """One member's star rating of a shared recipe.

    The unique pair (recipe_id, account_id) means a member has at most one
    rating per recipe; rating again updates the existing row instead of
    stacking a second vote. The recipe's rating_count and rating_sum are kept
    in step whenever a row here is written.
    """

    __tablename__ = "recipe_ratings"
    __table_args__ = (UniqueConstraint("recipe_id", "account_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    recipe_id: Mapped[int] = mapped_column(
        ForeignKey("community_recipes.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    stars: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(40))


class RecipeReport(Base):
    """A member's flag that a shared recipe needs a look.

    Recording a report bumps the recipe's report_count; once enough members
    flag the same recipe it is hidden from the browser automatically, pending
    a human review in a later stage.
    """

    __tablename__ = "recipe_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    recipe_id: Mapped[int] = mapped_column(
        ForeignKey("community_recipes.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    reason: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[str] = mapped_column(String(40))
