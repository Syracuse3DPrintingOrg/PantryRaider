"""The tunnel lifecycle across every path that ends a kitchen.

A fake agent built from the real agent's pure helpers (upsert_peer,
remove_peer_entry, render_caddy_config) records what the VPS would hold after
each cloud call, so these tests check the Caddy include the agent would
actually write: no kitchen deletion may leave its peer behind, a re-enable
with a fresh key may not leave the old one, and no name may render twice.
"""
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import tunnel_client
from app.config import settings
from app.database import SessionLocal, engine
from app.main import app
from app.models import Instance, TunnelPeer, TunnelTeardown

AGENT_PATH = Path(__file__).parent.parent / "vps" / "forager-tunnel-agent"


def _load_agent():
    loader = importlib.machinery.SourceFileLoader("forager_agent_lifecycle",
                                                  str(AGENT_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


agent = _load_agent()


def wg_key(tag: str) -> str:
    return tag + "A" * (43 - len(tag)) + "="


@pytest.fixture
def fake_agent(monkeypatch):
    state = {"peers": [], "wg": {}, "calls": [], "down": False}

    def add_peer(pk, ip, domain, app_port=9284):
        if state["down"]:
            raise tunnel_client.TunnelAgentError("down")
        state["calls"].append(("add", pk, ip, domain, app_port))
        for old in agent.displaced_keys(state["peers"], pk, ip, domain):
            state["wg"].pop(old, None)
        state["wg"][pk] = ip
        state["peers"] = agent.upsert_peer(state["peers"], pk, ip, domain,
                                           app_port)

    def remove_peer(pk):
        if state["down"]:
            raise tunnel_client.TunnelAgentError("down")
        state["calls"].append(("remove", pk))
        state["wg"].pop(pk, None)
        state["peers"] = agent.remove_peer_entry(state["peers"], pk)

    monkeypatch.setattr(tunnel_client, "add_peer", add_peer)
    monkeypatch.setattr(tunnel_client, "remove_peer", remove_peer)
    state["render"] = lambda: agent.render_caddy_config(state["peers"])
    return state


@pytest.fixture
def fk_on():
    """Postgres enforces ON DELETE CASCADE; turn SQLite's FK enforcement on so
    an instance delete cascades the peer row the way production does."""
    with engine.connect() as c:
        c.execute(text("PRAGMA foreign_keys=ON"))
    yield
    with engine.connect() as c:
        c.execute(text("PRAGMA foreign_keys=OFF"))


def _signup(client, email):
    # Signed up from an install and confirmed, so the account holds a trial.
    r = client.post("/v1/accounts/signup",
                    json={"email": email, "password": "correct-horse-1",
                          "install_key": f"install-{email}"})
    assert r.status_code == 200, r.text
    from app.database import SessionLocal
    from app.models import Account
    db = SessionLocal()
    try:
        db.query(Account).filter_by(email=email).first().email_verified = 1
        db.commit()
    finally:
        db.close()
    return r.json()["session_token"]


def _provision(client, email, name="Kitchen"):
    r = client.post("/v1/instances/provision",
                    json={"email": email, "password": "correct-horse-1",
                          "device_name": name})
    assert r.status_code == 200, r.text
    return r.json()["instance_token"], r.json()["instance_id"]


def _enable(client, inst_token, key, hint="pr", **extra):
    return client.post("/v1/tunnel/enable",
                       headers={"Authorization": f"Bearer {inst_token}"},
                       json={"public_key": key, "hostname_hint": hint, **extra})


def _sites(render: str) -> list[str]:
    return [line.split(" {")[0] for line in render.splitlines()
            if line.endswith(" {")]


def _portal_login(client, email):
    r = client.post("/login", data={"email": email,
                                    "password": "correct-horse-1"},
                    follow_redirects=False)
    assert r.status_code == 303
    client.cookies.set("forager_session", r.cookies.get("forager_session"))


# --- Every way a kitchen is removed takes its tunnel down ------------------

def test_portal_remove_kitchen_tears_the_tunnel_down(client, fake_agent, fk_on):
    _signup(client, "alice@example.com")
    a_tok, a_id = _provision(client, "alice@example.com", "Alice Pi")
    a_key = wg_key("ALICE")
    assert _enable(client, a_tok, a_key).status_code == 200

    _portal_login(client, "alice@example.com")
    r = client.post(f"/account/kitchens/{a_id}/remove", follow_redirects=False)
    assert r.status_code == 303
    client.cookies.clear()

    assert ("remove", a_key) in fake_agent["calls"]
    assert "pr.forager.pantryraider.app" not in fake_agent["render"]()

    # A different customer later picking the same name gets a clean route.
    _signup(client, "bob@example.com")
    b_tok, _ = _provision(client, "bob@example.com", "Bob Pi")
    assert _enable(client, b_tok, wg_key("BOB")).status_code == 200
    assert _sites(fake_agent["render"]()) == ["pr.forager.pantryraider.app"]


def test_app_unlink_tears_the_tunnel_down(client, fake_agent, fk_on):
    _signup(client, "carol@example.com")
    c_tok, _ = _provision(client, "carol@example.com")
    c_key = wg_key("CAROL")
    assert _enable(client, c_tok, c_key, hint="carols-kitchen").status_code == 200
    r = client.delete("/v1/instance", headers={"Authorization": f"Bearer {c_tok}"})
    assert r.status_code == 200
    assert ("remove", c_key) in fake_agent["calls"]

    _signup(client, "dave@example.com")
    d_tok, _ = _provision(client, "dave@example.com")
    assert _enable(client, d_tok, wg_key("DAVE"),
                   hint="daves-kitchen").status_code == 200
    render = fake_agent["render"]()
    # Carol's old address no longer routes anywhere.
    assert "carols-kitchen" not in render
    assert _sites(render) == ["daves-kitchen.forager.pantryraider.app"]


def test_admin_revoke_kitchen_tears_the_tunnel_down(client, fake_agent, fk_on,
                                                    monkeypatch):
    _signup(client, "erin@example.com")
    e_tok, e_id = _provision(client, "erin@example.com")
    e_key = wg_key("ERIN")
    assert _enable(client, e_tok, e_key).status_code == 200

    monkeypatch.setattr(settings, "admin_emails", "boss@example.com")
    admin = TestClient(app)
    assert admin.post("/signup", data={
        "email": "boss@example.com", "password": "correct-horse-1",
        "confirm_password": "correct-horse-1"},
        follow_redirects=False).status_code == 303
    from tests.conftest import make_admin_ready
    make_admin_ready("boss@example.com")
    db = SessionLocal()
    account_id = db.get(Instance, e_id).account_id
    db.close()
    admin.post(f"/admin/accounts/{account_id}/kitchens/{e_id}/revoke")
    assert ("remove", e_key) in fake_agent["calls"]
    assert fake_agent["render"]().count("reverse_proxy") == 0


def test_agent_down_on_delete_queues_a_retry_and_holds_the_slot(
        client, fake_agent, fk_on):
    _signup(client, "frank@example.com")
    f_tok, _ = _provision(client, "frank@example.com")
    f_key = wg_key("FRANK")
    assert _enable(client, f_tok, f_key, hint="franks").status_code == 200

    fake_agent["down"] = True
    r = client.delete("/v1/instance", headers={"Authorization": f"Bearer {f_tok}"})
    # The credential still dies: unlinking never waits on the VPS.
    assert r.status_code == 200
    db = SessionLocal()
    try:
        assert db.query(TunnelPeer).count() == 0
        queued = db.query(TunnelTeardown).one()
        assert queued.public_key == f_key
        assert queued.tunnel_ip == "10.99.0.2"
        assert queued.subdomain == "franks"
    finally:
        db.close()

    # While the old route may still be live, nobody else gets its address or
    # its name.
    fake_agent["down"] = False
    _signup(client, "gina@example.com")
    g_tok, _ = _provision(client, "gina@example.com")
    body = _enable(client, g_tok, wg_key("GINA"), hint="franks").json()
    assert body["tunnel_ip"] != "10.99.0.2"
    assert body["dns_name"] != "franks.forager.pantryraider.app"
    chosen = _enable(client, g_tok, wg_key("GINA"), subdomain="franks")
    assert chosen.status_code == 409

    # The sweep finishes the job once the agent answers again.
    from app import retention
    db = SessionLocal()
    try:
        assert retention.tunnel_sweep(db)["tunnel_teardowns_cleared"] == 1
        assert db.query(TunnelTeardown).count() == 0
    finally:
        db.close()
    assert ("remove", f_key) in fake_agent["calls"]


def test_failed_retry_counts_the_attempt(client, fake_agent):
    from app.routers.tunnel import retry_teardowns
    db = SessionLocal()
    try:
        db.add(TunnelTeardown(public_key=wg_key("OLD"), tunnel_ip="10.99.0.9",
                              subdomain="old", attempts=1, last_error="",
                              created_at="2026-10-01T00:00:00+00:00"))
        db.commit()
        fake_agent["down"] = True
        assert retry_teardowns(db) == 0
        row = db.query(TunnelTeardown).one()
        assert row.attempts == 2
        assert "down" in row.last_error
    finally:
        db.close()


def test_account_disable_with_agent_down_queues_teardown(client, fake_agent):
    from app.routers.tunnel import disable_tunnel_for_account
    _signup(client, "hal@example.com")
    h_tok, h_id = _provision(client, "hal@example.com")
    assert _enable(client, h_tok, wg_key("HAL")).status_code == 200
    fake_agent["down"] = True
    db = SessionLocal()
    try:
        account_id = db.get(Instance, h_id).account_id
        assert disable_tunnel_for_account(db, account_id) == 1
        assert db.query(TunnelTeardown).count() == 1
        assert db.get(Instance, h_id).public_url == ""
    finally:
        db.close()


# --- Re-enable with a fresh key --------------------------------------------

def test_reenable_with_fresh_key_removes_the_old_one(client, fake_agent):
    _signup(client, "ivy@example.com")
    tok, _ = _provision(client, "ivy@example.com")
    old, new = wg_key("IVYONE"), wg_key("IVYTWO")
    assert _enable(client, tok, old).status_code == 200
    assert _enable(client, tok, new).status_code == 200
    assert ("remove", old) in fake_agent["calls"]
    assert old not in fake_agent["wg"]
    assert _sites(fake_agent["render"]()) == ["pr.forager.pantryraider.app"]


def test_reenable_with_same_key_does_not_remove(client, fake_agent):
    _signup(client, "jay@example.com")
    tok, _ = _provision(client, "jay@example.com")
    key = wg_key("JAY")
    assert _enable(client, tok, key).status_code == 200
    assert _enable(client, tok, key).status_code == 200
    assert not [c for c in fake_agent["calls"] if c[0] == "remove"]


def test_reenable_refuses_when_old_key_cannot_be_removed(client, fake_agent,
                                                         monkeypatch):
    _signup(client, "kim@example.com")
    tok, _ = _provision(client, "kim@example.com")
    old = wg_key("KIMONE")
    assert _enable(client, tok, old).status_code == 200

    def remove_fails(pk):
        raise tunnel_client.TunnelAgentError("down")
    monkeypatch.setattr(tunnel_client, "remove_peer", remove_fails)
    r = _enable(client, tok, wg_key("KIMTWO"))
    assert r.status_code == 503
    db = SessionLocal()
    try:
        assert db.query(TunnelPeer).one().public_key == old
    finally:
        db.close()


# --- Input checks ------------------------------------------------------------

@pytest.mark.parametrize("port", [0, -1, 65536, 99999])
def test_app_port_out_of_range_is_refused(client, fake_agent, port):
    _signup(client, "lou@example.com")
    tok, _ = _provision(client, "lou@example.com")
    r = _enable(client, tok, wg_key("LOU"), app_port=port)
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_app_port"
    assert fake_agent["calls"] == []


@pytest.mark.parametrize("key", [
    "not a key at all",
    "A" * 44,                  # no padding
    "A" * 42 + "==",           # 31 bytes
    "A" * 42 + "}=",
])
def test_malformed_public_key_is_refused(client, fake_agent, key):
    _signup(client, "max@example.com")
    tok, _ = _provision(client, "max@example.com")
    r = _enable(client, tok, key)
    assert r.status_code == 400
    assert fake_agent["calls"] == []


# --- Caps and rate limits ----------------------------------------------------

def test_per_account_tunnel_cap(client, fake_agent, monkeypatch):
    monkeypatch.setattr(settings, "tunnel_max_per_account", 1)
    _signup(client, "ned@example.com")
    t1, _ = _provision(client, "ned@example.com", "One")
    t2, _ = _provision(client, "ned@example.com", "Two")
    assert _enable(client, t1, wg_key("NEDONE"), hint="one").status_code == 200
    # The kitchen that already has a tunnel may still re-enable.
    assert _enable(client, t1, wg_key("NEDONE"), hint="one").status_code == 200
    r = _enable(client, t2, wg_key("NEDTWO"), hint="two")
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "tunnel_limit"


def test_enable_is_rate_limited(client, fake_agent, monkeypatch):
    monkeypatch.setattr(settings, "tunnel_enable_rate_per_minute", 2)
    _signup(client, "oli@example.com")
    tok, _ = _provision(client, "oli@example.com")
    key = wg_key("OLI")
    assert _enable(client, tok, key).status_code == 200
    assert _enable(client, tok, key).status_code == 200
    assert _enable(client, tok, key).status_code == 429


def test_pairing_redeem_is_rate_limited(client, monkeypatch):
    monkeypatch.setattr(settings, "pairing_redeem_rate_per_minute", 3)
    for _ in range(3):
        assert client.post("/v1/pairing/redeem",
                           json={"code": "NOPE-NOPE"}).status_code == 400
    assert client.post("/v1/pairing/redeem",
                       json={"code": "NOPE-NOPE"}).status_code == 429
