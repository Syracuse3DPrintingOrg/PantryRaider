"""The install tag Settings shows for pinning a Cub (FoodAssistant-ux9hs).

The docs tell people to pin the install a Cub listens to with pr_install_tag,
so the tag Settings, Devices shows has to be the exact bytes the broadcast
carries: advertiser.install_tag over the device id the broadcaster uses. On a
satellite that is the main server's device id, mirrored down on each sync.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.routers import cub as cub_router
from foodassistant_gadgets import advertiser as adv

DEVICE = "aabbccdd00112233"


def test_the_tag_matches_what_the_broadcast_carries(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "device_id", DEVICE, raising=False)
    info = cub_router.broadcast_install_tag()
    assert info["install_tag"] == adv.install_tag(DEVICE).hex()
    assert len(info["install_tag"]) == 8
    assert info["source"] == "this"
    # And it is the same four bytes the packed packet ends with.
    packet = adv.pack_status({"view": "clock", "timers": [], "probes": [],
                              "alerts": [], "expiring": {}, "counts": {}},
                             DEVICE, 1, now=1750000000)
    assert adv.unpack_status(packet)["install_tag"] == info["install_tag"]


def test_a_satellite_shows_the_main_servers_tag(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    monkeypatch.setattr(settings, "device_id", "satellite-own-id", raising=False)
    monkeypatch.setattr(settings, "upstream_gadget_config",
                        {"device_id": DEVICE, "cub_ble_advertise": True},
                        raising=False)
    info = cub_router.broadcast_install_tag()
    assert info["install_tag"] == adv.install_tag(DEVICE).hex()
    assert info["source"] == "server"
    assert info["broadcast"] is True


def test_a_satellite_before_its_first_sync_has_no_tag_to_show(monkeypatch):
    """Guessing from the satellite's own id would show a tag the broadcast
    stops carrying once the first sync brings the server's id down, and a Cub
    pinned to it would then go blank."""
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    monkeypatch.setattr(settings, "device_id", "satellite-own-id", raising=False)
    monkeypatch.setattr(settings, "upstream_gadget_config", {}, raising=False)
    info = cub_router.broadcast_install_tag()
    assert info["install_tag"] == ""
    assert info["source"] == "server"


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "api_key", "", raising=False)
    monkeypatch.setattr(settings, "extra_api_keys", [], raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "device_id", DEVICE, raising=False)
    monkeypatch.setattr("app.hardware.is_raspberry_pi", lambda: False)
    with TestClient(app) as c:
        yield c


def test_the_endpoint_returns_the_tag_and_broadcast_state(client, monkeypatch):
    monkeypatch.setattr(settings, "cub_ble_advertise", False, raising=False)
    r = client.get("/cub/install-tag")
    assert r.status_code == 200
    assert r.json() == {"install_tag": adv.install_tag(DEVICE).hex(),
                        "source": "this", "broadcast": False}


def test_the_endpoint_requires_auth(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_password", "hunter2", raising=False)
    monkeypatch.setattr(settings, "api_key", "test-key", raising=False)
    r = client.get("/cub/install-tag", follow_redirects=False)
    assert r.status_code in (302, 303, 307, 401, 403)
    assert "install_tag" not in r.text


def test_settings_shows_the_tag_next_to_the_broadcast_setting():
    """The pane fills the tag from the endpoint, right under the toggle."""
    from pathlib import Path
    pane = (Path(__file__).resolve().parents[1] / "service" / "app" / "templates"
            / "setup" / "_pane_devices.html").read_text()
    toggle = pane.index('id="cub_ble_advertise"')
    tag = pane.index('id="cub-install-tag"')
    assert toggle < tag < pane.index("saveCubSettings(this)")
    assert 'fetch("cub/install-tag"' in pane
