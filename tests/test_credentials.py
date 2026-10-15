"""API keys (services/credentials.py and /api/credentials, I-3)."""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

import pytest

from app.config import settings
from app.passwords import hash_secret
from app.services import access, credentials

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


def _file(tmp_path):
    return json.loads((tmp_path / credentials.FILENAME).read_text())


# --- Minting and lookup -------------------------------------------------------

def test_minted_key_shape_and_storage(store):
    rec, secret = credentials.mint("ha", "Home Assistant", "manual", now=T0)
    assert re.match(r"^pr1\.c_[0-9a-f]{16}\.[A-Za-z0-9_-]{43}$", secret)
    assert len(secret) <= 79                        # fits the Cub's key slot
    assert not secret.startswith("pra1.")
    assert rec["scope"] == "ha" and rec["status"] == "active"
    raw = (store / credentials.FILENAME).read_text()
    assert secret not in raw and secret.split(".")[2] not in raw
    assert stat.S_IMODE(os.stat(store / credentials.FILENAME).st_mode) == 0o600
    assert credentials.lookup(secret)["id"] == rec["id"]
    assert credentials.lookup(secret[:-1] + ("A" if secret[-1] != "A" else "B")) is None


def test_mint_mirrors_into_extra_keys(store):
    rec, secret = credentials.mint("satellite", "kitchen-pi", "paired")
    assert settings.extra_api_keys == [secret]
    assert settings.extra_api_key_names == ["(mirror) kitchen-pi"]
    # A mirror is never looked up as an older key.
    assert credentials.lookup(secret)["kind"] == "paired"


def test_mint_pads_names_before_mirroring(store, monkeypatch):
    monkeypatch.setattr(settings, "extra_api_keys", ["old-a", "old-b"], raising=False)
    monkeypatch.setattr(settings, "extra_api_key_names", ["first"], raising=False)
    _rec, secret = credentials.mint("display", "Cub", "manual")
    assert settings.extra_api_keys == ["old-a", "old-b", secret]
    assert settings.extra_api_key_names == ["first", "", "(mirror) Cub"]


def test_no_mirror_while_the_switch_is_off(store, monkeypatch):
    monkeypatch.setattr(settings, "legacy_mirror", False, raising=False)
    credentials.mint("ha", "HA", "manual")
    assert settings.extra_api_keys == []


def test_admin_keys_expire_after_90_days(store):
    rec, secret = credentials.mint("admin", "script", "manual", now=T0)
    assert credentials.lookup(secret, now=T0 + 89 * DAY) is not None
    assert credentials.lookup(secret, now=T0 + 91 * DAY) is None


def test_unknown_scope_is_refused(store):
    with pytest.raises(credentials.CredentialError):
        credentials.mint("legacy", "x", "manual")


def test_legacy_lookup_and_sources(store, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "primary-key", raising=False)
    monkeypatch.setattr(settings, "extra_api_keys", ["extra-one", "pr1.c_0000000000000000.x"],
                        raising=False)
    monkeypatch.setattr(settings, "extra_api_key_names", ["Kitchen tablet", "(mirror) y"],
                        raising=False)
    p = credentials.lookup("primary-key")
    assert p["kind"] == "legacy" and p["scope"] == "legacy" and p["source"] == "api_key"
    assert p["id"] == credentials.legacy_id("primary-key")
    e = credentials.lookup("extra-one")
    assert e["source"] == "extra"
    assert credentials.display_name(p["id"]) == "primary key"
    assert credentials.display_name(e["id"]) == "Kitchen tablet"
    assert credentials.lookup("nope") is None


def test_env_key_source(store, monkeypatch):
    monkeypatch.setenv("API_KEY", "from-env")
    monkeypatch.setattr(settings, "api_key", "from-env", raising=False)
    rec = credentials.lookup("from-env")
    assert rec["source"] == "env"
    with pytest.raises(credentials.CredentialError) as exc:
        credentials.revoke(rec["id"])
    assert str(exc.value) == access.ENV_KEY_MSG
    assert settings.api_key == "from-env"


def test_revoke_pr1_removes_its_mirror(store):
    rec, secret = credentials.mint("ha", "HA", "manual")
    credentials.revoke(rec["id"])
    assert credentials.lookup(secret) is None
    assert settings.extra_api_keys == []
    assert settings.extra_api_key_names == []


