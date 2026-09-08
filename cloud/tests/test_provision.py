"""One-step provisioning, self-revoke, and the instance status fields."""


def provision(client, email="dan@example.com", password="hunter2222",
              device_name="Kitchen Pi"):
    return client.post("/v1/instances/provision",
                       json={"email": email, "password": password,
                             "device_name": device_name})


def test_provision_returns_the_full_contract(client, session_token):
    resp = provision(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["instance_token"].startswith("prc_")
    assert body["account_email"] == "dan@example.com"
    assert body["plan"] == "trial"
    assert body["quota"] == 2_000_000
    assert body["month_used"] == 0
    assert body["suggested_public_url"] is None

    # The token works immediately and the device name stuck.
    me = client.get("/v1/instance/me",
                    headers={"Authorization": f"Bearer {body['instance_token']}"})
    assert me.status_code == 200
    assert me.json()["name"] == "Kitchen Pi"


def test_provision_rejects_wrong_credentials(client, session_token):
    assert provision(client, password="wrong-pass").status_code == 401
    assert provision(client, email="who@example.com").status_code == 401


def test_provision_shares_the_login_rate_limit(client, session_token,
                                               monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "login_rate_per_minute", 2)
    # Login attempts and provision attempts guess the same passwords, so
    # they draw from one window.
    assert client.post("/v1/accounts/login",
                       json={"email": "dan@example.com", "password": "wrong"}
                       ).status_code == 401
    assert provision(client, password="wrong").status_code == 401
    assert provision(client).status_code == 429


def test_login_rate_limit(client, session_token, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "login_rate_per_minute", 2)
    creds = {"email": "dan@example.com", "password": "hunter2222"}
    for _ in range(2):
        assert client.post("/v1/accounts/login", json=creds).status_code == 200
    assert client.post("/v1/accounts/login", json=creds).status_code == 429


def test_instance_me_includes_account_email(client, instance_token):
    me = client.get("/v1/instance/me",
                    headers={"Authorization": f"Bearer {instance_token}"})
    assert me.status_code == 200
    assert me.json()["account_email"] == "dan@example.com"


def test_self_revoke(client, instance_token):
    headers = {"Authorization": f"Bearer {instance_token}"}
    resp = client.delete("/v1/instance", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"revoked": True}
    # The token is dead from that point on.
    assert client.get("/v1/instance/me", headers=headers).status_code == 401
    assert client.delete("/v1/instance", headers=headers).status_code == 401


def test_revoke_keeps_the_months_usage(client, session_token, instance_token):
    """Unlink-and-relink must not reset the quota: the ledger outlives the
    instance row."""
    from app import usage
    from app.database import SessionLocal
    from app.models import Account, Instance

    db = SessionLocal()
    account_id = db.query(Account).first().id
    instance_id = db.query(Instance).first().id
    usage.record(db, account_id, instance_id, 5_000, "food",
                 usage.month_key(), "2026-07-05T00:00:00+00:00")
    db.close()

    headers = {"Authorization": f"Bearer {instance_token}"}
    assert client.delete("/v1/instance", headers=headers).status_code == 200

    me = client.get("/v1/accounts/me",
                    headers={"Authorization": f"Bearer {session_token}"})
    assert me.json()["entitlement"]["used"] == 5_000
    # And a freshly provisioned instance reports the surviving usage.
    assert provision(client).json()["month_used"] == 5_000
