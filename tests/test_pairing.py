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
    from app.services import ha_events
    pairing.reset()
    yield
    pairing.reset()
    # Pairing requests raise an on-screen toast; keep it out of later tests.
    ha_events.reset()


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


def test_the_state_file_is_private_and_never_backed_up(tmp_path):
    """An approved key waits in the file until its device collects it."""
    import stat
    from app.routers import admin
    created = pairing.create_request("kitchen-pi", "192.168.1.44")
    pairing.approve(created["request_id"], "minted-key", "kitchen-pi")
    path = tmp_path / "pairing.json"
    assert "minted-key" in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".pairing.")]
    assert "pairing.json" in admin.DEVICE_LOCAL_FILES


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
        # conftest gives the client a LAN peer (not loopback), so loopback
        # trust never masks the auth checks; off-LAN cases pass their own peer.
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
    """Let the client count as on-LAN regardless of its Host header, so the
    handler logic past the gate can be exercised. The gate uses the stricter
    request-based check (FoodAssistant-gbs8), so patch that."""
    monkeypatch.setattr("app.routers.pairing.pairing.is_local_network_request",
                        lambda request: True)


def _off_lan_client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app, client=("203.0.113.9", 50000))


def _admin_key() -> dict:
    from app.services import credentials
    _rec, key = credentials.mint("admin", "test", "manual")
    return {"X-API-Key": key}


def test_request_is_refused_from_a_non_private_address(client, monkeypatch):
    _configured_server(monkeypatch)
    r = _off_lan_client().post("/api/pairing/request", json={"hostname": "kitchen-pi"})
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

    # An older full-access key cannot approve (it no longer covers admin
    # actions); an admin key mints a scoped key of the device's own.
    assert client.post("/api/pairing/approve", json={"request_id": d["request_id"]},
                       headers={"X-API-Key": "sesame"}).status_code == 403
    admin = _admin_key()
    a = client.post("/api/pairing/approve", json={"request_id": d["request_id"]},
                    headers=admin)
    assert a.status_code == 200 and a.json()["ok"]
    assert a.json()["name"] == "kitchen-pi"
    assert a.json()["kind"] == "satellite"
    # The new key is mirrored where older versions look for keys.
    assert "(mirror) kitchen-pi" in settings.extra_api_key_names
    minted = settings.extra_api_keys[settings.extra_api_key_names.index("(mirror) kitchen-pi")]
    assert minted.startswith("pr1.")

    # The satellite's poll now carries the key, and the key authenticates.
    s = client.get(f"/api/pairing/status/{d['request_id']}")
    assert s.json()["status"] == "approved" and s.json()["api_key"] == minted
    from app.services import credentials
    assert credentials.lookup(minted)["scope"] == "satellite"


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
    assert r.status_code == 403
    r = client.post("/api/pairing/deny", json={"request_id": d["request_id"]},
                    headers=_admin_key())
    assert r.status_code == 200 and r.json()["ok"]
    s = client.get(f"/api/pairing/status/{d['request_id']}")
    assert s.json()["status"] == "denied"
    assert [n for n in settings.extra_api_key_names if n != "(mirror) test"] == []


def test_status_endpoint_is_lan_gated_too(client, monkeypatch):
    _configured_server(monkeypatch)
    r = _off_lan_client().get("/api/pairing/status/whatever")
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


# --- Kinds and the delivery window (0.21) --------------------------------------

def test_kinds_are_kept_and_suggested_from_the_hostname():
    a = pairing.create_request("homeassistant", "192.168.1.20")
    b = pairing.create_request("bandit-cub-1", "192.168.1.30", "nonsense")
    c = pairing.create_request("kitchen-pi", "192.168.1.40", "ha_rest")
    rows = {r["request_id"]: r for r in pairing.pending_requests()}
    assert rows[a["request_id"]]["kind"] == "" and rows[a["request_id"]]["suggested_kind"] == "ha"
    assert rows[b["request_id"]]["kind"] == "" and rows[b["request_id"]]["suggested_kind"] == "display"
    assert rows[c["request_id"]]["suggested_kind"] == "ha_rest"
    assert pairing.suggest_kind("Home Assistant") == "ha"
    assert pairing.suggest_kind("hass-pi") == "ha"
    assert pairing.suggest_kind("pantry-two") == "satellite"


def test_the_key_is_handed_out_until_first_use_or_two_minutes(monkeypatch):
    from app.services import credentials
    monkeypatch.setattr(settings, "legacy_mirror", False, raising=False)
    created = pairing.create_request("kitchen-pi", "192.168.1.44")
    rec, key = credentials.mint("satellite", "kitchen-pi", "paired")
    assert pairing.approve(created["request_id"], key, "kitchen-pi", rec["id"])
    rid = created["request_id"]
    assert pairing.get_status(rid)["api_key"] == key
    assert pairing.get_status(rid)["api_key"] == key
    credentials.record_use(rec["id"], {"ip": "192.168.1.44"})
    assert pairing.get_status(rid) == {"status": "approved"}

    created = pairing.create_request("kitchen-pi-2", "192.168.1.45")
    rec2, key2 = credentials.mint("satellite", "kitchen-pi-2", "paired")
    pairing.approve(created["request_id"], key2, "kitchen-pi-2", rec2["id"])
    clock = {"t": pairing.time.time()}
    monkeypatch.setattr(pairing.time, "time", lambda: clock["t"])
    assert pairing.get_status(created["request_id"])["api_key"] == key2
    clock["t"] += 119
    assert pairing.get_status(created["request_id"])["api_key"] == key2
    clock["t"] += 2
    assert pairing.get_status(created["request_id"]) == {"status": "approved"}
    assert key2 not in (Path(settings.data_dir) / "pairing.json").read_text()


def test_approve_mints_the_chosen_kind(client, monkeypatch):
    _configured_server(monkeypatch)
    _trust_client_lan(monkeypatch)
    d = client.post("/api/pairing/request", json={"hostname": "bandit-cub-7"}).json()
    admin = _admin_key()
    bad = client.post("/api/pairing/approve", json={"request_id": d["request_id"], "kind": "admin"},
                      headers=admin)
    assert bad.status_code == 400
    a = client.post("/api/pairing/approve", json={"request_id": d["request_id"]}, headers=admin)
    assert a.json()["kind"] == "display"
    s = client.get(f"/api/pairing/status/{d['request_id']}").json()
    from app.services import credentials
    assert credentials.lookup(s["api_key"])["scope"] == "display"
    assert credentials.lookup(s["api_key"])["kind"] == "paired"


def test_a_viewer_cannot_approve(client, monkeypatch):
    _configured_server(monkeypatch)
    _trust_client_lan(monkeypatch)
    monkeypatch.setattr(settings, "viewer_password", hash_secret("kitchen"), raising=False)
    d = client.post("/api/pairing/request", json={"hostname": "kitchen-pi"}).json()
    client.post("/ui/login", data={"password": "kitchen"}, follow_redirects=False)
    r = client.post("/api/pairing/approve", json={"request_id": d["request_id"]})
    assert r.status_code == 403
