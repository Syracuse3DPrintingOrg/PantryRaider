"""Pure-logic tests for the Stream Deck controller.

These cover config loading, layout/paging, the action registry, status
polling, the commit handler, and key rendering. None of them need a deck
attached or the StreamDeck device library installed, so they import only the
hardware-free modules (config, layout, actions, render), never controller.

Run: python -m pytest tests/test_streamdeck.py -q
"""
from __future__ import annotations

import asyncio

import pytest

from foodassistant_streamdeck import actions, config, layout, render


# -- config ----------------------------------------------------------------


def test_defaults_have_known_actions():
    cfg = config.Config().validated()
    assert cfg.keys, "default key list should not be empty"
    assert all(name in actions.ACTIONS for name in cfg.keys)


def test_load_keys_as_plain_list(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('keys = ["pending", "commit"]\n')
    cfg = config.load(f)
    assert cfg.keys == ["pending", "commit"]


def test_load_keys_as_table_array(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text(
        "[[keys]]\naction = 'expiring'\n[[keys]]\naction = 'add'\n"
    )
    cfg = config.load(f)
    assert cfg.keys == ["expiring", "add"]


def test_unknown_keys_dropped_and_fallback(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('keys = ["bogus", "nope"]\n')
    cfg = config.load(f)
    # Nothing valid was given, so it falls back to the default order.
    assert cfg.keys == list(actions.DEFAULT_ORDER)


def test_numbers_are_clamped(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text("brightness = 999\npoll_seconds = 1\nsoon_days = -4\n")
    cfg = config.load(f)
    assert cfg.brightness == 100
    assert cfg.poll_seconds == 5
    assert cfg.soon_days == 0


def test_env_overrides_file(tmp_path, monkeypatch):
    f = tmp_path / "config.toml"
    f.write_text('base_url = "http://fromfile:1"\napi_key = "fromfile"\n')
    monkeypatch.setenv(config.ENV_BASE_URL, "http://fromenv:2")
    monkeypatch.setenv(config.ENV_API_KEY, "fromenv")
    cfg = config.load(f)
    assert cfg.base_url == "http://fromenv:2"
    assert cfg.api_key == "fromenv"


# -- layout / paging -------------------------------------------------------


def test_supported_sizes():
    assert layout.supported_key_counts() == (6, 15, 32)


def test_single_page_pads_to_key_count():
    pages = layout.build_pages(["pending", "commit"], 15)
    assert len(pages) == 1
    assert len(pages[0]) == 15
    assert pages[0][0].name == "pending"
    assert pages[0][2] is None  # padded blank


def test_no_paging_key_when_everything_fits():
    pages = layout.build_pages(list(actions.DEFAULT_ORDER), 15)
    names = [s.name for s in pages[0] if s is not None]
    assert "page_next" not in names


def test_mini_paginates_overflow():
    names = ["expiring", "pending", "commit", "add", "inventory", "cook", "brightness"]
    pages = layout.build_pages(names, 6)
    assert len(pages) == 2
    # Each page is exactly the deck size and ends with the page-cycle key.
    for page in pages:
        assert len(page) == 6
        assert page[-1].name == "page_next"
    # Five real actions fit before the More key on page one.
    first = [s.name for s in pages[0][:-1] if s is not None]
    assert first == ["expiring", "pending", "commit", "add", "inventory"]


def test_build_pages_rejects_bad_size():
    with pytest.raises(ValueError):
        layout.build_pages(["pending"], 0)


# -- action registry -------------------------------------------------------


def test_default_order_resolves():
    for name in actions.DEFAULT_ORDER:
        assert actions.resolve(name) is not None


def test_status_fields_match_poll_output():
    poll_keys = {"expiring", "pending"}
    for spec in actions.ACTIONS.values():
        if spec.kind == "status":
            assert spec.status_field in poll_keys


# -- polling ---------------------------------------------------------------


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    """Minimal async stand-in for httpx.AsyncClient."""

    def __init__(self, get_map=None, post_map=None):
        self.get_map = get_map or {}
        self.post_map = post_map or {}
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url))
        for suffix, resp in self.get_map.items():
            if url.endswith(suffix):
                return resp
        return _Resp(404, {})

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url))
        for suffix, resp in self.post_map.items():
            if url.endswith(suffix):
                return resp
        return _Resp(404, {})


