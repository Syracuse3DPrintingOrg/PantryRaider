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

from foodassistant_streamdeck import actions, config, layout, render, theme


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


def test_dump_config_emits_resolved_keys(tmp_path, monkeypatch, capsys):
    # The bridge calls `--dump-config` to serve the RESOLVED keys to the editor
    # (FoodAssistant-3y5p): invalid names dropped, valid ones kept in order.
    import json
    from foodassistant_streamdeck import __main__ as m
    f = tmp_path / "config.toml"
    f.write_text('keys = ["cook", "bogus", "blank", "expiring"]\n')
    monkeypatch.setattr(m.sys, "argv", ["x", "--dump-config", "--config", str(f)])
    rc = m.main([])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["keys"] == ["cook", "blank", "expiring"]


def test_dump_config_defaults_when_no_keys(tmp_path, monkeypatch, capsys):
    import json
    from foodassistant_streamdeck import __main__ as m
    from foodassistant_streamdeck import actions
    f = tmp_path / "config.toml"
    f.write_text('base_url = "http://x"\n')
    monkeypatch.setattr(m.sys, "argv", ["x", "--dump-config", "--config", str(f)])
    assert m.main([]) == 0
    assert json.loads(capsys.readouterr().out)["keys"] == list(actions.DEFAULT_ORDER)


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
    # The fuller default set fits a single 32-key XL page with no paging key.
    pages = layout.build_pages(list(actions.DEFAULT_ORDER), 32)
    assert len(pages) == 1
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
    poll_keys = {"expiring", "pending", "shopping", "ready"}
    for spec in actions.ACTIONS.values():
        if spec.kind == "status":
            assert spec.status_field in poll_keys


# -- new deck actions (FoodAssistant-4msn) ---------------------------------


def test_new_actions_resolve_and_have_icons():
    # Every new action the bead adds must resolve in ACTIONS and carry a glyph.
    for name in (
        "clock", "shopping_count", "ready", "meal_today", "cooked",
        "timer_eggs", "timer_pasta", "timer_rice",
    ):
        spec = actions.resolve(name)
        assert spec is not None, f"{name} does not resolve"
        assert actions.icon_for(name), f"{name} has no icon mapping"
        assert spec.icon == actions.ACTION_ICONS[name]


def test_preset_timers_carry_the_right_minutes():
    assert actions.ACTIONS["timer_eggs"].timer_minutes == 6
    assert actions.ACTIONS["timer_pasta"].timer_minutes == 10
    assert actions.ACTIONS["timer_rice"].timer_minutes == 18
    for name in ("timer_eggs", "timer_pasta", "timer_rice"):
        assert actions.ACTIONS[name].kind == "timer"


def test_new_status_actions_field_and_target():
    shop = actions.ACTIONS["shopping_count"]
    assert shop.kind == "status" and shop.status_field == "shopping"
    assert shop.target_path == "ui/shopping"
    ready = actions.ACTIONS["ready"]
    assert ready.kind == "status" and ready.status_field == "ready"
    assert ready.target_path == "ui/cook"


def test_meal_today_is_info_with_target():
    spec = actions.ACTIONS["meal_today"]
    assert spec.kind == "info"
    assert spec.status_field == "meal_today"
    assert spec.target_path == "ui/mealplan"


def test_clock_is_clock_kind_no_target():
    spec = actions.ACTIONS["clock"]
    assert spec.kind == "clock"
    assert spec.target_path == ""


def test_new_kinds_grouped_in_catalog():
    cat = {a["name"]: a for a in actions.catalog()}
    # Clock and the meal info tile group under "Info" in the web grid editor.
    assert cat["clock"]["group"] == "Info"
    assert cat["meal_today"]["group"] == "Info"


# -- deck-filler actions (FoodAssistant-t02x) ------------------------------


def test_filler_actions_resolve_and_have_icons():
    for name in (
        "scale_half", "scale_1x", "scale_2x", "screen_off", "screen_on",
        "health", "convert", "timers_view", "kiosk_restart", "update", "reboot",
    ):
        spec = actions.resolve(name)
        assert spec is not None, f"{name} does not resolve"
        assert actions.icon_for(name), f"{name} has no icon mapping"
        assert spec.icon == actions.ACTION_ICONS[name]


def test_recipe_scale_specs_carry_the_right_factor():
    assert actions.ACTIONS["scale_half"].scale_factor == 0.5
    assert actions.ACTIONS["scale_1x"].scale_factor == 1.0
    assert actions.ACTIONS["scale_2x"].scale_factor == 2.0
    for name in ("scale_half", "scale_1x", "scale_2x"):
        assert actions.ACTIONS[name].kind == "recipe_scale"


def test_display_power_specs_carry_power_flag():
    assert actions.ACTIONS["screen_off"].kind == "display_power"
    assert actions.ACTIONS["screen_off"].power_on is False
    assert actions.ACTIONS["screen_on"].kind == "display_power"
    assert actions.ACTIONS["screen_on"].power_on is True


def test_bridge_action_specs_carry_path():
    assert actions.ACTIONS["kiosk_restart"].kind == "bridge_action"
    assert actions.ACTIONS["kiosk_restart"].bridge_path == "/kiosk/restart"
    assert actions.ACTIONS["update"].bridge_path == "/update"
    assert actions.ACTIONS["reboot"].bridge_path == "/reboot"


def test_nav_filler_specs_carry_target_path():
    convert = actions.ACTIONS["convert"]
    assert convert.kind == "nav" and convert.target_path == "ui/convert"
    timers_view = actions.ACTIONS["timers_view"]
    assert timers_view.kind == "nav" and timers_view.target_path == "ui/timers"


def test_filler_kinds_grouped_in_catalog():
    cat = {a["name"]: a for a in actions.catalog()}
    assert cat["scale_half"]["group"] == "Recipe"
    assert cat["screen_off"]["group"] == "System"
    assert cat["health"]["group"] == "System"
    assert cat["update"]["group"] == "System"
    assert cat["convert"]["group"] == "Navigation"


def test_recipe_scale_run_action_posts_factor():
    client = _FakeClient(
        post_map={"/current-recipe/scale": _Resp(200, {"ok": True})}
    )
    ctx, _ = _ctx(client)
    msg = asyncio.run(actions.run_action(actions.ACTIONS["scale_half"], ctx))
    assert msg == "0.5x"
    assert ("POST", "http://x/current-recipe/scale") in client.calls


def test_recipe_scale_run_action_no_recipe_surfaces_face():
    # The scale endpoint 404s when no recipe is active; the press must surface a
    # short message and never crash.
    client = _FakeClient()  # everything 404s
    ctx, _ = _ctx(client)
    msg = asyncio.run(actions.run_action(actions.ACTIONS["scale_1x"], ctx))
    assert msg == "No recipe"


def test_display_power_run_action_posts_to_bridge():
    client = _FakeClient(post_map={"/display/blank": _Resp(200, {"ok": True})})
    ctx, _ = _ctx(client)
    ctx.host_bridge_url = "http://bridge:9299"
    msg = asyncio.run(actions.run_action(actions.ACTIONS["screen_off"], ctx))
    assert msg == "Off"
    assert ("POST", "http://bridge:9299/display/blank") in client.calls


def test_display_power_wake_posts_wake_path():
    client = _FakeClient(post_map={"/display/wake": _Resp(200, {"ok": True})})
    ctx, _ = _ctx(client)
    ctx.host_bridge_url = "http://bridge:9299"
    msg = asyncio.run(actions.run_action(actions.ACTIONS["screen_on"], ctx))
    assert msg == "On"
    assert ("POST", "http://bridge:9299/display/wake") in client.calls


def test_bridge_action_run_action_posts_to_expected_path():
    client = _FakeClient(post_map={"/kiosk/restart": _Resp(200, {"ok": True})})
    ctx, _ = _ctx(client)
    ctx.host_bridge_url = "http://bridge:9299"
    msg = asyncio.run(actions.run_action(actions.ACTIONS["kiosk_restart"], ctx))
    assert msg == "OK"
    assert ("POST", "http://bridge:9299/kiosk/restart") in client.calls


def test_bridge_action_without_bridge_is_safe():
    # Off-Pi: no host_bridge_url, so the press is a no-op face, never a crash.
    ctx, _ = _ctx(_FakeClient())
    msg = asyncio.run(actions.run_action(actions.ACTIONS["reboot"], ctx))
    assert msg == "No bridge"


def test_health_state_color_and_label_by_warnings():
    h = actions.HealthState()
    # Before any poll: neutral grey, base label.
    assert h.color("#000") == actions._HEALTH_COLOR_UNKNOWN
    assert h.label("Health") == "Health"
    # Reachable, no warnings: green and OK.
    h.apply(reachable=True, warnings=0)
    assert h.color("#000") == actions._HEALTH_COLOR_OK
    assert h.label("Health") == "OK"
    # Reachable with warnings: amber and a count.
    h.apply(reachable=True, warnings=2)
    assert h.color("#000") == actions._HEALTH_COLOR_WARN
    assert "2" in h.label("Health")
    # Unreachable: back to neutral grey.
    h.apply(reachable=False, warnings=0)
    assert h.color("#000") == actions._HEALTH_COLOR_UNKNOWN


# -- clock label (pure) ----------------------------------------------------


def test_clock_label_formats_time_and_date():
    from datetime import datetime
    now = datetime(2026, 6, 26, 9, 5)  # a Friday
    label = actions._clock_label(now)
    assert label.startswith("09:05")
    assert "\n" in label
    # Second line is the abbreviated weekday and day-of-month.
    assert label.split("\n")[1] == "Fri 26"


