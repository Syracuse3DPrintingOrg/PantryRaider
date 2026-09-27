"""Pairing-code lifecycle and instance authentication."""
from app.database import SessionLocal
from app.models import Instance, PairingCode


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_pair_and_instance_me(client, session_token):
    code = client.post("/v1/pairing/code", headers=_auth(session_token))
    assert code.status_code == 200
    assert len(code.json()["code"]) == 8

    redeemed = client.post("/v1/pairing/redeem",
                           json={"code": code.json()["code"], "name": "Kitchen Pi"})
    assert redeemed.status_code == 200
    token = redeemed.json()["instance_token"]
    assert token.startswith("prc_")

    me = client.get("/v1/instance/me", headers=_auth(token))
    assert me.status_code == 200
    assert me.json()["name"] == "Kitchen Pi"
    # A fresh signup runs on its trial, and the app reads "active" as "this
    # account can use Forager right now", so the trial counts.
    ent = me.json()["entitlement"]
    assert ent["active"] is True
    assert ent["plan"] == "trial"
    assert ent["source"] == "trial"
    assert ent["plan_label"].startswith("Trial until ")

    # The instance shows up in the account's portal view.
    acct = client.get("/v1/accounts/me", headers=_auth(session_token))
    assert [i["name"] for i in acct.json()["instances"]] == ["Kitchen Pi"]


def test_pairing_code_is_single_use(client, session_token):
    code = client.post("/v1/pairing/code",
                       headers=_auth(session_token)).json()["code"]
    assert client.post("/v1/pairing/redeem", json={"code": code}).status_code == 200
    assert client.post("/v1/pairing/redeem", json={"code": code}).status_code == 400


def test_pairing_code_expires(client, session_token):
    code = client.post("/v1/pairing/code",
                       headers=_auth(session_token)).json()["code"]
    db = SessionLocal()
    try:
        row = db.query(PairingCode).first()
        row.expires_at = "2000-01-01T00:00:00+00:00"
        db.commit()
    finally:
        db.close()
    assert client.post("/v1/pairing/redeem", json={"code": code}).status_code == 400


def test_redeem_unknown_code(client):
    assert client.post("/v1/pairing/redeem",
                       json={"code": "NOPE1234"}).status_code == 400


def test_instance_token_is_stored_hashed(client, instance_token):
    db = SessionLocal()
    try:
        inst = db.query(Instance).first()
        assert instance_token not in (inst.token_hash or "")
        assert len(inst.token_hash) == 64
    finally:
        db.close()


def test_instance_heartbeat_metadata(client, instance_token):
    client.get("/v1/instance/me", headers={
        **_auth(instance_token),
        "X-Device-Version": "0.9.9",
        "X-Device-Mode": "pi_hosted",
    })
    db = SessionLocal()
    try:
        inst = db.query(Instance).first()
        assert inst.app_version == "0.9.9"
        assert inst.deployment_mode == "pi_hosted"
        assert inst.last_seen_at
    finally:
        db.close()