def test_poll_status_sums_urgency_buckets():
    client = _FakeClient(
        get_map={
            "/expiring/summary": _Resp(
                200,
                {
                    "expired": 1,
                    "today": 2,
                    "within_3_days": 3,
                    "within_7_days": 4,
                    "within_30_days": 99,
                },
            ),
            "/pending/count": _Resp(200, {"count": 5}),
        }
    )
    out = asyncio.run(actions.poll_status(client, "http://x", soon_days=7))
    assert out == {"expiring": 1 + 2 + 3 + 4, "pending": 5}


def test_poll_status_excludes_week_bucket_for_short_window():
    client = _FakeClient(
        get_map={
            "/expiring/summary": _Resp(
                200,
                {"expired": 0, "today": 1, "within_3_days": 2, "within_7_days": 4},
            ),
            "/pending/count": _Resp(200, {"count": 0}),
        }
    )
    out = asyncio.run(actions.poll_status(client, "http://x", soon_days=3))
    assert out["expiring"] == 3  # week bucket dropped


def test_poll_status_tolerates_errors():
    out = asyncio.run(actions.poll_status(_FakeClient(), "http://x"))
    assert out == {"expiring": 0, "pending": 0}


# -- action handlers -------------------------------------------------------


def _ctx(client):
    refreshed = {"n": 0}

    async def refresh():
        refreshed["n"] += 1

    async def navigate(path):
        return True

    ctx = actions.ActionContext(
        client=client,
        base_url="http://x",
        refresh=refresh,
        navigate=navigate,
        cycle_brightness=lambda: 80,
        page_next=lambda: None,
        page_prev=lambda: None,
    )
    return ctx, refreshed


def test_commit_action_reports_count_and_refreshes():
    client = _FakeClient(post_map={"/pending/commit": _Resp(200, {"imported": 4})})
    ctx, refreshed = _ctx(client)
    msg = asyncio.run(actions.run_action(actions.ACTIONS["commit"], ctx))
    assert msg == "committed 4"
    assert refreshed["n"] == 1


def test_commit_action_handles_failure():
    client = _FakeClient(post_map={"/pending/commit": _Resp(500, {})})
    ctx, _ = _ctx(client)
    msg = asyncio.run(actions.run_action(actions.ACTIONS["commit"], ctx))
    assert "failed" in msg


def _ctx_recording(navigate_result=True):
    """Context that records navigate paths and refresh count.

    Returns (ctx, navigated, refreshed) where navigated is a list of the paths
    passed to navigate() and refreshed["n"] counts refresh() calls.
    """
    navigated = []
    refreshed = {"n": 0}

    async def refresh():
        refreshed["n"] += 1

    async def navigate(path):
        navigated.append(path)
        return navigate_result

    ctx = actions.ActionContext(
        client=_FakeClient(),
        base_url="http://x",
        refresh=refresh,
        navigate=navigate,
        cycle_brightness=lambda: 80,
        page_next=lambda: None,
        page_prev=lambda: None,
    )
    return ctx, navigated, refreshed


def test_status_press_without_target_only_refreshes():
    # A status spec with no target_path keeps the old refresh-only behavior and
    # never touches the kiosk display.
    spec = actions.ActionSpec(
        name="bare", label="Bare", color="#000", kind="status",
        status_field="pending",
    )
    ctx, navigated, refreshed = _ctx_recording()
    msg = asyncio.run(actions.run_action(spec, ctx))
    assert msg == "refreshed"
    assert refreshed["n"] == 1
    assert navigated == []


