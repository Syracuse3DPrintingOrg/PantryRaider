"""The generated backend sign-in is not handed to the whole network.

Until setup is finished there is no login on the device, so the wizard's
first-run routes answer whoever asks. A new appliance can sit on that screen for
days, which meant anyone on the network (or reaching it through a proxy) could
read the inventory account password it generated for itself. Now those two
routes want a browser genuinely on the home network, and the sign-in is shown
once per generated password until setup completes (security audit, Sep 2026).
"""
import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.routers.setup import first_run_reveal_mark  # noqa: E402


@pytest.fixture
def make_client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    from fastapi.testclient import TestClient
    from app.main import app

    def _make(host="192.168.1.50"):
        return TestClient(app, client=(host, 50000))

    try:
        yield _make
    finally:
        os.chdir(cwd)


def _unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "tunnel_enabled", False, raising=False)
    monkeypatch.setattr(settings, "qr_public_url", "", raising=False)
    monkeypatch.setattr(settings, "grocy_admin_password", "generated-pw", raising=False)
    monkeypatch.setattr(settings, "first_run_revealed", [], raising=False)


def test_mark_is_a_fingerprint_not_the_password():
    mark = first_run_reveal_mark("grocy", "generated-pw")
    assert mark.startswith("grocy:")
    assert "generated-pw" not in mark
    assert mark != first_run_reveal_mark("grocy", "other-pw")


def test_reveal_refuses_a_request_that_came_through_a_proxy(make_client, monkeypatch):
    _unconfigured(monkeypatch)
    client = make_client()
    r = client.post("/setup/first-run/reveal", json={"service": "grocy"},
                    headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 403
    assert "generated-pw" not in r.text


def test_reveal_refuses_a_caller_off_the_home_network(make_client, monkeypatch):
    _unconfigured(monkeypatch)
    client = make_client("8.8.8.8")
    r = client.post("/setup/first-run/reveal", json={"service": "grocy"})
    assert r.status_code == 403
    assert "generated-pw" not in r.text


def test_reveal_answers_once_before_setup_is_finished(make_client, monkeypatch):
    _unconfigured(monkeypatch)
    client = make_client()
    first = client.post("/setup/first-run/reveal", json={"service": "grocy"})
    assert first.status_code == 200
    assert first.json()["password"] == "generated-pw"
    again = client.post("/setup/first-run/reveal", json={"service": "grocy"})
    assert again.status_code == 403
    assert "generated-pw" not in again.text


def test_setting_a_backend_up_again_earns_a_new_showing(make_client, monkeypatch):
    _unconfigured(monkeypatch)
    client = make_client()
    assert client.post("/setup/first-run/reveal", json={"service": "grocy"}).status_code == 200
    monkeypatch.setattr(settings, "grocy_admin_password", "second-pw", raising=False)
    r = client.post("/setup/first-run/reveal", json={"service": "grocy"})
    assert r.status_code == 200
    assert r.json()["password"] == "second-pw"


def test_after_setup_the_settings_page_can_show_it_again(make_client, monkeypatch):
    _unconfigured(monkeypatch)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    assert settings.is_configured() is True
    client = make_client()
    for _ in range(2):
        r = client.post("/setup/first-run/reveal", json={"service": "grocy"})
        assert r.status_code == 200
        assert r.json()["password"] == "generated-pw"


def test_auto_setup_also_wants_the_home_network(make_client, monkeypatch):
    _unconfigured(monkeypatch)
    client = make_client("8.8.8.8")
    r = client.post("/setup/first-run/grocy", json={"base_url": ""})
    assert r.status_code == 403