def test_revoke_legacy_removes_it_from_settings(store, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "primary-key", raising=False)
    monkeypatch.setattr(settings, "extra_api_keys", ["extra-one", "extra-two"], raising=False)
    monkeypatch.setattr(settings, "extra_api_key_names", ["one", "two"], raising=False)
    credentials.revoke(credentials.legacy_id("extra-one"))
    assert settings.extra_api_keys == ["extra-two"] and settings.extra_api_key_names == ["two"]
    assert credentials.lookup("extra-one") is None
    credentials.revoke(credentials.legacy_id("primary-key"))
    assert settings.api_key == ""
    with pytest.raises(KeyError):
        credentials.revoke("c_ffffffffffffffff")


def test_set_scope_never_raises_to_admin(store, monkeypatch):
    rec, _ = credentials.mint("satellite", "sat", "manual")
    assert credentials.set_scope(rec["id"], "display") == "satellite"
    with pytest.raises(credentials.CredentialError):
        credentials.set_scope(rec["id"], "admin")
    adm, _ = credentials.mint("admin", "script", "manual")
    credentials.set_scope(adm["id"], "ha")
    assert credentials.set_scope(adm["id"], "admin") == "ha"
    monkeypatch.setattr(settings, "api_key", "primary-key", raising=False)
    lid = credentials.legacy_id("primary-key")
    assert credentials.set_scope(lid, "ha_rest") == "legacy"
    assert credentials.lookup("primary-key")["scope"] == "ha_rest"
    with pytest.raises(credentials.CredentialError):
        credentials.set_scope(lid, "admin")


def test_record_use_and_suggested_scope(store):
    rec, secret = credentials.mint("satellite", "Cub", "manual", now=T0)
    credentials.record_use(rec["id"], {"ip": "192.168.1.30", "client": "esp/1",
                                       "family": "kitchen", "method": "GET",
                                       "path": "/gadgets/esp-screen"}, now=T0)
    assert _file(store)["records"][rec["id"]]["last_used"]       # first use at once
    credentials.record_use(rec["id"], {"ip": "192.168.1.30", "family": "kitchen",
                                       "method": "POST", "path": "/gadgets/esp-action"},
                           now=T0 + 5)
    assert credentials.suggested_scope(rec["id"], now=T0 + 10) == "display"
    credentials.record_use(rec["id"], {"ip": "192.168.1.30", "family": "kitchen",
                                       "method": "GET", "path": "/timers"}, now=T0 + 20)
    assert credentials.suggested_scope(rec["id"], now=T0 + 30) == "ha_rest"
    credentials.record_use(rec["id"], {"ip": "192.168.1.30", "family": "fleet",
                                       "method": "GET", "path": "/api/config/satellite"},
                           now=T0 + 40)
    assert credentials.suggested_scope(rec["id"], now=T0 + 50) is None   # already satellite
    assert credentials.first_use(rec["id"])


def test_corrupt_file_is_set_aside_and_pr1_fails_closed(store, monkeypatch):
    rec, secret = credentials.mint("ha", "HA", "manual")
    (store / credentials.FILENAME).write_text("{not json")
    items = []
    monkeypatch.setattr(access, "raise_item",
                        lambda kind, *a, **k: items.append(kind))
    credentials.reset(None, persist=True)
    (store / credentials.FILENAME).write_text("{not json")
    # No good copy to put back (FoodAssistant-nw31c keeps one beside the file).
    (store / (credentials.FILENAME + ".last-good")).unlink(missing_ok=True)
    assert credentials.lookup(secret) is None
    assert any(p.name.startswith("credentials.json.corrupt-") for p in store.iterdir())
    assert items == ["credentials_unreadable"]


def test_damaged_file_comes_back_from_its_last_good_copy(store, monkeypatch):
    rec, secret = credentials.mint("ha", "HA", "manual")
    items = []
    monkeypatch.setattr(access, "raise_item",
                        lambda kind, *a, **k: items.append(kind))
    (store / credentials.FILENAME).write_bytes(b"\x00" * 64)   # the power went off
    assert credentials.lookup(secret)["id"] == rec["id"]
    assert any(p.name.startswith("credentials.json.corrupt-") for p in store.iterdir())
    assert items == []