def test_expiring_status_press_navigates_and_refreshes():
    # Pressing the expiring status key deep-links the kiosk to the expiring
    # list AND re-polls the counts.
    ctx, navigated, refreshed = _ctx_recording()
    msg = asyncio.run(actions.run_action(actions.ACTIONS["expiring"], ctx))
    assert navigated == ["ui/expiring"]
    assert refreshed["n"] == 1
    assert msg == "opened"


def test_pending_status_press_navigates_to_pending_view():
    ctx, navigated, refreshed = _ctx_recording()
    msg = asyncio.run(actions.run_action(actions.ACTIONS["pending"], ctx))
    assert navigated == ["ui/pending"]
    assert refreshed["n"] == 1
    assert msg == "opened"


def test_status_press_with_no_display_still_refreshes():
    # When no kiosk display is attached, navigate returns False. The press must
    # still refresh and report "refreshed" rather than raise.
    ctx, navigated, refreshed = _ctx_recording(navigate_result=False)
    msg = asyncio.run(actions.run_action(actions.ACTIONS["expiring"], ctx))
    assert navigated == ["ui/expiring"]
    assert refreshed["n"] == 1
    assert msg == "refreshed"


def test_brightness_action_returns_percent():
    ctx, _ = _ctx(_FakeClient())
    msg = asyncio.run(actions.run_action(actions.ACTIONS["brightness"], ctx))
    assert msg == "brightness 80%"


# -- rendering -------------------------------------------------------------


def test_render_key_size_and_mode():
    img = render.render_key(72, 72, label="Cook", color="#7e22ce")
    assert img.size == (72, 72)
    assert img.mode == "RGB"


def test_render_status_key_with_count():
    img = render.render_key(96, 96, label="Pending", color="#1d4ed8", count=3, alert=True)
    assert img.size == (96, 96)


def test_blank_key():
    img = render.blank_key(80, 80)
    assert img.size == (80, 80)
    assert img.mode == "RGB"


def test_long_label_shrinks_to_fit():
    # A wide label must not pick a font wider than the fit fraction of the key.
    from PIL import ImageDraw

    # Start from an oversized font; fit must step it down under the limit while
    # staying above the floor so the result is the shrink path, not the wrap one.
    img = render.render_key(96, 96, label="Inventory", color="#1d4ed8")
    draw = ImageDraw.Draw(img)
    limit = int(96 * 0.90)
    big = render._fit_font(draw, "Inventory", 40, limit, floor=12)
    assert render._text_width(draw, "Inventory", big) <= limit


def test_very_long_word_wraps_at_floor():
    from PIL import ImageDraw

    img = render.render_key(48, 48, label="Refrigeration", color="#1d4ed8")
    draw = ImageDraw.Draw(img)
    floor_font = render._font(render._MIN_FONT_PX)
    lines = render._wrap_single_word(draw, "Refrigeration", floor_font, int(48 * 0.90))
    assert len(lines) >= 2
    assert "".join(lines) == "Refrigeration"


def test_density_factor_clamped_and_inverse():
    # Smaller keys scale up, larger keys scale down, both within the band.
    small = render._density_factor(48, 96)
    large = render._density_factor(120, 96)
    assert 0.80 <= large < 1.0 < small <= 1.25
    assert render._density_factor(96, 96) == 1.0


# -- action -> icon mapping ------------------------------------------------


def test_every_real_action_has_an_icon_glyph():
    # Every bindable action (everything the web grid offers except the "blank"
    # placeholder) must map to a Bootstrap Icons glyph name.
    for name in actions.ACTIONS:
        glyph = actions.icon_for(name)
        assert glyph, f"action {name} has no icon mapping"


def test_action_specs_carry_their_icon():
    # The glyph stamped onto each ActionSpec must match the source-of-truth map.
    for name, spec in actions.ACTIONS.items():
        assert spec.icon == actions.ACTION_ICONS[name]


