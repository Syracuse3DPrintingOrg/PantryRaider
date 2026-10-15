"""Settings the panes post to /setup/save that used to be silently dropped
(FoodAssistant-35zqz).

Each of these was posted by a settings pane but was missing from the
/setup/save request model, which ignores unknown fields, so the Save button
reported success and nothing stuck. Every key here gets a POST that must land
on the live settings object and in settings.json.
"""
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SERVICE_DIR = Path(__file__).parent.parent / "service"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    cwd = os.getcwd()
    os.chdir(_SERVICE_DIR)
    try:
        from app.config import settings

        settings.data_dir = str(tmp_path_factory.mktemp("data"))
        from app.main import app

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


def _saved():
    from app.config import settings
    return json.loads((Path(settings.data_dir) / "settings.json").read_text())


def _post(client, body):
    r = client.post("/setup/save", json=body)
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True, r.json()


def _assert_persisted(key, value):
    from app.config import settings
    assert getattr(settings, key) == value
    assert _saved()[key] == value


@pytest.mark.parametrize("key,value", [
    ("display_margin_top", 12),
    ("display_margin_right", 34),
    ("display_margin_bottom", 5),
    ("display_margin_left", 7),
    ("forager_recipes_enabled", False),
    ("kiosk_readonly_when_locked", True),
    ("label_shape", "round"),
    ("label_show_logo", True),
    ("document_page_size", "a4"),
    ("document_color_mode", "monochrome"),
    ("document_duplex", "two-sided"),
    ("buttons_enabled", True),
    ("beszel_enabled", True),
    ("beszel_url", "http://beszel.local:8090"),
    ("tunnel_mode", "forager"),
])
def test_setup_save_persists(client, key, value):
    _post(client, {key: value})
    _assert_persisted(key, value)


def test_display_margins_are_clamped(client):
    _post(client, {"display_margin_top": 9999, "display_margin_left": -4})
    _assert_persisted("display_margin_top", 200)
    _assert_persisted("display_margin_left", 0)


def test_unknown_label_shape_falls_back_to_rectangle(client):
    _post(client, {"label_shape": "hexagon"})
    _assert_persisted("label_shape", "rectangle")


def test_unknown_document_choices_fall_back_to_defaults(client):
    _post(client, {"document_page_size": "tabloid", "document_color_mode": "sepia",
                   "document_duplex": "Two-Sided"})
    _assert_persisted("document_page_size", "auto")
    _assert_persisted("document_color_mode", "color")
    _assert_persisted("document_duplex", "two-sided")


def test_unknown_tunnel_mode_is_not_stored(client):
    _post(client, {"tunnel_mode": "cloudflare"})
    _post(client, {"tunnel_mode": "subscription"})
    _assert_persisted("tunnel_mode", "cloudflare")
    _post(client, {"tunnel_mode": ""})
    _assert_persisted("tunnel_mode", "")


def test_a_section_that_does_not_post_them_leaves_them_alone(client):
    _post(client, {"kiosk_readonly_when_locked": True, "beszel_url": "http://hub"})
    _post(client, {"perishable_days": 11})
    _assert_persisted("kiosk_readonly_when_locked", True)
    _assert_persisted("beszel_url", "http://hub")
    # An explicit null is "not posted" too.
    _post(client, {"kiosk_readonly_when_locked": None, "buttons_enabled": None})
    _assert_persisted("kiosk_readonly_when_locked", True)
