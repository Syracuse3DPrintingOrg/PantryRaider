"""Route + template smoke tests.

Drive the real FastAPI app via TestClient and assert every GET UI page returns
200 and its Jinja template renders. Grocy/Mealie are mocked so no network or
Docker is needed, and the setup-save flow is exercised end to end.
"""
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Jinja2Templates is configured with the relative path "app/templates", so the
# app must be imported (and run) with the working directory set to service/.
_SERVICE_DIR = Path(__file__).parent.parent / "service"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    cwd = os.getcwd()
    os.chdir(_SERVICE_DIR)
    try:
        from app.config import settings

        # Point data_dir at a temp dir BEFORE importing app.main: database.py
        # runs os.makedirs(settings.data_dir) at import time, which would fail
        # on the default /app/data outside the container (e.g. CI).
        data_dir = tmp_path_factory.mktemp("data")
        settings.data_dir = str(data_dir)

        from app.main import app

        # Make the app think it is fully configured so the setup-redirect
        # middleware is a no-op, and leave auth_password empty so the auth
        # middleware is a no-op too.
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


@pytest.fixture(autouse=True)
def _mock_services(monkeypatch):
    """Stub the Grocy/Mealie network calls used while rendering pages."""
    from app.services.grocy import GrocyClient

    async def _expiring(self, days=7):
        return []

    monkeypatch.setattr(GrocyClient, "get_expiring", _expiring)


GET_PAGES = [
    "/ui/",
    "/ui/inventory",
    "/ui/add",
    "/ui/pending",
    "/ui/recipes",
    "/ui/cook",
    "/ui/mealplan",
    "/ui/shopping",
    "/ui/expiring",
    "/ui/defaults",
    "/setup",
]


@pytest.mark.parametrize("path", GET_PAGES)
def test_get_page_renders(client, path):
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"
    assert "text/html" in r.headers["content-type"]
    # A rendered Jinja page, not an error stub.
    assert "<html" in r.text.lower() or "<!doctype" in r.text.lower()


