"""A same-machine nginx 1.30 left on its defaults (FoodAssistant-wj8cs).

From 1.30 nginx talks HTTP/1.1 to the app and, on its defaults, sends the
visitor's request with Host rewritten to the upstream address and no
forwarding header. Measured against nginx 1.30.5: the request the app sees is
byte for byte what a program on the same machine sends. So on a server with
none of the device's own programs (kitchen screen, Stream Deck, sensor
reader) the old trust for loopback never applies, and such a request is an
ordinary visitor that has to sign in.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.passwords import hash_secret
from app.services import access, auth_state, local_agents

SERVICE = Path(__file__).resolve().parents[1] / "service"
T0 = 1_800_000_000.0
DAY = 86400.0

# What nginx 1.30.5 forwards for a bare `curl http://proxy/x` and for a
# browser's first page (headers copied from a capture of the upstream side).
NGINX_BARE = {"User-Agent": "curl/8.5.0", "Accept": "*/*"}
NGINX_FIRST_PAGE = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
                    "Accept": "text/html", "Sec-Fetch-Site": "none",
                    "Sec-Fetch-Mode": "navigate", "Upgrade-Insecure-Requests": "1"}


@pytest.fixture
def server(tmp_path, monkeypatch):
    """An upgraded server install with a password (legacy trust carried over)."""
    monkeypatch.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret("hunter2"), raising=False)
    monkeypatch.setattr(settings, "api_key", "", raising=False)
    monkeypatch.setattr(settings, "extra_api_keys", [], raising=False)
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True, raising=False)
    assert auth_state.loopback_trust()["state"] == "legacy"
    return tmp_path


def _nginx(port=9284):
    """A client shaped like nginx on the same machine: loopback peer, Host
    naming the upstream address, HTTP/1.1."""
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app, client=("127.0.0.1", 50000),
                      base_url=f"http://127.0.0.1:{port}")


@pytest.mark.parametrize("headers", [NGINX_BARE, NGINX_FIRST_PAGE])
def test_a_bare_request_through_nginx_gets_no_trust_on_a_server(server, headers):
    c = _nginx()
    assert c.get("/setup", headers=headers, follow_redirects=False).status_code == 401
    assert c.get("/pending/count", headers=headers).status_code == 401
    r = c.get("/ui/", headers=headers, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/ui/login")
    assert not access.legacy_trust_applies()
    assert auth_state.load()["legacy_use"] == {}


def test_any_loopback_port_is_the_same(server):
    assert _nginx(19700).get("/pending/count", headers=NGINX_BARE).status_code == 401
    from fastapi.testclient import TestClient
    from app.main import app
    local = TestClient(app, client=("::1", 50000), base_url="http://localhost")
    assert local.get("/pending/count").status_code == 401


def test_a_pi_appliance_keeps_the_old_trust(server, monkeypatch):
    """The kitchen screen, Stream Deck and reader on a Pi may still need it
    until they prove themselves; the hourly rule ends it there."""
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted", raising=False)
    assert _nginx().get("/pending/count", headers=NGINX_BARE).status_code == 200


@pytest.mark.parametrize("program", ["kiosk", "streamdeck", "gadgets"])
def test_a_server_with_one_of_the_programs_keeps_it(server, program):
    local_agents._state["hw"] = {program: {"installed": True}}
    assert _nginx().get("/pending/count", headers=NGINX_BARE).status_code == 200


def test_an_owner_who_trusts_again_is_honored(server):
    auth_state.retrust(7)
    assert access.legacy_trust_applies()
    assert _nginx().get("/pending/count", headers=NGINX_BARE).status_code == 200


def test_device_credentials_still_work_on_a_server(server, bridge_token, kiosk_login):
    """The real callers on loopback carry their own proof and are unaffected."""
    c = _nginx()
    deck = local_agents.agent_key("streamdeck")
    assert c.get("/pending/count", headers={"X-API-Key": deck,
                                            "X-PR-Client": "streamdeck/0.21"}).status_code == 200
    from fastapi.testclient import TestClient
    from app.main import app
    screen = TestClient(app, client=("127.0.0.1", 50000), base_url="http://localhost")
    kiosk_login(screen)
    assert screen.get("/pending/count").status_code == 200


def test_no_password_still_answers_this_device(server, monkeypatch):
    """Without a password everything at home is open; the old trust is not
    what serves it, so nothing changes there."""
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    local = TestClient(app, client=("127.0.0.1", 50000), base_url="http://localhost")
    assert local.get("/pending/count").status_code == 200


# --- The hourly rule ------------------------------------------------------------


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    auth_state.reset(auth_state.test_state(now=T0), persist=False)


def test_a_server_without_a_bridge_ends_the_trust_at_once(legacy, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    assert local_agents.evaluate_retirement(T0 + 300, None) == "retired"
    assert auth_state.loopback_trust()["by"] == "auto"
    assert not auth_state.legacy_trust_active(now=T0 + 400)


def test_a_server_whose_bridge_reports_no_programs_ends_it(legacy, monkeypatch, bridge_token):
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    hw = {"kiosk": {"installed": False}, "streamdeck": {"installed": False},
          "gadgets": {"installed": False}}
    assert local_agents.evaluate_retirement(T0 + 300, hw) == "retired"


def test_a_silent_bridge_on_a_server_waits_for_its_answer(legacy, monkeypatch, bridge_token):
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    assert local_agents.evaluate_retirement(T0 + 300, None) == "kept"
    assert auth_state.loopback_trust()["state"] == "legacy"
    # In the meantime requests are not served on it.
    assert not access.legacy_trust_applies()


def test_a_pi_without_a_bridge_answer_is_watched_as_before(legacy, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted", raising=False)
    assert local_agents.evaluate_retirement(T0 + 300, None) == "kept"
    assert auth_state.legacy_trust_active(now=T0 + 400)


@pytest.mark.parametrize("mode,hw,expected", [
    ("pi_hosted", None, True),
    ("pi_remote", None, True),
    ("server", None, False),
    ("", None, False),
    ("server", {"kiosk": {"installed": False}}, False),
    ("server", {"kiosk": {"installed": True}}, True),
    ("server", {"gadgets": {"installed": True}}, True),
    ("server", {"streamdeck": "broken"}, False),
])
def test_own_programs_possible(monkeypatch, mode, hw, expected):
    monkeypatch.setattr(settings, "deployment_mode", mode, raising=False)
    assert local_agents.own_programs_possible(hw) is expected


@pytest.mark.parametrize("mode,asked", [("server", True), ("pi_hosted", False)])
def test_a_server_with_a_bridge_asks_which_programs_it_has_at_start(
        server, bridge_token, monkeypatch, mode, asked):
    """So a server with a kitchen screen keeps the old trust from its first
    request instead of after the first hourly pass."""
    import asyncio
    from app import main
    monkeypatch.setattr(settings, "deployment_mode", mode, raising=False)
    calls = []

    async def status():
        calls.append(1)
        local_agents._state["hw"] = {"kiosk": {"installed": True}}
        return local_agents._state["hw"]

    monkeypatch.setattr(local_agents, "bridge_hardware_status", status)

    async def go():
        await main._access_startup()
        for _ in range(3):
            await asyncio.sleep(0)

    asyncio.run(go())
    assert bool(calls) is asked
    if asked:
        assert access.legacy_trust_applies()
