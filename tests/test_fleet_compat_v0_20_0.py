"""A Bandit on v0.20.0 against this release's server, and the other way round.

The old satellite is the real v0.20.0 code: the config pull's functions and
constants as they shipped, vendored at build time into
tests/data/compat_v0_20_0_satellite.json (scripts/compat/
capture-v0-20-0-satellite.py), with the v0.20.0 pull field list from
tests/data/r1_fleet_pull_fields_v0_20_0.json. No test runs git.

The old code runs in its own namespace with a small settings object and an
httpx stand-in that hands each request to this release's app in-process.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import socket
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.models import db_models  # noqa: E402,F401

DATA = Path(__file__).parent / "data"
FIXTURE = json.loads((DATA / "compat_v0_20_0_satellite.json").read_text())
OLD_FIELDS = json.loads((DATA / "r1_fleet_pull_fields_v0_20_0.json").read_text())["fields"]
VECTORS = json.loads((Path(__file__).parent / "fixtures"
                      / "local_credentials_vectors.json").read_text())

PRIMARY = "primary-key-fake-0002"
EXTRA = "extra-key-fake-0002"
AI_KEY = "AIzaFAKEcompatKEY0000"
HA_TOKEN = "eyFAKE.compat.token"
CAMERAS = [
    {"name": "Door", "snapshot_url": "http://user:pw@192.168.1.60/snap.jpg"},
    {"name": "Yard", "source": "reolink", "host": "192.168.1.61", "username": "admin",
     "password": "reolink-fake-pw", "channel": 0},
    {"name": "Garden", "snapshot_url": "http://192.168.1.62/still.jpg"},
]
SERVER_URL = "http://192.168.1.10:9284"


def test_fixture_is_the_v0_20_0_pull():
    assert FIXTURE["tag"] == "v0.20.0"
    assert FIXTURE["file"] == "service/app/services/satellite.py"
    assert set(FIXTURE["names"]) == set(FIXTURE["sources"])
    assert "grocy_api_key" in OLD_FIELDS and "streamdeck_ha_token" in OLD_FIELDS


class OldSettings:
    """What the v0.20.0 code reads off settings, and a save that keeps it."""

    def __init__(self, **values):
        self.__dict__.update(values)

    def save(self, data):
        self.__dict__.update(data)

    def is_satellite(self):
        return True


def _noop(*a, **k):
    return 0


def old_satellite(key: str, device_id: str, client) -> types.SimpleNamespace:
    """The v0.20.0 pull, wired to this release's app through ``client``."""
    values = {f: "" for f in OLD_FIELDS}
    values.update(streamdeck_cameras=[], streamdeck_key_overrides=[], streamdeck_ha_slots=[])
    old = OldSettings(
        deployment_mode="pi_remote", remote_server_url=SERVER_URL, upstream_api_key=key,
        device_id=device_id, remote_server_ip="", remote_server_host="",
        server_sourced_fields=set(), **values)

    def _get(url, headers=None, timeout=None, **kw):
        u = urlparse(url)
        path = u.path + (f"?{u.query}" if u.query else "")
        return client.get(path, headers=headers or {})

    ns = {
        "__name__": "v0_20_0_satellite",
        "settings": old, "SATELLITE_PULL_FIELDS": list(OLD_FIELDS), "APP_VERSION": "0.20.0",
        "hashlib": hashlib, "hmac": hmac, "_json": json, "_secrets": secrets,
        "socket": socket, "logger": logging.getLogger("test.v0_20_0_satellite"),
        "datetime": datetime, "timezone": timezone,
        "urlparse": urlparse, "urlunparse": urlunparse,
        "httpx": types.SimpleNamespace(get=_get),
        "_record_last_sync": _noop, "_apply_defaults": _noop, "_apply_profiles": _noop,
        "_apply_gadget_config": _noop, "mirror_alarms": _noop,
        "_push_streamdeck_settings": _noop, "_push_timezone": _noop,
        "_push_update_channel": _noop, "_local_ip": lambda: "192.168.1.30",
    }
    for name in FIXTURE["names"]:
        exec(compile(FIXTURE["sources"][name], f"<v0.20.0 {name}>", "exec"), ns)  # noqa: S102
    return types.SimpleNamespace(settings=old, ns=ns,
                                 sync=lambda: ns["_do_sync_from_upstream"]())


