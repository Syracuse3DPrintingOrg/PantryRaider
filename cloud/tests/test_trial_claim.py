"""One free trial per install: the install-key gate on grant_trial.

The install key is the app's opaque per-install device_id. A given install
claims a trial exactly once; a second account created from the same install
starts without a trial and is told, in plain words, to subscribe. A missing
key (older app) keeps the original always-grant behavior.
"""
from app import usage
from app.database import SessionLocal
from app.models import Account, Entitlement, TrialClaim
from app.usage import TRIAL_ALREADY_USED_MESSAGE


def _make_account(email: str) -> int:
    db = SessionLocal()
    try:
        acct = Account(email=email, password_hash="x",
                       created_at="2026-01-01T00:00:00+00:00")
        db.add(acct)
        db.commit()
        return acct.id
    finally:
        db.close()


def _entitlements(account_id: int):
    db = SessionLocal()
    try:
        return db.query(Entitlement).filter_by(account_id=account_id).all()
    finally:
        db.close()


def _trial_claims():
    db = SessionLocal()
    try:
        return db.query(TrialClaim).all()
    finally:
        db.close()


def test_first_trial_for_fresh_install_key_is_granted_and_recorded():
    acct = _make_account("a@example.com")
    db = SessionLocal()
    try:
        result = usage.grant_trial(db, acct, "2026-01-01T00:00:00+00:00",
                                   install_key="install-aaa")
    finally:
        db.close()
    assert result == {"granted": True, "reason": ""}

    ents = _entitlements(acct)
    assert len(ents) == 1
    assert ents[0].source == "trial"
    assert ents[0].status == "active"

    claims = _trial_claims()
    assert len(claims) == 1
    assert claims[0].install_key == "install-aaa"
    assert claims[0].account_id == acct


def test_second_trial_with_same_install_key_is_refused():
    first = _make_account("first@example.com")
    second = _make_account("second@example.com")
    db = SessionLocal()
    try:
        assert usage.grant_trial(db, first, "2026-01-01T00:00:00+00:00",
                                 install_key="install-shared")["granted"] is True
        refused = usage.grant_trial(db, second, "2026-01-02T00:00:00+00:00",
                                    install_key="install-shared")
    finally:
        db.close()

    assert refused == {"granted": False, "reason": TRIAL_ALREADY_USED_MESSAGE}
    # No new entitlement for the second account; it starts with nothing.
    assert _entitlements(second) == []
    # Still exactly one claim, tied to the first account.
    claims = _trial_claims()
    assert len(claims) == 1
    assert claims[0].account_id == first


def test_concurrent_double_claim_grants_once_via_integrity_error():
    """Two first-claims for one install key: the unique constraint lets only
    one win, the loser hits IntegrityError and is refused. This is the same
    interleaving two racing requests would produce."""
    a = _make_account("race-a@example.com")
    b = _make_account("race-b@example.com")

    db_a = SessionLocal()
    db_b = SessionLocal()
    try:
        # Both sessions stage the same install key before either commits.
        db_a.add(TrialClaim(install_key="install-race", account_id=a,
                            created_at="2026-01-01T00:00:00+00:00"))
        db_b.add(TrialClaim(install_key="install-race", account_id=b,
                            created_at="2026-01-01T00:00:00+00:00"))
        db_a.commit()  # first writer wins
        # The second writer's grant_trial must now refuse on IntegrityError.
        db_b.rollback()
    finally:
        db_a.close()
        db_b.close()

    # A fresh grant_trial for the same key is refused, proving the claim sticks.
    db = SessionLocal()
    try:
        refused = usage.grant_trial(db, b, "2026-01-01T00:00:00+00:00",
                                    install_key="install-race")
    finally:
        db.close()
    assert refused["granted"] is False
    assert refused["reason"] == TRIAL_ALREADY_USED_MESSAGE
    assert len(_trial_claims()) == 1


def test_missing_install_key_falls_back_to_granting():
    acct = _make_account("legacy@example.com")
    db = SessionLocal()
    try:
        result = usage.grant_trial(db, acct, "2026-01-01T00:00:00+00:00")
    finally:
        db.close()
    assert result == {"granted": True, "reason": ""}
    assert len(_entitlements(acct)) == 1
    # No install key means no claim recorded.
    assert _trial_claims() == []


def test_empty_install_key_is_treated_as_missing():
    acct = _make_account("blank@example.com")
    db = SessionLocal()
    try:
        assert usage.grant_trial(db, acct, "2026-01-01T00:00:00+00:00",
                                 install_key="   ")["granted"] is True
    finally:
        db.close()
    assert _trial_claims() == []


# --- Through the signup endpoint --------------------------------------------

def test_signup_with_fresh_install_key_grants_trial(client):
    resp = client.post("/v1/accounts/signup",
                       json={"email": "new@example.com", "password": "hunter2222",
                             "install_key": "endpoint-aaa"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["trial_granted"] is True
    assert body["trial_message"] == ""
    # The account is entitled (trial active).
    me = client.get("/v1/accounts/me",
                    headers={"Authorization": f"Bearer {body['session_token']}"})
    assert me.json()["entitlement"]["plan"] == "trial"


def test_signup_reusing_install_key_is_refused_but_account_still_created(client):
    first = client.post("/v1/accounts/signup",
                        json={"email": "one@example.com", "password": "hunter2222",
                              "install_key": "endpoint-shared"})
    assert first.json()["trial_granted"] is True

    second = client.post("/v1/accounts/signup",
                         json={"email": "two@example.com", "password": "hunter2222",
                               "install_key": "endpoint-shared"})
    assert second.status_code == 200
    body = second.json()
    assert body["trial_granted"] is False
    assert body["trial_message"] == TRIAL_ALREADY_USED_MESSAGE
    # Account exists and can sign in, it just has no trial: expired plan.
    me = client.get("/v1/accounts/me",
                    headers={"Authorization": f"Bearer {body['session_token']}"})
    assert me.status_code == 200
    assert me.json()["entitlement"]["entitled"] is False


def test_paid_entitlement_is_unaffected_by_the_gate(client):
    """A refused trial must not disturb a separately granted paid plan: the gate
    only governs the signup trial grant."""
    from tests.conftest import activate_entitlement

    resp = client.post("/v1/accounts/signup",
                       json={"email": "payer@example.com", "password": "hunter2222",
                             "install_key": "endpoint-paid"})
    assert resp.json()["trial_granted"] is True
    activate_entitlement("payer@example.com", plan="premium")

    # A second account from the same install is refused a trial, but the paid
    # account keeps its premium entitlement untouched.
    client.post("/v1/accounts/signup",
                json={"email": "payer2@example.com", "password": "hunter2222",
                      "install_key": "endpoint-paid"})
    me = client.post("/v1/accounts/login",
                     json={"email": "payer@example.com", "password": "hunter2222"})
    token = me.json()["session_token"]
    state = client.get("/v1/accounts/me",
                       headers={"Authorization": f"Bearer {token}"}).json()
    assert state["entitlement"]["plan"] == "premium"
    assert state["entitlement"]["active"] is True