# --- mark_caps and the relay ratchet (round 2) --------------------------------

def test_shared_extra_key_ratchets_only_after_every_puller_relays(store, monkeypatch):
    monkeypatch.setattr(settings, "extra_api_keys", ["shared-key"], raising=False)
    rec = credentials.lookup("shared-key")
    cid = rec["id"]
    credentials.mark_caps(cid, {"relay1"}, device_id="dev-a", now=T0)
    credentials.mark_caps(cid, set(), device_id="dev-b", now=T0 + 1)
    assert not credentials.relay_ratcheted(cid)
    credentials.mark_caps(cid, {"relay1"}, device_id="dev-a", now=T0 + 2 * DAY)
    assert not credentials.relay_ratcheted(cid)
    credentials.mark_caps(cid, {"relay1"}, device_id="dev-b", now=T0 + 2 * DAY + 3)
    assert credentials.relay_ratcheted(cid)


def test_an_old_puller_does_not_hold_the_ratchet_back(store, monkeypatch):
    monkeypatch.setattr(settings, "extra_api_keys", ["shared-key"], raising=False)
    cid = credentials.lookup("shared-key")["id"]
    credentials.mark_caps(cid, set(), device_id="gone", now=T0)
    credentials.mark_caps(cid, {"relay1"}, device_id="dev-a", now=T0 + 31 * DAY)
    assert credentials.relay_ratcheted(cid)
    assert credentials.get(cid)["source"] == "extra"


@pytest.mark.parametrize("source", ["api_key", "env"])
def test_primary_and_env_keys_never_ratchet(store, monkeypatch, source):
    monkeypatch.setattr(settings, "api_key", "primary-key", raising=False)
    if source == "env":
        monkeypatch.setenv("API_KEY", "primary-key")
    rec = credentials.lookup("primary-key")
    assert rec["source"] == source
    credentials.mark_caps(rec["id"], {"relay1"}, device_id="dev-a", now=T0)
    assert not credentials.relay_ratcheted(rec["id"])


def test_a_shared_key_needs_a_day_of_pulls_on_record(store, monkeypatch):
    monkeypatch.setattr(settings, "extra_api_keys", ["shared-key"], raising=False)
    cid = credentials.lookup("shared-key", now=T0)["id"]
    credentials.mark_caps(cid, {"relay1"}, device_id="dev-a", now=T0)
    assert not credentials.relay_ratcheted(cid)
    credentials.mark_caps(cid, {"relay1"}, device_id="dev-a", now=T0 + 2 * DAY)
    assert credentials.relay_ratcheted(cid)


def test_a_pr1_key_with_one_puller_ratchets_on_its_first_relay_pull(store):
    rec, _ = credentials.mint("satellite", "sat", "paired")
    credentials.mark_caps(rec["id"], {"relay1"}, device_id="sat-1", now=T0)
    assert credentials.relay_ratcheted(rec["id"])


def test_the_puller_cap_keeps_recent_pullers(store, monkeypatch):
    monkeypatch.setattr(settings, "extra_api_keys", ["shared-key"], raising=False)
    cid = credentials.lookup("shared-key")["id"]
    credentials.mark_caps(cid, set(), device_id="ancient", now=T0)
    for i in range(16):
        credentials.mark_caps(cid, set(), device_id=f"dev-{i}", now=T0 + 40 * DAY + i)
    keys = [p["key"] for p in credentials.get(cid)["pullers"]]
    assert len(keys) == 16
    assert "ancient" not in keys and "dev-0" in keys


# --- Routes -------------------------------------------------------------------

@pytest.fixture
def client(store, monkeypatch):
    monkeypatch.chdir(SERVICE)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True, raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _password(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret("hunter2"), raising=False)


def test_list_never_carries_a_secret_or_hash(client, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "primary-key", raising=False)
    rec, secret = credentials.mint("ha", "HA", "manual")
    r = client.get("/api/credentials")
    assert r.status_code == 200
    body = r.text
    assert secret not in body and rec["secret_sha256"] not in body
    assert "primary-key" not in body
    names = {c["name"] for c in r.json()["credentials"]}
    assert {"HA", "primary key"} <= names
    assert r.json()["open"] is True


