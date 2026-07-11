"""LAN device pairing (FoodAssistant-4box): a satellite asks the main server
for its own API key with a short confirmation code shown on both ends.

Covers the pure state machine (create/approve/deny/expiry/rate-limit), the
private-address gate, the feature toggle, the endpoint auth split (request is
unauthenticated but LAN-gated; approve needs auth), and the middleware bypass
lists in main.py (the "Unexpected token <" class of bug).
"""
import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.passwords import hash_secret  # noqa: E402
from app.services import pairing  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    pairing.reset()
    yield
    pairing.reset()


# --- state machine ----------------------------------------------------------

def test_create_returns_four_digit_code_and_handle():
    created = pairing.create_request("kitchen-pi", "192.168.1.44")
    assert created is not None
    assert len(created["code"]) == 4 and created["code"].isdigit()
    assert len(created["request_id"]) >= 24
    assert pairing.get_status(created["request_id"]) == {"status": "pending"}


def test_approve_delivers_key_and_is_single_use():
    created = pairing.create_request("kitchen-pi", "192.168.1.44")
    rec = pairing.approve(created["request_id"], "minted-key", "kitchen-pi")
    assert rec is not None and rec["hostname"] == "kitchen-pi"
    st = pairing.get_status(created["request_id"])
    assert st["status"] == "approved" and st["api_key"] == "minted-key"
    # A decided request can never be approved or denied again.
    assert pairing.approve(created["request_id"], "another-key") is None
    assert pairing.deny(created["request_id"]) is False


def test_deny_and_unknown_ids():
    created = pairing.create_request("pantry-pi", "10.0.0.9")
    assert pairing.deny(created["request_id"]) is True
    assert pairing.get_status(created["request_id"]) == {"status": "denied"}
    # An unknown id reads as expired, so handles cannot be probed apart.
    assert pairing.get_status("nope") == {"status": "expired"}
    assert pairing.approve("nope", "k") is None
    assert pairing.deny("nope") is False


def test_requests_expire(monkeypatch):
    created = pairing.create_request("kitchen-pi", "192.168.1.44")
    real_time = pairing.time.time
    monkeypatch.setattr(pairing.time, "time",
                        lambda: real_time() + pairing.TTL_SECONDS + 1)
    assert pairing.get_status(created["request_id"]) == {"status": "expired"}
    assert pairing.approve(created["request_id"], "k") is None
    assert pairing.pending_requests() == []


def test_rate_limit_max_pending(monkeypatch):
    for _ in range(pairing.MAX_PENDING):
        assert pairing.create_request("dev", "192.168.1.44") is not None
    assert pairing.create_request("one-too-many", "192.168.1.44") is None
    # Deciding one frees a slot.
    rid = pairing.pending_requests()[0]["request_id"]
    pairing.deny(rid)
    assert pairing.create_request("dev", "192.168.1.44") is not None


def test_pending_list_hides_key_material():
    created = pairing.create_request("kitchen-pi", "192.168.1.44")
    rows = pairing.pending_requests()
    assert len(rows) == 1
    row = rows[0]
    assert row["hostname"] == "kitchen-pi" and row["code"] == created["code"]
    assert "api_key" not in row
    # Decided requests drop off the pending list.
    pairing.approve(created["request_id"], "k")
    assert pairing.pending_requests() == []


def test_state_survives_a_fresh_process_view(tmp_path):
    """The state file shares requests across workers: a reset of the in-memory
    view (a different worker) still sees the request from disk."""
    created = pairing.create_request("kitchen-pi", "192.168.1.44")
    # Simulate another worker: wipe the process-local cache, keep the file.
    pairing._requests = {}
    pairing._mtime = None
    assert pairing.get_status(created["request_id"]) == {"status": "pending"}


# --- LAN gate ---------------------------------------------------------------

def test_private_addresses_pass_public_and_junk_fail():
    for host in ("192.168.1.20", "10.4.2.1", "172.16.0.7", "127.0.0.1", "::1"):
        assert pairing.is_private_address(host), host
    for host in ("8.8.8.8", "104.16.132.229", "2607:f8b0::1", "testclient",
                 "evil.example.com", "", None):
        assert not pairing.is_private_address(host), host


# --- endpoints --------------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        # client_host defaults to "testclient", NOT a private IP, so the LAN
        # gate and loopback trust are both exercised for real.
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _configured_server(monkeypatch, password="hunter2"):
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret(password), raising=False)
    monkeypatch.setattr(settings, "api_key", "sesame", raising=False)
    monkeypatch.setattr(settings, "extra_api_keys", [], raising=False)
    monkeypatch.setattr(settings, "extra_api_key_names", [], raising=False)
    monkeypatch.setattr(settings, "local_device_pairing_enabled", True, raising=False)


def _trust_client_lan(monkeypatch):
    """The TestClient's host is 'testclient'; let it count as on-LAN so the
    handler logic past the gate can be exercised."""
    monkeypatch.setattr("app.routers.pairing.pairing.is_private_address",
                        lambda host: True)


