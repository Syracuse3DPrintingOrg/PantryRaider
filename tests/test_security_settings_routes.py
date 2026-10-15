"""Access settings and trust actions (/setup/security, C4)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import settings
from app.passwords import hash_secret
from app.services import access, auth_state, credentials

SERVICE = Path(__file__).resolve().parents[1] / "service"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "extra_api_keys", [], raising=False)
    monkeypatch.setattr(settings, "extra_api_key_names", [], raising=False)
    monkeypatch.setattr(settings, "require_key_families", {}, raising=False)
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True, raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _saved(tmp_path):
    return json.loads((Path(settings.data_dir) / "settings.json").read_text())


def test_state_shape(client):
    r = client.get("/setup/security/state")
    assert r.status_code == 200
    s = r.json()
    for key in ("open", "origin", "lan_cidrs", "lan_cidrs_editable", "home_host_names", "families",
                "kiosk_screen_is_admin", "legacy_mirror", "legacy_key_auto_retire",
                "satellite_legacy_credentials", "satellites_needing_update", "local_programs",
                "open_mode", "keyless_ha", "gateway_hint", "password_set", "two_factor_on",
                "kiosk_url_warning", "keyless_devices", "keyless_refused", "same_host_proxy"):
        assert key in s, key
    assert s["origin"] == "lan_direct" and s["lan_cidrs_editable"] is True
    assert set(s["families"]) == set(access.FAMILIES)
    assert s["copy"]["TWO_FACTOR_NEXT_MSG"] == access.TWO_FACTOR_NEXT_MSG


def test_every_field_is_saved_and_read_back(client, tmp_path):
    body = {"kiosk_screen_is_admin": False, "lan_cidrs": "100.64.0.0/10, fd00:1::/48",
            "home_host_names": "pantry.example.com", "legacy_mirror": True,
            "legacy_key_auto_retire": False, "satellite_legacy_credentials": "off",
            "require_key_families": {"ha": True}}
    r = client.post("/setup/security/settings", json=body)
    assert r.status_code == 200 and r.json()["ok"]
    assert set(r.json()["saved"]) == set(body)
    settings.reload()
    saved = _saved(tmp_path)
    for k, v in body.items():
        assert saved[k] == v
    s = client.get("/setup/security/state").json()
    assert s["kiosk_screen_is_admin"] is False and s["satellite_legacy_credentials"] == "off"
    assert s["families"]["ha"]["required"] is True


def test_families_merge_over_the_stored_choices(client, tmp_path):
    client.post("/setup/security/settings", json={"require_key_families": {"ha": True}})
    client.post("/setup/security/settings", json={"require_key_families": {"reader": False}})
    assert _saved(tmp_path)["require_key_families"] == {"ha": True, "reader": False}


@pytest.mark.parametrize("body,field", [
    ({"lan_cidrs": "10.0.0.0/4, nonsense"}, "lan_cidrs"),
    ({"home_host_names": "bad_name"}, "home_host_names"),
    ({"satellite_legacy_credentials": "sometimes"}, "satellite_legacy_credentials"),
    ({"require_key_families": {"printers": True}}, "require_key_families"),
    ({"require_key_families": {"ha": "yes"}}, "require_key_families"),
    ({"kiosk_screen_is_admin": "off"}, "kiosk_screen_is_admin"),
])
def test_bad_entries_are_named_and_nothing_is_saved(client, tmp_path, body, field):
    r = client.post("/setup/security/settings", json={**body, "legacy_mirror": False})
    assert r.status_code == 400
    assert field in r.json()["errors"]
    assert not (tmp_path / "settings.json").exists()


def test_bad_cidr_entries_are_listed(client):
    r = client.post("/setup/security/settings", json={"lan_cidrs": "10.0.0.0/4 nonsense"})
    msg = r.json()["errors"]["lan_cidrs"]
    assert "10.0.0.0/4" in msg and "nonsense" in msg


def test_home_networks_change_only_from_home(client):
    from fastapi.testclient import TestClient
    from app.main import app
    proxied = TestClient(app, headers={"X-Forwarded-For": "203.0.113.9"})
    r = proxied.post("/setup/security/settings", json={"lan_cidrs": "100.64.0.0/10"})
    assert r.status_code == 403 and r.json()["error"] == access.LAN_CIDRS_REMOTE_MSG
    assert proxied.get("/setup/security/state").json()["lan_cidrs_editable"] is False


def test_home_networks_change_from_the_home_assistant_sidebar(client, ingress_client):
    """The add-on has no LAN_CIDRS option, and Home Assistant's sidebar is
    home, so Home networks can be set from there."""
    assert ingress_client.get("/setup/security/state").json()["lan_cidrs_editable"] is True
    r = ingress_client.post("/setup/security/settings", json={"lan_cidrs": "100.64.0.0/10"})
    assert r.status_code == 200 and r.json()["saved"] == ["lan_cidrs"]


def test_the_pane_is_told_where_a_password_alone_works(client):
    from fastapi.testclient import TestClient
    from app.main import app
    assert client.get("/setup/security/state").json()["sign_in_outside"] is False
    public = TestClient(app, client=("203.0.113.9", 50000))
    # An open kitchen refuses visitors from outside only after the grace; the
    # conftest's upgraded install is still in it.
    assert public.get("/setup/security/state").json()["sign_in_outside"] is True


def test_turning_the_mirror_off_removes_every_copy(client, monkeypatch):
    monkeypatch.setattr(settings, "legacy_mirror", True, raising=False)
    monkeypatch.setattr(settings, "extra_api_keys", ["older-key"], raising=False)
    monkeypatch.setattr(settings, "extra_api_key_names", ["Old"], raising=False)
    _rec, secret = credentials.mint("ha", "HA", "manual")
    assert secret in settings.extra_api_keys
    client.post("/setup/security/settings", json={"legacy_mirror": False})
    assert settings.extra_api_keys == ["older-key"]
    assert settings.extra_api_key_names == ["Old"]
    assert credentials.lookup(secret) is not None


def test_trust_actions(client):
    r = client.post("/setup/security/local-trust", json={"action": "retire"})
    assert r.status_code == 200 and r.json()["loopback"]["state"] == "retired"
    assert r.json()["loopback"]["by"] == "admin"
    assert not auth_state.legacy_trust_active()
    r = client.post("/setup/security/local-trust", json={"action": "retrust"})
    assert r.json()["loopback"]["retrust_until"]
    assert auth_state.legacy_trust_active()
    assert client.post("/setup/security/local-trust", json={"action": "x"}).status_code == 400


def test_kiosk_signout(client):
    gen = auth_state.kiosk_gen()
    r = client.post("/setup/security/kiosk-signout")
    assert r.json() == {"ok": True, "kiosk_gen": gen + 1}


def test_viewers_and_older_keys_are_refused(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret("hunter2"), raising=False)
    monkeypatch.setattr(settings, "viewer_password", hash_secret("kitchen"), raising=False)
    monkeypatch.setattr(settings, "api_key", "older-key", raising=False)
    r = client.post("/setup/security/settings", json={"legacy_mirror": False},
                    headers={"X-API-Key": "older-key"})
    assert r.status_code == 403 and r.json()["detail"] == access.ADMIN_KEY_MSG
    client.post("/ui/login", data={"password": "kitchen"}, follow_redirects=False)
    r = client.post("/setup/security/settings", json={"legacy_mirror": False})
    assert r.status_code == 403 and r.json()["detail"] == access.VIEWER_MSG
    assert client.get("/setup/security/state").status_code == 403


def test_the_pane_carries_the_copy_exactly():
    pane = (SERVICE / "app" / "templates" / "setup" / "_pane_security.html").read_text()
    flat = " ".join(pane.split())
    for name in ("REQUIRE_PASSWORD_HELP", "KIOSK_ADMIN_HELP", "SATELLITE_KEYS_HELP",
                 "MIRROR_HELP", "AUTO_RETIRE_HELP", "OPEN_NOTICE"):
        assert getattr(access, name) in flat, name
    # NAME_ALLOW_HINT is the refusal page's advice ("add it under Other names
    # for this kitchen"); on that row itself it would point at itself.
    assert access.NAME_ALLOW_HINT not in flat
    assert "separated by commas" in flat
    # The pane that approves pairing is Bandit Remotes, as the side menu says.
    assert "Settings, Devices" not in flat and "Settings, Bandit Remotes" in flat
    for label in access.SCOPE_LABELS.values():
        if label != access.SCOPE_LABELS["legacy"]:
            assert label in flat, label
    for gone in ('id="api_key"', "satellite-extra-keys", "generateApiKey", "addSatelliteKey"):
        assert gone not in pane


def test_the_pane_script_writes_server_text_as_text():
    js = (SERVICE / "app" / "static" / "js" / "setup" / "credentials.js").read_text()
    assert "innerHTML" not in js
    assert "fetch('/" not in js and 'fetch("/' not in js      # relative URLs only
    helpers = (SERVICE / "app" / "static" / "js" / "setup" / "helpers.js").read_text()
    assert "function generateApiKey" not in helpers and "function addSatelliteKey" not in helpers
    # The key rows left with the old key UI, and so did everything that read them.
    assert "collectSatelliteKeys" not in helpers
    panes = (SERVICE / "app" / "static" / "js" / "setup" / "panes.js").read_text()
    assert "collectSatelliteKeys" not in panes and "satellite-extra-keys" not in panes
    assert "getElementById('api_key')" not in panes and "secretVal('api_key')" not in panes
    guard = (SERVICE / "app" / "static" / "js" / "setup" / "dirty-guard.js").read_text()
    assert "setup\\/security" in guard and "api\\/credentials" in guard
