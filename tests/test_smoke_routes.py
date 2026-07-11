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
    "/ui/current-recipe",
    "/ui/mealplan",
    "/ui/shopping",
    "/ui/expiring",
    "/ui/defaults",
    "/ui/convert",
    "/ui/scanner-setup",
    "/ui/kitchen-guide",
    "/ui/timers",
    "/ui/camera",
    "/ui/weather",
    "/setup",
]


def test_convert_page_has_known_conversion(client):
    r = client.get("/ui/convert")
    assert r.status_code == 200
    # A stable, accurate conversion that the cheat sheet must list.
    assert "240 ml" in r.text  # 1 cup


def test_setup_redirect_preserves_kiosk_flag(client, monkeypatch):
    """An unconfigured appliance display loads /ui/?kiosk=1; the setup redirect
    must carry kiosk=1 onto /setup so the display latches kiosk mode and polls
    for the navigate-home hand-off (FoodAssistant-joj1)."""
    from app.config import settings
    monkeypatch.setattr(settings, "grocy_base_url", "")
    assert not settings.is_configured()
    r = client.get("/ui/?kiosk=1", follow_redirects=False)
    assert r.status_code in (303, 307)
    assert r.headers["location"].endswith("/setup?kiosk=1")
    # A normal browser (no kiosk flag) still lands on a plain /setup.
    r2 = client.get("/ui/", follow_redirects=False)
    assert r2.headers["location"].endswith("/setup")


def test_touch_calibration_done_flag(client):
    """After a calibration applies, the remote UI's done poll fires once so the
    Cancel control clears (FoodAssistant-mox4). The flag is one-shot."""
    from app.routers import setup as s
    s._CAL_DONE_FLAG.write_text("1")
    first = client.get("/setup/calibrate/touch/done/pending").json()
    assert first["pending"] is True
    second = client.get("/setup/calibrate/touch/done/pending").json()
    assert second["pending"] is False


def test_touch_calibration_remote_cancel_flag(client):
    """The remote Cancel sets a one-shot flag the Pi calibration page polls
    (FoodAssistant-mox4): pending is true once, then cleared."""
    r = client.post("/setup/calibrate/touch/cancel")
    assert r.status_code == 200 and r.json().get("ok")
    first = client.get("/setup/calibrate/touch/cancel/pending").json()
    assert first["pending"] is True
    second = client.get("/setup/calibrate/touch/cancel/pending").json()
    assert second["pending"] is False


@pytest.mark.anyio
async def test_touch_sse_reads_events_in_pure_python():
    """The calibration stream reads the input device directly (no evtest):
    feed synthetic input_event records through a pipe and confirm it emits a
    ranges event then a tap on BTN_TOUCH release (FoodAssistant-mox4)."""
    import json
    import os
    import struct
    from app.routers import setup as s

    r, w = os.pipe()

    def _ev(etype, code, value):
        return struct.pack(s._INPUT_EVENT_FORMAT, 0, 0, etype, code, value)

    os.write(w, _ev(s._EV_ABS, s._ABS_X, 123)
             + _ev(s._EV_ABS, s._ABS_Y, 456)
             + _ev(s._EV_KEY, s._BTN_TOUCH, 0))

    real_open = os.open

    def _fake_open(path, flags, *a):
        return r if path == "FAKEDEV" else real_open(path, flags, *a)

    s.os.open = _fake_open
    try:
        msgs = []
        gen = s._evtest_sse("FAKEDEV")
        async for chunk in gen:
            msgs.append(json.loads(chunk.removeprefix("data: ").strip()))
            if len(msgs) >= 2:
                await gen.aclose()
                break
    finally:
        s.os.open = real_open
        os.close(w)

    assert msgs[0]["type"] == "ranges"            # ioctl falls back on a pipe
    assert msgs[1] == {"type": "tap", "x": 123, "y": 456}


