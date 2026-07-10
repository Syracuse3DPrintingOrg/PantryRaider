"""Thermometers settings surface (FoodAssistant-mnks).

Covers the reader heartbeat that drives the Settings status card, the
/gadgets/install endpoint's server-mode guidance, the Home Assistant entity
management endpoints, the rendered pane per deployment mode, the save payload
plumbing, and the host bridge's /gadgets-setup route. No radio, Home
Assistant, or bridge process needed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parents[1]
SERVICE = REPO / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services import gadgets  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    gadgets.reset()
    yield
    gadgets.reset()


@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd(); os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://g", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "gadgets_enabled", False, raising=False)
    monkeypatch.setattr(settings, "gadget_devices", [], raising=False)
    monkeypatch.setattr(settings, "gadget_ha_enabled", False, raising=False)
    monkeypatch.setattr(settings, "gadget_ha_entities", [], raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


# -- Reader heartbeat ---------------------------------------------------------

def test_reader_age_none_until_reader_contact(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    assert gadgets.get_state()["reader_age_seconds"] is None


def test_ingest_marks_reader_seen(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "gadget_devices", [], raising=False)
    gadgets.ingest({"devices": [{"id": "AA", "probes": [
        {"index": 1, "temp_c": 20.0}]}]}, now=1000.0)
    age = gadgets.get_state(now=1012.0)["reader_age_seconds"]
    assert age == 12.0


def test_ha_ingest_does_not_mark_reader_seen(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "gadget_devices", [], raising=False)
    gadgets.ingest({"devices": [{"id": "HA:SENSOR.X", "probes": [
        {"index": 1, "temp_c": 20.0}]}]}, now=1000.0, mark_reader=False)
    assert gadgets.get_state(now=1012.0)["reader_age_seconds"] is None


def test_mark_reader_seen_direct_and_throttled(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    gadgets.mark_reader_seen(now=1000.0)
    assert gadgets.get_state(now=1003.0)["reader_age_seconds"] == 3.0
    # Within the throttle window the timestamp stays put.
    gadgets.mark_reader_seen(now=1002.0)
    assert gadgets.get_state(now=1003.0)["reader_age_seconds"] == 3.0
    gadgets.mark_reader_seen(now=1010.0)
    assert gadgets.get_state(now=1011.0)["reader_age_seconds"] == 1.0


def test_config_pull_counts_as_reader_contact(client):
    assert client.get("/gadgets/state").json()["reader_age_seconds"] is None
    client.get("/gadgets/config")
    age = client.get("/gadgets/state").json()["reader_age_seconds"]
    assert age is not None and age < 60


# -- /gadgets/install ---------------------------------------------------------

def test_install_on_server_returns_manual_steps(client):
    d = client.post("/gadgets/install").json()
    assert d["ok"] is False
    assert "Bluetooth radio" in d["message"]
    assert "foodassistant-gadgets-setup" in d["message"]
    assert "Home Assistant" in d["message"]


# -- Home Assistant entity management ------------------------------------------

def test_add_ha_entity_creates_device_and_enables(client):
    r = client.post("/gadgets/ha-entities",
                    json={"entity_id": "Sensor.Grill_Probe", "name": "Grill"}).json()
    assert r["ok"] is True
    assert r["entities"] == ["sensor.grill_probe"]
    assert r["devices"][0] == {"id": "HA:SENSOR.GRILL_PROBE", "name": "Grill",
                               "protocol": "home_assistant", "targets": {}}
    assert settings.gadgets_enabled is True
    assert settings.gadget_ha_enabled is True
    # Re-adding updates, never duplicates.
    r = client.post("/gadgets/ha-entities",
                    json={"entity_id": "sensor.grill_probe", "name": "Smoker"}).json()
    assert r["entities"] == ["sensor.grill_probe"]
    assert len(r["devices"]) == 1 and r["devices"][0]["name"] == "Smoker"


def test_add_ha_entity_rejects_bad_id(client):
    r = client.post("/gadgets/ha-entities", json={"entity_id": "not an id"}).json()
    assert r["ok"] is False and "entity id" in r["error"]


def test_remove_ha_entity_drops_device_too(client):
    client.post("/gadgets/ha-entities", json={"entity_id": "sensor.grill"})
    r = client.request("DELETE", "/gadgets/ha-entities/sensor.grill").json()
    assert r["ok"] is True and r["entities"] == [] and r["devices"] == []


def test_ha_entities_listing_unconfigured(client):
    d = client.get("/gadgets/ha-entities").json()
    assert d == {"ok": True, "connected": False, "entities": [],
                 "configured": []}


# -- Save payload plumbing ------------------------------------------------------

def test_gadget_toggles_are_saveable_setup_fields():
    from app.routers.setup import SetupPayload
    from app.config import _SAVEABLE

    for field in ("gadgets_enabled", "gadget_ha_enabled"):
        assert field in SetupPayload.model_fields
        assert field in _SAVEABLE
    assert "gadget_ha_entities" in _SAVEABLE
    # The pane save posts only its own toggles; nothing else rides along.
    data = SetupPayload(gadgets_enabled=True,
                        gadget_ha_enabled=False).model_dump(exclude_unset=True)
    assert data == {"gadgets_enabled": True, "gadget_ha_enabled": False}


# -- Rendered pane per mode ------------------------------------------------------

def _render_setup(client, monkeypatch, *, mode: str, is_pi: bool) -> str:
    monkeypatch.setattr(settings, "deployment_mode", mode, raising=False)
    with patch.object(type(settings), "is_configured", lambda self: True), \
         patch("app.routers.setup.is_raspberry_pi", return_value=is_pi), \
         patch("app.templating.is_raspberry_pi", return_value=is_pi):
        r = client.get("/setup")
    assert r.status_code == 200
    return r.text


def test_pane_renders_on_server_with_manual_guidance(client, monkeypatch):
    html = _render_setup(client, monkeypatch, mode="server", is_pi=False)
    assert 'id="pane-gadgets"' in html
    assert 'data-bs-target="#pane-gadgets"' in html
    assert "Bluetooth radio" in html
    assert "foodassistant-gadgets-setup" in html
    assert "gadgets/README.md" in html
    assert "From Home Assistant" in html
    # No one-click install off a Pi: the app cannot install a host service.
    assert "installGadgetsReader" not in html


def test_pane_renders_on_pi_with_one_click_setup(client, monkeypatch):
    for mode in ("pi_hosted", "pi_remote"):
        html = _render_setup(client, monkeypatch, mode=mode, is_pi=True)
        assert 'id="pane-gadgets"' in html
        assert "installGadgetsReader" in html
        assert "Set up for me" in html
        # Honest about the manual paths too.
        assert "foodassistant-gadgets-setup" in html
        assert "From Home Assistant" in html


# -- Host bridge route -----------------------------------------------------------

def test_bridge_has_gadgets_setup_route():
    import importlib.machinery
    import importlib.util
    import inspect

    bridge_path = REPO / "scripts" / "image-build" / "foodassistant-host-bridge"
    spec = importlib.util.spec_from_loader(
        "fa_bridge_gadgets",
        importlib.machinery.SourceFileLoader("fa_bridge_gadgets", str(bridge_path)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    handler = next(obj for name, obj in vars(mod).items()
                   if inspect.isclass(obj) and hasattr(obj, "_print_setup"))
    assert hasattr(handler, "_gadgets_setup")
    src = inspect.getsource(handler.do_POST)
    assert '"/gadgets-setup"' in src
    # The handler runs the same helper the installer's opt-in path runs.
    assert "foodassistant-gadgets-setup" in inspect.getsource(handler._gadgets_setup)