def test_clock_label_time_only():
    from datetime import datetime
    now = datetime(2026, 1, 2, 23, 59)
    assert actions._clock_label(now, show_date=False) == "23:59"


# -- meal-today extraction (pure) ------------------------------------------


def test_meal_today_label_picks_today_entry():
    mealplan = {
        "start": "2026-06-26",
        "days": {
            "2026-06-26": [{"title": "Chicken Curry"}],
            "2026-06-27": [{"title": "Tacos"}],
        },
    }
    assert actions.meal_today_label(mealplan) == "Chicken Curry"


def test_meal_today_label_truncates_long_name():
    mealplan = {
        "start": "2026-06-26",
        "days": {"2026-06-26": [{"title": "Slow Braised Short Rib Ragu"}]},
    }
    out = actions.meal_today_label(mealplan)
    assert len(out) <= actions.MEAL_TODAY_LABEL_MAX


def test_meal_today_label_falls_back_when_empty():
    assert actions.meal_today_label({"start": "2026-06-26", "days": {}}) == "No meal"
    assert actions.meal_today_label({}, fallback="-") == "-"
    # An entry with a blank title is skipped in favour of the fallback.
    blank = {"start": "d", "days": {"d": [{"title": "   "}]}}
    assert actions.meal_today_label(blank) == "No meal"


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
    assert out["expiring"] == 1 + 2 + 3 + 4
    assert out["pending"] == 5


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
    assert out == {"expiring": 0, "pending": 0, "shopping": 0, "ready": 0}


def test_start_server_timer_posts_to_timers():
    # A preset deck timer mirrors to the shared server registry via POST /timers
    # so the web UI /timers page reflects it (FoodAssistant).
    client = _FakeClient(post_map={"/timers": _Resp(200, {"id": 1})})
    ok = asyncio.run(actions.start_server_timer(client, "http://x", "Eggs", 360))
    assert ok is True
    assert ("POST", "http://x/timers") in client.calls


def test_start_server_timer_tolerates_failure():
    # A non-200 (or unreachable server) returns False so the press still drives
    # the deck's own local countdown.
    ok = asyncio.run(actions.start_server_timer(_FakeClient(), "http://x", "Eggs", 360))
    assert ok is False


def test_poll_status_includes_shopping_and_ready_counts():
    client = _FakeClient(
        get_map={
            "/mealie/shopping/count": _Resp(200, {"count": 7}),
            "/mealie/suggest/ready-count": _Resp(200, {"count": 4}),
        }
    )
    out = asyncio.run(actions.poll_status(client, "http://x"))
    assert out["shopping"] == 7
    assert out["ready"] == 4
    # Missing expiring/pending endpoints still collapse to zero, never crash.
    assert out["expiring"] == 0 and out["pending"] == 0


def test_fetch_meal_today_extracts_label():
    client = _FakeClient(
        get_map={
            "/mealie/mealplan": _Resp(
                200,
                {"start": "2026-06-26",
                 "days": {"2026-06-26": [{"title": "Lasagna"}]}},
            )
        }
    )
    out = asyncio.run(actions.fetch_meal_today(client, "http://x"))
    assert out == "Lasagna"


def test_fetch_meal_today_falls_back_on_error():
    out = asyncio.run(actions.fetch_meal_today(_FakeClient(), "http://x", fallback="-"))
    assert out == "-"


def test_mark_current_recipe_cooked_consumes_and_reports():
    client = _FakeClient(
        get_map={"/current-recipe": _Resp(200, {"recipe": {"id": "stew", "title": "Stew"}})},
        post_map={"/mealie/cooked": _Resp(200, {"consumed": ["Carrot", "Onion"]})},
    )
    face = asyncio.run(actions.mark_current_recipe_cooked(client, "http://x"))
    assert face == "Cooked 2"
    assert ("POST", "http://x/mealie/cooked") in client.calls


def test_mark_current_recipe_cooked_no_active_recipe():
    client = _FakeClient(get_map={"/current-recipe": _Resp(200, {"recipe": None})})
    face = asyncio.run(actions.mark_current_recipe_cooked(client, "http://x"))
    assert face == "No recipe"
    # Nothing was posted: no active recipe to mark cooked.
    assert all(c[0] != "POST" for c in client.calls)


def test_cooked_action_dispatches_and_refreshes():
    client = _FakeClient(
        get_map={"/current-recipe": _Resp(200, {"recipe": {"id": "stew"}})},
        post_map={"/mealie/cooked": _Resp(200, {"consumed": ["Carrot"]})},
    )
    ctx, refreshed = _ctx(client)
    msg = asyncio.run(actions.run_action(actions.ACTIONS["cooked"], ctx))
    assert msg == "Cooked 1"
    assert refreshed["n"] == 1


# -- shopping_add + macro overrides (FoodAssistant-2w6o) -------------------


def test_add_shopping_item_posts_to_default_list():
    client = _FakeClient(
        get_map={"/mealie/shopping": _Resp(200, {"list": {"id": "L1"}})},
        post_map={"/mealie/shopping/items": _Resp(200, {"ok": True, "id": "i1"})},
    )
    face = asyncio.run(actions.add_shopping_item(client, "http://x", "Milk"))
    assert face == "Added"
    assert ("POST", "http://x/mealie/shopping/items") in client.calls


def test_add_shopping_item_no_list_does_not_post():
    client = _FakeClient(get_map={"/mealie/shopping": _Resp(200, {"list": None})})
    face = asyncio.run(actions.add_shopping_item(client, "http://x", "Milk"))
    assert face == "No list"
    assert all(c[0] != "POST" for c in client.calls)


def test_add_shopping_item_tolerates_errors():
    # No matching endpoints: the GET 404s, so the add fails without crashing.
    face = asyncio.run(actions.add_shopping_item(_FakeClient(), "http://x", "Milk"))
    assert face == "Failed"


def test_shopping_add_action_posts_item_and_refreshes():
    client = _FakeClient(
        get_map={"/mealie/shopping": _Resp(200, {"list": {"id": "L1"}})},
        post_map={"/mealie/shopping/items": _Resp(200, {"ok": True})},
    )
    spec = actions.override_to_spec(0, {"type": "shopping_add", "item": "Eggs"})
    ctx, refreshed = _ctx(client)
    msg = asyncio.run(actions.run_action(spec, ctx))
    assert msg == "Added"
    assert refreshed["n"] == 1
    # The configured item is what was posted.
    posted = [c for c in client.calls if c[0] == "POST"]
    assert posted and posted[0][1].endswith("/mealie/shopping/items")


def test_macro_runs_each_child_action_in_order():
    ran = []

    def record_brightness():
        ran.append("brightness")
        return 80

    ctx, _ = _ctx(_FakeClient())
    # Swap in recorders so we can observe order without real side effects.
    ctx.cycle_brightness = record_brightness
    ctx.page_next = lambda: ran.append("page_next")
    spec = actions.override_to_spec(
        0, {"type": "macro", "actions": ["brightness", "page_next"]}
    )
    msg = asyncio.run(actions.run_action(spec, ctx))
    assert msg == "Ran 2"
    assert ran == ["brightness", "page_next"]


def test_macro_skips_nested_macro_and_unknown_names():
    ran = []
    ctx, _ = _ctx(_FakeClient())
    ctx.cycle_brightness = lambda: (ran.append("brightness") or 80)
    # Register a temporary nested macro in the ACTIONS registry so resolve()
    # finds it; the macro handler must skip it rather than recurse.
    nested = actions.ActionSpec(
        name="nested_macro", label="Nested", color="#000000", kind="macro",
        macro_actions=("brightness",),
    )
    actions.ACTIONS["nested_macro"] = nested
    try:
        spec = actions.override_to_spec(
            0, {"type": "macro",
                "actions": ["brightness", "nested_macro", "does_not_exist"]}
        )
        msg = asyncio.run(actions.run_action(spec, ctx))
    finally:
        del actions.ACTIONS["nested_macro"]
    # Only the one resolvable, non-macro child ran.
    assert msg == "Ran 1"
    assert ran == ["brightness"]


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


def test_multiline_label_stays_on_key():
    # A two-line label like "Screen\nOff" must fit; the second line used to run
    # off the bottom of the key (FoodAssistant-f7ci).
    img = render.render_key(96, 96, label="Screen\nOff", color="#374151",
                            icon="lightbulb-off", key_style="rich",
                            action_name="screen_off")
    gray = img.convert("L")
    w, h = gray.size
    # The bottom two rows should be background, not bright label text.
    bottom = list(gray.crop((0, h - 2, w, h)).getdata())
    assert max(bottom) < 140, "label text reaches the bottom edge of the key"


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


def test_weather_forecast_use_smaller_icon_fraction():
    # Weather and forecast faces shrink the glyph so the temperature text reads;
    # every other kind keeps the standard size.
    standard = render.icon_fraction_for("status")
    assert render.icon_fraction_for("weather") < standard
    assert render.icon_fraction_for("forecast") < standard
    assert render.icon_fraction_for("status") == render._ICON_FRACTION


def test_weather_face_renders_with_small_icon():
    # A weather face (icon + multi-line temperature label) renders without raising
    # when handed the reduced glyph fraction.
    img = render.render_key(
        96,
        96,
        label="72F\nClear",
        color="#1d4ed8",
        icon="cloud-sun",
        icon_fraction=render.icon_fraction_for("weather"),
    )
    assert img.size == (96, 96)
    assert img.mode == "RGB"


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
    assert actions.icon_for("cook") == "fire"
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


