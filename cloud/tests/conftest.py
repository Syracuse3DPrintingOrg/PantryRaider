import os
import sys
from pathlib import Path

import pytest

# Make `app` importable the same way the container does (workdir /app == cloud/).
sys.path.insert(0, str(Path(__file__).parent.parent))

# Tests run on a shared in-memory SQLite database; production is Postgres
# (set via CLOUD_DATABASE_URL). Must be set before app.config is imported.
os.environ.setdefault("CLOUD_DATABASE_URL", "sqlite://")
# Rate limits off by default; the rate-limit tests re-enable them explicitly.
os.environ.setdefault("CLOUD_SIGNUP_RATE_PER_MINUTE", "0")
os.environ.setdefault("CLOUD_LOGIN_RATE_PER_MINUTE", "0")
os.environ.setdefault("CLOUD_PROXY_RATE_PER_MINUTE", "0")
os.environ.setdefault("CLOUD_LEARN_RATE_PER_MINUTE", "0")
# TestClient speaks plain HTTP, so a Secure cookie would never come back.
os.environ.setdefault("CLOUD_COOKIE_SECURE", "0")
# A real (non-placeholder) webhook secret so the signature path is exercised;
# the handler refuses the shipped placeholder, so tests must configure a value
# just like a real deployment does.
os.environ.setdefault("CLOUD_STRIPE_WEBHOOK_SECRET", "whsec_test_secret_override")
# A fixed valid Fernet key so two-factor enrollment (which now stores the seed
# encrypted at rest) works in the suite, exactly as a real deployment sets one.
# The startup fail-closed test clears it explicitly to exercise the missing-key
# path.
os.environ.setdefault("CLOUD_TOTP_SECRET_KEY",
                      "d7unhpVwhELezF83mmRqyIw_D7mrpagqElJ4SNZ6ZUg=")

from fastapi.testclient import TestClient  # noqa: E402

from app import ratelimit  # noqa: E402
from app.database import Base, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()


@pytest.fixture(autouse=True)
def clean_db():
    """Each test starts from an empty schema and a fresh rate-limit window."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    ratelimit.reset()
    yield


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def session_token(client):
    """A signed-up account's portal session token."""
    resp = client.post("/v1/accounts/signup",
                       json={"email": "dan@example.com", "password": "hunter2222"})
    assert resp.status_code == 200
    return resp.json()["session_token"]


@pytest.fixture
def instance_token(client, session_token):
    """A paired instance token for the signed-up account."""
    code = client.post("/v1/pairing/code",
                       headers={"Authorization": f"Bearer {session_token}"})
    resp = client.post("/v1/pairing/redeem",
                       json={"code": code.json()["code"], "name": "Kitchen Pi"})
    assert resp.status_code == 200
    return resp.json()["instance_token"]


def activate_entitlement(account_email="dan@example.com", plan="premium"):
    """Grant an active paid entitlement directly, standing in for the Stripe
    flow (source "stripe" so it outranks the signup trial)."""
    from app.config import PLAN_QUOTAS
    from app.database import SessionLocal
    from app.models import Account, Entitlement

    db = SessionLocal()
    try:
        account = db.query(Account).filter_by(email=account_email).first()
        db.add(Entitlement(account_id=account.id, plan=plan, status="active",
                           monthly_token_quota=PLAN_QUOTAS[plan],
                           source="stripe",
                           updated_at="2026-01-01T00:00:00+00:00"))
        db.commit()
        return account.id
    finally:
        db.close()


def expire_trial(account_email="dan@example.com"):
    """Push the signup trial's expiry into the past so an account with no
    paid plan resolves to the expired (zero-quota) state."""
    from app.database import SessionLocal
    from app.models import Account, Entitlement

    db = SessionLocal()
    try:
        account = db.query(Account).filter_by(email=account_email).first()
        ent = (db.query(Entitlement)
               .filter_by(account_id=account.id, source="trial").first())
        ent.expires_at = "2000-01-01T00:00:00+00:00"
        db.commit()
        return account.id
    finally:
        db.close()
