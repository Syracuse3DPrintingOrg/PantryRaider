"""Self-serve account deletion: the re-auth gate, the abort-on-billing rule,
and the full cascade (rows and files), with what must survive surviving."""
import pytest

from app import stripe_api
from app.config import settings
from app.database import SessionLocal
from app.deps import utc_now_iso
from app.models import (Account, AdminAction, AuthSession, CloudBackup,
                        CommunityRecipe, EmailToken, Entitlement, Instance,
                        PairingCode, RecipeRating, RecipeReport, RecoveryCode,
                        SharedRecipe, SharedRecipeReport, Subscription,
                        TotpChallenge, TrialClaim, UsageLedger,
                        WebAuthnChallenge, WebAuthnCredential)
from app.security import totp_now


PASSWORD = "hunter2222"


def _portal_login(client, email="dan@example.com"):
    resp = client.post("/signup", data={"email": email, "password": PASSWORD,
                                        "confirm_password": PASSWORD},
                       follow_redirects=False)
    assert resp.status_code == 303


@pytest.fixture
def backup_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "backup_storage_dir", str(tmp_path))
    return tmp_path


def _populate(backup_dir, email="dan@example.com"):
    """Give the account one of everything the cascade must handle, plus a
    bystander account whose data must survive untouched."""
    db = SessionLocal()
    try:
        now = utc_now_iso()
        account = db.query(Account).filter_by(email=email).first()
        other = Account(email="other@example.com", password_hash="x",
                        created_at=now)
        db.add(other)
        db.commit()
        aid = account.id

        # Rows keyed to the account across every table.
        db.add(EmailToken(token_hash="e1", account_id=aid, purpose="verify",
                          expires_at="2999-01-01", created_at=now))
        db.add(RecoveryCode(account_id=aid, code_hash="r1", created_at=now))
        db.add(TotpChallenge(token_hash="t1", account_id=aid,
                             expires_at="2999-01-01", created_at=now))
        db.add(WebAuthnCredential(account_id=aid, credential_id="c1",
                                  created_at=now))
        db.add(WebAuthnChallenge(token_hash="w1", purpose="register",
                                 account_id=aid, expires_at="2999-01-01",
                                 created_at=now))
        db.add(PairingCode(code_hash="p1", account_id=aid,
                           expires_at="2999-01-01", created_at=now))
        inst = Instance(token_hash="i1", account_id=aid, name="Kitchen",
                        created_at=now)
        db.add(inst)
        db.commit()
        db.add(UsageLedger(account_id=aid, instance_id=inst.id,
                           month_key="2026-07", tokens=5, kind="food",
                           created_at=now))
        db.add(Subscription(account_id=aid, stripe_customer_id="cus_1",
                            stripe_subscription_id="sub_del",
                            status="active", updated_at=now))
        db.add(TrialClaim(install_key="install-1", account_id=aid,
                          created_at=now))

        # A backup row with a real file on disk.
        acct_dir = backup_dir / str(aid)
        acct_dir.mkdir(parents=True, exist_ok=True)
        (acct_dir / "backup-1.zip").write_bytes(b"zip")
        db.add(CloudBackup(account_id=aid, instance_id=inst.id,
                           filename="backup-1.zip", size_bytes=3,
                           created_at=now))

        # Community content: their own recipe (to be anonymized), plus their
        # rating and flag on the other member's recipe (to be removed and
        # recounted).
        mine = CommunityRecipe(title="Mine", slug="mine", share_token="minetok001",
                               ingredients="[]", steps="[]",
                               attribution="By Dan", submitter_account_id=aid,
                               created_at=now)
        theirs = CommunityRecipe(title="Theirs", slug="theirs",
                                 share_token="theirstok01",
                                 ingredients="[]", steps="[]",
                                 attribution="x",
                                 submitter_account_id=other.id,
                                 rating_count=1, rating_sum=5, report_count=1,
                                 created_at=now)
        db.add_all([mine, theirs])
        db.commit()
        db.add(RecipeRating(recipe_id=theirs.id, account_id=aid, stars=5,
                            created_at=now))
        db.add(RecipeReport(recipe_id=theirs.id, account_id=aid, reason="meh",
                            created_at=now))

        # Shares: one they sent (with an anonymous report on it), one sent to
        # them by the other member, and their flag on the other member's share.
        my_share = SharedRecipe(token="tok-mine", owner_account_id=aid,
                                title="Soup", ingredients="[]", steps="[]",
                                attribution="Dan", created_at=now)
        to_me = SharedRecipe(token="tok-inbox", owner_account_id=other.id,
                             recipient_account_id=aid, title="Stew",
                             ingredients="[]", steps="[]", attribution="o",
                             created_at=now)
        db.add_all([my_share, to_me])
        db.commit()
        db.add(SharedRecipeReport(share_id=my_share.id,
                                  reporter_key="ip:abcd1234abcd1234",
                                  created_at=now))
        db.add(SharedRecipeReport(share_id=to_me.id,
                                  reporter_key=f"acct:{aid}", created_at=now))
        db.commit()
        return aid, other.id, mine.id, theirs.id
    finally:
        db.close()


def _delete(client, credential=PASSWORD, confirm="delete", totp=""):
    return client.post("/account/delete",
                       data={"credential": credential, "totp": totp,
                             "confirm_text": confirm},
                       follow_redirects=False)