def test_touchscreen_detection_by_name_and_capability():
    """_looks_like_touchscreen matches named controllers and unnamed direct
    pointers, and rejects a mouse (FoodAssistant-mox4)."""
    from app.routers.setup import _looks_like_touchscreen, _block_handler

    named = 'N: Name="ADS7846 Touchscreen"\nH: Handlers=mouse0 event0\nB: ABS=1000003'
    assert _looks_like_touchscreen(named)
    assert _block_handler(named) == "/dev/input/event0"

    # Capacitive panel whose name lacks the word "touch": caught by the known
    # controller hint and by INPUT_PROP_DIRECT (PROP bit 1) + ABS.
    capacitive = 'N: Name="generic ft5x06"\nH: Handlers=event4\nB: PROP=2\nB: EV=b\nB: ABS=2658000 3'
    assert _looks_like_touchscreen(capacitive)
    assert _block_handler(capacitive) == "/dev/input/event4"

    # Unnamed direct absolute pointer (no known hint) still detected via PROP.
    unnamed = 'N: Name="Vendor 0416 Device"\nH: Handlers=event6\nB: PROP=2\nB: ABS=3'
    assert _looks_like_touchscreen(unnamed)

    # A mouse is indirect (PROP=0) with relative axes: must NOT match.
    mouse = 'N: Name="Logitech USB Mouse"\nH: Handlers=mouse1 event5\nB: PROP=0\nB: EV=17\nB: REL=903'
    assert not _looks_like_touchscreen(mouse)


def test_setup_page_has_rotation_mode_guard(client):
    """Standalone pages (setup/login/pin) must carry data-rotation-mode like
    base.html, or kiosk-display.js applies a CSS rotation on top of the
    compositor's, double-rotating /setup on a Pi (FoodAssistant-anou)."""
    page = client.get("/setup").text
    assert "data-rotation-mode=" in page
    assert "data-display-rotation=" in page


def test_kitchen_guide_has_safe_temps(client):
    r = client.get("/ui/kitchen-guide")
    assert r.status_code == 200
    # Poultry safe minimum is a stable USDA reference the page must list.
    assert "165&deg;F / 74&deg;C" in r.text


def test_use_it_up_returns_static_tips(client):
    """Use-it-up always returns static tips, even with no inventory or AI
    (FoodAssistant-m6wq). Grocy is unconfigured in tests, so the item list is
    empty and only the tips come back."""
    r = client.post("/mealie/use-it-up", json={"days": 7})
    assert r.status_code == 200
    d = r.json()
    assert isinstance(d.get("tips"), list) and len(d["tips"]) > 0
    assert "suggestions" in d


def test_use_it_up_never_touches_stock(client, monkeypatch):
    """Use-it-up is read-only: it suggests recipes and tips for expiring items
    and must never consume anything (FoodAssistant-w0kh)."""
    from app.services.grocy import GrocyClient

    async def _expiring(self, days=7):
        return [{"name": "Milk", "product_id": 1, "amount": 1}]

    calls = []

    async def _consume(self, product_id, amount=1.0):
        calls.append(product_id)
        return {}

    monkeypatch.setattr(GrocyClient, "get_expiring", _expiring)
    monkeypatch.setattr(GrocyClient, "consume_stock", _consume)
    r = client.post("/mealie/use-it-up", json={"days": 7})
    assert r.status_code == 200
    assert calls == [], "use-it-up must never consume stock"
    d = r.json()
    assert d["items"] == ["Milk"]
    assert len(d["tips"]) > 0


def test_expiring_consume_confirms_and_names_the_item(client, monkeypatch):
    """The per-row checkmark on Expiring removes the whole stock amount, so it
    must ask before doing it and the toast must say which product went
    (FoodAssistant-w0kh)."""
    from app.services.grocy import GrocyClient

    async def _expiring(self, days=7):
        return [{
            "product_id": 42,
            "amount": 2,
            "days_remaining": 1,
            "best_before_date": "2026-07-04",
            "product": {"name": "Greek Yogurt"},
        }]

    monkeypatch.setattr(GrocyClient, "get_expiring", _expiring)
    r = client.get("/ui/expiring")
    assert r.status_code == 200
    assert "confirm(" in r.text
    assert "Mark all Greek Yogurt as consumed" in r.text
    # The form posts the product name so the redirect toast can include it.
    assert 'name="name" value="Greek Yogurt"' in r.text


