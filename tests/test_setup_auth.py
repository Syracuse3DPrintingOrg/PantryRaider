"""The setup surface must not bypass auth once the instance is configured
(rules audit, Jul 2026): before this guard, any LAN client could POST
/setup/save on a password-protected install and overwrite auth_password."""
import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.passwords import hash_secret  # noqa: E402


@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        # client_host defaults to "testclient", NOT loopback, so the loopback
        # trust path does not mask the middleware behavior under test.
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _configured(monkeypatch, password=""):
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "auth_required", bool(password), raising=False)
    monkeypatch.setattr(settings, "auth_password",
                        hash_secret(password) if password else "", raising=False)


def test_configured_instance_rejects_unauthenticated_setup_save(client, monkeypatch):
    _configured(monkeypatch, password="hunter2")
    r = client.post("/setup/save", json={"grocy_base_url": "http://evil"},
                    follow_redirects=False)
    assert r.status_code in (302, 303, 307, 401, 403)
    assert settings.grocy_base_url == "http://grocy.test"


def test_configured_instance_rejects_unauthenticated_setup_page(client, monkeypatch):
    _configured(monkeypatch, password="hunter2")
    r = client.get("/setup", follow_redirects=False)
    assert r.status_code in (302, 303, 307, 401, 403)


def test_unconfigured_instance_serves_the_wizard(client, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    r = client.get("/setup")
    assert r.status_code == 200


def test_unconfigured_first_run_grocy_answers_json_not_html(client, monkeypatch):
    # The wizard's Grocy auto-setup POSTs before the instance is configured. It
    # must reach the handler and get JSON back, not the setup-redirect HTML page
    # (which the JS would fail to parse: "Unexpected token '<'"). FoodAssistant.
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    r = client.post("/setup/first-run/grocy", json={"base_url": ""},
                    follow_redirects=False)
    assert r.status_code == 200
    assert "application/json" in r.headers.get("content-type", "")
    assert not r.text.lstrip().startswith("<")


def test_unconfigured_grocy_status_answers_json_not_html(client, monkeypatch):
    # The wizard's live inventory-setup indicator polls these before setup is
    # configured; they must return JSON, not the setup-redirect HTML page, or the
    # progress spinner stalls silently (FoodAssistant-f8kp).
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    for path in ("/setup/grocy/local-status", "/setup/logs/grocy"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 200, path
        assert not r.text.lstrip().startswith("<"), path


def test_configured_without_grocy_key_pending_provisioning(monkeypatch):
    # Finishing the wizard must not require the Grocy API key: first-run
    # provisioning fills it in automatically after Grocy comes up. Requiring it
    # bounced the user back to wizard page 1 in a loop (Dan, 2026-07-10).
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://localhost:9383", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_password", "hunter2", raising=False)
    assert settings.is_configured() is True


def test_unconfigured_qr_and_tunnel_answer_not_redirect(client, monkeypatch):
    # The kiosk splash <img> loads /ui/qr and the wizard's remote-access step
    # POSTs setup/tunnel/enable before setup completes; both must answer (JSON
    # or SVG), never the setup-redirect HTML page.
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    r = client.get("/ui/qr?url=http://x/setup", follow_redirects=False)
    assert r.status_code == 200
    assert "svg" in r.headers.get("content-type", "")
    r = client.get("/setup/tunnel/status", follow_redirects=False)
    assert r.status_code == 200
    assert not r.text.lstrip().startswith("<!")


def test_api_key_still_reaches_setup_endpoints(client, monkeypatch):
    _configured(monkeypatch, password="hunter2")
    monkeypatch.setattr(settings, "api_key", "sesame", raising=False)
    r = client.post("/setup/test/grocy", json={},
                    headers={"X-API-Key": "sesame"}, follow_redirects=False)
    # Authenticated: the handler runs (whatever it returns), no auth redirect.
    assert r.status_code not in (302, 303, 307, 401, 403)


def test_health_and_login_stay_public(client, monkeypatch):
    _configured(monkeypatch, password="hunter2")
    assert client.get("/health").status_code == 200
    # The login page must never bounce to itself (that would be an auth
    # redirect loop). Not following redirects keeps this robust against
    # settings state leaked by earlier test modules.
    r = client.get("/ui/login", follow_redirects=False)
    assert r.status_code in (200, 303, 307)
    assert "/ui/login" not in r.headers.get("location", "")