# --- an old Bandit on this release's server ----------------------------------------


@pytest.fixture
def server(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.routers import satellite as sat_router
    from app.services import devices, fleet_relay

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(devices, "SessionLocal", Session)
    monkeypatch.setattr(sat_router, "SessionLocal", Session)
    fleet_relay.reset_advisory_check()
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "deployment_mode", "server")
    monkeypatch.setattr(settings, "api_key", PRIMARY)
    monkeypatch.setattr(settings, "extra_api_keys", [EXTRA])
    monkeypatch.setattr(settings, "extra_api_key_names", ["Kitchen Bandit"])
    monkeypatch.setattr(settings, "auth_password", "")
    monkeypatch.setattr(settings, "satellite_legacy_credentials", "auto")
    monkeypatch.setattr(settings, "gemini_api_key", AI_KEY)
    monkeypatch.setattr(settings, "spoonacular_api_key", "spoon-fake")
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "http://192.168.1.40:8123")
    monkeypatch.setattr(settings, "streamdeck_ha_token", HA_TOKEN)
    monkeypatch.setattr(settings, "streamdeck_cameras", [dict(c) for c in CAMERAS])
    return TestClient(app, client=("192.168.1.30", 50000))


def _legacy_payload_applied(old) -> bool:
    s = old.settings
    return (s.gemini_api_key == AI_KEY and s.streamdeck_ha_token == HA_TOKEN
            and s.streamdeck_cameras == CAMERAS)


def _cut_off_applied(old) -> bool:
    s = old.settings
    text = json.dumps(s.streamdeck_cameras)
    return (s.gemini_api_key == "" and s.spoonacular_api_key == ""
            and s.streamdeck_ha_token == "" and "reolink-fake-pw" not in text
            and "user:pw@" not in text and len(s.streamdeck_cameras) == len(CAMERAS))


def _relay_pull(client, key, device):
    r = client.get("/api/config/satellite", headers={
        "X-API-Key": key, "X-Device-Id": device, "X-Device-Mode": "pi_remote",
        "X-Device-Version": "0.21.0", "X-PR-Caps": "relay1", "X-Config-Nonce": "n"})
    assert r.status_code == 200
    assert "fleet_relay" in r.json()["config"]


def _seed_puller(key, device):
    from app.services import credentials
    rec = credentials.lookup(key)
    credentials.mark_caps(rec["id"], [], device_id=device, now=time.time() - 2 * 86400)


def test_a_old_bandit_under_auto_keeps_everything(server):
    old = old_satellite(EXTRA, "bandit-1", server)
    out = old.sync()
    assert out["ok"] is True, out
    # It verified the signature: the credential fields were applied.
    assert "gemini_api_key" in out["applied"]
    assert _legacy_payload_applied(old)
    assert old.settings.grocy_api_key == ""  # never sent since 0.20.2


def test_b_owner_turned_older_bandits_off(server, monkeypatch):
    monkeypatch.setattr(settings, "satellite_legacy_credentials", "off")
    old = old_satellite(EXTRA, "bandit-1", server)
    old.settings.gemini_api_key = "held-before"
    old.settings.streamdeck_ha_token = "held-before"
    out = old.sync()
    assert out["ok"] is True, out
    assert _cut_off_applied(old)


def test_c1_same_bandit_flashed_back_is_cut_off(server):
    _seed_puller(EXTRA, "bandit-1")
    _relay_pull(server, EXTRA, "bandit-1")
    old = old_satellite(EXTRA, "bandit-1", server)
    out = old.sync()
    assert out["ok"] is True, out
    assert _cut_off_applied(old)