def test_consume_redirect_names_the_product(client, monkeypatch):
    from app.services.grocy import GrocyClient

    consumed = []

    async def _consume(self, product_id, amount=1.0):
        consumed.append((product_id, amount))
        return {}

    monkeypatch.setattr(GrocyClient, "consume_stock", _consume)
    r = client.post("/ui/consume/42", data={"amount": "2", "name": "Greek Yogurt"},
                    follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert consumed == [(42, 2.0)]
    assert "Greek%20Yogurt%20marked%20as%20consumed" in r.headers["location"]


def test_expiring_use_it_up_button_copy(client):
    """The header button offers ideas, not an action on stock; its copy and the
    result cards must say so (FoodAssistant-w0kh)."""
    r = client.get("/ui/expiring")
    assert r.status_code == 200
    assert "Use-it-up ideas" in r.text
    assert "never changes your stock" in r.text


def test_cook_page_generate_deep_link(client, monkeypatch):
    """/ui/cook?generate=<dish> opens the AI preview for that dish, so the
    Expiring page's Cook this buttons land on a real outcome."""
    from app.config import settings

    monkeypatch.setattr(settings, "mealie_base_url", "http://mealie.test", raising=False)
    monkeypatch.setattr(settings, "mealie_api_key", "test-key", raising=False)
    r = client.get("/ui/cook")
    assert r.status_code == 200
    assert ".get('generate')" in r.text
    assert "openAiPreview(dish.trim())" in r.text


def test_timers_page_has_empty_state(client):
    r = client.get("/ui/timers")
    assert r.status_code == 200
    assert "No timers running" in r.text


@pytest.mark.parametrize("path", GET_PAGES)
def test_get_page_renders(client, path):
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"
    assert "text/html" in r.headers["content-type"]
    # A rendered Jinja page, not an error stub.
    assert "<html" in r.text.lower() or "<!doctype" in r.text.lower()


def test_camera_snapshot_proxy_adds_bearer(client, monkeypatch):
    # An HA camera is fetched server-side with a bearer header and handed back as
    # an image, so the browser never needs the token (which HA rejects in a query).
    from app.config import settings

    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "http://ha.local:8123", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "tok", raising=False)
    monkeypatch.setattr(settings, "streamdeck_cameras",
                        [{"name": "Door", "ha_entity": "camera.door"}], raising=False)

    seen = {}

    class _Resp:
        status_code = 200
        content = b"\xff\xd8jpegbytes"
        headers = {"content-type": "image/jpeg"}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, headers=None, **k):
            seen["url"] = url
            seen["headers"] = headers
            return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    r = client.get("/ui/camera/0/snapshot")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/jpeg")
    assert r.content == b"\xff\xd8jpegbytes"
    assert seen["url"] == "http://ha.local:8123/api/camera_proxy/camera.door"
    assert seen["headers"] == {"Authorization": "Bearer tok"}


def test_camera_snapshot_proxy_unknown_index(client):
    r = client.get("/ui/camera/999/snapshot")
    assert r.status_code == 404


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


def test_setup_save_persists_cameras(client):
    """Cameras round-trip through /save and land on settings (FoodAssistant-oewn)."""
    from app.config import settings

    cams = [{"name": "Front", "stream_url": "http://x/stream", "snapshot_url": "http://x/snap"}]
    r = client.post("/setup/save", json={"streamdeck_cameras": cams})
    assert r.status_code == 200
    assert settings.streamdeck_cameras == cams


def test_streamdeck_style_persists_and_validates(client):
    """Stream Deck key style + icon colour round-trip; bad values are dropped
    (FoodAssistant-fygv)."""
    from app.config import settings

    r = client.post("/setup/save", json={
        "streamdeck_key_style": "glass",
        "streamdeck_icon_color": "mono",
    })
    assert r.status_code == 200
    assert settings.streamdeck_key_style == "glass"
    assert settings.streamdeck_icon_color == "mono"

    # Unknown values are dropped, leaving the stored ones untouched.
    r = client.post("/setup/save", json={
        "streamdeck_key_style": "neon", "streamdeck_icon_color": "rainbow",
    })
    assert r.status_code == 200
    assert settings.streamdeck_key_style == "glass"
    assert settings.streamdeck_icon_color == "mono"


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