def test_text_only_kind_flags_info_heavy_kinds():
    # Clock, weather, forecast, and today's-meal info keys carry a
    # multi-character value, so they render text-only (no main icon).
    for kind in ("clock", "weather", "forecast", "info"):
        assert render.text_only_kind(kind)
    # Ordinary action kinds stay icon-forward.
    for kind in ("status", "nav", "trigger", "timer", "ha_entity"):
        assert not render.text_only_kind(kind)


def test_text_only_render_drops_main_icon():
    # An info-heavy face renders without its main glyph: handing render_key an
    # icon while text_only is set must match the plain text-only face byte for
    # byte, so the value gets the whole key instead of a truncated glyph layout.
    plain = render.render_key(96, 96, label="12:34\nThu 26", color="#1f2937")
    info = render.render_key(
        96, 96, label="12:34\nThu 26", color="#1f2937",
        icon="clock", text_only=True,
    )
    assert info.size == plain.size == (96, 96)
    assert info.tobytes() == plain.tobytes()


def test_text_only_face_differs_from_iconed_face():
    # Sanity check that the icon would otherwise have changed the face, so the
    # byte-equality above is meaningful rather than vacuous. Skipped when the
    # icon font is not vendored (the iconed path already degrades to text).
    if not (render._icon_codepoints() and render._icon_font(16) is not None):
        pytest.skip("bootstrap-icons font not vendored")
    iconed = render.render_key(
        96, 96, label="12:34\nThu 26", color="#1f2937", icon="clock"
    )
    text_only = render.render_key(
        96, 96, label="12:34\nThu 26", color="#1f2937",
        icon="clock", text_only=True,
    )
    assert iconed.tobytes() != text_only.tobytes()


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


# -- 6-key orientation (FoodAssistant-na7) ---------------------------------
#
# The Mini's native grid is 2 rows by 3 cols (phys 0..2 top row, 3..5 bottom):
#     0 1 2
#     3 4 5
# Rotating the deck is a rigid clockwise turn of the whole page, the same turn
# `_draw_page` applies to each key face via `image.rotate(-rotation)`. The old
# code turned the index the wrong way (it had 90 and 270 swapped), so a face
# drawn upright landed on the key for the opposite rotation. These tables are
# the geometrically correct slot -> physical-key maps per rotation.


def test_rotated_index_6key_each_rotation():
    # slot indices are row-major over the *displayed* grid: 0 native (3x2), and
    # the portrait 2x3 grid for 90/270.
    expected = {
        0: list(range(6)),               # identity
        90: [2, 5, 1, 4, 0, 3],
        180: [5, 4, 3, 2, 1, 0],
        270: [3, 0, 4, 1, 5, 2],
    }
    for rotation, table in expected.items():
        got = [layout.rotated_index(s, 6, rotation) for s in range(6)]
        assert got == table, f"rotation={rotation}: {got} != {table}"


def test_rotated_index_6key_90_corner_sanity():
    # The reported bug: at 90 the displayed bottom-right and top-right slots
    # landed on the wrong physical keys. Displayed grid at 90 is 2 cols x 3 rows,
    # so top-right is slot 1 and bottom-right is slot 5.
    assert layout.rotated_index(1, 6, 90) == 5   # top-right -> phys bottom-right
    assert layout.rotated_index(5, 6, 90) == 3   # bottom-right -> phys bottom-left


def test_rotated_index_6key_round_trips_every_rotation():
    for rotation in (0, 90, 180, 270):
        d_cols, d_rows = layout.display_dims(6, rotation)
        for slot in range(d_cols * d_rows):
            phys = layout.rotated_index(slot, 6, rotation)
            assert 0 <= phys < 6
            assert layout.slot_for_physical(phys, 6, rotation) == slot


def test_rotated_index_larger_decks_unchanged():
    # Guard against regressing the 15- and 32-key transforms while fixing the
    # Mini. These are the same geometrically correct rigid-turn maps the fix
    # produces, pinned so a future change to either size is caught.
    assert [layout.rotated_index(s, 15, 90) for s in range(15)] == [
        4, 9, 14, 3, 8, 13, 2, 7, 12, 1, 6, 11, 0, 5, 10
    ]
    assert [layout.rotated_index(s, 15, 270) for s in range(15)] == [
        10, 5, 0, 11, 6, 1, 12, 7, 2, 13, 8, 3, 14, 9, 4
    ]
    assert [layout.rotated_index(s, 15, 180) for s in range(15)] == list(
        range(14, -1, -1)
    )
    assert [layout.rotated_index(s, 32, 90) for s in range(32)] == [
        7, 15, 23, 31, 6, 14, 22, 30, 5, 13, 21, 29, 4, 12, 20, 28,
        3, 11, 19, 27, 2, 10, 18, 26, 1, 9, 17, 25, 0, 8, 16, 24,
    ]
    assert [layout.rotated_index(s, 32, 270) for s in range(32)] == [
        24, 16, 8, 0, 25, 17, 9, 1, 26, 18, 10, 2, 27, 19, 11, 3,
        28, 20, 12, 4, 29, 21, 13, 5, 30, 22, 14, 6, 31, 23, 15, 7,
    ]
    assert [layout.rotated_index(s, 32, 180) for s in range(32)] == list(
        range(31, -1, -1)
    )


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


# -- recipe timer key specs (FoodAssistant-sbu3) ---------------------------


def test_clean_timer_label_collapses_and_trims():
    assert actions.clean_timer_label("  Boil   Pasta\nNow  ", max_len=20) == "Boil Pasta Now"
    assert actions.clean_timer_label("Sauce") == "Sauce"
    # Empty / whitespace-only yields "" so the caller can fall back to a default.
    assert actions.clean_timer_label("") == ""
    assert actions.clean_timer_label("   \n  ") == ""


def test_clean_timer_label_truncates_with_ellipsis():
    long = "Simmer the marinara sauce gently"
    out = actions.clean_timer_label(long, max_len=10)
    assert len(out) <= 10
    assert out.endswith("…")
    # A label exactly at the limit is left untouched (no ellipsis).
    exact = "ExactlyTen"  # 10 chars
    assert actions.clean_timer_label(exact, max_len=10) == "ExactlyTen"


def test_recipe_timer_specs_empty_yields_unchanged_defaults():
    defaults = ["Timer 1", "Timer 2", "Timer 3"]
    specs = actions.recipe_timer_key_specs([], 3, defaults)
    assert [s["label"] for s in specs] == defaults
    # No suggestion means no preset duration, so the key stays a manual timer.
    assert all(s["seconds"] is None for s in specs)
    assert all(s["step_index"] is None for s in specs)


def test_recipe_timer_specs_fewer_suggestions_than_slots():
    suggestions = [{"label": "Pasta", "seconds": 600, "step_index": 1}]
    defaults = ["Timer 1", "Timer 2", "Timer 3"]
    specs = actions.recipe_timer_key_specs(suggestions, 3, defaults)
    assert specs[0] == {"label": "Pasta", "seconds": 600, "step_index": 1}
    # The unfilled slots fall back to their stock label and manual behaviour.
    assert specs[1]["label"] == "Timer 2" and specs[1]["seconds"] is None
    assert specs[2]["label"] == "Timer 3" and specs[2]["seconds"] is None


def test_recipe_timer_specs_equal_count_maps_one_to_one():
    suggestions = [
        {"label": "Pasta", "seconds": 600, "step_index": 1},
        {"label": "Sauce", "seconds": 300, "step_index": 2},
    ]
    specs = actions.recipe_timer_key_specs(suggestions, 2, ["Timer 1", "Timer 2"])
    assert [s["label"] for s in specs] == ["Pasta", "Sauce"]
    assert [s["seconds"] for s in specs] == [600, 300]
    assert [s["step_index"] for s in specs] == [1, 2]


def test_recipe_timer_specs_more_suggestions_than_slots_truncates():
    suggestions = [
        {"label": "Pasta", "seconds": 600, "step_index": 1},
        {"label": "Sauce", "seconds": 300, "step_index": 2},
        {"label": "Garlic", "seconds": 120, "step_index": 3},
    ]
    specs = actions.recipe_timer_key_specs(suggestions, 2, ["Timer 1", "Timer 2"])
    assert len(specs) == 2
    assert [s["label"] for s in specs] == ["Pasta", "Sauce"]


def test_recipe_timer_specs_cleans_and_falls_back_on_blank_label():
    suggestions = [
        {"label": "  Boil   pasta water  ", "seconds": 600, "step_index": 1},
        {"label": "   ", "seconds": 300, "step_index": 2},
    ]
    specs = actions.recipe_timer_key_specs(suggestions, 2, ["Timer 1", "Timer 2"])
    # Whitespace collapsed and length capped to the deck-safe maximum.
    assert len(specs[0]["label"]) <= actions.RECIPE_TIMER_LABEL_MAX
    # A blank suggestion label falls back to the stock default for that slot.
    assert specs[1]["label"] == "Timer 2"
    assert specs[1]["seconds"] == 300


def test_recipe_timer_specs_default_label_generic_when_list_short():
    # With no default labels supplied, fallback slots use a generic "Timer".
    specs = actions.recipe_timer_key_specs([], 2)
    assert [s["label"] for s in specs] == ["Timer", "Timer"]