def test_root_redirects_to_ui(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (303, 307)
    assert r.headers["location"].endswith("/ui/")


def test_system_health_off_pi_is_clean(client, monkeypatch):
    # On a non-Pi host there is no bridge to probe, so the navbar indicator's
    # endpoint must return a clean empty-warnings shape, never an error.
    import app.routers.setup as setup_router

    monkeypatch.setattr(setup_router, "is_raspberry_pi", lambda: False)
    r = client.get("/setup/system/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["warnings"] == []


def test_health_ok_shape(client):
    # When configured, /health calls the provider + Grocy health checks; mock both.
    from app.services.grocy import GrocyClient
    import app.dependencies as deps

    async def _ok(self):
        return True

    GrocyClient.health_check = _ok

    class _Provider:
        async def health_check(self):
            return True

    deps.get_vision_provider.cache_clear()
    deps._build_provider = lambda *a, **k: _Provider()  # noqa: ARG005
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_setup_save_round_trips(client):
    from app.config import settings

    payload = {
        "vision_provider": "gemini",
        "gemini_api_key": "round-trip-key",
        "grocy_base_url": "http://grocy.example",
        "grocy_api_key": "round-trip-grocy",
        "perishable_days": 9,
        "expiring_soon_days": 3,
        "staple_items": "miso, nori",
    }
    r = client.post("/setup/save", json=payload)
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    # Applied to the live settings object...
    assert settings.perishable_days == 9
    assert settings.expiring_soon_days == 3
    assert settings.staple_items == "miso, nori"

    # ...and persisted to settings.json.
    import json
    saved = json.loads((Path(settings.data_dir) / "settings.json").read_text())
    assert saved["perishable_days"] == 9
    assert saved["staple_items"] == "miso, nori"
    assert saved["gemini_api_key"] == "round-trip-key"


def test_setup_save_persists_streamdeck_fields(client):
    # Regression: these were dropped by SetupPayload (unknown fields ignored), so
    # idle timeouts, key overrides, and Stream Deck weather never persisted
    # through /save (FoodAssistant-bra).
    from app.config import settings

    payload = {
        "streamdeck_idle_timeout": 12,
        "display_idle_timeout": 6,
        "streamdeck_weather_location": "Portland",
        "streamdeck_weather_units": "c",
        "streamdeck_key_overrides": [{"slot": 0, "type": "weather", "location": "Portland"}],
    }
    r = client.post("/setup/save", json=payload)
    assert r.status_code == 200
    assert settings.streamdeck_idle_timeout == 12
    assert settings.display_idle_timeout == 6
    assert settings.streamdeck_weather_location == "Portland"
    assert settings.streamdeck_weather_units == "c"
    assert settings.streamdeck_key_overrides == [
        {"slot": 0, "type": "weather", "location": "Portland"}
    ]


def test_setup_save_subset_leaves_others_untouched(client):
    # Per-section Save buttons post only their own fields. The server uses
    # model_dump(exclude_unset=True), so an unrelated stored setting must keep
    # its value when a different section saves (FoodAssistant-53ik).
    from app.config import settings

    # Seed a known unrelated value via a prior full-ish save.
    client.post("/setup/save", json={"staple_items": "anchovy, capers"})
    assert settings.staple_items == "anchovy, capers"

    # Save only the Grocy section.
    r = client.post("/setup/save", json={"grocy_base_url": "http://grocy.subset"})
    assert r.status_code == 200
    assert settings.grocy_base_url == "http://grocy.subset"

    # The unrelated staple_items value survives in memory and on disk.
    assert settings.staple_items == "anchovy, capers"
    import json
    saved = json.loads((Path(settings.data_dir) / "settings.json").read_text())
    assert saved["grocy_base_url"] == "http://grocy.subset"
    assert saved["staple_items"] == "anchovy, capers"


def test_streamdeck_profiles_crud(client):
    """Profile save/list/delete roundtrip (FoodAssistant-aqa)."""
    # Clear any profiles left by other tests sharing this DB
    from app.database import SessionLocal
    from app.models.db_models import StreamDeckProfile
    db = SessionLocal()
    try:
        db.query(StreamDeckProfile).delete()
        db.commit()
    finally:
        db.close()

    r = client.get("/setup/streamdeck/profiles")
    assert r.status_code == 200
    assert r.json()["profiles"] == []

    # Save a profile
    payload = {"name": "kitchen", "deck_size": 15, "key_overrides": [{"slot": 0, "type": "expiring"}]}
    r = client.post("/setup/streamdeck/profiles", json=payload)
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["profile"]["name"] == "kitchen"

    # List shows it
    r = client.get("/setup/streamdeck/profiles")
    assert r.status_code == 200
    profiles = r.json()["profiles"]
    assert len(profiles) == 1
    assert profiles[0]["name"] == "kitchen"
    assert profiles[0]["deck_size"] == 15

    # Update (same name)
    payload2 = {"name": "kitchen", "deck_size": 15, "key_overrides": []}
    r = client.post("/setup/streamdeck/profiles", json=payload2)
    assert r.status_code == 200
    assert r.json()["profile"]["key_overrides"] == []

    # Delete
    r = client.delete("/setup/streamdeck/profiles/kitchen")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # Gone
    r = client.get("/setup/streamdeck/profiles")
    assert r.json()["profiles"] == []


def test_streamdeck_profiles_validation(client):
    """Profile endpoint rejects invalid deck_size and blank names."""
    r = client.post("/setup/streamdeck/profiles", json={"name": "", "deck_size": 15, "key_overrides": []})
    assert r.status_code == 400

    r = client.post("/setup/streamdeck/profiles", json={"name": "bad", "deck_size": 7, "key_overrides": []})
    assert r.status_code == 400

    r = client.delete("/setup/streamdeck/profiles/nonexistent")
    assert r.status_code == 404


def test_kiosk_activity_endpoints_off_pi(client):
    """Off a Pi, the kiosk activity proxy is a graceful no-op (FoodAssistant-otiy)."""
    r = client.post("/setup/kiosk/activity")
    assert r.status_code == 200
    assert r.json()["woke"] is False

    r = client.get("/setup/kiosk/activity")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["display_blanked"] is False


def test_display_blank_wake_off_pi(client):
    """Display blank/wake report not-available off a Pi rather than erroring."""
    r = client.post("/setup/display/blank")
    assert r.status_code == 200
    assert r.json()["ok"] is False
    r = client.post("/setup/display/wake")
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_floating_nav_settings_persist_and_validate(client):
    """Floating nav position + autohide round-trip; bad position is rejected
    (FoodAssistant-bzuu)."""
    from app.config import settings

    r = client.post("/setup/save", json={
        "floating_nav_position": "bottom-right",
        "floating_nav_autohide_streamdeck": True,
    })
    assert r.status_code == 200
    assert settings.floating_nav_position == "bottom-right"
    assert settings.floating_nav_autohide_streamdeck is True

    # An invalid position is dropped, leaving the stored value untouched.
    r = client.post("/setup/save", json={"floating_nav_position": "middle"})
    assert r.status_code == 200
    assert settings.floating_nav_position == "bottom-right"


def test_floating_nav_orientation_persists_and_validates(client):
    """Floating nav orientation round-trips; a bad value is rejected
    (FoodAssistant-76mw)."""
    from app.config import settings

    r = client.post("/setup/save", json={"floating_nav_orientation": "horizontal"})
    assert r.status_code == 200
    assert settings.floating_nav_orientation == "horizontal"

    # An invalid orientation is dropped, leaving the stored value untouched.
    r = client.post("/setup/save", json={"floating_nav_orientation": "diagonal"})
    assert r.status_code == 200
    assert settings.floating_nav_orientation == "horizontal"


def test_floating_nav_renders_on_page(client):
    """The floating nav container renders with its data attributes so the JS
    can place it."""
    from app.config import settings
    settings.floating_nav_position = "top-right"
    r = client.get("/ui/")
    assert r.status_code == 200
    assert 'id="floatNav"' in r.text
    assert 'data-position="top-right"' in r.text


def test_settings_menu_has_logical_groups(client):
    """The revamped settings menu renders its section headers and the default
    non-satellite Services pills (FoodAssistant-y9nd)."""
    r = client.get("/setup")
    assert r.status_code == 200
    for header in ("Services", "App", "Devices &amp; Hardware", "System"):
        assert header in r.text, f"missing menu group: {header}"
    # Non-satellite: the backend service pills are present.
    assert 'data-bs-target="#pane-inventory"' in r.text
    assert 'data-bs-target="#pane-ai"' in r.text
    # Satellite-only Main Server pill is not shown on a normal server.
    assert 'data-bs-target="#pane-upstream"' not in r.text