def test_full_cascade_removes_everything_and_keeps_what_must_stay(
        client, backup_dir, monkeypatch):
    _portal_login(client)
    aid, other_id, mine_id, theirs_id = _populate(backup_dir)
    cancelled = {}
    monkeypatch.setattr(stripe_api, "cancel_now",
                        lambda sub_id: cancelled.setdefault("sub", sub_id))

    resp = _delete(client)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/?deleted=1"
    assert cancelled["sub"] == "sub_del"

    db = SessionLocal()
    try:
        # The account row and everything keyed to it are gone.
        assert db.get(Account, aid) is None
        for model in (AuthSession, EmailToken, RecoveryCode, TotpChallenge,
                      WebAuthnCredential, WebAuthnChallenge, PairingCode,
                      Instance, UsageLedger, Subscription, Entitlement,
                      CloudBackup):
            assert db.query(model).filter_by(account_id=aid).count() == 0, model
        assert db.query(RecipeRating).filter_by(account_id=aid).count() == 0
        assert db.query(RecipeReport).filter_by(account_id=aid).count() == 0
        assert (db.query(SharedRecipe).filter_by(owner_account_id=aid)
                .count() == 0)
        assert (db.query(SharedRecipeReport)
                .filter_by(reporter_key=f"acct:{aid}").count() == 0)

        # The backup file is really off the disk.
        assert not (backup_dir / str(aid) / "backup-1.zip").exists()

        # Their community recipe survives, anonymized.
        mine = db.get(CommunityRecipe, mine_id)
        assert mine is not None
        assert mine.submitter_account_id is None
        assert mine.attribution == "a former member"

        # The other member's recipe had its denormalized totals recomputed.
        theirs = db.get(CommunityRecipe, theirs_id)
        assert theirs.rating_count == 0 and theirs.rating_sum == 0
        assert theirs.report_count == 0

        # The share aimed at the deleted account lost its recipient link but
        # still belongs to its owner.
        inbox = db.query(SharedRecipe).filter_by(token="tok-inbox").first()
        assert inbox.recipient_account_id is None
        assert inbox.owner_account_id == other_id

        # What must survive: the trial claim (install abuse gate) and an
        # audit row recording the deletion, with no email in it.
        assert db.query(TrialClaim).filter_by(install_key="install-1").count() == 1
        audit = db.query(AdminAction).filter_by(action="account-delete").first()
        assert audit is not None and audit.account_id == aid
        assert "@" not in (audit.admin_email + audit.detail)

        # The bystander account is untouched.
        assert db.get(Account, other_id) is not None
    finally:
        db.close()

    # The old session cookie no longer works.
    assert client.get("/account", follow_redirects=False).headers[
        "location"] == "/login"


def test_deletion_requires_the_password_and_the_typed_delete(client, backup_dir):
    _portal_login(client)
    assert _delete(client, credential="wrong-password").status_code == 401
    assert _delete(client, confirm="").status_code == 400
    assert _delete(client, confirm="remove me").status_code == 400
    db = SessionLocal()
    try:
        assert db.query(Account).count() == 1
    finally:
        db.close()


def test_deletion_requires_the_totp_code_when_2fa_is_on(client, backup_dir):
    _portal_login(client)
    db = SessionLocal()
    try:
        account = db.query(Account).first()
        account.totp_secret = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
        account.totp_enabled = 1
        db.commit()
        secret = account.totp_secret
    finally:
        db.close()
    assert _delete(client, totp="000000").status_code == 401
    assert _delete(client, totp=totp_now(secret)).status_code == 303
    db = SessionLocal()
    try:
        assert db.query(Account).count() == 0
    finally:
        db.close()


def test_google_account_confirms_with_its_typed_email(client, backup_dir):
    # A Google-created account has no password; it re-authenticates by typing
    # its own email address in full.
    db = SessionLocal()
    try:
        db.add(Account(email="goog@example.com", password_hash="",
                       auth_provider="google", email_verified=1,
                       created_at=utc_now_iso()))
        db.commit()
    finally:
        db.close()
    from app.routers.accounts import _issue_session
    db = SessionLocal()
    try:
        account = db.query(Account).filter_by(email="goog@example.com").first()
        token = _issue_session(db, account.id)
    finally:
        db.close()
    client.cookies.set("forager_session", token)
    assert _delete(client, credential="not-my-email").status_code == 401
    assert _delete(client, credential="GOOG@example.com").status_code == 303


def test_billing_failure_aborts_the_whole_deletion(client, backup_dir,
                                                   monkeypatch):
    _portal_login(client)
    aid, *_ = _populate(backup_dir)

    def boom(sub_id):
        raise stripe_api.StripeApiError("stripe down")

    monkeypatch.setattr(stripe_api, "cancel_now", boom)
    resp = _delete(client)
    assert resp.status_code == 503
    assert "nothing was deleted" in resp.text
    db = SessionLocal()
    try:
        # Nothing changed: account, subscription, backups all still there.
        assert db.get(Account, aid) is not None
        assert db.query(Subscription).filter_by(account_id=aid).count() == 1
        assert db.query(CloudBackup).filter_by(account_id=aid).count() == 1
    finally:
        db.close()
    assert (backup_dir / str(aid) / "backup-1.zip").exists()


def test_deletion_sends_a_goodbye_email_best_effort(client, backup_dir,
                                                    monkeypatch):
    _portal_login(client)
    sent = {}

    def fake_send(to, subject, text, html=None):
        sent["to"] = to
        sent["subject"] = subject
        return True

    monkeypatch.setattr("app.routers.billing.send_email", fake_send)
    assert _delete(client).status_code == 303
    assert sent["to"] == "dan@example.com"
    assert "deleted" in sent["subject"]


def test_delete_page_needs_a_session(client):
    resp = client.get("/account/delete", follow_redirects=False)
    assert resp.headers["location"] == "/login"
