"""A Bandit's kitchen screen keeps its PIN unlock when it signs itself in again.

The screen signs itself in with a launch ticket each time it starts (after an
update, a reboot or a relaunch). That sign-in starts a fresh session, which
also dropped the unlock from the screen's PIN, so a Bandit with a kiosk PIN
sat on the PIN pad after every start. Before 0.21 the screen's 30-day cookie
kept it. The unlock now carries over to the new session.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.passwords import hash_secret
from app.services import auth_state

SERVICE = Path(__file__).resolve().parents[1] / "service"


@pytest.fixture
def bandit(bridge_token, monkeypatch):
    monkeypatch.chdir(SERVICE)
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    monkeypatch.setattr(settings, "remote_server_url", "http://server.test", raising=False)
    monkeypatch.setattr(settings, "upstream_api_key", "up-key", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret("bandit-pass"), raising=False)
    monkeypatch.setattr(settings, "kiosk_pin", hash_secret("2468"), raising=False)
    monkeypatch.setattr(settings, "kiosk_readonly_when_locked", False, raising=False)
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True, raising=False)
    auth_state.retire("admin")
    return bridge_token


def _locked(client) -> bool:
    r = client.get("/ui/pin", follow_redirects=False)
    return r.status_code == 200


def test_the_unlock_survives_the_screen_signing_in_again(bandit, loopback_client, kiosk_login):
    kiosk_login(loopback_client)
    assert _locked(loopback_client)
    r = loopback_client.post("/ui/pin/verify", data={"pin": "2468"}, follow_redirects=False)
    assert r.status_code == 303
    assert not _locked(loopback_client)
    # The screen restarts and redeems a new ticket.
    kiosk_login(loopback_client)
    assert not _locked(loopback_client)


def test_a_new_screen_still_asks_for_the_pin(bandit, loopback_client, kiosk_login):
    kiosk_login(loopback_client)
    kiosk_login(loopback_client)
    assert _locked(loopback_client)