def test_custom_theme_persists_and_renders(client):
    """Custom theme builder (FoodAssistant-hatd).

    Saving the palette persists every custom_theme_* field, rendering a page
    with ui_theme="custom" emits the swatches as inline Bootstrap CSS variables,
    and theme_info/context reports the chosen light/dark base mode.
    """
    import json
    from app.config import settings, theme_info

    palette = {
        "ui_theme": "custom",
        "custom_theme_base": "light",
        "custom_theme_primary": "#ff5733",
        "custom_theme_accent": "#33ff57",
        "custom_theme_bg": "#fafafa",
        "custom_theme_surface": "#eeeeee",
        "custom_theme_text": "#101010",
    }
    r = client.post("/setup/save", json=palette)
    assert r.status_code == 200

    # Applied to the live settings object and persisted to disk.
    for k, v in palette.items():
        assert getattr(settings, k) == v
    saved = json.loads((Path(settings.data_dir) / "settings.json").read_text())
    for k, v in palette.items():
        assert saved[k] == v

    # theme_info reports the chosen base mode for the custom theme.
    assert theme_info("custom")["mode"] == "light"

    # A rendered page carries data-bs-theme=light and the inline custom vars.
    page = client.get("/ui/").text
    assert 'data-bs-theme="light"' in page
    assert "--bs-primary: #ff5733;" in page
    assert "--bs-body-bg: #fafafa;" in page
    assert "--bs-body-color: #101010;" in page
    assert "--bs-tertiary-bg: #eeeeee;" in page

    # Restore the default theme so later tests render the dark default.
    client.post("/setup/save", json={"ui_theme": "dark"})


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
    """Docked nav edge + autohide round-trip; a bad edge is rejected
    (FoodAssistant-bzuu, -i181)."""
    from app.config import settings

    r = client.post("/setup/save", json={
        "floating_nav_position": "bottom",
        "floating_nav_autohide_streamdeck": True,
    })
    assert r.status_code == 200
    assert settings.floating_nav_position == "bottom"
    assert settings.floating_nav_autohide_streamdeck is True

    # An invalid edge is dropped, leaving the stored value untouched.
    r = client.post("/setup/save", json={"floating_nav_position": "middle"})
    assert r.status_code == 200
    assert settings.floating_nav_position == "bottom"


def test_ai_options_hidden_when_no_ai(client):
    """AI-only affordances disappear across feature pages when no AI provider is
    configured, and reappear when one is (FoodAssistant-9vgx)."""
    from app.config import settings

    saved = (settings.vision_provider, settings.gemini_api_key)
    try:
        settings.vision_provider = "gemini"
        settings.gemini_api_key = ""  # no key -> ai_configured() is False
        assert settings.ai_configured() is False
        # add.html's Photo/Receipt tab gates purely on ai_configured (no Mealie
        # dependency), so it is a clean signal in both directions.
        assert "Photo / Receipt" not in client.get("/ui/add").text

        settings.gemini_api_key = "back-on"  # ai_configured() True again
        assert settings.ai_configured() is True
        assert "Photo / Receipt" in client.get("/ui/add").text
    finally:
        settings.vision_provider, settings.gemini_api_key = saved


def test_floating_nav_renders_on_page(client):
    """The floating nav container renders with its data attributes so the JS
    can place it."""
    from app.config import settings
    settings.floating_nav_position = "right"
    r = client.get("/ui/")
    assert r.status_code == 200
    assert 'id="floatNav"' in r.text
    assert 'data-position="right"' in r.text


def test_small_screen_nav_markup_present(client):
    """Phone-visible primary nav (FoodAssistant-97al).

    All page tabs live in the primary nav, which sits outside the Bootstrap
    collapse and scrolls sideways on a narrow panel, so the main menu stays
    visible instead of folding behind the hamburger. Assert the server-rendered
    hooks the CSS acts on are present.
    """
    page = client.get("/ui/").text
    # The primary nav is the always-visible scrollable tab row.
    assert "nav-primary" in page
    # The hamburger collapse now holds only the utility controls.
    assert 'id="nav"' in page


def test_kiosk_auto_enable_hooks_present(client):
    """Pi display auto-enable (FoodAssistant-h437).

    The page exposes the is_pi flag as data-is-pi on <html> and loads the
    auto-enable script; the script itself respects an explicit user choice and is
    a no-op off-Pi (JS-side, not unit-tested here).
    """
    page = client.get("/ui/").text
    assert "data-is-pi=" in page
    assert "kiosk-auto.js" in page


def test_current_recipe_endpoints(client):
    """Set / get / scale / clear the active recipe over HTTP (FoodAssistant-879b)."""
    # Empty to start (other tests may have left state, so clear first).
    client.delete("/current-recipe")
    r = client.get("/current-recipe")
    assert r.status_code == 200
    assert r.json()["recipe"] is None

    r = client.post("/current-recipe", json={
        "title": "Stew", "source": "ai", "servings": 4,
        "ingredients": [{"name": "Carrot", "quantity": 3, "unit": "ea"}],
        "steps": ["Chop", "Simmer"],
    })
    assert r.status_code == 200
    assert r.json()["recipe"]["title"] == "Stew"

    r = client.post("/current-recipe/scale", json={"factor": 2})
    assert r.status_code == 200
    body = r.json()["recipe"]
    assert body["servings_scale"] == 2
    assert body["ingredients"][0]["scaled_quantity"] == 6.0

    r = client.delete("/current-recipe")
    assert r.status_code == 200
    assert client.get("/current-recipe").json()["recipe"] is None

    # Scaling with nothing loaded is a 404.
    assert client.post("/current-recipe/scale", json={"factor": 2}).status_code == 404


