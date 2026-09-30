"""Remote-access tunnel: allocation purity, the enable/disable API, the
entitlement gate, tls-check, and the public_url that now rides me/provision."""
import pytest

from app import tunnel as alloc
from app import tunnel_client
from tests.conftest import expire_trial


# --- Pure allocation -------------------------------------------------------

def test_allocate_ip_starts_at_dot_two():
    # .0 (network) and .1 (server) are skipped, so the first kitchen is .0.2.
    assert alloc.allocate_ip([]) == "10.99.0.2"


def test_allocate_ip_picks_the_lowest_free_host():
    taken = ["10.99.0.2", "10.99.0.3", "10.99.0.5"]
    assert alloc.allocate_ip(taken) == "10.99.0.4"


def test_allocate_ip_ignores_garbage_entries():
    assert alloc.allocate_ip(["not-an-ip", "10.99.0.2"]) == "10.99.0.3"


def test_allocate_ip_crosses_octet_boundary():
    taken = [f"10.99.0.{n}" for n in range(2, 256)]
    assert alloc.allocate_ip(taken) == "10.99.1.0"


def test_sanitize_subdomain_basic():
    assert alloc.sanitize_subdomain("Kitchen Pi") == "kitchen-pi"
    assert alloc.sanitize_subdomain("dan.pi") == "dan-pi"
    assert alloc.sanitize_subdomain("Dan's  Kitchen!!") == "dan-s-kitchen"


def test_sanitize_subdomain_collapses_and_trims_dashes():
    assert alloc.sanitize_subdomain("--a___b--") == "a-b"


def test_sanitize_subdomain_falls_back_when_empty():
    assert alloc.sanitize_subdomain("") == "kitchen"
    assert alloc.sanitize_subdomain("!!!") == "kitchen"


def test_sanitize_subdomain_caps_length():
    out = alloc.sanitize_subdomain("x" * 100)
    assert len(out) == alloc.MAX_SUBDOMAIN_LENGTH


def test_ensure_unique_subdomain_suffixes_on_collision():
    assert alloc.ensure_unique_subdomain("kitchen", []) == "kitchen"
    assert alloc.ensure_unique_subdomain("kitchen", ["kitchen"]) == "kitchen-2"
    assert alloc.ensure_unique_subdomain(
        "kitchen", ["kitchen", "kitchen-2"]) == "kitchen-3"


def test_ensure_unique_subdomain_keeps_within_length():
    base = "x" * alloc.MAX_SUBDOMAIN_LENGTH
    out = alloc.ensure_unique_subdomain(base, [base])
    assert len(out) <= alloc.MAX_SUBDOMAIN_LENGTH
    assert out.endswith("-2")


# --- The enable/disable API (stubbed agent) --------------------------------

@pytest.fixture
def stub_agent(monkeypatch):
    """Record agent calls instead of talking to a real VPS agent."""
    calls = {"add": [], "remove": []}
    monkeypatch.setattr(tunnel_client, "add_peer",
                        lambda pk, ip, domain, app_port=9284:
                        calls["add"].append((pk, ip, domain, app_port)))
    monkeypatch.setattr(tunnel_client, "remove_peer",
                        lambda pk: calls["remove"].append(pk))
    return calls


def _auth(instance_token):
    return {"Authorization": f"Bearer {instance_token}"}