def test_known_action_icons_match_web_ui():
    # Spot-check the glyphs the web UI uses for the same action so the deck and
    # the browser stay in sync (navigation.py + pending.html commit button).
    assert actions.icon_for("inventory") == "grid"
    assert actions.icon_for("expiring") == "clock-history"
    assert actions.icon_for("add") == "plus-circle"
    assert actions.icon_for("pending") == "hourglass-split"
    assert actions.icon_for("cook") == "lightbulb"
    assert actions.icon_for("commit") == "cloud-upload"
    assert actions.icon_for("shopping") == "cart"
    assert actions.icon_for("defaults") == "table"


def test_catalog_exposes_icons():
    cat = {a["name"]: a for a in actions.catalog()}
    assert cat["commit"]["icon"] == "cloud-upload"
    # The blank placeholder is not an action and carries no glyph.
    assert cat["blank"].get("icon", "") == ""


def test_every_action_glyph_resolves_to_a_codepoint():
    # If the font map is vendored, every action glyph must resolve to a real
    # codepoint. When the map is absent (font not yet dropped in), skip rather
    # than fail, since the renderer degrades to text-only by design.
    if not render._icon_codepoints():
        pytest.skip("bootstrap-icons.json not vendored")
    for name in actions.ACTIONS:
        glyph = actions.icon_for(name)
        assert render._icon_char(glyph) is not None, f"{name}:{glyph} unresolved"


def test_icon_char_accepts_bi_prefix():
    if not render._icon_codepoints():
        pytest.skip("bootstrap-icons.json not vendored")
    assert render._icon_char("bi-cart") == render._icon_char("cart")


def test_icon_char_unknown_is_none():
    assert render._icon_char("definitely-not-a-real-glyph-xyz") is None
    assert render._icon_char("") is None


# -- icon rendering --------------------------------------------------------


def test_render_key_with_icon_size_and_mode():
    img = render.render_key(96, 96, label="Cook", color="#7e22ce", icon="lightbulb")
    assert img.size == (96, 96)
    assert img.mode == "RGB"


def test_render_key_with_missing_glyph_falls_back_to_text():
    # An unknown glyph must not raise; it renders the same as a text-only key.
    plain = render.render_key(96, 96, label="Cook", color="#7e22ce")
    missing = render.render_key(
        96, 96, label="Cook", color="#7e22ce", icon="no-such-glyph"
    )
    assert missing.size == plain.size == (96, 96)
    assert missing.tobytes() == plain.tobytes()


def test_status_key_ignores_icon_and_keeps_count_layout():
    # Status keys keep their count-dominant layout regardless of an icon arg.
    with_icon = render.render_key(
        96, 96, label="Pending", color="#1d4ed8", count=3, icon="hourglass-split"
    )
    without = render.render_key(
        96, 96, label="Pending", color="#1d4ed8", count=3
    )
    assert with_icon.tobytes() == without.tobytes()


# -- rotation config -------------------------------------------------------


def test_rotation_defaults_to_zero():
    assert config.Config().validated().rotation == 0


def test_rotation_accepts_allowed_values(tmp_path):
    for deg in (0, 90, 180, 270):
        f = tmp_path / "c.toml"
        f.write_text(f"rotation = {deg}\n")
        assert config.load(f).rotation == deg


