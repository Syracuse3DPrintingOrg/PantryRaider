"""Tests for the tunnel router and TunnelService.

All subprocess calls are mocked so tests run without Docker.
"""
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

_SERVICE_DIR = Path(__file__).parent.parent / "service"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    cwd = os.getcwd()
    os.chdir(_SERVICE_DIR)
    try:
        from app.config import settings

        data_dir = tmp_path_factory.mktemp("data_tunnel")
        settings.data_dir = str(data_dir)

        from app.main import app

        # Mark as configured so setup-redirect middleware doesn't intercept
        settings.grocy_base_url = "http://grocy.test"
        settings.grocy_api_key = "test-grocy-key"
        settings.vision_provider = "gemini"
        settings.gemini_api_key = "test-gemini-key"
        settings.auth_required = False
        settings.auth_password = ""
        assert settings.is_configured()

        with TestClient(app) as c:
            yield c
    finally:
        os.chdir(cwd)


# ---------------------------------------------------------------------------
# GET /tunnel/status
# ---------------------------------------------------------------------------

def test_tunnel_status_returns_correct_shape(client):
    """Status endpoint returns running/url/mode even when docker is unavailable."""
    # subprocess.run raises FileNotFoundError when docker is absent
    with patch("app.services.tunnel.subprocess.run", side_effect=FileNotFoundError):
        r = client.get("/tunnel/status")
    assert r.status_code == 200
    data = r.json()
    assert "running" in data
    assert "url" in data
    assert "mode" in data
    assert isinstance(data["running"], bool)
    assert isinstance(data["url"], str)
    assert isinstance(data["mode"], str)


def test_tunnel_status_not_running_when_docker_absent(client):
    with patch("app.services.tunnel.subprocess.run", side_effect=FileNotFoundError):
        r = client.get("/tunnel/status")
    assert r.json()["running"] is False


# ---------------------------------------------------------------------------
# POST /tunnel/start
# ---------------------------------------------------------------------------

def test_tunnel_start_cloudflare_saves_config_and_returns_result(client):
    """Start with cloudflare mode saves settings and returns ok/error without crashing."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "abc123\n"
    mock_result.stderr = ""

    with patch("app.services.tunnel.subprocess.run", return_value=mock_result):
        r = client.post("/tunnel/start", json={"mode": "cloudflare", "token": "test-token"})

    assert r.status_code == 200
    data = r.json()
    assert "ok" in data
    # Config should reflect the saved mode
    from app.config import settings
    assert settings.tunnel_mode == "cloudflare"


def test_tunnel_start_docker_missing_returns_error(client):
    """Start returns ok=False gracefully when docker is not available."""
    with patch("app.services.tunnel.subprocess.run", side_effect=FileNotFoundError):
        r = client.post("/tunnel/start", json={"mode": "cloudflare", "token": "test"})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False
    assert "docker" in data["error"].lower()


def test_tunnel_start_subscription_is_refused(client):
    """The old subscription mode registered against a domain that does not
    resolve and answered every failure with a fake success and a demo address,
    so the pane reported a connection that was never made. It is refused now,
    and a mode this install cannot run is never written to settings."""
    from app.config import settings
    with patch("app.services.tunnel.subprocess.run", side_effect=FileNotFoundError):
        r = client.post("/tunnel/start", json={"mode": "subscription", "token": "sub-token"})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False
    assert "_mock" not in data
    assert "demo.foodassistant.app" not in str(data)
    assert settings.tunnel_mode != "subscription"


def test_tunnel_service_has_no_subscription_registration():
    """No caller, and no live endpoint: the stub is gone rather than dormant."""
    from app.services import tunnel as tunnel_service
    assert not hasattr(tunnel_service.TunnelService, "_start_subscription")
    assert "cloud.foodassistant.app" not in Path(
        tunnel_service.__file__).read_text()


def test_tunnel_start_stores_a_mode_this_install_runs(client):
    from app.config import settings
    with patch("app.services.tunnel.subprocess.run", side_effect=FileNotFoundError):
        client.post("/tunnel/start", json={"mode": "cloudflare", "token": "t"})
    assert settings.tunnel_mode == "cloudflare"


# ---------------------------------------------------------------------------
# POST /tunnel/stop
# ---------------------------------------------------------------------------

def test_tunnel_stop_docker_missing_doesnt_crash(client):
    """Stop endpoint returns without crashing when docker is absent."""
    with patch("app.services.tunnel.subprocess.run", side_effect=FileNotFoundError):
        r = client.post("/tunnel/stop")
    assert r.status_code == 200
    data = r.json()
    assert "ok" in data


def test_tunnel_stop_no_container_doesnt_crash(client):
    """Stop endpoint when container doesn't exist returns ok=False gracefully."""
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "No such container: foodassistant-tunnel"

    with patch("app.services.tunnel.subprocess.run", return_value=mock_result):
        r = client.post("/tunnel/stop")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False


# ---------------------------------------------------------------------------
# Unit tests for TunnelService internals
# ---------------------------------------------------------------------------

def test_tunnel_service_get_url_from_logs_extracts_trycloudflare():
    """URL extraction regex finds trycloudflare.com URLs in log output."""
    from app.services.tunnel import TunnelService

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""
    mock_result.stderr = (
        "2024-01-01T00:00:00Z INF +----------------------------+\n"
        "2024-01-01T00:00:00Z INF |  Your quick Tunnel link is  |\n"
        "2024-01-01T00:00:00Z INF | https://abc-def.trycloudflare.com |\n"
    )

    with patch("app.services.tunnel.subprocess.run", return_value=mock_result):
        svc = TunnelService()
        url = svc.get_url_from_cloudflare_logs()

    assert url == "https://abc-def.trycloudflare.com"


def test_tunnel_service_get_url_returns_empty_when_docker_absent():
    from app.services.tunnel import TunnelService

    with patch("app.services.tunnel.subprocess.run", side_effect=FileNotFoundError):
        svc = TunnelService()
        url = svc.get_url_from_cloudflare_logs()

    assert url == ""


# --------------------------------------------------------------------------- #
# a mode this install cannot run is said out loud, not offered as a dead button
# --------------------------------------------------------------------------- #

def test_cloudflare_availability_needs_both_the_cli_and_the_socket():
    """Every call in the service shells out to docker, so it needs the CLI on
    PATH AND a socket to talk to. The published image has neither."""
    from app.services import tunnel

    both = tunnel.cloudflare_available(which=lambda n: "/usr/bin/docker",
                                       exists=lambda p: True)
    assert both is True

    # The Compose reality: no CLI baked into the app image.
    assert tunnel.cloudflare_available(which=lambda n: None,
                                       exists=lambda p: True) is False
    # CLI present but no socket mounted (mounting it would hand the app root
    # on the host, which is why no compose file does).
    assert tunnel.cloudflare_available(which=lambda n: "/usr/bin/docker",
                                       exists=lambda p: False) is False


def test_starting_cloudflare_where_it_cannot_run_says_what_to_do_instead(monkeypatch):
    """The old failure blamed Docker for being down ("is Docker running?"),
    sending the reader off to debug a daemon that was fine all along. It is
    out of reach, not down, and the answer is to run the helper alongside."""
    from app.services import tunnel

    monkeypatch.setattr(tunnel, "cloudflare_available", lambda: False)
    r = tunnel.TunnelService().start("cloudflare", "a-token")
    assert r["ok"] is False
    assert "docker" not in r["error"].lower(), \
        "the refusal must not send the user off to debug Docker"
    assert "cloudflared" in r["error"] and "docs" in r["error"].lower()
