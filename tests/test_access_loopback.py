"""The old trust for programs on this device (legacy loopback) with a password."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.passwords import hash_secret
from app.services import access, auth_state, credentials, local_agents

SERVICE = Path(__file__).resolve().parents[1] / "service"


@pytest.fixture
def items(monkeypatch):
    seen = []
    monkeypatch.setattr(access, "raise_item",
                        lambda kind, title, body, dedupe, level="info":
                        seen.append({"kind": kind, "dedupe": dedupe, "body": body}))
    return seen


@pytest.fixture
def install(tmp_path, monkeypatch):
    monkeypatch.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret("hunter2"), raising=False)
    monkeypatch.setattr(settings, "api_key", "older-key", raising=False)
    monkeypatch.setattr(settings, "extra_api_keys", [], raising=False)
    monkeypatch.setattr(settings, "legacy_mirror", False, raising=False)
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True, raising=False)
    return tmp_path


def _client(peer="127.0.0.1", base_url="http://localhost"):
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app, client=(peer, 50000), base_url=base_url)


def test_plain_loopback_reaches_settings_while_trust_lasts(install):
    assert _client().get("/setup/security/state").status_code == 200
    assert _client(base_url="http://127.0.0.1:9284").get("/setup/security/state").status_code == 200


@pytest.mark.parametrize("kwargs,headers", [
    ({}, {"X-Forwarded-For": "203.0.113.9"}),
    ({"base_url": "http://evil.example"}, {}),
    ({"peer": "::ffff:127.0.0.1"}, {"X-Forwarded-For": "203.0.113.9"}),
])
def test_proxies_and_rebinding_get_no_trust(install, kwargs, headers):
    assert _client(**kwargs).get("/setup/security/state", headers=headers).status_code == 401


def test_a_cross_site_write_gets_no_trust(install):
    r = _client().post("/setup/security/kiosk-signout", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code in (401, 403)
    # The cross-site check in front of the auth middleware passes 'none', so
    # this one shows the access rule itself: only a same-origin write keeps
    # the trust.
    gen = auth_state.kiosk_gen()
    r = _client().post("/setup/security/kiosk-signout", headers={"Sec-Fetch-Site": "none"})
    assert r.status_code == 401
    assert auth_state.kiosk_gen() == gen
    r = _client().post("/setup/security/kiosk-signout", headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200


def test_retire_and_retrust(install):
    auth_state.retire("admin")
    assert _client().get("/setup/security/state").status_code == 401
    auth_state.retrust(7)
    assert _client().get("/setup/security/state").status_code == 200


def test_an_unknown_key_on_loopback_is_ignored_while_trust_lasts(install):
    c = _client()
    assert c.get("/timers", headers={"X-API-Key": "stale-key"}).status_code == 200
    assert c.get("/timers", headers={"X-API-Key": "pra1.streamdeck." + "0" * 64,
                                     "User-Agent": "python-httpx/0.27"}).status_code == 200
    uses = auth_state.load()["legacy_use"]
    assert uses["deck"]["count"] >= 1
    auth_state.retire("admin")
    assert c.get("/timers", headers={"X-API-Key": "stale-key"}).status_code == 401


def test_keyless_health_is_never_recorded(install):
    _client().get("/health")
    assert auth_state.load()["legacy_use"] == {}


@pytest.mark.parametrize("kind", ["legacy", "ha"])
def test_a_key_on_settings_from_this_device_works_while_trust_lasts(install, items, kind):
    if kind == "legacy":
        key = "older-key"
        cid = credentials.legacy_id(key)
    else:
        rec, key = credentials.mint("ha", "Script", "manual")
        cid = rec["id"]
    c = _client()
    h = {"X-API-Key": key, "X-PR-Client": "my-script/1"}
    assert c.post("/setup/security/kiosk-signout", headers=h).status_code == 200
    assert c.post("/setup/security/kiosk-signout", headers=h).status_code == 200
    got = [i for i in items if i["kind"] == "key_admin_loopback"]
    assert len({i["dedupe"] for i in got}) == 1
    assert got[0]["dedupe"].startswith(f"key_admin_loopback:{cid}:")
    assert "my-script/1" in got[0]["body"]
    auth_state.retire("admin")
    r = c.post("/setup/security/kiosk-signout", headers=h)
    assert r.status_code == 403 and r.json()["detail"] == access.ADMIN_KEY_MSG
    assert any(i["kind"] == "key_admin_refused" for i in items)


def test_a_key_on_settings_from_the_lan_is_refused(install, items):
    from fastapi.testclient import TestClient
    from app.main import app
    for _ in range(3):
        r = TestClient(app).post("/setup/security/kiosk-signout", headers={"X-API-Key": "older-key"})
        assert r.status_code == 403 and r.json()["detail"] == access.ADMIN_KEY_MSG
    # A script that repeats the call raises the day's item once, not per request.
    assert [i["kind"] for i in items] == ["key_admin_refused"]
    assert credentials.get(credentials.legacy_id("older-key"))["refusals"]


def test_a_store_page_return_counts_as_a_same_host_proxy_use(install, tmp_path):
    c = _client()
    h = {"Referer": "https://www.amazon.com/", "Sec-Fetch-Site": "cross-site",
         "User-Agent": "Mozilla/5.0"}
    assert c.get("/setup/security/state", headers=h).status_code == 200
    uses = auth_state.load()["legacy_use"]
    assert uses["same_host_proxy"]["count"] == 1
    assert "browser" not in uses
    # It does not hold the trust open (rule b), with no bridge on this box,
    # once the week of watching has passed.
    import time
    assert local_agents.evaluate_retirement(time.time() + 8 * 86400, None) == "retired"


# --- Start-up and the hourly housekeeping (D6) -----------------------------------

def _stub_module(monkeypatch, name, **attrs):
    import sys
    import types
    import app.services as services_pkg
    mod = types.ModuleType(f"app.services.{name}")
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, f"app.services.{name}", mod)
    monkeypatch.setattr(services_pkg, name, mod, raising=False)
    return mod


@pytest.mark.parametrize("mode", ["server", "pi_remote"])
def test_startup_runs_the_later_zones_hooks(install, monkeypatch, mode):
    import asyncio
    from app import main
    monkeypatch.setattr(settings, "deployment_mode", mode, raising=False)
    calls = []
    _stub_module(monkeypatch, "ha_policy_migration", migrate_once=lambda db: calls.append("migrate"))
    _stub_module(monkeypatch, "deck_config", scrub_ha_camera_token_urls=lambda: calls.append("scrub") or 0)
    import app.services.printing as printing

    async def _refresh():
        calls.append("print")
        raise RuntimeError("never surfaces")

    monkeypatch.setattr(printing, "refresh_print_service_and_notice", _refresh, raising=False)

    async def go():
        await main._access_startup()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(go())
    assert "migrate" in calls and "scrub" in calls
    assert ("print" in calls) is (mode == "server")


def test_startup_survives_missing_hooks(install, monkeypatch):
    import asyncio
    import sys
    from app import main
    for name in ("ha_policy_migration", "deck_config"):
        monkeypatch.setitem(sys.modules, f"app.services.{name}", None)
    asyncio.run(main._access_startup())


def test_housekeeping_steps_fail_independently(install, monkeypatch):
    import asyncio
    from app import main
    ran = []

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(local_agents, "evaluate_retirement", boom)
    monkeypatch.setattr(credentials, "retirement_pass", lambda now: ran.append("keys"))
    monkeypatch.setattr(local_agents, "check_kiosk_url", lambda hw: ran.append("kiosk"))

    async def deck():
        ran.append("deck")

    monkeypatch.setattr(local_agents, "refresh_deck_config_if_key_changed", deck)
    asyncio.run(main._access_housekeeping_pass())
    assert ran == ["kiosk", "keys", "deck"]


def test_the_deck_refresh_follows_the_push_record(install, monkeypatch, bridge_token):
    import asyncio
    pushes = []
    state = {"lp": None}
    _stub_module(monkeypatch, "deck_config", last_push=lambda: state["lp"],
                 push_to_deck=lambda: pushes.append(1) or True)
    asyncio.run(local_agents.refresh_deck_config_if_key_changed())
    assert pushes == []                                   # the deck is not proven
    auth_state.record_agent("streamdeck", caps={"navorigin1"})
    asyncio.run(local_agents.refresh_deck_config_if_key_changed())
    assert pushes == [1]
    import hashlib
    kh = hashlib.sha256(local_agents.agent_key("streamdeck").encode()).hexdigest()
    state["lp"] = {"form": "proxy", "key_sha256": kh}
    asyncio.run(local_agents.refresh_deck_config_if_key_changed())
    assert pushes == [1]
    state["lp"] = {"form": "proxy", "key_sha256": "old"}
    asyncio.run(local_agents.refresh_deck_config_if_key_changed())
    assert pushes == [1, 1]