def test_request_is_refused_from_a_non_private_address(client, monkeypatch):
    _configured_server(monkeypatch)
    r = client.post("/api/pairing/request", json={"hostname": "kitchen-pi"})
    assert r.status_code == 403
    assert "local network" in r.json()["error"]


def test_request_is_refused_when_pairing_is_disabled(client, monkeypatch):
    _configured_server(monkeypatch)
    _trust_client_lan(monkeypatch)
    monkeypatch.setattr(settings, "local_device_pairing_enabled", False, raising=False)
    r = client.post("/api/pairing/request", json={"hostname": "kitchen-pi"})
    assert r.status_code == 403
    assert "turned off" in r.json()["error"]


def test_request_is_refused_on_a_satellite(client, monkeypatch):
    _configured_server(monkeypatch)
    _trust_client_lan(monkeypatch)
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    r = client.post("/api/pairing/request", json={"hostname": "kitchen-pi"})
    assert r.status_code == 403


def test_request_needs_no_auth_but_approve_does(client, monkeypatch):
    """The auth split: a keyless satellite can open and poll a request, but
    only an authenticated caller can approve, deny, or list them."""
    _configured_server(monkeypatch)
    _trust_client_lan(monkeypatch)

    # Unauthenticated request creation works (LAN + toggle gates pass).
    r = client.post("/api/pairing/request", json={"hostname": "kitchen-pi"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] and len(d["code"]) == 4 and d["request_id"]

    # Unauthenticated status poll works too (the long request_id is the handle).
    s = client.get(f"/api/pairing/status/{d['request_id']}")
    assert s.status_code == 200 and s.json()["status"] == "pending"

    # Approve, deny, and the pending list are auth-required.
    assert client.post("/api/pairing/approve",
                       json={"request_id": d["request_id"]}).status_code == 401
    assert client.post("/api/pairing/deny",
                       json={"request_id": d["request_id"]}).status_code == 401
    assert client.get("/api/pairing/pending").status_code == 401

    # With the API key, approval mints and stores a named satellite key.
    a = client.post("/api/pairing/approve", json={"request_id": d["request_id"]},
                    headers={"X-API-Key": "sesame"})
    assert a.status_code == 200 and a.json()["ok"]
    assert a.json()["name"] == "kitchen-pi"
    assert len(settings.extra_api_keys) == 1
    assert settings.extra_api_key_names == ["kitchen-pi"]
    minted = settings.extra_api_keys[0]

    # The satellite's poll now carries the key, and the key authenticates.
    s = client.get(f"/api/pairing/status/{d['request_id']}")
    assert s.json()["status"] == "approved" and s.json()["api_key"] == minted
    assert minted in settings.valid_api_keys()


def test_request_queues_an_onscreen_toast(client, monkeypatch):
    _configured_server(monkeypatch)
    _trust_client_lan(monkeypatch)
    from app.services import ha_events
    ha_events.reset()
    r = client.post("/api/pairing/request", json={"hostname": "kitchen-pi"})
    code = r.json()["code"]
    events = ha_events.poll(0)["events"]
    assert any(e["type"] == "warning" and code in e["message"]
               and e.get("pane") == "pane-devices" for e in events)
    ha_events.reset()


def test_deny_endpoint_refuses_the_device(client, monkeypatch):
    _configured_server(monkeypatch)
    _trust_client_lan(monkeypatch)
    d = client.post("/api/pairing/request", json={"hostname": "kitchen-pi"}).json()
    r = client.post("/api/pairing/deny", json={"request_id": d["request_id"]},
                    headers={"X-API-Key": "sesame"})
    assert r.status_code == 200 and r.json()["ok"]
    s = client.get(f"/api/pairing/status/{d['request_id']}")
    assert s.json()["status"] == "denied"
    assert settings.extra_api_keys == []


def test_status_endpoint_is_lan_gated_too(client, monkeypatch):
    _configured_server(monkeypatch)
    r = client.get("/api/pairing/status/whatever")
    assert r.status_code == 403


# --- middleware bypass lists (the "Unexpected token <" class of bug) --------

def test_bypass_lists_cover_the_pairing_paths():
    from app import main
    # The satellite relays run during its wizard, before it is configured.
    assert "/setup/pairing/request" in main._SETUP_BYPASS
    assert "/setup/pairing/status" in main._SETUP_BYPASS
    # The server endpoints must answer a keyless device even when the server
    # is configured and password-protected.
    assert "/api/pairing/request" in main._ALWAYS_PUBLIC
    assert "/api/pairing/request" in main._SETUP_BYPASS
    assert any(p == "/api/pairing/status/" for p in main._ALWAYS_PUBLIC_PREFIXES)


def test_satellite_relay_answers_json_before_setup(client, monkeypatch):
    # An unconfigured satellite mid-wizard posts the relay; it must get JSON
    # back, never the setup-redirect HTML page.
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "remote_server_url", "", raising=False)
    r = client.post("/setup/pairing/request", json={"server_url": ""},
                    follow_redirects=False)
    assert r.status_code == 200
    assert "application/json" in r.headers.get("content-type", "")
    assert not r.text.lstrip().startswith("<")
