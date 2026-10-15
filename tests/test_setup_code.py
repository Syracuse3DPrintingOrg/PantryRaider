"""The setup code (services/setup_code.py, /ui/setup-code, D2 step 4)."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.config import settings
from app.passwords import hash_secret
from app.services import access, auth_state, setup_code
from page_scripts import template_source, with_page_scripts

SERVICE = Path(__file__).resolve().parents[1] / "service"
PROXIED = {"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Host": "pantry.example.com"}


@pytest.fixture
def items(monkeypatch):
    seen = []
    monkeypatch.setattr(access, "raise_item",
                        lambda kind, title, body, dedupe, level="info":
                        seen.append({"kind": kind, "body": body}))
    return seen


@pytest.fixture
def base(tmp_path, monkeypatch):
    monkeypatch.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "viewer_password", "", raising=False)
    monkeypatch.setattr(settings, "local_totp_enabled", False, raising=False)
    monkeypatch.setattr(settings, "local_totp_secret", "", raising=False)
    monkeypatch.setattr(settings, "totp_secret", "", raising=False)
    from app.services import readiness
    monkeypatch.setattr(readiness, "gate_possible", lambda: False)
    return tmp_path


@pytest.fixture
def configured(base, monkeypatch):
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True, raising=False)
    return base


@pytest.fixture
def unconfigured(base, monkeypatch):
    monkeypatch.setattr(type(settings), "is_configured",
                        lambda self: bool(self.auth_password) and False, raising=False)
    return base


def _client(headers=None, peer=("192.168.1.50", 50000)):
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app, client=peer, headers=headers or {})


# --- The service --------------------------------------------------------------------

def test_the_code_is_printed_once_and_never_logged(capsys, caplog):
    caplog.set_level(logging.DEBUG)
    code = setup_code.ensure("test", now=1000.0)
    assert setup_code.ensure("again", now=2000.0) == code
    out = capsys.readouterr().out
    assert out.count(code) == 1
    assert out.startswith("Pantry Raider setup code: ")
    assert code not in caplog.text
    assert len(code) >= 32


def test_codes_expire_after_a_day():
    code = setup_code.ensure("test", now=1000.0)
    assert setup_code.current(now=1000.0 + 86399) == code
    assert setup_code.current(now=1000.0 + 86401) is None
    assert setup_code.verify(code, "k", now=1000.0 + 86401) == "wrong"
    assert setup_code.ensure("new", now=1000.0 + 86401) != code


def test_the_file_is_private(tmp_path, monkeypatch):
    import os
    import stat
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    setup_code.reset(persist=True)
    setup_code.ensure("test")
    path = tmp_path / setup_code.FILENAME
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    setup_code.clear()
    assert not path.exists()


def test_wrong_codes_are_limited_per_address():
    code = setup_code.ensure("test", now=1000.0)
    for i in range(5):
        assert setup_code.verify("nope", "203.0.113.9", now=1000.0 + i) == "wrong"
    assert setup_code.verify(code, "203.0.113.9", now=1010.0) == "slow_down"
    assert setup_code.verify(code, "198.51.100.7", now=1010.0) == "ok"
    assert setup_code.verify(code, "203.0.113.9", now=1070.0) == "ok"


def test_a_flood_from_made_up_addresses_blocks_nobody():
    code = setup_code.ensure("test", now=1000.0)
    for i in range(10_000):
        setup_code.verify("nope", f"10.{i // 65536}.{(i // 256) % 256}.{i % 256}",
                          now=1000.0 + i * 0.005)
    assert setup_code.limiter.size() <= 1024
    assert setup_code.verify(code, "192.0.2.77", now=1055.0) == "ok"


def test_the_limiter_key_follows_the_forwarded_address():
    from starlette.requests import Request

    def req(peer, headers):
        raw = [(b"host", b"pantry.example.com")] + [(k.encode(), v.encode()) for k, v in headers]
        return Request({"type": "http", "method": "POST", "path": "/ui/setup-code",
                        "headers": raw, "client": (peer, 1), "query_string": b"",
                        "http_version": "1.1", "scheme": "http", "server": ("x", 80)})

    assert setup_code.limiter_key(req("10.0.0.2", [("x-forwarded-for", "203.0.113.9, 10.0.0.1")])) == "203.0.113.9"
    assert setup_code.limiter_key(req("10.0.0.2", [("x-forwarded-for", "garbage")])) == "10.0.0.2"
    assert setup_code.limiter_key(req("192.168.1.5", [])) == "192.168.1.5"


# --- Unconfigured installs ---------------------------------------------------------

def test_unconfigured_from_outside_gets_the_setup_code(unconfigured, capsys):
    c = _client(PROXIED)
    r = c.get("/ui/", headers={"Accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 403 and "Enter the setup code" in r.text
    r = c.get("/timers")
    assert r.status_code == 403
    assert r.json() == {"detail": access.SETUP_CODE_MSG, "code": "setup_code_required"}
    assert "Pantry Raider setup code:" in capsys.readouterr().out


def test_unconfigured_from_home_runs_the_wizard(unconfigured, ingress_client):
    assert _client().get("/setup", follow_redirects=False).status_code == 200
    assert ingress_client.get("/setup", follow_redirects=False).status_code == 200


def test_setup_from_outside_sets_the_password(unconfigured, monkeypatch):
    c = _client(PROXIED)
    assert c.get("/ui/setup-code").status_code == 200
    code = setup_code.current()
    r = c.post("/ui/setup-code", data={"code": code, "password": "short", "confirm": "short"},
               follow_redirects=False)
    assert r.status_code == 400 and access.PASSWORD_SHORT_MSG in r.text
    r = c.post("/ui/setup-code", data={"code": code, "password": "longpassword1",
                                       "confirm": "different123"}, follow_redirects=False)
    assert r.status_code == 400
    r = c.post("/ui/setup-code", data={"code": code, "password": "longpassword1",
                                       "confirm": "longpassword1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    assert settings.auth_password and settings.auth_required is True
    assert setup_code.current() is None
    assert c.get("/setup", follow_redirects=False).status_code == 200


def test_an_environment_password_takes_the_code_alone(unconfigured, monkeypatch):
    monkeypatch.setattr(settings, "auth_password", hash_secret("from-env-pass"), raising=False)
    c = _client(PROXIED)
    page = c.get("/ui/setup-code")
    assert access.SETUP_CODE_INTRO_HAS_PASSWORD in page.text
    r = c.post("/ui/setup-code", data={"code": setup_code.current()}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"


def test_a_wrong_code(unconfigured):
    c = _client(PROXIED)
    c.get("/ui/setup-code")
    r = c.post("/ui/setup-code", data={"code": "nope", "password": "longpassword1",
                                       "confirm": "longpassword1"}, follow_redirects=False)
    assert r.status_code == 401 and "did not match" in r.text


# --- Configured, no password ------------------------------------------------------

def test_open_install_through_a_proxy_lands_on_two_factor(configured, grace_over):
    c = _client(PROXIED)
    assert c.get("/timers").status_code == 403
    page = c.get("/ui/setup-code")
    assert page.status_code == 200 and access.SETUP_CODE_INTRO_OPEN in page.text
    r = c.post("/ui/setup-code", data={"code": setup_code.current(), "password": "longpassword1",
                                       "confirm": "longpassword1"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/setup?twofactor=needed#pane-security"
    assert c.get("/timers").status_code == 200


def test_the_open_form_warns_about_keyless_devices_without_naming_them(configured, items):
    auth_state.record_keyless_ha("192.168.1.20")
    auth_state.record_keyless_family("display", "esphome", "192.168.1.30")
    c = _client(PROXIED)
    page = c.get("/ui/setup-code").text
    note = access.KEYLESS_SETUP_CODE_NOTE
    assert note in page
    assert page.index(note) < page.index('name="password"')
    assert "Bandit Cubs" not in page and "Home Assistant" not in page
    c.post("/ui/setup-code", data={"code": setup_code.current(), "password": "longpassword1",
                                   "confirm": "longpassword1"}, follow_redirects=False)
    got = [i for i in items if i["kind"] == "keyless_devices_need_key"]
    assert len(got) == 1
    assert "Home Assistant" in got[0]["body"] and "Bandit Cubs and small displays" in got[0]["body"]


def test_no_keyless_note_without_keyless_devices(configured, items):
    c = _client(PROXIED)
    assert access.KEYLESS_SETUP_CODE_NOTE not in c.get("/ui/setup-code").text
    c.post("/ui/setup-code", data={"code": setup_code.current(), "password": "longpassword1",
                                   "confirm": "longpassword1"}, follow_redirects=False)
    assert not [i for i in items if i["kind"] == "keyless_devices_need_key"]


# --- Rescue: a password, no two-factor, only reachable from outside ----------------

@pytest.fixture
def rescue(configured, grace_over, monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret("env-password-1"), raising=False)
    monkeypatch.setattr(settings, "viewer_password", hash_secret("kitchen-pass"), raising=False)
    settings.model_fields_set.add("auth_password")
    yield
    settings.model_fields_set.discard("auth_password")


def test_a_password_alone_is_refused_from_outside(rescue):
    r = _client(PROXIED).post("/ui/login", data={"password": "env-password-1"},
                              follow_redirects=False)
    assert r.status_code == 401 and "second factor" in r.text


def test_the_rescue_signs_in_once_and_goes_to_two_factor(rescue):
    c = _client(PROXIED)
    page = c.get("/ui/setup-code")
    assert page.status_code == 200 and access.SETUP_CODE_INTRO_RESCUE in page.text
    assert 'autocomplete="current-password"' in page.text
    r = c.post("/ui/setup-code", data={"code": setup_code.current(), "password": "env-password-1"},
               follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/setup?twofactor=needed#pane-security"
    assert c.get("/setup", follow_redirects=False).status_code == 200


def test_a_wrong_rescue_password_is_counted_and_keeps_the_code(rescue):
    c = _client(PROXIED)
    c.get("/ui/setup-code")
    code = setup_code.current()
    r = c.post("/ui/setup-code", data={"code": code, "password": "wrong"}, follow_redirects=False)
    assert r.status_code == 401 and access.RESCUE_PASSWORD_WRONG_MSG.replace("'", "&#39;") in r.text
    assert setup_code.current() == code
    r = c.post("/ui/setup-code", data={"code": code, "password": "kitchen-pass"},
               follow_redirects=False)
    assert r.status_code == 401
    assert c.get("/timers").status_code == 401
    for _ in range(3):
        c.post("/ui/setup-code", data={"code": code, "password": "wrong"})
    r = c.post("/ui/setup-code", data={"code": code, "password": "env-password-1"},
               follow_redirects=False)
    assert r.status_code == 429


def test_rescue_is_only_for_outside_without_two_factor(rescue, monkeypatch):
    r = _client().get("/ui/setup-code", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/ui/login")
    monkeypatch.setattr(settings, "local_totp_enabled", True, raising=False)
    r = _client(PROXIED).get("/ui/setup-code", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/ui/login")
    r = _client(PROXIED).post("/ui/setup-code", data={"code": "x"}, follow_redirects=False)
    assert r.status_code == 303


def test_the_start_clears_the_code_once_a_password_exists(configured, monkeypatch):
    import asyncio
    from app import main
    setup_code.ensure("test")
    monkeypatch.setattr(settings, "auth_password", hash_secret("x" * 10), raising=False)
    asyncio.run(main._access_startup())
    assert setup_code.current() is None


def test_a_restart_prints_the_unconfigured_code_again(unconfigured, capsys):
    """A recreated container starts with an empty log, so a code that is
    still valid is printed again at start (and not replaced)."""
    import asyncio
    from app import main
    asyncio.run(main._access_startup())
    first = setup_code.current()
    assert first and capsys.readouterr().out.count(first) == 1
    asyncio.run(main._access_startup())
    assert setup_code.current() == first
    assert capsys.readouterr().out.count(first) == 1


# --- The sign-in page ------------------------------------------------------------

def test_the_sign_in_page_carries_the_kiosk_self_heal(configured, monkeypatch):
    monkeypatch.setattr(settings, "auth_password", hash_secret("hunter2"), raising=False)
    page = with_page_scripts(_client().get("/ui/login").text)
    assert "ui/kiosk/relaunch" in page and access.RELAUNCH_NOTE in page
    assert access.SETUP_CODE_LINK not in page


def test_the_self_heal_runs_on_the_devices_bare_host_name():
    """A screen that opens the device by its bare host name counts as the
    device itself (I-5), so it heals itself the same way as on localhost."""
    import re
    import shutil
    import subprocess
    tpl = template_source("login.html")
    m = re.search(r"var local = (.*?);\n", tpl, re.S)
    assert m, "the self-heal's local check is missing"
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    script = ("const check = (h) => (" + m.group(1) + ");\n"
              "const out = {};\n"
              "for (const h of ['localhost', '127.0.0.1', '[::1]', 'pr', 'kitchen-pi',"
              " 'pr.local', 'pantry.example.com', '192.168.1.5', '[fd00::1]'])"
              " out[h] = check(h);\n"
              "console.log(JSON.stringify(out));")
    script = script.replace("location.hostname", "h")
    import json
    res = json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True,
                                    timeout=30).stdout)
    assert res == {"localhost": True, "127.0.0.1": True, "[::1]": True, "pr": True,
                   "kitchen-pi": True, "pr.local": False, "pantry.example.com": False,
                   "192.168.1.5": False, "[fd00::1]": False}


def test_the_sign_in_page_can_offer_the_setup_code(configured):
    from starlette.requests import Request
    from app.routers.ui import _render_login
    req = Request({"type": "http", "method": "GET", "path": "/ui/login",
                   "headers": [(b"host", b"testserver")], "client": ("192.168.1.5", 1),
                   "query_string": b"", "http_version": "1.1", "scheme": "http",
                   "server": ("testserver", 80), "root_path": ""})
    html = _render_login(req, setup_code_link=True).body.decode()
    assert 'href="ui/setup-code"' in html and access.SETUP_CODE_LINK in html


def test_the_refusal_page_order(configured, grace_over):
    page = _client(PROXIED).get("/ui/", headers={"Accept": "text/html"}).text
    order = [page.index("This kitchen has no password"),
             page.index('action="ui/setup-code"'),
             page.index("At home, open this kitchen at"),
             page.index("LAN_CIDRS")]
    assert order == sorted(order)
    assert access.RESCUE_2FA in page
