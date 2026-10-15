"""Remote access ends with the entitlement: a trial that runs out on its own
loses its tunnel at the next sweep, and Caddy gets no new certificate for it
even before then."""
import pytest

from app import retention, tunnel_client
from app.database import SessionLocal
from app.models import TunnelPeer, TunnelTeardown
from tests.conftest import activate_entitlement, expire_trial

DOMAIN = "trialkitchen.forager.pantryraider.app"


def wg_key(tag: str) -> str:
    return tag + "A" * (43 - len(tag)) + "="


@pytest.fixture
def agent_calls(monkeypatch):
    calls = {"add": [], "remove": [], "down": False}

    def remove(pk):
        if calls["down"]:
            raise tunnel_client.TunnelAgentError("down")
        calls["remove"].append(pk)

    monkeypatch.setattr(tunnel_client, "add_peer",
                        lambda pk, *a, **k: calls["add"].append(pk))
    monkeypatch.setattr(tunnel_client, "remove_peer", remove)
    return calls


def _enable(client, instance_token, key):
    r = client.post("/v1/tunnel/enable",
                    headers={"Authorization": f"Bearer {instance_token}"},
                    json={"public_key": key, "hostname_hint": "trialkitchen"})
    assert r.status_code == 200, r.text


def _sweep():
    db = SessionLocal()
    try:
        return retention.tunnel_sweep(db)
    finally:
        db.close()


def test_expired_trial_loses_tls_and_its_tunnel(client, instance_token,
                                                agent_calls):
    key = wg_key("TRIAL")
    _enable(client, instance_token, key)
    assert client.get(f"/v1/tunnel/tls-check?domain={DOMAIN}").status_code == 200

    expire_trial()
    # No new certificate, straight away.
    assert client.get(f"/v1/tunnel/tls-check?domain={DOMAIN}").status_code == 404

    counts = _sweep()
    assert counts["tunnels_lapsed"] == 1
    assert agent_calls["remove"] == [key]
    hdr = {"Authorization": f"Bearer {instance_token}"}
    assert client.get("/v1/tunnel/status", headers=hdr).json()["enabled"] is False
    me = client.get("/v1/instance/me", headers=hdr).json()
    assert me["public_url"] is None


def test_sweep_leaves_entitled_accounts_alone(client, instance_token,
                                              agent_calls):
    _enable(client, instance_token, wg_key("PAID"))
    activate_entitlement()
    expire_trial()  # the paid plan still covers remote access
    counts = _sweep()
    assert counts["tunnels_lapsed"] == 0
    assert agent_calls["remove"] == []
    assert client.get(f"/v1/tunnel/tls-check?domain={DOMAIN}").status_code == 200


def test_sweep_queues_a_retry_when_the_agent_is_down(client, instance_token,
                                                     agent_calls):
    key = wg_key("TRIAL")
    _enable(client, instance_token, key)
    expire_trial()
    agent_calls["down"] = True
    counts = _sweep()
    assert counts["tunnels_lapsed"] == 1
    assert counts["tunnel_teardowns_cleared"] == 0
    db = SessionLocal()
    try:
        assert db.query(TunnelPeer).count() == 0
        assert db.query(TunnelTeardown).one().public_key == key
    finally:
        db.close()
    agent_calls["down"] = False
    assert _sweep()["tunnel_teardowns_cleared"] == 1
    assert agent_calls["remove"] == [key]
