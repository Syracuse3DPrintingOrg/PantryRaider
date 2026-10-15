"""This device's own programs: agent keys, kiosk tickets, the kiosk URL check (I-5)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import settings
from app.passwords import hash_secret
from app.services import access, bridge, local_agents

SERVICE = Path(__file__).resolve().parents[1] / "service"
VECTORS = json.loads((Path(__file__).parent / "fixtures" / "local_credentials_vectors.json").read_text())


def test_vectors_match(bridge_token):
    assert bridge_token == VECTORS["token"]
    for agent, key in VECTORS["agent_keys"].items():
        assert local_agents.agent_key(agent) == key
        assert local_agents.match_agent_key(key) == agent
    for t in VECTORS["tickets"]:
        assert local_agents.make_ticket(bridge_token, t["t"], t["n"]) == t["ticket"]


def test_identity_proof_vectors():
    import hashlib
    import hmac
    for p in VECTORS["identity_proofs"]:
        mac = hmac.new(VECTORS["token"].encode(),
                       b"pantryraider-bridge-identity-v1:" + p["nonce"].encode(),
                       hashlib.sha256).hexdigest()
        assert mac == p["proof"]


def test_no_token_means_no_agent_keys_or_tickets(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    bridge.invalidate_bridge_token()
    assert local_agents.agent_key("streamdeck") is None
    assert local_agents.match_agent_key(VECTORS["agent_keys"]["streamdeck"]) is None
    t = VECTORS["tickets"][0]
    assert not local_agents.verify_ticket(t["ticket"], now=t["t"])


def test_a_new_token_is_picked_up_on_a_mismatch(bridge_token, tmp_path):
    old = VECTORS["agent_keys"]["gadgets"]
    assert local_agents.match_agent_key(old) == "gadgets"
    (tmp_path / "bridge-token").write_text("a-rotated-test-token")
    new = local_agents._agent_key_for("a-rotated-test-token", "gadgets")
    # The cached token still answers the old key; the new key forces one re-read.
    assert local_agents.match_agent_key(new) == "gadgets"
    assert local_agents.match_agent_key(old) is None
    # Re-reads are limited to one every 30 seconds.
    (tmp_path / "bridge-token").write_text("yet-another-test-token")
    newer = local_agents._agent_key_for("yet-another-test-token", "gadgets")
    assert local_agents.match_agent_key(newer) is None


def test_ticket_rules(bridge_token):
    import secrets
    now = 1_800_000_000
    good = local_agents.make_ticket(bridge_token, now, secrets.token_hex(16))
    assert local_agents.verify_ticket(good, now=now + 10)
    assert not local_agents.verify_ticket(good, now=now + 11)          # replay
    old = local_agents.make_ticket(bridge_token, now - 181, secrets.token_hex(16))
    assert not local_agents.verify_ticket(old, now=now)                 # too old
    ahead = local_agents.make_ticket(bridge_token, now + 31, secrets.token_hex(16))
    assert not local_agents.verify_ticket(ahead, now=now)               # too far ahead
    wrong = local_agents.make_ticket("not-the-token", now, secrets.token_hex(16))
    assert not local_agents.verify_ticket(wrong, now=now)
    for bad in ("", "v1.x.y.z", good.upper(), good + "0"):
        assert not local_agents.verify_ticket(bad, now=now)


def test_nonces_survive_a_module_reset(bridge_token):
    import secrets
    local_agents.reset(persist=True)
    now = 1_800_000_000
    ticket = local_agents.make_ticket(bridge_token, now, secrets.token_hex(16))
    assert local_agents.verify_ticket(ticket, now=now)
    local_agents.reset(persist=True)
    assert not local_agents.verify_ticket(ticket, now=now + 5)
    nonces = json.loads((Path(settings.data_dir) / local_agents.NONCE_FILE).read_text())
    assert len(nonces) == 1


# --- Through HTTP ---------------------------------------------------------------

@pytest.fixture
def password_install(bridge_token, monkeypatch):
    monkeypatch.chdir(SERVICE)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret("hunter2"), raising=False)
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True, raising=False)
    return bridge_token


def test_agent_keys_count_only_from_plain_loopback(password_install, loopback_client,
                                                  lan_client, grace_over):
    from app.services import auth_state
    auth_state.retire("admin")
    deck = {"X-API-Key": VECTORS["agent_keys"]["streamdeck"], "X-PR-Client": "streamdeck/1.0",
            "X-PR-Caps": "navorigin1"}
    assert loopback_client.get("/timers", headers=deck).status_code == 200
    assert lan_client.get("/timers", headers=deck).status_code == 401
    assert loopback_client.get("/timers").status_code == 401


@pytest.mark.parametrize("method,path,deck,reader", [
    ("GET", "/timers", True, False),
    ("POST", "/pending/scan", True, True),
    ("GET", "/gadgets/config", False, True),
    ("POST", "/gadgets/readings", False, True),
    ("POST", "/gadgets/key-action", True, True),
    ("GET", "/cub/summary", False, True),
    ("GET", "/api/proxy/ha/api/states/light.kitchen", True, False),
    ("GET", "/api/proxy/grocy/api/stock", False, False),
    ("POST", "/setup/save", False, False),
    ("GET", "/ha/state", False, False),
])
def test_deck_and_reader_tables(method, path, deck, reader):
    assert access.allows_raw("deck", method, path, kiosk_is_admin=True) is deck
    assert access.allows_raw("reader", method, path, kiosk_is_admin=True) is reader


def test_record_agent_use_proves_the_deck_once(monkeypatch):
    started = []
    monkeypatch.setattr(local_agents, "_push_deck_quietly", lambda: started.append(1))
    local_agents.record_agent_use("streamdeck", frozenset(), "1.0", "http://localhost:9284")
    assert not local_agents.deck_proven()
    local_agents.record_agent_use("streamdeck", frozenset({"navorigin1"}), "1.0",
                                  "http://localhost:9284")
    local_agents.record_agent_use("streamdeck", frozenset({"navorigin1"}), "1.0",
                                  "http://localhost:9284")
    import time
    time.sleep(0.05)
    assert local_agents.deck_proven()["origin"] == "http://localhost:9284"
    assert started == [1]
    assert local_agents.deck_origin() == "http://localhost:9284"


# --- The kiosk URL check (D7) ---------------------------------------------------

@pytest.fixture
def items(monkeypatch):
    seen = []
    monkeypatch.setattr(access, "raise_item",
                        lambda kind, title, body, dedupe, level="info": seen.append((kind, body, dedupe)))
    return seen


def _hw(url_local, url_host="pantry.example.com", installed=True):
    kiosk = {"installed": installed}
    if url_local is not None:
        kiosk["url_local"] = url_local
        kiosk["url_host"] = url_host
    return {"kiosk": kiosk}


def test_kiosk_url_item_names_the_host(items):
    local_agents.check_kiosk_url(_hw(False))
    assert len(items) == 1
    kind, body, dedupe = items[0]
    assert kind == "kiosk_url_not_local" and "pantry.example.com" in body
    assert dedupe == "kiosk_url_not_local:pantry.example.com"
    assert "sudo foodassistant-host-harden kiosk-url-local" in body


@pytest.mark.parametrize("hw", [_hw(True), _hw(None), {}, None, _hw(False, installed=False)])
def test_no_kiosk_url_item_when_local_or_unknown(items, hw):
    local_agents.check_kiosk_url(hw)
    assert items == []


def test_a_bandit_showing_its_server_is_fine(items, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    monkeypatch.setattr(settings, "remote_server_url", "http://pantry.example.com:9284",
                        raising=False)
    local_agents.check_kiosk_url(_hw(False))
    assert items == []


def test_the_bare_host_name_is_local(items, monkeypatch):
    monkeypatch.setattr(access, "own_loopback_names",
                        lambda: frozenset({"kitchen-pi", "kitchen-pi.local"}))
    local_agents.check_kiosk_url(_hw(False, "kitchen-pi"))
    assert items == []
    local_agents.check_kiosk_url(_hw(False, "kitchen-pi.local"))
    assert len(items) == 1


def test_kiosk_url_status_drives_the_security_state(password_install, loopback_client):
    local_agents._state["hw"] = _hw(False)
    status = local_agents.kiosk_url_status()
    assert status == {"local": False, "host": "pantry.example.com"}
    r = loopback_client.get("/setup/security/state")
    assert r.status_code == 200
    assert r.json()["kiosk_url_warning"] == access.KIOSK_URL_WARNING.format(host="pantry.example.com")
    local_agents._state["hw"] = _hw(True, "localhost")
    assert loopback_client.get("/setup/security/state").json()["kiosk_url_warning"] == ""
