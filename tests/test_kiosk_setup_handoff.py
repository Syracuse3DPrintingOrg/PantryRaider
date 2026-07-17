"""The Pi's own display leaves the setup QR splash when setup finishes
elsewhere (FoodAssistant-6v9q).

Finishing the setup wizard from a phone used to leave the appliance's attached
display sitting on the QR splash until something external reloaded the browser.
Two independent breaks, either one fatal:

  * the completion signal was never written: the wizard fired POST
    setup/kiosk/navigate/request right AFTER the save that turns auth on, so
    the phone's now-sessionless request answered 401 and the flag file never
    appeared;
  * nothing on the kiosk read it anyway: the only poller lived in base.html,
    and the setup page is a standalone template that never had one.

The fix writes the flag server-side inside the very save that completes setup,
and puts a poller on the setup page itself (both the wizard splash and the
settings rendering). These tests pin the whole contract: the poll's answer
before setup, at the completion moment, and after; that the answer is JSON
(never the setup-redirect HTML) for the kiosk in every phase; that auth is not
weakened for real browsers; and that a routine settings save never yanks the
display home.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.passwords import hash_secret  # noqa: E402
from app.routers import setup as setup_router  # noqa: E402
from app.services import readiness  # noqa: E402

PENDING = "/setup/kiosk/navigate/pending"
REQUEST = "/setup/kiosk/navigate/request"


@pytest.fixture
def env(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    # The flag path is resolved at import time from the then-current data_dir,
    # so repoint it at this test's data_dir alongside the setting.
    flag = tmp_path / "kiosk_navigate.flag"
    monkeypatch.setattr(setup_router, "_KIOSK_NAV_FLAG", flag, raising=False)
    readiness.reset()
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield SimpleNamespace(
            # A LAN browser: host "testclient" is NOT loopback, so the loopback
            # trust never masks the middleware behavior under test.
            phone=TestClient(app),
            # The appliance's own display: the kiosk service loads
            # http://localhost/... under host networking, so it is a loopback
            # client of the app.
            kiosk=TestClient(app, client=("127.0.0.1", 40000)),
            flag=flag,
        )
    finally:
        readiness.reset()
        os.chdir(cwd)


def _unconfigured(monkeypatch, mode="server"):
    monkeypatch.setattr(settings, "deployment_mode", mode, raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)


def _configured(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret("hunter2"),
                        raising=False)


def _assert_json_pending(r, expected: bool):
    assert r.status_code == 200
    assert "application/json" in r.headers.get("content-type", "")
    assert r.json() == {"pending": expected}


# -- the poll's answer, phase by phase ----------------------------------------

def test_pending_answers_json_before_setup(env, monkeypatch):
    """Pre-setup the poll must reach the handler (it is in _SETUP_BYPASS) and
    answer plain JSON, not the setup-redirect HTML page, for the kiosk and for
    any LAN client alike. Nothing sensitive: a single boolean, always False
    before setup because the flag is only ever written by the completing save."""
    _unconfigured(monkeypatch)
    for c in (env.kiosk, env.phone):
        r = c.get(PENDING, follow_redirects=False)
        _assert_json_pending(r, False)


def test_completing_save_raises_the_flag_server_side(env, monkeypatch):
    """The save that flips the install to configured writes the kiosk hand-off
    flag itself. The phone's old follow-up POST cannot do it (auth just went
    live and it has no session), so the server-side write is the signal."""
    _unconfigured(monkeypatch)
    r = env.phone.post("/setup/save", json={
        "grocy_base_url": "http://grocy.test", "grocy_api_key": "k",
        "auth_required": True, "auth_password": "hunter2",
    })
    assert r.status_code == 200 and r.json().get("ok") is True
    assert settings.is_configured() is True
    assert env.flag.exists(), (
        "the completing save did not raise the kiosk hand-off flag; the "
        "attached display will sit on the QR splash indefinitely")


def test_kiosk_poll_is_one_shot_at_the_completion_moment(env, monkeypatch):
    """Right after the completing save, the kiosk's poll reads True exactly
    once (it navigates on that answer) and False afterwards, so the display is
    never looped back to the dashboard on every later tick."""
    _unconfigured(monkeypatch)
    env.phone.post("/setup/save", json={
        "grocy_base_url": "http://grocy.test", "grocy_api_key": "k",
        "auth_required": True, "auth_password": "hunter2",
    })
    _assert_json_pending(env.kiosk.get(PENDING), True)
    _assert_json_pending(env.kiosk.get(PENDING), False)


def test_routine_save_does_not_yank_the_kiosk_home(env, monkeypatch):
    """Only the transition to configured raises the flag. A settings save on an
    already-configured install must not send the display to the home screen."""
    _configured(monkeypatch)
    r = env.kiosk.post("/setup/save", json={"quiet_mode": True})
    assert r.status_code == 200 and r.json().get("ok") is True
    assert not env.flag.exists()


# -- auth posture after setup --------------------------------------------------

def test_pending_is_auth_gated_after_setup_for_lan_clients(env, monkeypatch):
    """Once configured, the poll is as protected as the rest of /setup: an
    unauthenticated LAN client gets a 401 (JSON, still never HTML) and cannot
    consume the one-shot flag out from under the display. The request writer
    is equally gated, so no LAN client can yank the physical display around."""
    _configured(monkeypatch)
    env.flag.write_text("1")
    r = env.phone.get(PENDING, follow_redirects=False)
    assert r.status_code == 401
    assert "application/json" in r.headers.get("content-type", "")
    assert env.flag.exists(), "an unauthorized poll must not consume the flag"
    assert env.phone.post(REQUEST, follow_redirects=False).status_code == 401
    # The appliance's own display is a loopback client, which auth already
    # trusts: it reads (and consumes) the flag with no session.
    _assert_json_pending(env.kiosk.get(PENDING), True)


def test_pending_never_regresses_to_html_for_the_kiosk(env, monkeypatch):
    """The JS contract: in every phase the kiosk's poll gets {"pending": bool}
    JSON. Unconfigured, mid first-boot while the readiness gate is steering
    navigation to /ui/getting-ready, and configured."""
    # Phase 1: plain unconfigured install.
    _unconfigured(monkeypatch)
    _assert_json_pending(env.kiosk.get(PENDING, follow_redirects=False), False)
    # Phase 2: fresh pi_hosted appliance mid first boot, inventory not answering
    # yet, so the gate is actively steering page navigation.
    _unconfigured(monkeypatch, mode="pi_hosted")
    monkeypatch.setattr(settings, "grocy_base_url", "http://localhost:9383",
                        raising=False)

    async def _never_answers():
        return False

    monkeypatch.setattr(readiness, "grocy_answering", _never_answers)
    steered = env.kiosk.get("/setup?kiosk=1", follow_redirects=False)
    assert steered.status_code in (302, 303, 307)
    assert "getting-ready" in steered.headers.get("location", "")  # gate is live
    _assert_json_pending(env.kiosk.get(PENDING, follow_redirects=False), False)
    # Phase 3: configured.
    _configured(monkeypatch)
    _assert_json_pending(env.kiosk.get(PENDING, follow_redirects=False), False)


# -- the poller on the setup page ----------------------------------------------

def test_setup_page_polls_in_both_renderings(env, monkeypatch):
    """setup.html does not extend base.html, so it needs its own hand-off
    poller: on the wizard rendering (the QR splash lives there) AND on the
    configured settings rendering (where the getting-ready page can strand the
    kiosk when setup completed while it waited). The poller is latched on
    kiosk mode and navigates back with the kiosk flag carried."""
    _unconfigured(monkeypatch)
    wizard_html = env.kiosk.get("/setup?kiosk=1").text
    _configured(monkeypatch)
    settings_html = env.kiosk.get("/setup?kiosk=1").text
    for html in (wizard_html, settings_html):
        assert "setup/kiosk/navigate/pending" in html
        assert "kioskMode" in html
        assert "ui/?kiosk=1" in html


# -- the bead's field scenario, end to end --------------------------------------

def test_foodassistant_6v9q_kiosk_leaves_the_splash_after_phone_setup(env, monkeypatch):
    """Fresh pi_hosted flash, inventory already connected (the splash only
    appears after the getting-ready gate releases), auth on, no Stream Deck
    needed: the display sits on the QR splash, Dan finishes the wizard on a
    phone, and the display's next polls must hand it off to the app instead of
    leaving it on the splash until a reboot."""
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://localhost:9383",
                        raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)

    # The kiosk boots to the wizard and shows the QR splash, poller included.
    splash = env.kiosk.get("/setup?kiosk=1")
    assert splash.status_code == 200
    assert 'id="kiosk-setup-hint"' in splash.text
    assert "setup/kiosk/navigate/pending" in splash.text
    # Nothing pending while Dan is still filling in the wizard on the phone.
    _assert_json_pending(env.kiosk.get(PENDING), False)

    # Dan finishes setup on the phone: the save turns auth on and completes
    # setup in one privileged act.
    r = env.phone.post("/setup/save",
                       json={"auth_required": True, "auth_password": "hunter2"})
    assert r.status_code == 200 and r.json().get("ok") is True
    assert settings.is_configured() is True

    # The display's next 3s poll sees the hand-off and navigates to ui/?kiosk=1.
    _assert_json_pending(env.kiosk.get(PENDING), True)

    # And that landing works: the loopback display is trusted, so ui/?kiosk=1
    # serves the app (or its usual start-page redirect, kiosk flag carried),
    # never the login page and never a bounce back to /setup.
    landed = env.kiosk.get("/ui/?kiosk=1", follow_redirects=False)
    assert landed.status_code in (200, 302, 303, 307)
    loc = landed.headers.get("location", "")
    assert "login" not in loc and "/setup" not in loc
    if landed.status_code != 200:
        assert "kiosk=1" in loc
