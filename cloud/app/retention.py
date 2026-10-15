"""Retention sweeps: expired and stale rows get purged instead of piling up.

Single-use credentials (email tokens, sign-in challenges, pairing codes) and
sessions all carry their own expiry and simply stop working when it passes,
but the dead rows used to sit in the tables forever. This module deletes
them on a daily cadence, plus share reports past their retention window, so
the database holds personal data only as long as it is doing a job.

The sweep itself (``sweep``) is a plain synchronous function over one
session, so it unit-tests without an event loop. The background driver
(``start``) is an asyncio task created at startup: run one sweep, sleep the
configured interval, repeat, and never let an exception kill the loop (a
broken sweep logs and tries again next interval). CLOUD_RETENTION_SWEEP_HOURS=0
turns the background task off entirely.

The tunnel sweep (``tunnel_sweep``) runs on its own, shorter cadence
(CLOUD_TUNNEL_SWEEP_MINUTES) because it is about the VPS, not about stale
rows: it retries tunnel removals the agent could not take when a kitchen was
deleted, and takes down the tunnels of accounts that lost remote access on
their own (a trial that ran out, a plan that lapsed without a webhook).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from .config import settings
from .database import SessionLocal
from .deps import utc_now_iso
from .models import (Account, AuthSession, EmailToken, ExpiryObservation,
                     PairingCode, SharedRecipeReport, TotpChallenge,
                     WebAuthnChallenge)

logger = logging.getLogger("forager.retention")


def sweep(db: Session, now_iso: str | None = None) -> dict[str, int]:
    """Run one purge pass and return what was deleted, by table.

    Everything here is already inert: expired or used single-use tokens,
    expired challenges and pairing codes, sessions past their expiry,
    challenges whose account is gone, and share reports older than the
    retention window (their moderation effect was applied when filed and is
    not undone)."""
    now = now_iso or utc_now_iso()
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=max(1, int(settings.report_retention_days))))
    report_cutoff = cutoff.isoformat(timespec="seconds")
    learn_cutoff = (datetime.now(timezone.utc).date() - timedelta(
        days=max(1, int(settings.learn_retention_days)))).isoformat()

    counts = {
        "email_tokens": (
            db.query(EmailToken)
            .filter((EmailToken.expires_at < now) | (EmailToken.used == 1))
            .delete(synchronize_session=False)),
        "totp_challenges": (
            db.query(TotpChallenge)
            .filter(TotpChallenge.expires_at < now)
            .delete(synchronize_session=False)),
        "webauthn_challenges": (
            db.query(WebAuthnChallenge)
            .filter(WebAuthnChallenge.expires_at < now)
            .delete(synchronize_session=False)),
        "pairing_codes": (
            db.query(PairingCode)
            .filter(PairingCode.expires_at < now)
            .delete(synchronize_session=False)),
        "auth_sessions": (
            db.query(AuthSession)
            .filter(AuthSession.expires_at < now)
            .delete(synchronize_session=False)),
        "shared_recipe_reports": (
            db.query(SharedRecipeReport)
            .filter(SharedRecipeReport.created_at < report_cutoff)
            .delete(synchronize_session=False)),
        "expiry_observations": (
            db.query(ExpiryObservation)
            .filter(ExpiryObservation.received_date < learn_cutoff)
            .delete(synchronize_session=False)),
    }
    # Challenges left behind by an account that no longer exists (deleted
    # mid-login): dead weight with no expiry path of their own if the clock
    # above already passed them by, so sweep them explicitly.
    live_ids = db.query(Account.id)
    counts["totp_challenges"] += (
        db.query(TotpChallenge)
        .filter(~TotpChallenge.account_id.in_(live_ids))
        .delete(synchronize_session=False))
    db.commit()
    return counts


def tunnel_sweep(db: Session) -> dict[str, int]:
    """One tunnel pass: take down tunnels whose account lost remote access,
    then retry the removals the VPS agent missed (including any the first
    step just queued). Agent calls are blocking network requests, so the
    loop runs this in a worker thread."""
    from .routers.tunnel import retry_teardowns, sweep_unentitled_tunnels
    lapsed = sweep_unentitled_tunnels(db)
    return {"tunnels_lapsed": lapsed,
            "tunnel_teardowns_cleared": retry_teardowns(db)}


async def _loop(interval_seconds: float, job=sweep, name: str = "Retention",
                in_thread: bool = False) -> None:
    def run_once() -> dict[str, int]:
        db = SessionLocal()
        try:
            return job(db)
        finally:
            db.close()

    while True:
        try:
            counts = (await asyncio.to_thread(run_once) if in_thread
                      else run_once())
            logger.info(
                "%s sweep: %s", name,
                ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the sweep must never die for good
            logger.exception("%s sweep failed; retrying next interval", name)
        await asyncio.sleep(interval_seconds)


def start() -> asyncio.Task | None:
    """Kick off the background sweep task, or None when disabled. Called
    from the app's lifespan, so a running event loop is guaranteed."""
    hours = float(settings.retention_sweep_hours or 0)
    if hours <= 0:
        return None
    return asyncio.create_task(_loop(hours * 3600))


def start_tunnel_sweep() -> asyncio.Task | None:
    """Kick off the tunnel sweep task, or None when disabled."""
    minutes = float(settings.tunnel_sweep_minutes or 0)
    if minutes <= 0:
        return None
    return asyncio.create_task(_loop(minutes * 60, job=tunnel_sweep,
                                     name="Tunnel", in_thread=True))