def test_timer_endpoints(client):
    """Create / list / get / cancel timers over HTTP (FoodAssistant-y0vh)."""
    r = client.post("/timers", json={"label": "Pasta", "seconds": 600})
    assert r.status_code == 200
    tid = r.json()["timer"]["id"]
    assert r.json()["timer"]["running"] is True

    r = client.get("/timers")
    assert r.status_code == 200
    assert any(t["id"] == tid for t in r.json()["timers"])

    r = client.get(f"/timers/{tid}")
    assert r.status_code == 200
    assert r.json()["timer"]["label"] == "Pasta"

    assert client.delete(f"/timers/{tid}").status_code == 200
    assert client.get(f"/timers/{tid}").status_code == 404

    # A non-positive duration is rejected.
    assert client.post("/timers", json={"label": "x", "seconds": 0}).status_code == 400


def test_streamdeck_count_endpoints(client, monkeypatch):
    """The Stream Deck count endpoints return a tiny JSON int and degrade to
    zero when Mealie is unreachable (FoodAssistant-4msn)."""
    from app.config import settings
    from app.services.mealie import MealieClient
    from app.services.grocy import GrocyClient

    saved = (settings.mealie_base_url, settings.mealie_api_key)
    try:
        settings.mealie_base_url = "http://mealie.test"
        settings.mealie_api_key = "test-mealie-key"
        assert settings.mealie_configured()

        async def _lists(self):
            return [{"id": "l1", "name": "Groceries"}]

        async def _list(self, list_id):
            return {"listItems": [
                {"checked": False, "note": "milk"},
                {"checked": True, "note": "eggs"},
                {"checked": False, "note": "bread"},
            ]}

        async def _recipes(self):
            return [
                {"name": "Toast", "slug": "toast",
                 "recipeIngredient": [{"note": "bread"}]},
            ]

        async def _stock(self):
            return [{"name": "bread", "amount": 1, "product_id": 1,
                     "days_remaining": 5}]

        monkeypatch.setattr(MealieClient, "get_shopping_lists", _lists)
        monkeypatch.setattr(MealieClient, "get_shopping_list", _list)
        monkeypatch.setattr(MealieClient, "get_recipes_with_ingredients", _recipes)
        monkeypatch.setattr(GrocyClient, "get_full_stock", _stock)

        r = client.get("/mealie/shopping/count")
        assert r.status_code == 200
        assert r.json() == {"count": 2}  # two unchecked items

        r = client.get("/mealie/suggest/ready-count")
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body["count"], int)
        assert body["count"] >= 1  # Toast is cookable from bread alone
    finally:
        settings.mealie_base_url, settings.mealie_api_key = saved


def test_streamdeck_count_endpoints_degrade_when_unconfigured(client):
    """With Mealie unconfigured the count endpoints answer a clean zero rather
    than erroring, so the deck poll stays cheap (FoodAssistant-4msn)."""
    from app.config import settings

    saved = (settings.mealie_base_url, settings.mealie_api_key)
    try:
        settings.mealie_base_url = ""
        settings.mealie_api_key = ""
        assert not settings.mealie_configured()
        assert client.get("/mealie/shopping/count").json() == {"count": 0}
        assert client.get("/mealie/suggest/ready-count").json() == {"count": 0}
    finally:
        settings.mealie_base_url, settings.mealie_api_key = saved


def test_settings_menu_has_intent_groups(client):
    """The single grouped settings menu renders every pane pill on a normal
    server, including the dedicated Network and Home Assistant panes
    (FoodAssistant-s6q, -42n4)."""
    r = client.get("/setup")
    assert r.status_code == 200
    for pane in ("pane-appearance", "pane-screen", "pane-network",
                 "pane-start-page", "pane-personalization-recipes",
                 "pane-scanning", "pane-inventory", "pane-connections",
                 "pane-home-assistant", "pane-devices",
                 "pane-security", "pane-backups", "pane-advanced"):
        assert f'data-bs-target="#{pane}"' in r.text, f"missing pill: {pane}"
    # Satellite-only Main Server card is not shown on a normal server.
    assert 'id="remote_server_url"' not in r.text


