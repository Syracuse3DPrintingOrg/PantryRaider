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