def test_fetch_timer_suggestions_returns_list():
    client = _FakeClient(
        get_map={
            "/current-recipe/timer-suggestions": _Resp(
                200, {"suggestions": [{"label": "Pasta", "seconds": 600, "step_index": 1}]}
            )
        }
    )
    out = asyncio.run(actions.fetch_timer_suggestions(client, "http://x"))
    assert out == [{"label": "Pasta", "seconds": 600, "step_index": 1}]


def test_fetch_timer_suggestions_tolerates_errors_and_no_recipe():
    # A 404 (no endpoint) or any error collapses to an empty list.
    out = asyncio.run(actions.fetch_timer_suggestions(_FakeClient(), "http://x"))
    assert out == []


def test_start_recipe_timer_posts_and_reports_success():
    client = _FakeClient(post_map={"/current-recipe/timers/start": _Resp(200, {"timer": {}})})
    ok = asyncio.run(
        actions.start_recipe_timer(client, "http://x", step_index=1, label="Pasta", seconds=600)
    )
    assert ok is True
    assert client.calls == [("POST", "http://x/current-recipe/timers/start")]


def test_start_recipe_timer_tolerates_failure():
    client = _FakeClient(post_map={"/current-recipe/timers/start": _Resp(404, {})})
    ok = asyncio.run(actions.start_recipe_timer(client, "http://x", step_index=9))
    assert ok is False


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


def test_weather_press_cycles_not_refetches():
    # A weather press now cycles the visible stat rather than re-fetching; the
    # data refreshes on its own timer. Confirm the cycle hook fires and the
    # network refresh is left alone.
    cycled = []
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
        weather_cycle=lambda name: cycled.append(name),
    )
    asyncio.run(actions.run_action(actions.ACTIONS["weather"], ctx))
    assert cycled == ["weather"]
    assert refreshed == []


# -- weather stat / forecast day cycle (FoodAssistant-3vz, -l2b) -----------


def _fetched_weather(units="f"):
    """A WeatherState carrying a fake successful fetch, ready to cycle.

    Pokes the parsed fields refresh() would set so the pure cycle logic can be
    exercised without any network.
    """
    import time as _time

    w = actions.WeatherState(location="", units=units)
    w._label = "72°F Sunny"
    w._color = "#1e40af"
    w._cond = {
        "temp_F": "72", "temp_C": "22",
        "FeelsLikeF": "75", "FeelsLikeC": "24",
        "humidity": "53",
        "windspeedMiles": "8", "windspeedKmph": "13",
    }
    w._forecast_days = [
        {"hi": "75", "lo": "55", "tag": "Today"},
        {"hi": "80", "lo": "60", "tag": "Tmrw"},
        {"hi": "70", "lo": "50", "tag": "Day 3"},
    ]
    w._fc_label = "H75 L55"
    w._fetched_at = _time.monotonic()
    return w


def test_weather_stat_index_zero_matches_legacy_default():
    # Index 0 must render exactly the old temp+condition label and colour, so an
    # un-pressed deck looks identical to the pre-cycle behaviour.
    w = _fetched_weather()
    assert w._stat_idx == 0
    assert w.current_stat_label("Weather") == "72°F Sunny"
    assert w.current_stat_label("Weather") == w.label("Weather")
    assert w.current_stat_color("#000") == "#1e40af"


def test_weather_cycle_advances_and_labels_differ():
    w = _fetched_weather()
    seen = [w.current_stat_label("Weather")]
    for _ in range(w.stat_count - 1):
        w.cycle_stat()
        seen.append(w.current_stat_label("Weather"))
    # Every stat in the cycle renders a distinct label.
    assert len(set(seen)) == w.stat_count
    # The named stats are present.
    joined = "\n".join(seen)
    assert "Feels" in joined
    assert "Humid" in joined
    assert "Wind" in joined


def test_weather_cycle_wraps_around_to_default():
    w = _fetched_weather()
    for _ in range(w.stat_count):
        w.cycle_stat()
    # A full lap returns to index 0 and the stock label.
    assert w._stat_idx == 0
    assert w.current_stat_label("Weather") == "72°F Sunny"


def test_weather_cycle_units_celsius_uses_metric_fields():
    w = _fetched_weather(units="c")
    w.cycle_stat()                       # feels-like
    assert "24" in w.current_stat_label("Weather")
    w.cycle_stat()                       # humidity
    w.cycle_stat()                       # wind
    label = w.current_stat_label("Weather")
    assert "13" in label and "kph" in label


def test_weather_cycle_idle_uses_base_label():
    # Before any fetch, cycling still works but the label falls back to base.
    w = actions.WeatherState()
    w.cycle_stat()
    assert w.current_stat_label("Weather") == "Weather"


def test_forecast_day_count_and_cycle_wraps():
    w = _fetched_weather()
    assert w.forecast_day_count == 3
    first = w.current_forecast_label("Forecast")
    w.cycle_forecast_day()
    second = w.current_forecast_label("Forecast")
    assert first != second
    w.cycle_forecast_day()
    w.cycle_forecast_day()               # back to day 0
    assert w._day_idx == 0
    assert w.current_forecast_label("Forecast") == first


def test_forecast_label_for_each_day_differs():
    w = _fetched_weather()
    labels = [w.forecast_label_for(i) for i in range(w.forecast_day_count)]
    assert len(set(labels)) == 3
    assert "Today" in labels[0]
    assert "Tmrw" in labels[1]


def test_forecast_index_zero_matches_legacy_default():
    w = _fetched_weather()
    # The day-0 forecast carries the same high/low the old single-day label did.
    assert "H75 L55" in w.current_forecast_label("Forecast")
    assert w.forecast_label("Forecast") == w.current_forecast_label("Forecast")


def test_weather_reset_to_default_returns_both_cycles():
    w = _fetched_weather()
    w.cycle_stat()
    w.cycle_forecast_day()
    assert w._stat_idx != 0 or w._day_idx != 0
    assert w.last_interaction > 0
    w.reset_to_default()
    assert w._stat_idx == 0
    assert w._day_idx == 0
    assert w.last_interaction == 0.0


def test_should_auto_reset_boundary():
    # Just inside the window: not yet. At/just past the window: reset.
    win = actions.WEATHER_AUTO_RESET_SECS
    last = 1000.0
    assert actions.should_auto_reset(last + win - 0.01, last, win) is False
    assert actions.should_auto_reset(last + win, last, win) is True
    assert actions.should_auto_reset(last + win + 5, last, win) is True


def test_should_auto_reset_never_when_at_default():
    # last_interaction of 0 means the key is already on its default index, so it
    # must never trigger a reset regardless of how much time has passed.
    assert actions.should_auto_reset(99999.0, 0.0) is False


def test_cycle_stamps_last_interaction():
    import time as _time
    w = _fetched_weather()
    assert w.last_interaction == 0.0
    before = _time.monotonic()
    w.cycle_stat()
    assert w.last_interaction >= before


def test_weather_press_cycles_stat_via_context():
    cycled = []

    async def noop():
        pass

    opened = []
    ctx = actions.ActionContext(
        client=None,
        base_url="http://x",
        refresh=noop,
        navigate=lambda p: (opened.append(p), noop())[1],
        cycle_brightness=lambda: 80,
        page_next=lambda: None,
        page_prev=lambda: None,
        weather_cycle=lambda name: cycled.append(("weather", name)),
        forecast_cycle=lambda name: cycled.append(("forecast", name)),
    )
    msg = asyncio.run(actions.run_action(actions.ACTIONS["weather"], ctx))
    assert msg == "weather"
    assert cycled == [("weather", "weather")]
    # A press also opens the full weather page on the attached kiosk display.
    assert opened == ["ui/weather"]


def test_forecast_press_cycles_day_via_context():
    cycled = []
    opened = []

    async def noop():
        pass

    ctx = actions.ActionContext(
        client=None,
        base_url="http://x",
        refresh=noop,
        navigate=lambda p: (opened.append(p), noop())[1],
        cycle_brightness=lambda: 80,
        page_next=lambda: None,
        page_prev=lambda: None,
        weather_cycle=lambda name: cycled.append(("weather", name)),
        forecast_cycle=lambda name: cycled.append(("forecast", name)),
    )
    msg = asyncio.run(actions.run_action(actions.ACTIONS["forecast"], ctx))
    assert msg == "forecast"
    assert cycled == [("forecast", "forecast")]
    assert opened == ["ui/weather"]


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


# -- PIN keypad ------------------------------------------------------------


def test_pin_buffer_accumulates_digits():
    b = actions.PinBuffer()
    assert b.is_empty()
    for ch in "1234":
        b.digit(ch)
    assert b.length() == 4
    assert b.value == "1234"
    assert not b.is_empty()


def test_pin_buffer_masks_and_never_shows_digits():
    b = actions.PinBuffer()
    for ch in "9173":
        b.digit(ch)
    masked = b.masked()
    assert len(masked) == 4
    # The mask must not leak any actual digit.
    assert not any(c.isdigit() for c in masked)


def test_pin_buffer_backspace_and_clear():
    b = actions.PinBuffer()
    for ch in "555":
        b.digit(ch)
    b.backspace()
    assert b.value == "55"
    b.clear()
    assert b.is_empty()
    assert b.value == ""
    # Backspace on an empty buffer is a no-op, not an error.
    b.backspace()
    assert b.is_empty()


