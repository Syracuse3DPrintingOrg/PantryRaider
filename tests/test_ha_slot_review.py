"""The older HA 1 to HA 5 keys can be confirmed or removed in Settings, Start Page.

The 0.21 key migration lets an older HA 1 to HA 5 key that unlocks, opens or
disarms keep working, marks one whose call is not valid as off, and lists both
in an inbox item that says to confirm or remove each one in Settings, Start
Page. Those keys have had no editor there for a while, so the owner could do
neither. The Start Page pane now lists every such key with Allow, Stop and
Remove, and the inbox item points there.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.services import ha_allowlist, ha_policy_migration

SERVICE = Path(__file__).resolve().parents[1] / "service"

SLOTS = [
    {"entity_id": "light.kitchen", "label": "Kitchen"},
    {"entity_id": "cover.garage", "service": "cover.open_cover", "label": "Garage",
     "allow_elevated": True, "elevated_grandfathered": True},
    {"entity_id": "all", "service": "light.turn_off", "label": "Everything",
     "disabled_reason": "invalid"},
    {"entity_id": "lock.front_door", "service": "lock.unlock"},
]


def test_the_review_lists_only_the_keys_that_need_a_decision():
    review = ha_allowlist.slot_review([dict(s) for s in SLOTS])
    assert [(r["position"], r["name"], r["verdict"], r["allowed"]) for r in review] == [
        (1, "Garage", "elevated", True),
        (2, "Everything", "invalid", False),
        (3, "HA 4", "elevated", False),
    ]
    assert review[0]["entity_id"] == "cover.garage"
    assert review[0]["service"] == "cover.open_cover"
    assert review[0]["grandfathered"] is True


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_slots", [dict(s) for s in SLOTS],
                        raising=False)
    monkeypatch.setattr(settings, "ha_key_policy_version", 1, raising=False)
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True, raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _slot(position):
    return [s for s in settings.streamdeck_ha_slots if isinstance(s, dict)][position]


def _permitted(position):
    from app.services import ha_policy
    slot = _slot(position)
    return ha_policy.permitted(slot["entity_id"], slot["service"], slot, "screen",
                               enforce=True)[0]


def test_stop_unticks_a_key_allowed_during_the_update(client):
    assert _permitted(1) is True
    r = client.post("/setup/ha-slots/1", json={"action": "stop"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert _slot(1)["allow_elevated"] is False
    assert "elevated_grandfathered" not in _slot(1)
    assert _permitted(1) is False
    # It stays listed, so it can be allowed again.
    assert [x["position"] for x in r.json()["review"]] == [1, 2, 3]
    r = client.post("/setup/ha-slots/1", json={"action": "allow"})
    assert r.status_code == 200 and _permitted(1) is True
    assert "elevated_grandfathered" not in _slot(1)


def test_remove_clears_a_key_that_is_not_valid(client):
    assert client.post("/setup/ha-slots/2", json={"action": "allow"}).status_code == 400
    r = client.post("/setup/ha-slots/2", json={"action": "remove"})
    assert r.status_code == 200
    slot = _slot(2)
    assert slot["entity_id"] == "" and slot.get("service", "") == ""
    assert "disabled_reason" not in slot and slot["label"] == "Everything"
    assert [x["position"] for x in r.json()["review"]] == [1, 3]


def test_a_key_that_needs_no_decision_or_does_not_exist_is_refused(client):
    assert client.post("/setup/ha-slots/0", json={"action": "remove"}).status_code == 404
    assert client.post("/setup/ha-slots/9", json={"action": "remove"}).status_code == 404
    assert client.post("/setup/ha-slots/1", json={"action": "delete"}).status_code == 400
    assert _slot(0) == SLOTS[0]


def test_the_start_page_pane_lists_them(client):
    page = client.get("/setup").text
    assert 'id="ha-slot-review"' in page
    assert "Garage" in page and "cover.garage" in page and "lock.front_door" in page
    assert "Everything" in page


def test_the_route_is_part_of_settings():
    from app.services import access
    assert access.route_family("POST", "/setup/ha-slots/1") == "admin"


def test_the_inbox_item_points_at_the_pane():
    for body in (ha_policy_migration.BODY_GRANDFATHERED, ha_policy_migration.BODY_INVALID):
        assert "Settings, Start Page" in body
    assert "Thermometers & Sensors" in ha_policy_migration.BODY_BLE