def test_c2_second_bandit_sharing_the_key_keeps_its_payload(server):
    _seed_puller(EXTRA, "bandit-1")
    _relay_pull(server, EXTRA, "bandit-2")
    old = old_satellite(EXTRA, "bandit-1", server)
    out = old.sync()
    assert out["ok"] is True, out
    assert _legacy_payload_applied(old)


def test_c3_flashed_back_bandit_gets_its_keys_once_the_owner_allows_it(server, monkeypatch):
    """Let this key receive service keys again, in Settings, Security
    (FoodAssistant-xym8y), undoes the cut-off for that one key."""
    from app.services import credentials
    # The owner's side: a set-up kitchen, so Settings answers instead of the wizard.
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True, raising=False)
    _seed_puller(EXTRA, "bandit-1")
    _relay_pull(server, EXTRA, "bandit-1")
    old = old_satellite(EXTRA, "bandit-1", server)
    assert old.sync()["ok"] is True
    assert _cut_off_applied(old)
    cid = credentials.lookup(EXTRA)["id"]
    keys = {c["id"]: c for c in server.get("/api/credentials").json()["credentials"]}
    assert keys[cid]["service_keys_withheld"] is True
    r = server.post(f"/api/credentials/{cid}/share-service-keys")
    assert r.status_code == 200 and r.json()["ok"] is True
    out = old.sync()
    assert out["ok"] is True, out
    assert _legacy_payload_applied(old)
    keys = {c["id"]: c for c in server.get("/api/credentials").json()["credentials"]}
    assert keys[cid]["service_keys_withheld"] is False
    # Updated again, the Bandit relays and its key stops receiving them.
    _relay_pull(server, EXTRA, "bandit-1")
    assert credentials.relay_ratcheted(cid)


def test_d_primary_key_is_never_cut_off(server):
    _seed_puller(PRIMARY, "bandit-1")
    _relay_pull(server, PRIMARY, "bandit-1")
    old = old_satellite(PRIMARY, "bandit-1", server)
    out = old.sync()
    assert out["ok"] is True, out
    assert _legacy_payload_applied(old)


def test_old_bandit_wrong_key_still_reads_the_old_text(server):
    old = old_satellite("not-a-key", "bandit-1", server)
    out = old.sync()
    assert out["ok"] is False
    assert out["error"] == "server 401: Invalid API key."


# --- this release's Bandit on a v0.20.0 server -----------------------------------------


LOCK_KEY = {"id": "front-lock", "slot": 2, "type": "ha_action", "label": "Unlock",
            "entity_id": "lock.front_door", "service": "lock.unlock"}
SLOTS = [{"entity_id": "light.kitchen", "service": "light.toggle", "label": "Kitchen"}]


def _old_server_reply(nonce: str, key: str) -> dict:
    """What a v0.20.0 server answers: its field list, signed its way."""
    old_sign = old_satellite(key, "x", None).ns["config_signature"]
    cfg = {f: getattr(settings, f, "") for f in OLD_FIELDS}
    cfg.update(gemini_api_key=AI_KEY, grocy_api_key="server-grocy-key",
               streamdeck_ha_base_url="http://192.168.1.40:8123",
               streamdeck_ha_token=HA_TOKEN, streamdeck_key_overrides=[dict(LOCK_KEY)],
               streamdeck_ha_slots=[dict(s) for s in SLOTS])
    cfg = json.loads(json.dumps(cfg, default=str))
    return {"ok": True, "config": cfg, "config_nonce": nonce,
            "config_signature": old_sign(key, nonce, cfg),
            "server_hostname": "old-server", "server_version": "0.20.0",
            "expiry_defaults": [], "streamdeck_profiles": [], "gadget_alarms": [],
            "command": None}


class _Reply:
    def __init__(self, body):
        self.status_code = 200
        self._body = body
        self.text = ""

    def json(self):
        return self._body


