"""Let a ratcheted satellite key receive service keys again (FoodAssistant-xym8y).

Once every device pulling with a key has sent relay1, the key is ratcheted:
a later pull without relay1 (a Bandit flashed back to 0.20.x) gets the
cut-off payload, so AI, recipes, Home Assistant and cameras stop on it.
Settings, Security shows such a key as holding service keys back and offers
Let this key receive service keys again, which clears the ratchet for that
one key. It is an admin action, refused from another site.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app.config import settings
from app.passwords import hash_secret
from app.services import credentials

SERVICE = Path(__file__).resolve().parents[1] / "service"
T0 = 1_800_000_000.0
DAY = 86400.0


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "api_key", "", raising=False)
    monkeypatch.setattr(settings, "extra_api_keys", [], raising=False)
    monkeypatch.setattr(settings, "extra_api_key_names", [], raising=False)
    monkeypatch.setattr(settings, "legacy_mirror", True, raising=False)
    credentials.reset(None, persist=True)
    yield tmp_path
    credentials.reset(persist=False)


@pytest.fixture
def client(store, monkeypatch):
    monkeypatch.chdir(SERVICE)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "satellite_legacy_credentials", "auto", raising=False)
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True, raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _ratcheted(name="garage-bandit", device="garage-1", now=T0):
    rec, secret = credentials.mint("satellite", name, "paired", now=now)
    credentials.mark_caps(rec["id"], {"relay1"}, device_id=device, now=now)
    assert credentials.relay_ratcheted(rec["id"])
    return rec, secret


def _downgraded(name="garage-bandit", device="garage-1"):
    """A key ratcheted an hour ago whose device just pulled without relay1."""
    now = time.time()
    rec, secret = _ratcheted(name, device, now=now - 3600)
    credentials.mark_caps(rec["id"], set(), device_id=device, now=now - 60)
    return rec, secret


def _withheld(cid, now=None) -> bool:
    return credentials.service_keys_withheld(credentials.get(cid), now=now)


# --- The key store ------------------------------------------------------------------

def test_a_pull_without_relay1_shows_the_key_as_holding_service_keys_back(store):
    rec, _ = _ratcheted(now=T0)
    cid = rec["id"]
    assert not _withheld(cid, now=T0 + 1)                 # an up-to-date Bandit
    credentials.mark_caps(cid, set(), device_id="garage-1", now=T0 + 60)
    assert credentials.relay_ratcheted(cid)
    assert _withheld(cid, now=T0 + 120)
    assert not _withheld(cid, now=T0 + 31 * DAY)          # nobody pulled for a month
    plain, _ = credentials.mint("satellite", "older-bandit", "paired", now=T0)
    credentials.mark_caps(plain["id"], set(), device_id="older-1", now=T0)
    assert not _withheld(plain["id"], now=T0 + 1)         # never ratcheted: it gets them
    assert not credentials.service_keys_withheld(None)


def test_clearing_lets_the_key_get_service_keys_until_its_device_updates(store):
    rec, _ = _ratcheted(now=T0)
    cid = rec["id"]
    credentials.mark_caps(cid, set(), device_id="garage-1", now=T0 + 60)
    assert credentials.clear_relay_ratchet(cid, now=T0 + 120) is True
    assert not credentials.relay_ratcheted(cid)
    assert not _withheld(cid, now=T0 + 121)
    assert [p["relay1_at"] for p in credentials.get(cid)["pullers"]] == [None]
    # Saved at once, not only in memory.
    saved = json.loads((store / credentials.FILENAME).read_text())["records"][cid]
    assert saved["relay_capable_at"] is None
    assert [p["relay1_at"] for p in saved["pullers"]] == [None]
    # The old version keeps pulling: the key stays clear.
    credentials.mark_caps(cid, set(), device_id="garage-1", now=T0 + 900)
    assert not credentials.relay_ratcheted(cid)
    # Updated again, it asks for the relay and the ratchet sets once more.
    credentials.mark_caps(cid, {"relay1"}, device_id="garage-1", now=T0 + 2 * DAY)
    assert credentials.relay_ratcheted(cid)
    assert credentials.clear_relay_ratchet(cid, now=T0 + 3 * DAY) is True
    assert credentials.clear_relay_ratchet(cid, now=T0 + 3 * DAY) is False


def test_another_bandit_on_a_shared_key_does_not_undo_the_clear(store, monkeypatch):
    monkeypatch.setattr(settings, "extra_api_keys", ["shared-key"], raising=False)
    cid = credentials.lookup("shared-key", now=T0)["id"]
    credentials.mark_caps(cid, {"relay1"}, device_id="dev-a", now=T0)
    credentials.mark_caps(cid, {"relay1"}, device_id="dev-b", now=T0 + 2 * DAY)
    assert credentials.relay_ratcheted(cid)
    credentials.mark_caps(cid, set(), device_id="dev-b", now=T0 + 2 * DAY + 60)
    assert _withheld(cid, now=T0 + 2 * DAY + 61)
    credentials.clear_relay_ratchet(cid, now=T0 + 2 * DAY + 120)
    credentials.mark_caps(cid, set(), device_id="dev-b", now=T0 + 2 * DAY + 900)
    # dev-a still relays, but dev-b has not updated: the key stays clear.
    credentials.mark_caps(cid, {"relay1"}, device_id="dev-a", now=T0 + 2 * DAY + 1000)
    assert not credentials.relay_ratcheted(cid)
    credentials.mark_caps(cid, {"relay1"}, device_id="dev-b", now=T0 + 3 * DAY)
    assert credentials.relay_ratcheted(cid)


def test_clearing_needs_a_key_that_is_still_on(store):
    with pytest.raises(KeyError):
        credentials.clear_relay_ratchet("c_ffffffffffffffff")
    rec, _ = _ratcheted(now=T0)
    credentials.revoke(rec["id"], now=T0 + 1)
    with pytest.raises(credentials.CredentialError):
        credentials.clear_relay_ratchet(rec["id"], now=T0 + 2)


# --- Settings, Security -------------------------------------------------------------

def _listed(client) -> dict:
    return {c["id"]: c for c in client.get("/api/credentials").json()["credentials"]}


def test_the_key_list_says_which_key_holds_service_keys_back(client):
    held, _ = _downgraded()
    current, _ = _ratcheted("kitchen-bandit", "kitchen-1", now=time.time() - 60)
    listed = _listed(client)
    assert listed[held["id"]]["service_keys_withheld"] is True
    assert listed[current["id"]]["service_keys_withheld"] is False


def test_letting_the_key_receive_service_keys_again(client):
    rec, _ = _downgraded()
    r = client.post(f"/api/credentials/{rec['id']}/share-service-keys")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "changed": True,
                        "message": "garage-bandit gets service keys again on its next sync."}
    assert not credentials.relay_ratcheted(rec["id"])
    assert _listed(client)[rec["id"]]["service_keys_withheld"] is False


def test_the_answer_says_when_older_satellites_are_set_to_get_none(client, monkeypatch):
    monkeypatch.setattr(settings, "satellite_legacy_credentials", "off", raising=False)
    rec, _ = _downgraded()
    r = client.post(f"/api/credentials/{rec['id']}/share-service-keys")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert "set to Never" in r.json()["message"]
    assert not credentials.relay_ratcheted(rec["id"])


def test_unknown_and_revoked_keys_are_refused(client):
    assert client.post("/api/credentials/c_ffffffffffffffff/share-service-keys").status_code == 404
    rec, _ = _downgraded()
    credentials.revoke(rec["id"])
    r = client.post(f"/api/credentials/{rec['id']}/share-service-keys")
    assert r.status_code == 400 and r.json()["error"] == "That key was revoked."


def test_only_the_admin_can_do_it_and_only_from_this_kitchen(client, monkeypatch):
    rec, secret = _downgraded()
    url = f"/api/credentials/{rec['id']}/share-service-keys"
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret("hunter2"), raising=False)
    monkeypatch.setattr(settings, "totp_secret", "", raising=False)
    monkeypatch.setattr(settings, "viewer_password", hash_secret("kitchen"), raising=False)
    assert client.post(url).status_code == 401
    # The Bandit cannot let its own key back in.
    assert client.post(url, headers={"X-API-Key": secret}).status_code == 403
    client.post("/ui/login", data={"password": "kitchen"}, follow_redirects=False)
    assert client.post(url).status_code == 403
    assert credentials.relay_ratcheted(rec["id"])
    client.cookies.clear()
    client.post("/ui/login", data={"password": "hunter2"}, follow_redirects=False)
    for site in ("cross-site", "same-site"):
        assert client.post(url, headers={"Sec-Fetch-Site": site}).status_code == 403
    assert credentials.relay_ratcheted(rec["id"])
    assert client.post(url, headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert not credentials.relay_ratcheted(rec["id"])


def test_another_site_is_refused_on_a_kitchen_with_no_password(client):
    rec, _ = _downgraded()
    r = client.post(f"/api/credentials/{rec['id']}/share-service-keys",
                    headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert credentials.relay_ratcheted(rec["id"])


def test_the_pane_script_offers_the_button_next_to_that_key():
    js = (SERVICE / "app" / "static" / "js" / "setup" / "credentials.js").read_text()
    assert "if (c.service_keys_withheld) {" in js
    assert "'Let this key receive service keys again'" in js
    assert "'api/credentials/' + encodeURIComponent(c.id) + '/share-service-keys'" in js
