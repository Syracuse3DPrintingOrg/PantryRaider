"""Tests for GET /ui/qr — phone deep-link QR code endpoint."""
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SERVICE_DIR = Path(__file__).parent.parent / "service"


@pytest.fixture(scope="module")
def qr_client(tmp_path_factory):
    """Minimal client fixture for the QR endpoint tests."""
    cwd = os.getcwd()
    os.chdir(_SERVICE_DIR)
    try:
        from app.config import settings

        data_dir = tmp_path_factory.mktemp("qr_data")
        settings.data_dir = str(data_dir)
        # Satisfy is_configured() so the setup-redirect middleware passes through.
        settings.grocy_base_url = "http://grocy.test"
        settings.grocy_api_key = "test-grocy-key"
        settings.vision_provider = "gemini"
        settings.gemini_api_key = "test-gemini-key"
        settings.auth_required = False
        settings.auth_password = ""

        from app.main import app

        with TestClient(app, base_url="http://testserver") as c:
            yield c
    finally:
        os.chdir(cwd)


def test_qr_returns_200_and_svg(qr_client):
    r = qr_client.get("/ui/qr")
    assert r.status_code == 200, f"Expected 200, got {r.status_code}"
    assert "svg" in r.headers["content-type"].lower(), (
        f"Expected SVG content-type, got {r.headers['content-type']}"
    )


def test_qr_body_contains_request_host(qr_client):
    r = qr_client.get("/ui/qr")
    assert r.status_code == 200
    # The host should appear in the SVG body because it is encoded in the URL
    # embedded within the QR code path data.
    assert "testserver" in r.text, (
        "Expected request host 'testserver' to appear in the QR SVG body"
    )


def test_qr_encodes_explicit_url(qr_client):
    # The kiosk setup hint passes a LAN setup URL to encode (cssj).
    r = qr_client.get("/ui/qr", params={"url": "http://foodassistant.local:9284/setup"})
    assert r.status_code == 200
    assert "http://foodassistant.local:9284/setup" in r.text
    assert "/ui/add" not in r.text


def test_qr_rejects_non_http_url(qr_client):
    # A non-http(s) scheme must be ignored, not encoded, so the code can never
    # carry javascript:/data: payloads. Falls back to the default add link.
    r = qr_client.get("/ui/qr", params={"url": "javascript:alert(1)"})
    assert r.status_code == 200
    assert "javascript:alert" not in r.text
    assert "/ui/add" in r.text


# --- phone_base_url: pick an address a phone can actually reach (75ak) -------

def _phone_base_url():
    from app.routers.qr import phone_base_url
    return phone_base_url


def test_phone_base_swaps_localhost_for_lan_ip(qr_client):
    # The kiosk browser hits the app at localhost; the QR must encode the LAN
    # address instead so a phone scanning it lands somewhere reachable.
    f = _phone_base_url()
    assert f("localhost:9284", "auto", "", "192.168.1.42") == "http://192.168.1.42:9284"
    assert f("127.0.0.1:9284", "auto", "", "192.168.1.42") == "http://192.168.1.42:9284"
    assert f("0.0.0.0:9284", "auto", "", "192.168.1.42") == "http://192.168.1.42:9284"


def test_phone_base_keeps_reachable_host(qr_client):
    f = _phone_base_url()
    assert f("192.168.1.50:9284", "auto", "", "192.168.1.42") == "http://192.168.1.50:9284"
    assert f("kitchen-pi.local:9284", "auto", "", "192.168.1.42") == "http://kitchen-pi.local:9284"


def test_phone_base_localhost_without_lan_ip_is_kept(qr_client):
    # No LAN IP available: keep the request host rather than emit a broken URL.
    f = _phone_base_url()
    assert f("localhost:9284", "auto", "", "") == "http://localhost:9284"


def test_phone_base_drops_default_ports(qr_client):
    f = _phone_base_url()
    assert f("localhost:80", "auto", "", "192.168.1.42") == "http://192.168.1.42"
    assert f("localhost", "auto", "", "192.168.1.42") == "http://192.168.1.42"


def test_phone_base_public_mode_uses_public_url(qr_client):
    f = _phone_base_url()
    assert f("localhost:9284", "public", "https://pantry.example.com/",
             "192.168.1.42") == "https://pantry.example.com"
    # An empty public URL falls back to the auto behavior.
    assert f("localhost:9284", "public", "", "192.168.1.42") == "http://192.168.1.42:9284"


def test_qr_endpoint_swaps_localhost_host(qr_client, monkeypatch):
    from app.routers import qr as qr_module
    monkeypatch.setattr(qr_module, "_lan_ip", lambda: "192.168.1.42")
    r = qr_client.get("/ui/qr", headers={"host": "localhost:9284"})
    assert r.status_code == 200
    assert "http://192.168.1.42:9284/ui/add" in r.text
    assert "localhost" not in r.text


def test_qr_url_endpoint_reports_encoded_url(qr_client, monkeypatch):
    from app.routers import qr as qr_module
    monkeypatch.setattr(qr_module, "_lan_ip", lambda: "192.168.1.42")
    r = qr_client.get("/ui/qr/url", headers={"host": "127.0.0.1:9284"})
    assert r.status_code == 200
    assert r.json()["url"] == "http://192.168.1.42:9284/ui/add"


def test_qr_endpoint_public_mode(qr_client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "qr_url_mode", "public", raising=False)
    monkeypatch.setattr(settings, "qr_public_url", "https://pantry.example.com", raising=False)
    r = qr_client.get("/ui/qr", headers={"host": "localhost:9284"})
    assert r.status_code == 200
    assert "https://pantry.example.com/ui/add" in r.text