def test_create_with_a_password_needs_admin_and_the_password(client, monkeypatch):
    _password(monkeypatch)
    assert client.post("/api/credentials", json={"scope": "ha", "name": "HA"}).status_code == 401
    client.post("/ui/login", data={"password": "hunter2"}, follow_redirects=False)
    r = client.post("/api/credentials", json={"scope": "ha", "name": "HA", "password": "wrong"})
    assert r.status_code == 403 and r.json()["error"] == access.MINT_PASSWORD_MSG
    r = client.post("/api/credentials", json={"scope": "admin", "name": "Script",
                                              "password": "hunter2"})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert r.json()["key"].startswith("pr1.") and r.json()["expires"]


def test_create_without_a_password_is_home_only_and_never_admin(client, monkeypatch):
    r = client.post("/api/credentials", json={"scope": "admin", "name": "Script"})
    assert r.status_code == 403 and r.json()["error"] == access.ADMIN_KEY_OPEN_MSG
    r = client.post("/api/credentials", json={"scope": "display", "name": "Cub"})
    assert r.status_code == 200
    r = client.post("/api/credentials", json={"scope": "display", "name": ""})
    assert r.status_code == 400
    r = client.post("/api/credentials", json={"scope": "nope", "name": "x"})
    assert r.status_code == 400
    from fastapi.testclient import TestClient
    from app.main import app
    proxied = TestClient(app, headers={"X-Forwarded-For": "203.0.113.9"})
    r = proxied.post("/api/credentials", json={"scope": "display", "name": "Cub"})
    assert r.status_code == 403


def test_revoke_and_scope_routes(client, monkeypatch):
    rec, secret = credentials.mint("satellite", "sat", "manual")
    r = client.post(f"/api/credentials/{rec['id']}/scope", json={"scope": "display"})
    assert r.json() == {"ok": True, "scope": "display", "previous": "satellite"}
    assert client.post(f"/api/credentials/{rec['id']}/scope",
                       json={"scope": "admin"}).status_code == 400
    assert client.post(f"/api/credentials/{rec['id']}/revoke").json() == {"ok": True}
    assert client.post("/api/credentials/c_ffffffffffffffff/revoke").status_code == 404
    monkeypatch.setattr(settings, "api_key", "env-key", raising=False)
    monkeypatch.setenv("API_KEY", "env-key")
    r = client.post(f"/api/credentials/{credentials.legacy_id('env-key')}/revoke")
    assert r.status_code == 400 and r.json()["error"] == access.ENV_KEY_MSG


def test_scoped_keys_reach_only_their_routes(client, monkeypatch):
    _password(monkeypatch)
    _rec, ha = credentials.mint("ha", "HA", "manual")
    assert client.get("/ha/state", headers={"X-API-Key": ha}).status_code == 200
    r = client.get("/timers", headers={"X-API-Key": ha})
    assert r.status_code == 403 and r.json()["detail"] == access.SCOPE_MSG
    r = client.post("/setup/save", json={}, headers={"X-API-Key": ha})
    assert r.status_code == 403 and r.json()["detail"] == access.ADMIN_KEY_MSG


def test_a_key_with_bytes_outside_ascii_is_refused_not_an_error(store, monkeypatch):
    """A header can carry any byte. Such a key matches nothing and the request
    gets the normal answer, never a server error."""
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(settings, "api_key", "older-shared-key", raising=False)
    assert credentials.lookup("été") is None
    client = TestClient(app, raise_server_exceptions=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret("a-password-here"), raising=False)
    r = client.get("/timers", headers={"X-API-Key": b"\xe9t\xe9"}, follow_redirects=False)
    assert r.status_code == 401
    # A key saved with such characters still matches when sent as UTF-8.
    monkeypatch.setattr(settings, "api_key", "clé-partagée", raising=False)
    r = client.get("/timers", headers={"X-API-Key": "clé-partagée".encode("utf-8")},
                   follow_redirects=False)
    # The key is accepted, so the request gets past sign-in to the next step,
    # which sends an install that is not set up yet to the wizard.
    assert r.status_code == 303 and r.headers["location"].endswith("/setup")
