"""Settings Status dashboard (FoodAssistant-w00b).

Two halves: the pure state-mapping helpers in ``services.status_summary`` (raw
check result -> pill state + fix pane), and the route/template wiring (the
Status pane renders its group panels and a menu pill, gated by mode, and
``/setup/status/summary`` returns the mapped shape).
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(_SERVICE))

from app.config import settings  # noqa: E402
from app.services import status_summary as ss  # noqa: E402


# --- Pure state-mapping helpers -----------------------------------------

def test_pi_health_states():
    assert ss.map_pi_health(None)["state"] == "unknown"
    assert ss.map_pi_health({"ok": False})["state"] == "unknown"
    assert ss.map_pi_health({"ok": True, "warnings": []})["state"] == "good"
    warned = ss.map_pi_health({"ok": True, "warnings": [{"message": "Undervoltage"}]})
    assert warned["state"] == "warn"
    assert "Undervoltage" in warned["detail"]
    assert ss.map_pi_health({"ok": True})["fix_pane"] == "pane-network"


def test_connection_states():
    assert ss.map_connection(None)["state"] == "unknown"
    assert ss.map_connection({"ok": True, "active_connection": "wired"})["state"] == "good"
    wifi = ss.map_connection({"ok": True, "active_connection": "wifi", "ssid": "Home"})
    assert wifi["state"] == "good" and "Home" in wifi["detail"]
    assert ss.map_connection({"ok": True, "active_connection": ""})["state"] == "bad"


def test_update_states():
    assert ss.map_update(0.0, False, "")["state"] == "unknown"
    avail = ss.map_update(123.0, True, "0.9.9")
    assert avail["state"] == "warn" and "0.9.9" in avail["detail"]
    assert avail["fix_pane"] == "pane-backups"
    assert ss.map_update(123.0, False, "")["state"] == "good"


def test_forager_states():
    assert ss.map_forager(None)["state"] == "unknown"
    assert ss.map_forager({"linked": False})["state"] == "warn"
    assert ss.map_forager({"linked": True, "reachable": False})["state"] == "bad"
    assert ss.map_forager({"linked": True, "reachable": True, "valid": False})["state"] == "bad"
    good = ss.map_forager({"linked": True, "reachable": True, "valid": True,
                           "account_email": "a@b.co"})
    assert good["state"] == "good" and "a@b.co" in good["detail"]
    assert good["fix_pane"] == "pane-forager"


def test_remote_access_states():
    assert ss.map_remote_access(None)["state"] == "unknown"
    assert ss.map_remote_access({"enabled": False})["state"] == "warn"
    assert ss.map_remote_access({"enabled": True, "up": True})["state"] == "good"
    assert ss.map_remote_access({"enabled": True, "up": False})["state"] == "bad"


def test_main_server_states():
    assert ss.map_main_server(None)["state"] == "warn"
    assert ss.map_main_server({})["state"] == "warn"
    assert ss.map_main_server({"at": "t", "ok": True})["state"] == "good"
    bad = ss.map_main_server({"at": "t", "ok": False, "error": "boom"})
    assert bad["state"] == "bad" and bad["detail"] == "boom"
    assert bad["fix_pane"] == "pane-devices"


def test_service_states():
    assert ss.map_service("grocy", "Grocy", False, False)["state"] == "warn"
    assert ss.map_service("grocy", "Grocy", True, True)["state"] == "good"
    assert ss.map_service("grocy", "Grocy", True, False)["state"] == "bad"
    assert ss.map_service("grocy", "Grocy", True, True)["fix_pane"] == "pane-inventory"
    assert ss.map_service("mealie", "Mealie", True, True)["fix_pane"] == "pane-personalization-recipes"
    assert ss.map_service("home_assistant", "HA", True, True)["fix_pane"] == "pane-home-assistant"


def test_build_summary_maps_only_present_keys():
    raw = {
        "connection": {"ok": True, "active_connection": "wired"},
        "update": {"checked_at": 1.0, "available": False, "latest": ""},
        "grocy": {"configured": True, "ok": True},
    }
    out = ss.build_summary(raw)
    assert out["ok"] is True
    assert set(out["items"]) == {"connection", "update", "grocy"}
    assert out["items"]["grocy"]["state"] == "good"
    # Every item carries the render contract the JS relies on.
    for it in out["items"].values():
        assert {"key", "label", "state", "detail", "fix_pane"} <= set(it)


# --- Route + template wiring --------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app

    cwd = os.getcwd()
    os.chdir(_SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    # A configured install has a connected inventory; without the key a
    # pi_hosted render is held on the first-boot gate (FoodAssistant-6v9q).
    monkeypatch.setattr(settings, "grocy_api_key", "test-key", raising=False)
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "auth_password", "")
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _render(client, monkeypatch, *, mode: str, is_pi: bool) -> str:
    monkeypatch.setattr(settings, "deployment_mode", mode)
    with patch.object(type(settings), "is_configured", lambda self: True), \
         patch("app.routers.setup.is_raspberry_pi", return_value=is_pi), \
         patch("app.templating.is_raspberry_pi", return_value=is_pi):
        r = client.get("/setup")
    assert r.status_code == 200
    return r.text


def _status_region(html: str) -> str:
    assert 'id="pane-status"' in html, "Status pane missing"
    return html.split('id="pane-status"', 1)[1].split('id="pane-', 1)[0]


# The row keys the Status pane shows per mode (kept in step with both the Jinja
# gating and the route's probe selection).
_EXPECTED_ROWS = {
    "server": {"update", "forager", "remote_access", "grocy"},
    "pi_hosted": {"pi_health", "connection", "update", "forager", "remote_access", "grocy"},
    "pi_remote": {"connection", "update", "main_server"},
}


def test_status_menu_pill_present_all_modes(client, monkeypatch):
    for mode, is_pi in (("server", False), ("pi_hosted", True), ("pi_remote", True)):
        html = _render(client, monkeypatch, mode=mode, is_pi=is_pi)
        assert re.search(
            r'data-bs-toggle="pill" data-bs-target="#pane-status"', html), mode
        # The pill lazy-loads the summary on open.
        assert 'onclick="loadStatusSummary()"' in html, mode


def test_status_pane_rows_gated_by_mode(client, monkeypatch):
    for mode, is_pi in (("server", False), ("pi_hosted", True), ("pi_remote", True)):
        region = _status_region(_render(client, monkeypatch, mode=mode, is_pi=is_pi))
        rows = set(re.findall(r'id="status-pill-([a-z_]+)"', region))
        assert rows == _EXPECTED_ROWS[mode], (mode, rows)
        # Group panels frame the rows.
        for group in ("Software", "Cloud"):
            assert f">{group}<" in region, (mode, group)


def test_status_summary_route_shape(client, monkeypatch):
    """The endpoint returns {ok, items{key: {state, detail, fix_pane}}} and the
    item keys match the rows the same mode renders."""
    for mode, is_pi in (("server", False), ("pi_hosted", True), ("pi_remote", True)):
        monkeypatch.setattr(settings, "deployment_mode", mode)
        with patch.object(type(settings), "is_configured", lambda self: True), \
             patch("app.routers.setup.is_raspberry_pi", return_value=is_pi):
            # Pi and cloud probes have no host bridge or account here, so they
            # fail soft to their defaults: the test stays offline and fast.
            r = client.get("/setup/status/summary")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert set(body["items"]) == _EXPECTED_ROWS[mode], (mode, set(body["items"]))
        for it in body["items"].values():
            assert it["state"] in {"good", "warn", "bad", "unknown"}
            assert {"state", "detail", "fix_pane"} <= set(it)


def test_status_loader_registered_in_js(client):
    assert "function loadStatusSummary(" in client.get("static/js/setup/status.js").text