def test_pin_buffer_ignores_non_digits_and_overflow():
    b = actions.PinBuffer(max_len=3)
    b.digit("a")     # not a digit
    b.digit("12")    # not a single char
    assert b.is_empty()
    for ch in "12345":
        b.digit(ch)  # caps at max_len
    assert b.length() == 3
    assert b.value == "123"


def test_pin_action_registered():
    assert "pin" in actions.ACTIONS
    assert actions.ACTIONS["pin"].kind == "pin"


def test_keypad_specs_cover_digits_and_controls():
    specs = actions.keypad_specs()
    for d in "0123456789":
        assert specs[f"keypad_{d}"].keypad_key == d
        assert specs[f"keypad_{d}"].kind == "keypad"
    for ctl in (actions.KEYPAD_CLEAR, actions.KEYPAD_ENTER, actions.KEYPAD_CANCEL):
        assert specs[f"keypad_{ctl}"].keypad_key == ctl


def test_pin_action_enters_keypad_via_context():
    entered = {"n": 0}

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
        keypad_enter=lambda: entered.__setitem__("n", entered["n"] + 1),
    )
    msg = asyncio.run(actions.run_action(actions.ACTIONS["pin"], ctx))
    assert msg == "keypad"
    assert entered["n"] == 1


def test_keypad_press_dispatched_via_context():
    pressed = []

    async def fake_keypad_press(key):
        pressed.append(key)

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
        keypad_press=fake_keypad_press,
    )
    spec = actions.keypad_specs()["keypad_7"]
    msg = asyncio.run(actions.run_action(spec, ctx))
    assert msg == "keypad 7"
    assert pressed == ["7"]


def test_submit_pin_success():
    client = _FakeClient(post_map={"/ui/login": _Resp(303, {})})
    ok = asyncio.run(actions.submit_pin(client, "http://x", "1234"))
    assert ok is True
    assert client.calls == [("POST", "http://x/ui/login")]


def test_submit_pin_failure_on_401():
    client = _FakeClient(post_map={"/ui/login": _Resp(401, {})})
    ok = asyncio.run(actions.submit_pin(client, "http://x", "0000"))
    assert ok is False


def test_submit_pin_tolerates_errors():
    class Boom:
        async def post(self, *a, **k):
            raise RuntimeError("network down")

    ok = asyncio.run(actions.submit_pin(Boom(), "http://x", "1234"))
    assert ok is False


# -- keypad layout ---------------------------------------------------------


def test_keypad_pages_cover_all_keys_across_pages():
    for key_count in layout.supported_key_counts():
        pages = layout.build_keypad_pages(key_count)
        assert pages
        for page in pages:
            assert len(page) == key_count
        # Gather every keypad key across all pages: the full pad must be present
        # somewhere, even on a deck that has to paginate.
        keys = {
            s.keypad_key
            for page in pages
            for s in page
            if s is not None and s.kind == "keypad"
        }
        for d in "0123456789":
            assert d in keys
        assert actions.KEYPAD_CLEAR in keys
        assert actions.KEYPAD_ENTER in keys
        assert actions.KEYPAD_CANCEL in keys


def test_keypad_single_page_when_it_fits():
    # The 15-key Original holds the whole pad on one page.
    assert len(layout.build_keypad_pages(15)) == 1
    assert len(layout.build_keypad_pages(32)) == 1


def test_keypad_paginates_on_mini():
    # The 6-key Mini cannot fit the 13-key pad, so it spills onto more pages,
    # each ending in a wrapping page-cycle key.
    pages = layout.build_keypad_pages(6)
    assert len(pages) > 1
    for page in pages:
        assert page[-1].name == "page_next"


def test_keypad_pages_reject_bad_size():
    with pytest.raises(ValueError):
        layout.build_keypad_pages(0)


def test_keypad_xl_phone_block():
    # On the XL (8x4) the first three digits sit in the top-left row.
    page = layout.build_keypad_pages(32)[0]
    assert page[0].keypad_key == "1"
    assert page[1].keypad_key == "2"
    assert page[2].keypad_key == "3"


def test_ha_run_action_calls_service():
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
        asyncio.run(actions.run_action(actions.ACTIONS["ha_1"], ctx))

    actions.ACTIONS["ha_1"] = original_spec
    assert calls, "should have POSTed to HA"
    assert "light/toggle" in calls[0]["url"]
    assert refreshed, "ha_entity_refresh should have been called"


# -- idle blank ----------------------------------------------------------------


class _FakeThread:
    """Stand-in for the StreamDeck library's background read thread."""

    def __init__(self, alive: bool = True) -> None:
        self.alive = alive

    def is_alive(self) -> bool:
        return self.alive


class _FakeDeck:
    """Minimal stand-in for a StreamDeck device used in idle-blank tests."""

    def __init__(self, key_count=15):
        self._key_count = key_count
        self.brightness_calls: list[int] = []
        self.reset_calls: int = 0
        self.open_calls: int = 0
        self.close_calls: int = 0
        self._callback = None
        # When True, the liveness probe and key writes raise, modelling a deck
        # whose worker thread has died or whose USB transport has errored.
        self.dead: bool = False
        # When True, open() raises, modelling a deck that is physically absent
        # (unplugged and not yet back).
        self.open_raises: bool = False
        # Fake background read thread; set alive=False to model a disconnect.
        self._read_thread: _FakeThread = _FakeThread(alive=True)

    def key_count(self) -> int:
        if self.dead:
            raise RuntimeError("deck not responding")
        return self._key_count

    def key_image_format(self) -> dict:
        if self.dead:
            raise RuntimeError("deck not responding")
        return {"size": (72, 72)}

    def deck_type(self) -> str:
        return "FakeDeck"

    def open(self) -> None:
        self.open_calls += 1
        if self.open_raises:
            raise OSError("USB device not found")
        # A successful re-open means the deck is answering again.
        self.dead = False
        self._read_thread = _FakeThread(alive=True)

    def close(self) -> None:
        self.close_calls += 1

    def set_brightness(self, pct: int) -> None:
        self.brightness_calls.append(pct)

    def reset(self) -> None:
        self.reset_calls += 1

    def set_key_callback(self, cb) -> None:
        self._callback = cb

    def set_key_image(self, key, image) -> None:
        pass

    def press(self, key: int) -> None:
        """Simulate a key press+release pair."""
        if self._callback:
            self._callback(self, key, True)
            self._callback(self, key, False)

    def press_down(self, key: int) -> None:
        """Simulate only the press event (key held down)."""
        if self._callback:
            self._callback(self, key, True)

    def release(self, key: int) -> None:
        """Simulate only the release event."""
        if self._callback:
            self._callback(self, key, False)


def _make_controller(idle_timeout_minutes: int = 0):
    """Build a Controller backed by a fake deck with no real hardware."""
    from foodassistant_streamdeck.controller import Controller

    cfg = config.Config(idle_timeout_minutes=idle_timeout_minutes).validated()
    deck = _FakeDeck()
    ctrl = Controller(deck, cfg)
    # Give the controller a minimal event loop reference so _on_key can schedule.
    loop = asyncio.new_event_loop()
    ctrl.loop = loop
    # Wire up the key callback as run() would do.
    deck.set_key_callback(ctrl._on_key)
    # Stub _draw_page to avoid importing the real StreamDeck library.
    ctrl._draw_page = lambda: None
    return ctrl, deck, loop


def test_key_press_updates_last_activity():
    import time as _time
    ctrl, deck, loop = _make_controller()
    before = ctrl._last_activity
    _time.sleep(0.02)
    deck.press_down(0)
    assert ctrl._last_activity > before
    loop.close()


def test_idle_blanks_deck_after_timeout():
    import time as _time
    ctrl, deck, loop = _make_controller(idle_timeout_minutes=1)
    # Wind the clock back so the controller looks idle.
    ctrl._last_activity = _time.monotonic() - 70
    loop.run_until_complete(ctrl._idle_loop_once())
    assert ctrl._idle_blanked
    assert deck.reset_calls >= 1
    assert 0 in deck.brightness_calls
    loop.close()


def test_idle_loop_does_not_blank_when_timeout_zero():
    import time as _time
    ctrl, deck, loop = _make_controller(idle_timeout_minutes=0)
    ctrl._last_activity = _time.monotonic() - 3600
    loop.run_until_complete(ctrl._idle_loop_once())
    assert not ctrl._idle_blanked
    assert deck.reset_calls == 0
    loop.close()


def test_wake_on_key_press_when_blanked():
    ctrl, deck, loop = _make_controller(idle_timeout_minutes=1)
    # Force blanked state.
    ctrl._idle_blanked = True
    # Press down a key -- should record the key as a wake key.
    deck.press_down(0)
    assert 0 in ctrl._wake_keys
    # Drain any pending coroutines scheduled by run_coroutine_threadsafe.
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()


def test_wake_key_release_does_not_trigger_action():
    ctrl, deck, loop = _make_controller(idle_timeout_minutes=1)
    ctrl._idle_blanked = True
    handled = []

    async def fake_handle(spec, long_press=False):
        handled.append(spec)

    ctrl._handle = fake_handle
    # Press down while blanked: key lands in _wake_keys.
    deck.press_down(0)
    # Clear the blanked flag (as _wake_from_idle would do) so release can run.
    ctrl._idle_blanked = False
    # Release: action must be swallowed because the key is in _wake_keys.
    deck.release(0)
    loop.run_until_complete(asyncio.sleep(0))
    assert handled == [], "action must not fire on a wake press"
    loop.close()


