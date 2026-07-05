"""Database models for the cloud platform.

Timestamps are stored as UTC ISO-8601 strings, matching the convention the
app uses in its device registry. Bearer tokens (sessions and instances) are
stored only as SHA-256 hashes, so the database never holds a usable
credential.
"""
from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String
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
    # Admin kill switch. A disabled account cannot log in, provision, or use
    # the AI proxy; every seam answers with a clear message.
    disabled: Mapped[int] = mapped_column(Integer, default=0)
    # Per-account failed-login lockout, enforced in accounts.authenticate.
    # failed_logins counts consecutive wrong passwords; once it crosses the
    # configured threshold, locked_until holds an ISO timestamp until which
    # even the right password is refused. A successful login resets both.
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[str] = mapped_column(String(40))


class AuthSession(Base):
    """A portal login session, keyed by the hash of its bearer token."""

    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    expires_at: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[str] = mapped_column(String(40))


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