def test_rotation_rejects_bad_value(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text("rotation = 45\n")
    assert config.load(f).rotation == 0


# -- rotation index remap --------------------------------------------------


def test_display_dims_unrotated():
    assert layout.display_dims(15, 0) == (5, 3)
    assert layout.display_dims(32, 0) == (8, 4)
    assert layout.display_dims(32, 180) == (8, 4)


def test_display_dims_rotated_90():
    # 90 and 270 swap cols/rows so the editor matches the physical orientation.
    assert layout.display_dims(32, 90) == (4, 8)
    assert layout.display_dims(32, 270) == (4, 8)
    assert layout.display_dims(15, 90) == (3, 5)


def test_rotated_index_zero_is_identity():
    for i in range(32):
        assert layout.rotated_index(i, 32, 0) == i


def test_rotated_index_180_reverses_grid():
    # 15-key deck (5x3): top-left (0) maps to bottom-right (14) and back.
    assert layout.rotated_index(0, 15, 180) == 14
    assert layout.rotated_index(14, 15, 180) == 0
    # 180 is its own inverse for every key.
    for i in range(15):
        assert layout.rotated_index(layout.rotated_index(i, 15, 180), 15, 180) == i


def test_rotated_index_unknown_size_passthrough():
    assert layout.rotated_index(3, 7, 180) == 3


def test_slot_for_physical_inverts_rotated_index():
    # For every rotation and every known deck size, slot_for_physical must be
    # the exact inverse of rotated_index across all valid slots.
    for key_count in (6, 15, 32):
        for rotation in (0, 90, 180, 270):
            d_cols, d_rows = layout.display_dims(key_count, rotation)
            total = d_cols * d_rows
            for slot in range(total):
                phys = layout.rotated_index(slot, key_count, rotation)
                assert layout.slot_for_physical(phys, key_count, rotation) == slot, (
                    f"round-trip failed: key_count={key_count} rotation={rotation} slot={slot} phys={phys}"
                )


def test_rotated_index_90_xl_top_left():
    # XL (8x4) rotated 90 CW: visual slot 0 (top-left of the portrait grid)
    # must land on a valid physical key and round-trip exactly.
    phys = layout.rotated_index(0, 32, 90)
    assert 0 <= phys < 32
    assert layout.slot_for_physical(phys, 32, 90) == 0


def test_rotated_index_270_xl_bijection():
    # 270 must also be a bijection over all 32 keys (no two slots map same phys).
    physicals = [layout.rotated_index(i, 32, 270) for i in range(32)]
    assert len(set(physicals)) == 32, "rotated_index(270) is not injective"


# -- timer widget ----------------------------------------------------------


def test_timer_idle_shows_base_label():
    t = actions.TimerState()
    assert t.label("Timer 1") == "Timer 1"
    assert not t.is_running()
    assert not t.alerting


def test_timer_short_press_starts_at_one_minute():
    t = actions.TimerState()
    t.short_press()
    assert t.is_running()
    assert t._minutes == 1
    assert t.remaining_seconds() > 55


def test_timer_rapid_presses_accumulate():
    t = actions.TimerState()
    t.short_press()  # 1 min
    t.short_press()  # 2 min
    t.short_press()  # 3 min
    assert t._minutes == 3
    assert t.is_running()


def test_timer_press_alias_works():
    t = actions.TimerState()
    t.press()  # same as short_press
    assert t._minutes == 1
    assert t.is_running()


def test_timer_long_press_resets_to_idle():
    t = actions.TimerState()
    t.short_press()
    assert t.is_running()
    t.long_press()
    assert not t.is_running()
    assert t._minutes == 0
    assert t.label("T") == "T"


def test_timer_label_shows_countdown():
    t = actions.TimerState()
    t.short_press()  # 1 min
    label = t.label("Timer")
    assert ":" in label  # MM:SS format


def test_timer_alerting_on_expiry():
    t = actions.TimerState()
    t.short_press()
    # Force expiry by backdating the deadline
    t._deadline = t._deadline - 400
    expired = t.tick()
    assert expired
    assert t.alerting
    assert t.label("T") == "Done!"


def test_timer_dismiss_alert_via_short_press():
    t = actions.TimerState()
    t.short_press()
    t._deadline = t._deadline - 400
    t.tick()
    assert t.alerting
    t.short_press()  # dismiss
    assert not t.alerting
    assert t.label("T") == "T"


def test_timer_long_press_dismisses_alert():
    t = actions.TimerState()
    t.short_press()
    t._deadline = t._deadline - 400
    t.tick()
    assert t.alerting
    t.long_press()
    assert not t.alerting
    assert t._minutes == 0


def test_timer_alert_blinks_bright_and_dim_by_phase():
    t = actions.TimerState()
    t.short_press()
    t._deadline = t._deadline - 400
    t.tick()
    assert t.alerting
    bright = t.alert_color(0)
    dim = t.alert_color(1)
    assert bright != dim
    # Even phases are bright, odd phases are dim, so the key alternates.
    assert t.alert_color(2) == bright
    assert t.alert_color(3) == dim
    assert t.alert_color(4) == bright


def test_timer_color_uses_blink_phase_while_alerting():
    t = actions.TimerState()
    t.short_press()
    t._deadline = t._deadline - 400
    t.tick()
    assert t.color("#000000", blink_phase=0) == t.alert_color(0)
    assert t.color("#000000", blink_phase=1) == t.alert_color(1)
    assert t.color("#000000", blink_phase=0) != t.color("#000000", blink_phase=1)


def test_timer_color_ignores_blink_phase_when_not_alerting():
    t = actions.TimerState()
    # Idle: phase must never change the resting colour.
    assert t.color("#123456", blink_phase=0) == "#123456"
    assert t.color("#123456", blink_phase=1) == "#123456"
    # Running: countdown colour is phase-independent.
    t.short_press()
    assert t.color("#123456", blink_phase=0) == t.color("#123456", blink_phase=1)


def test_timer_action_registered():
    for name in ("timer_1", "timer_2", "timer_3"):
        assert name in actions.ACTIONS
        assert actions.ACTIONS[name].kind == "timer"


def test_timer_press_via_action_context():
    pressed = {}

    def fake_timer_press(name, long_press=False):
        pressed["name"] = name
        pressed["long"] = long_press

    ctx = actions.ActionContext(
        client=None,
        base_url="http://x",
        refresh=lambda: None,
        navigate=lambda _: None,
        cycle_brightness=lambda: 80,
        page_next=lambda: None,
        page_prev=lambda: None,
        timer_press=fake_timer_press,
    )
    asyncio.run(actions.run_action(actions.ACTIONS["timer_1"], ctx))
    assert pressed.get("name") == "timer_1"
    assert pressed.get("long") is False


def test_timer_long_press_via_action_context():
    pressed = {}

    def fake_timer_press(name, long_press=False):
        pressed["name"] = name
        pressed["long"] = long_press

    ctx = actions.ActionContext(
        client=None,
        base_url="http://x",
        refresh=lambda: None,
        navigate=lambda _: None,
        cycle_brightness=lambda: 80,
        page_next=lambda: None,
        page_prev=lambda: None,
        timer_press=fake_timer_press,
    )
    asyncio.run(actions.run_action(actions.ACTIONS["timer_1"], ctx, long_press=True))
    assert pressed.get("name") == "timer_1"
    assert pressed.get("long") is True


# -- weather widget ---------------------------------------------------------


def test_weather_action_registered():
    assert "weather" in actions.ACTIONS
    spec = actions.ACTIONS["weather"]
    assert spec.kind == "weather"


def test_weather_idle_shows_base_label():
    w = actions.WeatherState(location="", units="f")
    assert w.label("Weather") == "Weather"


def test_weather_label_after_fake_fetch():
    w = actions.WeatherState(location="", units="f")
    # Simulate a successful fetch by poking internal state directly.
    w._label = "72°F Sunny"
    w._fetched_at = __import__("time").monotonic()
    assert w.label("Weather") == "72°F Sunny"


def test_weather_color_default():
    w = actions.WeatherState()
    assert w.color("#123456") == "#1e40af"


def test_weather_config_loaded(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('weather_location = "New York"\nweather_units = "c"\nweather_poll_minutes = 30\n')
    cfg = config.load(f)
    assert cfg.weather_location == "New York"
    assert cfg.weather_units == "c"
    assert cfg.weather_poll_minutes == 30


def test_weather_refresh_via_context():
    refreshed = []

    async def fake_weather_refresh():
        refreshed.append(True)

    async def noop():
        pass

    ctx = actions.ActionContext(
        client=None,
        base_url="http://x",
        refresh=noop,
        navigate=lambda _: noop(),
        cycle_brightness=lambda: 80,
        page_next=lambda: None,
        page_prev=lambda: None,
        weather_refresh=fake_weather_refresh,
    )
    asyncio.run(actions.run_action(actions.ACTIONS["weather"], ctx))
    assert refreshed, "weather_refresh should have been called"


# -- HA entity -------------------------------------------------------------

def test_ha_actions_registered():
    for i in range(1, 6):
        name = f"ha_{i}"
        assert name in actions.ACTIONS
        assert actions.ACTIONS[name].kind == "ha_entity"


def test_ha_entity_state_idle():
    h = actions.HaEntityState("light.kitchen")
    assert h.label("Kitchen") == "Kitchen"
    assert h.color("#000") == "#000"


def test_ha_entity_state_on():
    import time
    h = actions.HaEntityState("light.kitchen", color_on="#f59e0b")
    h._state = "on"
    h._fetched_at = time.monotonic()
    assert h.is_on()
    assert "On" in h.label("Kitchen")
    assert h.color("#000") == "#f59e0b"


def test_ha_entity_state_off():
    import time
    h = actions.HaEntityState("light.kitchen", color_off="#334155")
    h._state = "off"
    h._fetched_at = time.monotonic()
    assert not h.is_on()
    assert "Off" in h.label("Kitchen")
    assert h.color("#000") == "#334155"


def test_ha_entity_state_unavailable():
    import time
    h = actions.HaEntityState("light.kitchen")
    h._state = "unavailable"
    h._fetched_at = time.monotonic()
    assert h.color("#000") == actions._HA_STATE_COLOR_ERROR


def test_ha_config_loaded(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text(
        'ha_base_url = "http://192.168.1.50:8123"\n'
        'ha_token = "abc123"\n'
        'ha_poll_seconds = 15\n'
        '[[ha_slots]]\n'
        'entity_id = "light.kitchen"\n'
        'service = "light.toggle"\n'
        'label = "Kitchen"\n'
    )
    cfg = config.load(f)
    assert cfg.ha_base_url == "http://192.168.1.50:8123"
    assert cfg.ha_token == "abc123"
    assert cfg.ha_poll_seconds == 15
    assert len(cfg.ha_slots) == 1
    assert cfg.ha_slots[0]["entity_id"] == "light.kitchen"


def test_ha_run_action_unconfigured():
    async def noop():
        pass

    ctx = actions.ActionContext(
        client=None,
        base_url="http://x",
        refresh=noop,
        navigate=lambda _: noop(),
        cycle_brightness=lambda: 80,
        page_next=lambda: None,
        page_prev=lambda: None,
    )
    msg = asyncio.run(actions.run_action(actions.ACTIONS["ha_1"], ctx))
    assert "not configured" in msg


def test_ha_run_action_calls_service():
    import json
    calls = []

    class FakeResp:
        status_code = 200

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        async def post(self, url, **kwargs):
            calls.append({"url": url, **kwargs})
            return FakeResp()

    original_spec = actions.ACTIONS["ha_1"]
    actions.ACTIONS["ha_1"] = actions.ActionSpec(
        name="ha_1", label="Kitchen", color="#000",
        kind="ha_entity",
        ha_entity_id="light.kitchen",
        ha_service="light.toggle",
    )

    refreshed = []

    async def fake_ha_refresh():
        refreshed.append(True)

    async def noop():
        pass

    import unittest.mock as mock
    with mock.patch("httpx.AsyncClient", return_value=FakeClient()):
        ctx = actions.ActionContext(
            client=None,
            base_url="http://x",
            refresh=noop,
            navigate=lambda _: noop(),
            cycle_brightness=lambda: 80,
            page_next=lambda: None,
            page_prev=lambda: None,
            ha_base_url="http://192.168.1.50:8123",
            ha_token="tok",
            ha_entity_refresh=fake_ha_refresh,
        )
        msg = asyncio.run(actions.run_action(actions.ACTIONS["ha_1"], ctx))

    actions.ACTIONS["ha_1"] = original_spec
    assert calls, "should have POSTed to HA"
    assert "light/toggle" in calls[0]["url"]
    assert refreshed, "ha_entity_refresh should have been called"
