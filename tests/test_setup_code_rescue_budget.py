"""A setup code gives a limited number of password tries, wherever they come from.

From outside, a kitchen with a password but no two-factor takes the setup
code plus its password (the rescue). Wrong tries are limited per client
address, and behind a proxy that address is the one the proxy reports, which
a visitor can make up afresh on every try. So a setup code that leaked (the
log was shared, say) would have let someone guess the password without end.
Each code now allows a few wrong passwords in all; after that it is used up,
and opening the page again prints a new one to the log.
"""
from __future__ import annotations

import html
from pathlib import Path

import pytest

from app.config import settings
from app.passwords import hash_secret
from app.services import access, setup_code

SERVICE = Path(__file__).resolve().parents[1] / "service"


@pytest.fixture
def rescue(tmp_path, monkeypatch, grace_over):
    monkeypatch.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret("the-real-pass"), raising=False)
    monkeypatch.setattr(settings, "viewer_password", "", raising=False)
    monkeypatch.setattr(settings, "local_totp_enabled", False, raising=False)
    monkeypatch.setattr(settings, "local_totp_secret", "", raising=False)
    monkeypatch.setattr(settings, "totp_secret", "", raising=False)
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True, raising=False)
    from app.services import readiness
    monkeypatch.setattr(readiness, "gate_possible", lambda: False)


def _client(xff):
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app, client=("192.168.1.5", 50000),
                      headers={"X-Forwarded-For": xff, "X-Forwarded-Host": "pantry.example.com"})


def test_made_up_addresses_do_not_buy_more_password_tries(rescue):
    _client("203.0.113.1").get("/ui/setup-code")
    code = setup_code.current()
    assert code
    for i in range(setup_code.WRONG_PASSWORDS_PER_CODE):
        r = _client(f"198.51.100.{i + 1}").post(
            "/ui/setup-code", data={"code": code, "password": f"guess-{i}"},
            follow_redirects=False)
        assert r.status_code == 401
    # The code is used up: even the right password with it is refused now.
    assert setup_code.current() is None
    r = _client("192.0.2.77").post("/ui/setup-code",
                                   data={"code": code, "password": "the-real-pass"},
                                   follow_redirects=False)
    assert r.status_code == 401
    assert access.SETUP_CODE_WRONG_MSG in html.unescape(r.text)
    # Opening the page again prints a new code, which works.
    _client("203.0.113.1").get("/ui/setup-code")
    fresh = setup_code.current()
    assert fresh and fresh != code
    r = _client("203.0.113.1").post("/ui/setup-code",
                                    data={"code": fresh, "password": "the-real-pass"},
                                    follow_redirects=False)
    assert r.status_code == 303


def test_the_last_wrong_try_says_the_code_is_used_up(rescue):
    _client("203.0.113.1").get("/ui/setup-code")
    code = setup_code.current()
    last = None
    for i in range(setup_code.WRONG_PASSWORDS_PER_CODE):
        last = _client(f"198.51.100.{i + 1}").post(
            "/ui/setup-code", data={"code": code, "password": "nope"}, follow_redirects=False)
    assert access.RESCUE_CODE_USED_UP_MSG in html.unescape(last.text)


def test_a_few_typos_keep_the_code(rescue):
    c = _client("203.0.113.1")
    c.get("/ui/setup-code")
    code = setup_code.current()
    c.post("/ui/setup-code", data={"code": code, "password": "typo"}, follow_redirects=False)
    assert setup_code.current() == code
    r = c.post("/ui/setup-code", data={"code": code, "password": "the-real-pass"},
               follow_redirects=False)
    assert r.status_code == 303


def test_the_count_is_shared_between_workers(tmp_path, monkeypatch):
    """The tries are counted in the code's own file, so every worker sees them."""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    setup_code.reset(persist=True)
    try:
        code = setup_code.ensure("test", now=1000.0)
        assert setup_code.record_wrong_password(now=1001.0) is False
        on_disk = (tmp_path / setup_code.FILENAME).read_text()
        assert '"wrong_passwords": 1' in on_disk
        assert setup_code.current(now=1002.0) == code
    finally:
        setup_code.reset(persist=False)