def test_remote_access_card_hidden_on_satellite(client, monkeypatch):
    """The remote access (tunnel) card is meaningless on a pi_remote
    satellite, so it must not render there but stay on a normal server
    (FoodAssistant-3bu5); it lives in the Connections pane now."""
    from app.config import settings

    # Normal server: the card renders inside Connections.
    page = client.get("/setup").text
    assert 'id="pane-connections"' in page
    assert 'id="tunnel-result"' in page
    assert 'name="tunnel_mode"' in page

    # Satellite: the card is gone.
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    assert settings.is_satellite()
    sat = client.get("/setup").text
    assert 'id="tunnel-result"' not in sat
    assert 'id="tunnel_mode_cloudflare"' not in sat


def test_generate_recipe_threads_custom_prompt(client, monkeypatch):
    """The Cook custom prompt reaches the provider's generate_recipe as an extra
    instruction (FoodAssistant-2mh9)."""
    import app.routers.mealie as mealie_router
    from app.config import settings
    # Isolate the custom-prompt behaviour from the appliance clause.
    monkeypatch.setattr(settings, "kitchen_appliances", [])

    captured = {}

    class FakeProvider:
        async def generate_recipe(self, name, extra_instructions=""):
            captured["name"] = name
            captured["extra"] = extra_instructions
            return {"name": name, "ingredients": [], "instructions": []}

    monkeypatch.setattr(mealie_router, "get_enrich_provider", lambda: FakeProvider())
    r = client.post("/mealie/recipes/generate",
                    json={"name": "Chili", "custom_prompt": "extra spicy, no beans"})
    assert r.status_code == 200
    assert captured["name"] == "Chili"
    assert "extra spicy, no beans" in captured["extra"]
    assert "USER NOTE" in captured["extra"]

    # With no custom prompt and no appliances, the instruction is empty (default).
    r = client.post("/mealie/recipes/generate", json={"name": "Soup"})
    assert r.status_code == 200
    assert captured["extra"] == ""


def test_generate_recipe_threads_appliances(client, monkeypatch):
    """The owned kitchen appliances steer generate_recipe so the AI only proposes
    dishes the kitchen can make (FoodAssistant-k2kv framework)."""
    import app.routers.mealie as mealie_router
    from app.config import settings
    monkeypatch.setattr(settings, "kitchen_appliances", ["stove", "sous_vide"])

    captured = {}

    class FakeProvider:
        async def generate_recipe(self, name, extra_instructions=""):
            captured["extra"] = extra_instructions
            return {"name": name, "ingredients": [], "instructions": []}

    monkeypatch.setattr(mealie_router, "get_enrich_provider", lambda: FakeProvider())
    r = client.post("/mealie/recipes/generate", json={"name": "Steak"})
    assert r.status_code == 200
    assert "Sous vide" in captured["extra"]
    assert "Stovetop" in captured["extra"]


def test_suggest_llm_folds_in_custom_prompt(client, monkeypatch):
    """The custom prompt is folded into the suggestion preferences (2mh9)."""
    import app.routers.mealie as mealie_router
    from app.services.grocy import GrocyClient

    async def _stock(self):
        return [{"name": "rice", "days_remaining": 3}]

    monkeypatch.setattr(GrocyClient, "get_full_stock", _stock)

    captured = {}

    class FakeProvider:
        async def suggest_from_inventory(self, items, limit=8, preferences=""):
            captured["preferences"] = preferences
            return [{"name": "Rice Bowl", "uses": ["rice"]}]

    monkeypatch.setattr(mealie_router, "get_enrich_provider", lambda: FakeProvider())
    r = client.post("/mealie/suggest/llm",
                    json={"preferences": "quick", "custom_prompt": "no onions"})
    assert r.status_code == 200
    assert "quick" in captured["preferences"]
    assert "no onions" in captured["preferences"]


def test_security_headers_present():
    """Every response carries the conservative security headers added after the
    Jul 2026 audit (clickjacking, MIME sniffing, referrer leakage)."""
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        r = c.get("/health")
    assert r.headers.get("X-Frame-Options") == "SAMEORIGIN"
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("Referrer-Policy") == "same-origin"


def test_content_security_policy_present(client):
    """Every response carries the CSP (FoodAssistant-rnnn). Validated against
    all major pages in a kiosk-emulating Chromium 149 with zero violations;
    the img-src http(s) allowance is deliberate (external screensaver photos)."""
    r = client.get("/health")
    csp = r.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'self'" in csp
