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
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from .config import settings
from .database import SessionLocal
from .deps import utc_now_iso
from .models import (Account, AuthSession, EmailToken, PairingCode,
                     SharedRecipeReport, TotpChallenge, WebAuthnChallenge)

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


async def _loop(interval_seconds: float) -> None:
    while True:
        try:
            db = SessionLocal()
            try:
                counts = sweep(db)
            finally:
                db.close()
            logger.info(
                "Retention sweep: %s",
                ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the sweep must never die for good
            logger.exception("Retention sweep failed; retrying next interval")
        await asyncio.sleep(interval_seconds)


def start() -> asyncio.Task | None:
    """Kick off the background sweep task, or None when disabled. Called
    from the app's lifespan, so a running event loop is guaranteed."""
    hours = float(settings.retention_sweep_hours or 0)
    if hours <= 0:
        return None
    return asyncio.create_task(_loop(hours * 3600))