def test_wake_restores_page_and_clears_blank_flag():
    ctrl, deck, loop = _make_controller(idle_timeout_minutes=1)
    ctrl._idle_blanked = True
    loop.run_until_complete(ctrl._wake_from_idle())
    assert not ctrl._idle_blanked
    loop.close()


# -- re-init on orientation change + watchdog -----------------------------


def test_reinit_tears_down_and_reopens_the_deck():
    # A clean re-init (the path an orientation change takes) must close the old
    # HID handle and open it again, not just flip a flag.
    ctrl, deck, loop = _make_controller()
    ok = loop.run_until_complete(ctrl.reinit())
    assert ok is True
    assert deck.close_calls >= 1
    assert deck.open_calls >= 1
    # Brightness is re-asserted so the re-opened deck is visible again.
    assert deck.brightness_calls
    loop.close()


def test_deck_health_probe_detects_dead_deck():
    ctrl, deck, loop = _make_controller()
    assert ctrl._deck_is_healthy() is True
    deck.dead = True
    assert ctrl._deck_is_healthy() is False
    loop.close()


def test_health_probe_skipped_while_blanked():
    # While intentionally blanked for idle, the watchdog must not treat the
    # quiet deck as crashed.
    ctrl, deck, loop = _make_controller(idle_timeout_minutes=1)
    ctrl._idle_blanked = True
    deck.dead = True
    assert ctrl._deck_is_healthy() is True
    loop.close()


def test_watchdog_reinitializes_a_crashed_deck():
    ctrl, deck, loop = _make_controller()
    deck.dead = True
    before = deck.open_calls
    loop.run_until_complete(ctrl._watchdog_once())
    # The watchdog noticed the dead deck and re-opened it; open() clears the
    # dead flag in the fake, modelling a recovered device.
    assert deck.open_calls > before
    assert ctrl._deck_is_healthy() is True
    loop.close()


def test_watchdog_reloads_config_on_file_change(tmp_path):
    from foodassistant_streamdeck.controller import Controller

    cfg_file = tmp_path / "streamdeck.toml"
    cfg_file.write_text("rotation = 0\n")
    cfg = config.load(cfg_file)
    deck = _FakeDeck()
    ctrl = Controller(deck, cfg, config_path=str(cfg_file))
    loop = asyncio.new_event_loop()
    ctrl.loop = loop
    ctrl._draw_page = lambda: None
    assert ctrl.config.rotation == 0

    # Simulate the setup page rewriting the config with a new orientation.
    import os as _os
    cfg_file.write_text("rotation = 90\n")
    # Force a different mtime even on coarse-resolution filesystems.
    _os.utime(cfg_file, (ctrl._config_mtime + 5, ctrl._config_mtime + 5))

    before_open = deck.open_calls
    loop.run_until_complete(ctrl._watchdog_once())
    # The new rotation took effect and the deck was re-initialised in-process.
    assert ctrl.config.rotation == 90
    assert deck.open_calls > before_open
    assert deck.close_calls >= 1
    loop.close()


def test_watchdog_no_op_when_nothing_changed():
    ctrl, deck, loop = _make_controller()
    before_open = deck.open_calls
    loop.run_until_complete(ctrl._watchdog_once())
    # Healthy deck, no config change: the watchdog must not churn the device.
    assert deck.open_calls == before_open
    loop.close()


def test_health_check_detects_dead_read_thread():
    # A physically disconnected deck leaves key_count() working (in-memory
    # constant) but kills the read thread. The health probe must catch this.
    ctrl, deck, loop = _make_controller()
    assert ctrl._deck_is_healthy() is True
    deck._read_thread.alive = False
    assert ctrl._deck_is_healthy() is False
    loop.close()


def test_watchdog_retries_until_deck_replugged():
    # When reinit() fails (deck absent), _deck_live goes False. The next
    # watchdog tick must retry even though the health probe passes on the closed
    # handle, so the deck recovers as soon as it is physically replugged.
    ctrl, deck, loop = _make_controller()

    # Simulate unplug: thread dies and the device cannot be opened yet.
    deck._read_thread.alive = False
    deck.open_raises = True

    # First tick: detects dead thread, calls reinit, but open() raises.
    loop.run_until_complete(ctrl._watchdog_once())
    assert ctrl._deck_live is False

    # Replug: the device is back; open() succeeds again.
    deck.open_raises = False

    # Second tick: _deck_live is False so the watchdog retries and recovers.
    loop.run_until_complete(ctrl._watchdog_once())
    assert ctrl._deck_live is True
    assert deck.open_calls >= 2
    loop.close()


# -- theme palette (FoodAssistant-gxl) ------------------------------------

def test_theme_default_keeps_action_colors():
    for name, spec in actions.ACTIONS.items():
        assert theme.themed_color(name, spec.color, "dark") == spec.color


def test_theme_unknown_falls_back():
    assert theme.themed_color("pending", "#123456", "no-such-theme") == "#123456"


def test_theme_recolors_by_role():
    c = theme.themed_color("commit", "#000000", "synthwave")
    assert c == theme.THEME_PALETTES["synthwave"]["success"]
    assert theme.themed_color("pending", "#000000", "darkly") == theme.THEME_PALETTES["darkly"]["primary"]


def test_theme_role_of_suffixed_actions():
    assert theme.role_of("timer_2") == "timer"
    assert theme.role_of("ha_3") == "accent"
    assert theme.role_of("keypad_5") == "muted"
    assert theme.role_of("nonexistent") is None


def test_theme_every_palette_covers_every_role():
    roles = set(theme.ROLE_BY_ACTION.values()) | {"timer", "accent", "muted"}
    for name, palette in theme.THEME_PALETTES.items():
        missing = roles - set(palette)
        assert not missing, f"{name} palette missing roles: {missing}"


def test_text_color_dark_on_light_background():
    # A light key wants near-black text for contrast.
    assert theme.text_color_for("#ffffff") == theme._DARK_TEXT
    assert theme.text_color_for("#e0e0e0") == theme._DARK_TEXT


def test_text_color_light_on_dark_background():
    # A dark key wants near-white text.
    assert theme.text_color_for("#000000") == theme._LIGHT_TEXT
    assert theme.text_color_for("#1a1a1a") == theme._LIGHT_TEXT
    assert theme.text_color_for("#375a7f") == theme._LIGHT_TEXT  # darkly primary


def test_text_color_light_green_commit_is_readable():
    # The reported bug: white label text on a light green Commit (success) key.
    # The synthwave theme's success green is genuinely light, so the contrast
    # helper must now flip its label text to dark instead of the old white.
    light_green_commit = theme.themed_color("commit", "#000000", "synthwave")
    assert theme.text_color_for(light_green_commit) == theme._DARK_TEXT
    # A plain pale green is likewise treated as a light background.
    assert theme.text_color_for("#90ee90") == theme._DARK_TEXT


def test_text_color_handles_malformed_hex():
    # A bad colour must not raise; it falls back to a mid grey -> light text.
    assert theme.text_color_for("not-a-color") == theme._LIGHT_TEXT
    assert theme.text_color_for("#xyzxyz") == theme._LIGHT_TEXT


def test_relative_luminance_monotonic():
    # Black is darkest, white is brightest, and grey sits between.
    assert theme.relative_luminance("#000000") == 0.0
    assert theme.relative_luminance("#ffffff") == pytest.approx(1.0)
    mid = theme.relative_luminance("#808080")
    assert 0.0 < mid < 1.0


