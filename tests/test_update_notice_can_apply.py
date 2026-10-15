"""The update prompt offers Update now only to someone who may start it.

The prompt runs on every page, including a kitchen screen that is not allowed
to open Settings (The kitchen screen can open Settings turned off) and a
viewer's session. Those cannot start an update, so the prompt shows them the
release instead of an Update now that can only fail; and when a start is
refused, the prompt shows the reason the app gave.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.config import settings
from app.passwords import hash_secret
from app.services import auth_state

SERVICE = Path(__file__).resolve().parents[1] / "service"


@pytest.fixture
def install(bridge_token, monkeypatch):
    from app.services import update_path
    monkeypatch.chdir(SERVICE)
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret("hunter22"), raising=False)
    monkeypatch.setattr(settings, "viewer_password", hash_secret("viewer22"), raising=False)
    monkeypatch.setattr(settings, "check_for_updates", True, raising=False)
    monkeypatch.setattr(settings, "update_last_checked", time.time() - 10, raising=False)
    monkeypatch.setattr(settings, "update_last_latest", "99.0.0", raising=False)
    monkeypatch.setattr(settings, "update_last_available", True, raising=False)
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True, raising=False)
    from app.services import readiness
    monkeypatch.setattr(readiness, "gate_possible", lambda: False)

    async def _resolve(force=False):
        return {"kind": update_path.KIND_PI, "bridge": update_path.BRIDGE_NOT_CHECKED,
                "bridge_hint": False}
    monkeypatch.setattr(update_path, "resolve", _resolve)
    auth_state.retire("admin")
    return bridge_token


def _notice(client):
    r = client.get("/admin/update-notice")
    assert r.status_code == 200, r.text
    return r.json()


def test_the_owner_can_update_from_the_prompt(install, lan_client):
    lan_client.post("/ui/login", data={"password": "hunter22"}, follow_redirects=False)
    d = _notice(lan_client)
    assert d["show"] is True and d["can_update_in_app"] is True and d["can_apply"] is True


def test_a_screen_that_cannot_open_settings_is_shown_the_release(install, loopback_client,
                                                                  kiosk_login, monkeypatch):
    monkeypatch.setattr(settings, "kiosk_screen_is_admin", False, raising=False)
    kiosk_login(loopback_client)
    d = _notice(loopback_client)
    assert d["can_update_in_app"] is True and d["can_apply"] is False
    monkeypatch.setattr(settings, "kiosk_screen_is_admin", True, raising=False)
    assert _notice(loopback_client)["can_apply"] is True


def test_a_viewer_never_gets_the_prompt(install, lan_client):
    lan_client.post("/ui/login", data={"password": "viewer22"}, follow_redirects=False)
    assert lan_client.get("/admin/update-notice").status_code == 403


def test_a_bandit_without_a_password_offers_no_update_to_visitors(install, lan_client,
                                                                   monkeypatch):
    """Updating a Bandit with no password stays on its own screen."""
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    d = _notice(lan_client)
    assert d["can_apply"] is False


def test_the_prompt_reads_both_answers():
    js = (SERVICE / "app" / "static" / "js" / "update-notice.js").read_text()
    assert "can_apply" in js
    assert "res.detail" in js
