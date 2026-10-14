"""The retention sweep: expired and stale rows are purged, live ones stay."""
from datetime import datetime, timedelta, timezone

from app import retention
from app.config import settings
from app.database import SessionLocal
from app.deps import utc_now_iso
from app.models import (Account, AuthSession, EmailToken, PairingCode,
                        SharedRecipe, SharedRecipeReport, TotpChallenge,
                        WebAuthnChallenge)

PAST = "2020-01-01T00:00:00+00:00"
FUTURE = "2999-01-01T00:00:00+00:00"


def _seed():
    db = SessionLocal()
    now = utc_now_iso()
    account = Account(email="dan@example.com", password_hash="x",
                      created_at=now)
    db.add(account)
    db.commit()
    aid = account.id

    # One expired and one live row per expiring table, plus a used token.
    db.add_all([
        EmailToken(token_hash="dead", account_id=aid, purpose="reset",
                   expires_at=PAST, created_at=now),
        EmailToken(token_hash="used", account_id=aid, purpose="verify",
                   expires_at=FUTURE, used=1, created_at=now),
        EmailToken(token_hash="live", account_id=aid, purpose="verify",
                   expires_at=FUTURE, created_at=now),
        TotpChallenge(token_hash="dead-t", account_id=aid,
                      expires_at=PAST, created_at=now),
        TotpChallenge(token_hash="live-t", account_id=aid,
                      expires_at=FUTURE, created_at=now),
        TotpChallenge(token_hash="orphan-t", account_id=aid + 999,
                      expires_at=FUTURE, created_at=now),
        WebAuthnChallenge(token_hash="dead-w", purpose="auth",
                          expires_at=PAST, created_at=now),
        WebAuthnChallenge(token_hash="live-w", purpose="auth",
                          expires_at=FUTURE, created_at=now),
        PairingCode(code_hash="dead-p", account_id=aid, expires_at=PAST,
                    created_at=now),
        PairingCode(code_hash="live-p", account_id=aid, expires_at=FUTURE,
                    created_at=now),
        AuthSession(token_hash="dead-s", account_id=aid, expires_at=PAST,
                    created_at=now),
        AuthSession(token_hash="live-s", account_id=aid, expires_at=FUTURE,
                    created_at=now),
    ])
    share = SharedRecipe(token="tok", owner_account_id=aid, title="Soup",
                         ingredients="[]", steps="[]", attribution="d",
                         created_at=now)
    db.add(share)
    db.commit()
    old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat(
        timespec="seconds")
    db.add_all([
        SharedRecipeReport(share_id=share.id, reporter_key="ip:old",
                           created_at=old),
        SharedRecipeReport(share_id=share.id, reporter_key="ip:new",
                           created_at=now),
    ])
    db.commit()
    return db


def test_sweep_purges_expired_used_orphaned_and_old(monkeypatch):
    monkeypatch.setattr(settings, "report_retention_days", 90)
    db = _seed()
    try:
        counts = retention.sweep(db)
        assert counts["email_tokens"] == 2       # expired + used
        assert counts["totp_challenges"] == 2    # expired + orphan
        assert counts["webauthn_challenges"] == 1
        assert counts["pairing_codes"] == 1
        assert counts["auth_sessions"] == 1
        assert counts["shared_recipe_reports"] == 1

        # The live rows all survive.
        assert db.query(EmailToken).count() == 1
        assert db.query(EmailToken).first().token_hash == "live"
        assert db.query(TotpChallenge).count() == 1
        assert db.query(WebAuthnChallenge).count() == 1
        assert db.query(PairingCode).count() == 1
        assert db.query(AuthSession).count() == 1
        reports = db.query(SharedRecipeReport).all()
        assert [r.reporter_key for r in reports] == ["ip:new"]
    finally:
        db.close()


def test_sweep_is_a_quiet_no_op_on_a_clean_database():
    db = SessionLocal()
    try:
        counts = retention.sweep(db)
        assert all(v == 0 for v in counts.values())
    finally:
        db.close()


def test_background_task_respects_the_off_switch(monkeypatch):
    import asyncio

    monkeypatch.setattr(settings, "retention_sweep_hours", 0)
    assert retention.start() is None

    # Turned on, it schedules the loop; cancel it right away.
    monkeypatch.setattr(settings, "retention_sweep_hours", 24)

    async def run():
        task = retention.start()
        assert task is not None
        task.cancel()

    asyncio.run(run())