def test_config_loads_theme(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('theme = "cyborg"\n')
    cfg = config.load(p)
    assert cfg.theme == "cyborg"


def test_every_app_theme_has_a_deck_palette():
    # Every web UI theme must either carry a Stream Deck palette or be the
    # default "dark" theme, which deliberately keeps the per-action colours.
    # Importing the app config keeps this honest if a theme is added there but
    # the deck palette is forgotten (FoodAssistant-ap8m).
    import sys
    from pathlib import Path

    service_dir = Path(__file__).resolve().parent.parent / "service"
    sys.path.insert(0, str(service_dir))
    try:
        from app.config import THEMES
    except Exception:  # pragma: no cover - app deps not installed in this env
        pytest.skip("app package not importable")

    allowed_missing = {"dark"}
    for name in THEMES:
        if name in allowed_missing:
            continue
        assert name in theme.THEME_PALETTES, (
            f"app theme {name!r} has no THEME_PALETTES entry"
        )


# -- shared activity / cross-wake (FoodAssistant-otiy) ----------------------

def test_external_activity_is_fresh():
    from foodassistant_streamdeck.controller import _external_activity_is_fresh as fresh
    now = 1000.0
    assert fresh(now - 1, now) is True       # 1s ago: fresh
    assert fresh(now - 11, now) is True      # within 12s window
    assert fresh(now - 13, now) is False     # older than the window
    assert fresh(None, now) is False         # no data
    assert fresh(0, now) is False            # unset epoch
    assert fresh("nope", now) is False       # wrong type
    assert fresh(now + 5, now) is False      # future timestamp ignored


def test_config_loads_host_bridge_url(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('host_bridge_url = "http://127.0.0.1:9299"\n')
    cfg = config.load(p)
    assert cfg.host_bridge_url == "http://127.0.0.1:9299"


def test_poll_shared_activity_wakes_blanked_deck(monkeypatch):
    import foodassistant_streamdeck.controller as controller_mod

    class _Resp:
        def json(self):
            import time as _t
            return {"last_activity": _t.time(), "display_blanked": False}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url):
            return _Resp()

    monkeypatch.setattr(controller_mod.httpx, "AsyncClient", _FakeClient)
    ctrl, deck, loop = _make_controller()
    ctrl.config.host_bridge_url = "http://127.0.0.1:9299"
    ctrl._idle_blanked = True
    woke = {"v": False}
    async def _fake_wake():
        woke["v"] = True
        ctrl._idle_blanked = False
    ctrl._wake_from_idle = _fake_wake
    loop.run_until_complete(ctrl._poll_shared_activity())
    assert woke["v"] is True
    assert ctrl._idle_blanked is False
    loop.close()


def test_poll_shared_activity_noop_without_bridge_url(monkeypatch):
    ctrl, deck, loop = _make_controller()
    ctrl.config.host_bridge_url = ""
    ctrl._idle_blanked = True
    # No bridge configured: must not touch state or raise.
    loop.run_until_complete(ctrl._poll_shared_activity())
    assert ctrl._idle_blanked is True
    loop.close()


# -- default key set (FoodAssistant-fygv) ----------------------------------


def test_default_order_includes_new_feature_actions():
    # The fuller default fills a 15/32 key deck with real actions instead of
    # leaving most faces blank. Every entry must resolve in ACTIONS.
    expected = {
        "expiring", "pending", "commit", "add", "inventory", "cook",
        "recipes", "mealplan", "shopping",
        "timer_1", "timer_2", "timer_3",
        "weather", "forecast", "brightness",
    }
    assert expected.issubset(set(actions.DEFAULT_ORDER))
    for name in actions.DEFAULT_ORDER:
        assert actions.resolve(name) is not None, f"{name} missing from ACTIONS"


def test_default_order_fills_a_15_key_deck_with_real_actions():
    # A 15-key Original fills entirely with bound actions (no padded blanks). The
    # fuller default set (sized for the 32-key XL) overflows 15 keys now, so the
    # Original paginates with a wrapping page-cycle key rather than showing gaps.
    pages = layout.build_pages(list(actions.DEFAULT_ORDER), 15)
    assert len(pages) >= 1
    first = pages[0]
    assert len(first) == 15
    # Every face on the first page is a real bound action or the paging key, never
    # a padded blank, so the deck reads full.
    assert all(s is not None for s in first)
    assert first[-1].name == "page_next"


def test_default_order_fills_a_32_key_deck_in_one_page():
    # The 32-key XL holds the whole fuller default set on a single page with no
    # paging key, which is what the longer DEFAULT_ORDER is sized for.
    pages = layout.build_pages(list(actions.DEFAULT_ORDER), 32)
    assert len(pages) == 1
    names = [s.name for s in pages[0] if s is not None]
    assert "page_next" not in names
    assert len(names) == len(actions.DEFAULT_ORDER)


def test_default_order_still_paginates_on_a_6_key_mini():
    # The Mini cannot hold the full default set, so it paginates with a wrapping
    # page-cycle key on each page, exactly as before.
    pages = layout.build_pages(list(actions.DEFAULT_ORDER), 6)
    assert len(pages) > 1
    for page in pages:
        assert len(page) == 6
        assert page[-1].name == "page_next"


# -- style config (FoodAssistant-fygv) -------------------------------------


def test_key_style_and_icon_color_defaults():
    cfg = config.Config().validated()
    assert cfg.key_style == "rich"
    assert cfg.icon_color == "full"


def test_unknown_key_style_falls_back_to_default(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('key_style = "bogus"\nicon_color = "nope"\n')
    cfg = config.load(f)
    assert cfg.key_style == config.DEFAULT_KEY_STYLE
    assert cfg.icon_color == config.DEFAULT_ICON_COLOR


def test_valid_key_style_and_icon_color_loaded(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('key_style = "glass"\nicon_color = "mono"\n')
    cfg = config.load(f)
    assert cfg.key_style == "glass"
    assert cfg.icon_color == "mono"


# -- render style helpers (FoodAssistant-fygv) -----------------------------


def test_lighten_and_darken_move_luminance():
    base = (40, 90, 140)
    base_hex = render._rgb_to_hex(base)
    lighter = render._lighten(base, 0.4)
    darker = render._darken(base, 0.4)
    assert theme.relative_luminance(render._rgb_to_hex(lighter)) > \
        theme.relative_luminance(base_hex)
    assert theme.relative_luminance(render._rgb_to_hex(darker)) < \
        theme.relative_luminance(base_hex)
    # Zero amount is the identity for both.
    assert render._lighten(base, 0.0) == base
    assert render._darken(base, 0.0) == base


def test_lighten_darken_stay_in_range():
    for c in ((0, 0, 0), (255, 255, 255), (200, 10, 60)):
        for amt in (0.0, 0.5, 1.0):
            for out in (render._lighten(c, amt), render._darken(c, amt)):
                assert all(0 <= ch <= 255 for ch in out)


def test_vertical_gradient_size_and_endpoints():
    top = (240, 240, 240)
    bottom = (10, 10, 10)
    img = render._vertical_gradient((20, 30), top, bottom)
    assert img.size == (20, 30)
    assert img.mode == "RGB"
    # Top row is the top colour, bottom row the bottom colour.
    assert img.getpixel((0, 0)) == top
    assert img.getpixel((0, 29)) == bottom


def test_glass_panel_size_and_mode():
    img = render._glass_panel((72, 72), (30, 100, 160))
    assert img.size == (72, 72)
    assert img.mode == "RGB"


@pytest.mark.parametrize("style", ["minimal", "rich", "glass"])
@pytest.mark.parametrize("color", ["#7e22ce", "#15803d", "#ffffff", "#000000"])
def test_render_key_each_style_returns_correct_image(style, color):
    img = render.render_key(
        96, 96, label="Cook", color=color, key_style=style, icon="fire",
        action_name="cook",
    )
    assert img.size == (96, 96)
    assert img.mode == "RGB"


def test_render_key_unknown_style_does_not_crash():
    # An unrecognised style must degrade to the flat minimal fill, not raise.
    img = render.render_key(72, 72, label="Cook", color="#7e22ce", key_style="bogus")
    plain = render.render_key(72, 72, label="Cook", color="#7e22ce", key_style="minimal")
    assert img.tobytes() == plain.tobytes()


def test_render_key_defaults_preserve_legacy_minimal_behaviour():
    # The positional/default call (no style args) must render exactly as the old
    # flat minimal fill, so existing callers and pinned tests are unaffected.
    legacy = render.render_key(72, 72, label="Cook", color="#7e22ce")
    minimal = render.render_key(
        72, 72, label="Cook", color="#7e22ce", key_style="minimal", icon_color="mono"
    )
    assert legacy.tobytes() == minimal.tobytes()


def test_full_icon_color_falls_back_when_too_close_to_background():
    # When the accent luminance sits within the guard band of the mid colour,
    # the glyph fill drops back to the contrast text colour for legibility.
    text_fill = (235, 235, 235)
    mid = render._hex_to_rgb(theme.role_accent("commit"))
    out = render._icon_fill("full", "commit", mid, text_fill)
    assert out == text_fill


def test_mono_icon_color_keeps_text_fill():
    text_fill = (235, 235, 235)
    out = render._icon_fill("mono", "cook", (20, 20, 20), text_fill)
    assert out == text_fill


# -- full-colour icon set + clean style (FoodAssistant: colour icons) --------


def test_emoji_for_maps_actions_to_slugs():
    assert actions.emoji_for("cook") == "fire"
    assert actions.emoji_for("shopping_count") == "cart"
    assert actions.emoji_for("nonexistent") == ""


def test_bundled_colour_icons_exist():
    # The slugs referenced by ACTION_EMOJI must have a bundled PNG.
    slugs = set(actions.ACTION_EMOJI.values())
    for slug in slugs:
        assert render.emoji_available(slug), f"missing colour icon: {slug}"


def test_color_icon_renders_without_crashing():
    img = render.render_key(
        96, 96, "Cook", "#7e22ce", icon="fire",
        key_style="clean", icon_color="color",
        action_name="cook", emoji=actions.emoji_for("cook"),
    )
    assert img.size == (96, 96)
    assert img.mode == "RGB"


def test_color_icon_falls_back_to_glyph_when_missing():
    # An action with no colour icon still renders (mono glyph path), no crash.
    img = render.render_key(
        96, 96, "X", "#333333", icon="grid",
        key_style="clean", icon_color="color", action_name="inventory", emoji="",
    )
    assert img.size == (96, 96)


def test_clean_style_renders_and_mid_is_dark():
    from foodassistant_streamdeck.render import _mid_color, _CLEAN_BG
    assert _mid_color("clean", (200, 50, 50)) == _CLEAN_BG
    img = render.render_key(96, 96, "Cook", "#7e22ce", icon="fire", key_style="clean")
    assert img.size == (96, 96)


def test_config_accepts_clean_and_color():
    c = config.Config(key_style="clean", icon_color="color").validated()
    assert c.key_style == "clean"
    assert c.icon_color == "color"
    # Unknown values still fall back to the defaults.
    c2 = config.Config(key_style="neon", icon_color="rainbow").validated()
    assert c2.key_style == "rich"
    assert c2.icon_color == "full"


# -- camera (snapshot + full-deck overlay) ---------------------------------


def _tiny_jpeg(width: int = 64, height: int = 48, color=(200, 60, 60)) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="JPEG")
    return buf.getvalue()


def test_image_from_jpeg_decodes_to_requested_size():
    img = render.image_from_jpeg(_tiny_jpeg(80, 40), (72, 72))
    assert img is not None
    assert img.size == (72, 72)
    assert img.mode == "RGB"


def test_image_from_jpeg_bad_bytes_returns_none():
    assert render.image_from_jpeg(b"not a jpeg", (72, 72)) is None
    assert render.image_from_jpeg(b"", (72, 72)) is None


def test_slice_full_image_3x5_no_spacing():
    src = _tiny_jpeg(640, 384)
    from PIL import Image
    import io

    image = Image.open(io.BytesIO(src)).convert("RGB")
    tiles = render.slice_full_image(image, 3, 5, (72, 72))
    assert len(tiles) == 3 * 5
    for tile in tiles:
        assert tile.size == (72, 72)
        assert tile.mode == "RGB"


def test_slice_full_image_4x8_with_spacing():
    from PIL import Image

    image = Image.new("RGB", (400, 300), (10, 120, 200))
    tiles = render.slice_full_image(image, 4, 8, (96, 96), spacing=6)
    assert len(tiles) == 4 * 8
    assert all(t.size == (96, 96) for t in tiles)


def test_slice_full_image_row_major_order():
    from PIL import Image

    image = Image.new("RGB", (300, 200), (0, 0, 0))
    rows, cols = 3, 5
    tiles = render.slice_full_image(image, rows, cols, (40, 40))
    # Index r*cols + c maps to a single key; the count and per-tile size confirm
    # the row-major contract the controller relies on.
    assert len(tiles) == rows * cols
    assert tiles[rows * cols - 1].size == (40, 40)


def test_slice_full_image_degenerate_returns_empty():
    from PIL import Image

    image = Image.new("RGB", (10, 10), (0, 0, 0))
    assert render.slice_full_image(image, 0, 5, (40, 40)) == []
    assert render.slice_full_image(image, 3, 0, (40, 40)) == []


def test_camera_actions_resolve_and_have_icons():
    cam = actions.resolve("camera")
    full = actions.resolve("camera_full")
    assert cam is not None and cam.kind == "camera"
    assert full is not None and full.kind == "camera_full"
    assert cam.icon and render.icon_available(cam.icon)
    assert full.icon and render.icon_available(full.icon)


def test_camera_actions_group_under_camera():
    items = {i["name"]: i for i in actions.catalog()}
    assert items["camera"]["group"] == "Camera"
    assert items["camera_full"]["group"] == "Camera"


def test_camera_config_loads_cameras_and_refresh(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text(
        "camera_full_refresh_seconds = 8\n"
        "[[cameras]]\n"
        'name = "Front"\n'
        'snapshot_url = "http://cam/snap.jpg"\n'
    )
    cfg = config.load(f)
    assert cfg.camera_full_refresh_seconds == 8
    assert cfg.cameras and cfg.cameras[0]["snapshot_url"] == "http://cam/snap.jpg"


def test_camera_full_refresh_clamped():
    c = config.Config(camera_full_refresh_seconds=0).validated()
    assert c.camera_full_refresh_seconds == 1


def test_camera_action_run_opens_feed():
    opened = []

    async def navigate(path):
        opened.append(path)
        return True

    ctx = actions.ActionContext(
        client=None, base_url="http://x", refresh=lambda: asyncio.sleep(0),
        navigate=navigate, cycle_brightness=lambda: 0,
        page_next=lambda: None, page_prev=lambda: None,
    )
    msg = asyncio.run(actions.run_action(actions.resolve("camera"), ctx))
    assert msg == "opened"
    assert opened == ["ui/camera"]


def test_camera_full_action_is_marker():
    ctx = actions.ActionContext(
        client=None, base_url="http://x", refresh=lambda: asyncio.sleep(0),
        navigate=lambda p: asyncio.sleep(0), cycle_brightness=lambda: 0,
        page_next=lambda: None, page_prev=lambda: None,
    )
    msg = asyncio.run(actions.run_action(actions.resolve("camera_full"), ctx))
    assert msg == "Camera"


def test_camera_override_chooses_camera_and_full():
    # A "camera" override targets a named camera and can take over the whole deck.
    spec = actions.override_to_spec(2, {"type": "camera", "camera": "Garage"})
    assert spec is not None and spec.kind == "camera" and spec.camera_name == "Garage"
    full = actions.override_to_spec(3, {"type": "camera", "camera": "Door", "full": True})
    assert full is not None and full.kind == "camera_full" and full.camera_name == "Door"
    # No camera named: still builds (resolves to the first camera at draw time),
    # and the label defaults sensibly.
    blank = actions.override_to_spec(1, {"type": "camera"})
    assert blank is not None and blank.kind == "camera" and blank.camera_name == ""
    assert blank.label == "Camera"


def test_camera_override_in_override_types():
    assert "camera" in actions.OVERRIDE_TYPES


def test_camera_url_for_selects_named_camera():
    import types
    from foodassistant_streamdeck.controller import Controller

    c = Controller.__new__(Controller)
    c.config = types.SimpleNamespace(cameras=[
        {"name": "Front", "snapshot_url": "http://a/snap"},
        {"name": "Garage", "snapshot_url": "http://b/snap"},
    ], ha_base_url="", ha_token="")
    # Named match (case-insensitive), blank -> first, unknown -> first fallback.
    assert c._camera_url_for("Garage") == "http://b/snap"
    assert c._camera_url_for("garage") == "http://b/snap"
    assert c._camera_url_for("") == "http://a/snap"
    assert c._camera_url_for("Nope") == "http://a/snap"
    # No cameras at all -> empty string, never raises.
    empty = Controller.__new__(Controller)
    empty.config = types.SimpleNamespace(cameras=[], ha_base_url="", ha_token="")
    assert empty._camera_url_for("x") == ""


def test_camera_snapshot_target_uses_bearer_for_ha():
    # An ha_entity resolves to a bearer-authenticated HA URL (no token in query).
    url, headers = actions.camera_snapshot_target(
        {"name": "Door", "ha_entity": "camera.front_door"},
        "http://ha.local:8123", "tok",
    )
    assert url == "http://ha.local:8123/api/camera_proxy/camera.front_door"
    assert headers == {"Authorization": "Bearer tok"}
    # A legacy token-baked URL is recovered to the same bearer form.
    url2, headers2 = actions.camera_snapshot_target(
        {"name": "Door", "snapshot_url": "http://ha.local:8123/api/camera_proxy/camera.front_door?token=LLAT"},
        "http://ha.local:8123", "tok",
    )
    assert url2 == "http://ha.local:8123/api/camera_proxy/camera.front_door"
    assert headers2 == {"Authorization": "Bearer tok"}
    # A plain camera keeps its URL and sends no auth header.
    url3, headers3 = actions.camera_snapshot_target(
        {"name": "Cam", "snapshot_url": "http://192.168.1.5/snap.jpg"}, "", "")
    assert url3 == "http://192.168.1.5/snap.jpg" and headers3 is None


def test_media_override_builds_ha_service_spec():
    # A media override is a stateless HA service call (kind ha_service), not a
    # polled entity, with the service and glyph from MEDIA_ACTIONS.
    spec = actions.override_to_spec(4, {"type": "media", "entity_id": "media_player.kitchen", "action": "next"})
    assert spec is not None
    assert spec.kind == "ha_service"
    assert spec.ha_entity_id == "media_player.kitchen"
    assert spec.ha_service == "media_player.media_next_track"
    assert spec.icon == "skip-forward"
    assert spec.label == "Next"
    # An unknown action falls back to play/pause; a missing entity is dropped.
    assert actions.override_to_spec(1, {"type": "media", "entity_id": "x", "action": "zzz"}).ha_service == "media_player.media_play_pause"
    assert actions.override_to_spec(1, {"type": "media", "action": "next"}) is None
    assert "media" in actions.OVERRIDE_TYPES


def test_scan_mode_action_cycles_via_app():
    # The scan_mode key posts the cycle endpoint and shows the returned label.
    posted = []

    class _Resp:
        status_code = 200
        def json(self):
            return {"mode": "consume", "label": "Use"}

    class _Client:
        async def post(self, url, **k):
            posted.append(url)
            return _Resp()

    async def noop():
        pass

    ctx = actions.ActionContext(
        client=_Client(), base_url="http://x", refresh=noop,
        navigate=lambda p: noop(), cycle_brightness=lambda: 0,
        page_next=lambda: None, page_prev=lambda: None,
    )
    spec = actions.resolve("scan_mode")
    assert spec is not None and spec.kind == "scan_mode"
    msg = asyncio.run(actions.run_action(spec, ctx))
    assert msg == "Use"
    assert posted == ["http://x/pending/scanner-mode/cycle"]
    # It is grouped under Actions in the web key editor.
    items = {i["name"]: i for i in actions.catalog()}
    assert items["scan_mode"]["group"] == "Actions"


def test_overrides_skip_unplaced_and_ignore_id():
    # Custom keys carry a stable id and may be unplaced (slot -1, kept only in the
    # library). The deck applies placed ones (by slot) and ignores the id field.
    overrides = [
        {"id": "c1", "slot": 2, "type": "timer", "minutes": 10, "label": "Tea"},
        {"id": "c2", "slot": -1, "type": "timer", "minutes": 5, "label": "Unplaced"},
    ]
    specs = actions.overrides_to_specs(overrides, key_count=15)
    assert set(specs.keys()) == {2}            # only the placed one
    assert specs[2].kind == "timer"
    assert specs[2].label == "Tea"
    assert specs[2].timer_minutes == 10