@pytest.fixture
def new_bandit(monkeypatch, tmp_path):
    from app.services import satellite as sat
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    monkeypatch.setattr(settings, "remote_server_url", SERVER_URL)
    monkeypatch.setattr(settings, "upstream_api_key", EXTRA)
    monkeypatch.setattr(settings, "device_id", "bandit-new")
    monkeypatch.setattr(settings, "auth_password", "")
    monkeypatch.setattr(settings, "has_streamdeck", False)
    monkeypatch.setattr(settings, "fleet_relay", {})
    monkeypatch.setattr(settings, "upstream_ha_policy_version", 0)
    monkeypatch.setattr(settings, "gemini_api_key", "")
    for name in ("_apply_defaults", "_apply_profiles", "_push_streamdeck_settings",
                 "_push_timezone", "_push_update_channel"):
        monkeypatch.setattr(sat, name, _noop)
    monkeypatch.setattr(sat, "_resolve_host", lambda host: "")
    monkeypatch.setattr(sat, "_device_trust", lambda: "none")
    monkeypatch.setattr(sat.httpx, "get", lambda url, headers=None, timeout=None, **kw: _Reply(
        _old_server_reply(headers["X-Config-Nonce"], EXTRA)))
    out = sat.sync_from_upstream()
    assert out["ok"] is True, out
    return tmp_path


@pytest.fixture
def ha_calls(monkeypatch):
    from app.services import ha_client
    calls = []

    def _handler(request):
        calls.append((request.method, request.url.path, json.loads(request.content or b"{}")))
        return httpx.Response(200, json=[])

    monkeypatch.setattr(ha_client, "_direct_client",
                        lambda timeout=6.0: httpx.AsyncClient(
                            transport=httpx.MockTransport(_handler)))
    return calls


def test_e_new_bandit_on_old_server_stays_on_its_own_keys(new_bandit, ha_calls):
    from app.services import deck_config, start_actions
    assert settings.fleet_relay == {}
    assert settings.upstream_ha_policy_version == 0
    # Nothing was scrubbed: the AI key and the token arrived and stay.
    assert settings.gemini_api_key == AI_KEY
    assert settings.streamdeck_ha_token == HA_TOKEN
    assert settings.streamdeck_key_overrides == [LOCK_KEY]
    # The Grocy key is still never taken.
    assert settings.grocy_api_key != "server-grocy-key"
    # The lock key still works from the screen: the old server does not enforce
    # the Home Assistant opt-in, so this Bandit does not either.
    result = asyncio.run(start_actions.fire_key("front-lock", source="screen"))
    assert result["ok"] is True, result
    assert ha_calls == [("POST", "/api/services/lock/unlock", {"entity_id": "lock.front_door"})]
    merged, form = deck_config.merge_deck_config({}, deck_config.deck_inputs())
    assert form == "legacy"
    assert merged["ha_policy_version"] == 0
    assert merged["key_overrides"] == [LOCK_KEY]
    assert merged["ha_slots"] == SLOTS


def test_e_the_decks_proxy_form_keeps_the_lock_key(new_bandit, ha_calls, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.services import bridge
    (new_bandit / "bridge-token").write_text(VECTORS["token"] + "\n")
    bridge.invalidate_bridge_token()
    deck_key = VECTORS["agent_keys"]["streamdeck"]
    local = TestClient(app, client=("127.0.0.1", 50000), base_url="http://localhost")
    try:
        r = local.post("/api/proxy/ha/api/services/lock/unlock",
                       json={"entity_id": "lock.front_door"},
                       headers={"Authorization": f"Bearer {deck_key}"})
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True}
        assert ha_calls == [("POST", "/api/services/lock/unlock",
                             {"entity_id": "lock.front_door"})]
        # Once the server enforces the opt-in, the same request is refused,
        # so the request above went through the policy, not around it.
        monkeypatch.setattr(settings, "upstream_ha_policy_version", 1)
        r = local.post("/api/proxy/ha/api/services/lock/unlock",
                       json={"entity_id": "lock.front_door"},
                       headers={"Authorization": f"Bearer {deck_key}"})
        assert r.status_code == 403
        assert r.json()["code"] == "ha_not_allowed"
        assert len(ha_calls) == 1
    finally:
        bridge.invalidate_bridge_token()