def test_enable_happy_path(client, instance_token, stub_agent):
    resp = client.post("/v1/tunnel/enable",
                       json={"public_key": "PUBKEYAAA=", "hostname_hint": "Kitchen Pi"},
                       headers=_auth(instance_token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["tunnel_ip"] == "10.99.0.2"
    assert body["dns_name"] == "kitchen-pi.forager.pantryraider.app"
    assert body["public_url"] == "https://kitchen-pi.forager.pantryraider.app"
    assert body["tunnel_cidr"] == "10.99.0.0/16"
    assert body["keepalive"] == 25
    assert body["allowed_ips"] == "10.99.0.1/32"
    assert body["server_endpoint"] == "forager.pantryraider.app:51820"
    # The agent was asked to wire it up, with the full domain for Caddy and the
    # default 9284 app port (no app_port sent, so a Pi appliance is assumed).
    assert stub_agent["add"] == [
        ("PUBKEYAAA=", "10.99.0.2", "kitchen-pi.forager.pantryraider.app", 9284)]


def test_enable_forwards_the_app_port_and_stores_it(client, instance_token,
                                                    stub_agent):
    """A server sends its in-container port; it reaches the agent and persists
    on the peer so a later Caddy regeneration keeps routing to it."""
    from app.database import SessionLocal
    from app.models import TunnelPeer
    resp = client.post("/v1/tunnel/enable",
                       json={"public_key": "SRVKEY", "hostname_hint": "Server",
                             "app_port": 8000},
                       headers=_auth(instance_token))
    assert resp.status_code == 200
    assert stub_agent["add"][-1] == (
        "SRVKEY", "10.99.0.2", "server.forager.pantryraider.app", 8000)
    db = SessionLocal()
    peer = db.query(TunnelPeer).first()
    assert peer.app_port == 8000
    db.close()


def test_enable_defaults_app_port_when_omitted(client, instance_token,
                                               stub_agent):
    from app.database import SessionLocal
    from app.models import TunnelPeer
    client.post("/v1/tunnel/enable",
                json={"public_key": "PIKEY", "hostname_hint": "Kitchen Pi"},
                headers=_auth(instance_token))
    db = SessionLocal()
    peer = db.query(TunnelPeer).first()
    assert peer.app_port == 9284
    db.close()


def test_enable_reuses_the_existing_peer_and_updates_the_key(
        client, instance_token, stub_agent):
    first = client.post("/v1/tunnel/enable",
                        json={"public_key": "KEY1", "hostname_hint": "Kitchen Pi"},
                        headers=_auth(instance_token)).json()
    second = client.post("/v1/tunnel/enable",
                         json={"public_key": "KEY2", "hostname_hint": "Other Name"},
                         headers=_auth(instance_token)).json()
    # Same IP and subdomain kept; only the key changed.
    assert second["tunnel_ip"] == first["tunnel_ip"]
    assert second["dns_name"] == first["dns_name"]
    assert stub_agent["add"][-1][0] == "KEY2"


def test_enable_requires_entitlement(client, instance_token, stub_agent):
    expire_trial()
    resp = client.post("/v1/tunnel/enable",
                       json={"public_key": "KEY", "hostname_hint": "Pi"},
                       headers=_auth(instance_token))
    assert resp.status_code == 402
    assert resp.json()["detail"]["error"] == "no_subscription"
    # Nothing was wired up.
    assert stub_agent["add"] == []


def test_enable_rolls_back_on_agent_failure(client, instance_token, monkeypatch):
    def boom(pk, ip, domain, app_port=9284):
        raise tunnel_client.TunnelAgentError("down")
    monkeypatch.setattr(tunnel_client, "add_peer", boom)
    resp = client.post("/v1/tunnel/enable",
                       json={"public_key": "KEY", "hostname_hint": "Pi"},
                       headers=_auth(instance_token))
    assert resp.status_code == 503
    # No row persisted: status still reports disabled.
    status = client.get("/v1/tunnel/status", headers=_auth(instance_token))
    assert status.json()["enabled"] is False


def test_status_and_disable(client, instance_token, stub_agent):
    client.post("/v1/tunnel/enable",
                json={"public_key": "KEY", "hostname_hint": "Kitchen Pi"},
                headers=_auth(instance_token))
    status = client.get("/v1/tunnel/status", headers=_auth(instance_token)).json()
    assert status["enabled"] is True
    assert status["public_url"] == "https://kitchen-pi.forager.pantryraider.app"

    resp = client.post("/v1/tunnel/disable", headers=_auth(instance_token))
    assert resp.status_code == 200
    assert resp.json() == {"disabled": True}
    assert stub_agent["remove"] == ["KEY"]
    # Idempotent: disabling again with no peer still succeeds.
    assert client.post("/v1/tunnel/disable",
                       headers=_auth(instance_token)).status_code == 200
    assert client.get("/v1/tunnel/status",
                      headers=_auth(instance_token)).json()["enabled"] is False


def test_enable_sets_public_url_on_me_and_provision(client, instance_token,
                                                    stub_agent):
    client.post("/v1/tunnel/enable",
                json={"public_key": "KEY", "hostname_hint": "Kitchen Pi"},
                headers=_auth(instance_token))
    me = client.get("/v1/instance/me", headers=_auth(instance_token)).json()
    assert me["public_url"] == "https://kitchen-pi.forager.pantryraider.app"

    # A freshly provisioned instance has no tunnel yet: the field is present
    # and null (the app-side contract stays stable).
    prov = client.post("/v1/instances/provision",
                       json={"email": "dan@example.com", "password": "hunter2222",
                             "device_name": "Second Pi"}).json()
    assert prov["suggested_public_url"] is None


def test_disable_clears_public_url(client, instance_token, stub_agent):
    client.post("/v1/tunnel/enable",
                json={"public_key": "KEY", "hostname_hint": "Kitchen Pi"},
                headers=_auth(instance_token))
    client.post("/v1/tunnel/disable", headers=_auth(instance_token))
    me = client.get("/v1/instance/me", headers=_auth(instance_token)).json()
    assert me["public_url"] is None


# --- tls-check (unauthenticated Caddy ask target) --------------------------

def test_tls_check_allows_a_live_subdomain(client, instance_token, stub_agent):
    client.post("/v1/tunnel/enable",
                json={"public_key": "KEY", "hostname_hint": "Kitchen Pi"},
                headers=_auth(instance_token))
    resp = client.get("/v1/tunnel/tls-check",
                      params={"domain": "kitchen-pi.forager.pantryraider.app"})
    assert resp.status_code == 200
    assert resp.json() == {"allow": True}


def test_tls_check_denies_unknown_or_foreign_domains(client, instance_token,
                                                     stub_agent):
    client.post("/v1/tunnel/enable",
                json={"public_key": "KEY", "hostname_hint": "Kitchen Pi"},
                headers=_auth(instance_token))
    # Unknown subdomain under our apex.
    assert client.get("/v1/tunnel/tls-check",
                      params={"domain": "nope.forager.pantryraider.app"}
                      ).status_code == 404
    # A domain that is not ours at all.
    assert client.get("/v1/tunnel/tls-check",
                      params={"domain": "evil.example.com"}).status_code == 404
    # The apex itself is not a kitchen subdomain.
    assert client.get("/v1/tunnel/tls-check",
                      params={"domain": "forager.pantryraider.app"}
                      ).status_code == 404
    # A deeper label must not slip through.
    assert client.get("/v1/tunnel/tls-check",
                      params={"domain": "a.kitchen-pi.forager.pantryraider.app"}
                      ).status_code == 404


def test_tls_check_is_unauthenticated(client, instance_token, stub_agent):
    # No Authorization header at all, yet it answers (Caddy calls it blind).
    client.post("/v1/tunnel/enable",
                json={"public_key": "KEY", "hostname_hint": "Kitchen Pi"},
                headers=_auth(instance_token))
    resp = client.get("/v1/tunnel/tls-check",
                      params={"domain": "kitchen-pi.forager.pantryraider.app"})
    assert resp.status_code == 200


# --- Choose-your-own web address ------------------------------------------

def _second_instance(client, session_token, name="Second Pi"):
    """Pair a second kitchen under the same (entitled) account."""
    code = client.post("/v1/pairing/code",
                       headers={"Authorization": f"Bearer {session_token}"})
    resp = client.post("/v1/pairing/redeem",
                       json={"code": code.json()["code"], "name": name})
    return resp.json()["instance_token"]


def test_subdomain_available_when_free(client, instance_token, stub_agent):
    resp = client.get("/v1/tunnel/subdomain-available",
                      params={"name": "My Kitchen"},
                      headers=_auth(instance_token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["sanitized"] == "my-kitchen"
    assert body["suggestion"] == "my-kitchen"
    assert body["apex"] == "forager.pantryraider.app"


def test_subdomain_available_reports_taken_with_suggestion(
        client, session_token, instance_token, stub_agent):
    other = _second_instance(client, session_token)
    # The other kitchen grabs "shared" first.
    client.post("/v1/tunnel/enable",
                json={"public_key": "OTHERKEY", "subdomain": "shared"},
                headers=_auth(other))
    resp = client.get("/v1/tunnel/subdomain-available",
                      params={"name": "shared"},
                      headers=_auth(instance_token)).json()
    assert resp["available"] is False
    assert resp["suggestion"] == "shared-2"


def test_subdomain_available_ignores_your_own_name(
        client, instance_token, stub_agent):
    client.post("/v1/tunnel/enable",
                json={"public_key": "KEY", "subdomain": "mine"},
                headers=_auth(instance_token))
    # Re-checking your own current address still reads as available.
    resp = client.get("/v1/tunnel/subdomain-available",
                      params={"name": "mine"},
                      headers=_auth(instance_token)).json()
    assert resp["available"] is True
    assert resp["suggestion"] == "mine"


def test_enable_with_explicit_subdomain_wins_over_hint(
        client, instance_token, stub_agent):
    resp = client.post("/v1/tunnel/enable",
                       json={"public_key": "KEY", "hostname_hint": "Ignored Host",
                             "subdomain": "My Cool Kitchen"},
                       headers=_auth(instance_token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["dns_name"] == "my-cool-kitchen.forager.pantryraider.app"
    assert body["public_url"] == "https://my-cool-kitchen.forager.pantryraider.app"
    assert stub_agent["add"][-1][2] == "my-cool-kitchen.forager.pantryraider.app"


def test_enable_with_taken_subdomain_conflicts(
        client, session_token, instance_token, stub_agent):
    other = _second_instance(client, session_token)
    client.post("/v1/tunnel/enable",
                json={"public_key": "OTHERKEY", "subdomain": "shared"},
                headers=_auth(other))
    resp = client.post("/v1/tunnel/enable",
                       json={"public_key": "KEY", "subdomain": "shared"},
                       headers=_auth(instance_token))
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "subdomain_taken"
    assert detail["suggestion"] == "shared-2"
    # Nothing was wired up for the conflicting request.
    assert [c for c in stub_agent["add"] if c[0] == "KEY"] == []


def test_disabled_account_tunnel_is_torn_down(client, session_token,
                                              instance_token, stub_agent):
    """The admin kill switch removes remote access too."""
    from app.database import SessionLocal
    from app.models import Account, TunnelPeer
    client.post("/v1/tunnel/enable",
                json={"public_key": "KEY", "hostname_hint": "Kitchen Pi"},
                headers=_auth(instance_token))
    db = SessionLocal()
    account = db.query(Account).filter_by(email="dan@example.com").first()
    from app.routers.tunnel import disable_tunnel_for_account
    removed = disable_tunnel_for_account(db, account.id)
    assert removed == 1
    assert db.query(TunnelPeer).count() == 0
    db.close()
    assert stub_agent["remove"] == ["KEY"]
