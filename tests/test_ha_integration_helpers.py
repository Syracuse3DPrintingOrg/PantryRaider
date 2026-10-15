"""Pure-logic tests for the Home Assistant integration's payload helpers.

The integration lives under homeassistant/custom_components/pantry_raider and
most of its modules import the heavyweight ``homeassistant`` package, which the
repo test suite deliberately does not install. helpers.py is written to be free
of that dependency, so this test loads it directly by file path. If it ever
grows a Home Assistant import (a mistake), loading it fails and so does every
test here.

The integration's other modules are checked without Home Assistant too: every
file compiles, no Home Assistant auth callback is awaited directly, the two
strings files match, and services.yaml lists every service.
"""

import ast
import base64
import importlib.util
import json
import py_compile
import sys
from pathlib import Path

import pytest
import yaml

_COMPONENT = (
    Path(__file__).resolve().parent.parent
    / "homeassistant"
    / "custom_components"
    / "pantry_raider"
)
_HELPERS_PATH = _COMPONENT / "helpers.py"


def _load_helpers():
    # No skip: helpers.py holds every decision about the Home Assistant
    # account and tokens, so a Home Assistant import leaking in must fail
    # loudly rather than silently skip those tests.
    spec = importlib.util.spec_from_file_location(
        "pantry_raider_helpers", _HELPERS_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helpers = _load_helpers()


def _server_payload():
    return {
        "app": "pantryraider",
        "version": "0.18.21",
        "mode": "pi_hosted",
        "device_id": "srv-1",
        "hostname": "pr",
        "display": {
            "idle_timeout": 10,
            "screensaver_minutes": 5,
            "screensaver_mode": "bounce",
            "wake_on_presence": "auto",
        },
        "presence": {"available": True, "detected": False},
        "printers": {"label_queue": "idle", "document_queue": "idle", "queues": []},
        "expiring": {
            "expired": 2,
            "today": 1,
            "within_3_days": 3,
            "within_7_days": 4,
        },
        "counts": {"pending": 6, "action_items": 7},
        "timers": {
            "running": 2,
            "next": {"label": "Pasta", "remaining_seconds": 125.0},
            "active": [
                {"label": "Pasta", "remaining_seconds": 462.0, "expired": False},
                {"label": "Soft egg", "remaining_seconds": 130.0, "expired": False},
            ],
        },
        "thermometers": [
            {
                "id": "t1",
                "name": "TempSpike",
                "battery": 80,
                "stale": False,
                "probes": [
                    {"index": 0, "role": "food", "role_label": "Internal", "temp_c": 55.0, "target_c": 74.0},
                ],
            }
        ],
        "satellites": [
            {"device_id": "bandit-9", "hostname": "kitchen", "ip": "192.168.1.50", "version": "0.18.21"},
            {"hostname": "no-id", "ip": "192.168.1.51"},
        ],
    }


def _remote_payload():
    return {
        "app": "pantryraider",
        "version": "0.18.21",
        "mode": "pi_remote",
        "device_id": "bandit-9",
        "hostname": "kitchen",
        "display": {
            "idle_timeout": 8,
            "screensaver_minutes": 3,
            "screensaver_mode": "starfield",
            "wake_on_presence": "on",
        },
        "presence": {"available": True, "detected": True},
        "printers": {"label_queue": "idle", "document_queue": "idle", "queues": []},
    }


def test_is_server_mode():
    assert helpers.is_server_mode("server")
    assert helpers.is_server_mode("pi_hosted")
    assert not helpers.is_server_mode("pi_remote")
    assert not helpers.is_server_mode(None)


def test_server_device_model():
    assert helpers.server_device_model("server") == "Pantry Raider Server"
    assert helpers.server_device_model("pi_hosted") == "Pantry Raider Appliance"
    assert helpers.server_device_model("pi_remote") == "Pantry Raider Bandit"
    assert helpers.server_device_model("weird") == "Pantry Raider Server"


def test_format_timer_remaining():
    assert helpers.format_timer_remaining(125) == "2:05"
    assert helpers.format_timer_remaining(0) == "0:00"
    assert helpers.format_timer_remaining(-4) == "0:00"
    assert helpers.format_timer_remaining(3661) == "61:01"
    assert helpers.format_timer_remaining(None) == "0:00"


def test_next_timer_state():
    assert helpers.next_timer_state({"label": "Eggs", "remaining_seconds": 61}) == "Eggs 1:01"
    assert helpers.next_timer_state(None) == "none"
    assert helpers.next_timer_state({"label": "", "remaining_seconds": 5}) == "none"


def test_applicable_sensor_keys_by_mode():
    server_keys = helpers.applicable_sensor_keys(_server_payload())
    assert "expired" in server_keys and "next_timer" in server_keys
    assert "version" in server_keys and "label_queue" in server_keys
    assert "timers_summary" in server_keys

    remote_keys = helpers.applicable_sensor_keys(_remote_payload())
    assert remote_keys == ["version", "label_queue"]


def test_sensor_value_reads_nested_fields():
    data = _server_payload()
    assert helpers.sensor_value("expired", data) == 2
    assert helpers.sensor_value("today", data) == 1
    assert helpers.sensor_value("within_3_days", data) == 3
    assert helpers.sensor_value("within_7_days", data) == 4
    assert helpers.sensor_value("pending", data) == 6
    assert helpers.sensor_value("action_items", data) == 7
    assert helpers.sensor_value("timers_running", data) == 2
    assert helpers.sensor_value("timers_summary", data) == "Pasta 7:42, Soft egg 2:10"
    assert helpers.sensor_value("next_timer", data) == "Pasta 2:05"
    assert helpers.sensor_value("label_queue", data) == "idle"
    assert helpers.sensor_value("version", data) == "0.18.21"


def test_sensor_value_tolerates_missing_sections():
    assert helpers.sensor_value("expired", {}) is None
    assert helpers.sensor_value("label_queue", {}) is None
    assert helpers.sensor_value("next_timer", {}) == "none"


def test_expiring_attention():
    assert helpers.expiring_attention(_server_payload()) is True
    assert helpers.expiring_attention({"expiring": {"expired": 0, "today": 0}}) is False
    assert helpers.expiring_attention({}) is False


def test_probe_naming_and_ids():
    assert helpers.probe_display_name("TempSpike", "Internal") == "TempSpike Internal"
    assert helpers.probe_display_name("TempSpike", "") == "TempSpike"
    assert helpers.probe_display_name(None, None) == "Thermometer"
    assert helpers.probe_unique_id("srv-1", "t1", 0) == "srv-1_t1_p0"


def test_iter_probes_skips_bad_rows():
    data = {
        "thermometers": [
            {"id": "t1", "name": "A", "probes": [{"index": 0}, "junk"]},
            "not-a-dict",
        ]
    }
    pairs = list(helpers.iter_probes(data))
    assert len(pairs) == 1
    assert pairs[0][1]["index"] == 0


def test_satellite_device_ids_drops_rows_without_id():
    ids = helpers.satellite_device_ids(_server_payload())
    assert ids == ["bandit-9"]
    assert helpers.satellite_device_ids({}) == []


def test_active_timers_shapes_rows():
    active = helpers.active_timers(_server_payload())
    assert active == [
        {"label": "Pasta", "remaining_seconds": 462.0, "expired": False},
        {"label": "Soft egg", "remaining_seconds": 130.0, "expired": False},
    ]


def test_active_timers_degrades_when_absent():
    # Older servers omit timers.active entirely.
    assert helpers.active_timers({"timers": {"running": 1}}) == []
    assert helpers.active_timers({}) == []
    assert helpers.active_timers({"timers": {"active": "nope"}}) == []


def test_active_timers_skips_bad_rows_and_defaults_expired():
    data = {
        "timers": {
            "active": [
                {"label": "A", "remaining_seconds": 5},
                "junk",
                {"label": "B", "remaining_seconds": 0, "expired": True},
            ]
        }
    }
    active = helpers.active_timers(data)
    assert active == [
        {"label": "A", "remaining_seconds": 5, "expired": False},
        {"label": "B", "remaining_seconds": 0, "expired": True},
    ]


def test_active_timers_caps_at_twenty():
    data = {"timers": {"active": [{"label": f"t{i}", "remaining_seconds": i} for i in range(30)]}}
    assert len(helpers.active_timers(data)) == 20


def test_timers_summary_string():
    active = [
        {"label": "Pasta", "remaining_seconds": 462.0, "expired": False},
        {"label": "Soft egg", "remaining_seconds": 130.0, "expired": False},
    ]
    assert helpers.timers_summary(active) == "Pasta 7:42, Soft egg 2:10"
    assert helpers.timers_summary([]) == "none"
    assert helpers.timers_summary("nope") == "none"
    # Rows without a label are skipped, not rendered as blank entries.
    assert helpers.timers_summary([{"remaining_seconds": 5}]) == "none"


def test_timers_summary_clips_to_state_limit():
    active = [{"label": "Long label here", "remaining_seconds": 61} for _ in range(40)]
    summary = helpers.timers_summary(active)
    assert len(summary) <= 255


def test_normalize_detection_type():
    assert helpers.normalize_detection_type("person") == "person"
    assert helpers.normalize_detection_type("  Vehicle ") == "vehicle"
    assert helpers.normalize_detection_type("ANIMAL") == "animal"
    assert helpers.normalize_detection_type("visitor") == "visitor"
    assert helpers.normalize_detection_type("dragon") == ""
    assert helpers.normalize_detection_type("") == ""
    assert helpers.normalize_detection_type(None) == ""
    assert helpers.normalize_detection_type(3) == ""


def test_detection_types_match_the_app():
    # The app's /events/camera-detect accepts exactly these four.
    assert helpers.DETECTION_TYPES == ("person", "vehicle", "animal", "visitor")


def test_clamp_popup_seconds():
    assert helpers.clamp_popup_seconds(20) == 20
    assert helpers.clamp_popup_seconds("45") == 45
    assert helpers.clamp_popup_seconds(0) == 0
    # Over the five-minute cap comes back clamped, not rejected.
    assert helpers.clamp_popup_seconds(9999) == 300
    # Bad input degrades to 0, meaning "use the install's default".
    assert helpers.clamp_popup_seconds(-5) == 0
    assert helpers.clamp_popup_seconds(None) == 0
    assert helpers.clamp_popup_seconds("soon") == 0


def test_match_install():
    sats = ["bandit-9", "bandit-10"]
    assert helpers.match_install("srv-1", "srv-1", sats) == "main"
    assert helpers.match_install("bandit-9", "srv-1", sats) == "bandit-9"
    assert helpers.match_install("bandit-11", "srv-1", sats) is None
    assert helpers.match_install("", "srv-1", sats) is None
    assert helpers.match_install(None, "srv-1", sats) is None
    # Ids are compared as strings, however they arrive.
    assert helpers.match_install(42, 42, []) == "main"
    assert helpers.match_install("42", 42, []) == "main"
    # A dict's keys view works as the satellite list.
    assert helpers.match_install("b1", "srv", {"b1": object()}.keys()) == "b1"


def test_has_display():
    # A plain server drives no screen of its own.
    assert not helpers.has_display("server", False)
    # Appliances and bandits do.
    assert helpers.has_display("pi_hosted", False)
    assert helpers.has_display("pi_remote", False)
    # Any discovered bandit (is_satellite) always has a display.
    assert helpers.has_display("server", True)
    assert helpers.has_display(None, True)


# --- Home Assistant account and token handover ---------------------------------

NOW = "2026-10-01T12:00:00+00:00"
NEW_APP = {
    "mode": "server",
    "features": ["ha_token_dedicated", "probe_alert_service", "ha_proxy",
                 "pairing_kinds", "credential_exchange"],
    "ha_access": {"needs": ["read"], "token_kind": "dedicated"},
}
OLD_APP = {"mode": "server"}


def _app(**access):
    state = json.loads(json.dumps(NEW_APP))
    state["ha_access"].update(access)
    return state


def _plan(**kw):
    args = {"is_server": True, "connect_back": True, "state": NEW_APP,
            "options": {}, "now_iso": NOW, "used_internal_url": True}
    args.update(kw)
    return helpers.handover_plan(**args)


def test_helpers_module_loaded():
    assert hasattr(helpers, "handover_plan")


def test_client_header():
    assert helpers.client_header("0.21.0") == "ha-component/0.21.0"
    assert helpers.client_header("") == "ha-component/unknown"
    assert helpers.client_header(None) == "ha-component/unknown"
    assert len(helpers.client_header("9" * 200)) == 64


def test_dedicated_supported():
    assert helpers.dedicated_supported(NEW_APP) is True
    assert helpers.dedicated_supported(OLD_APP) is False
    assert helpers.dedicated_supported({"features": ["ha_token_dedicated"]}) is False
    assert helpers.dedicated_supported({"features": "ha_token_dedicated"}) is False
    assert helpers.dedicated_supported(None) is False


def test_desired_group():
    assert helpers.desired_group(["read", "control"]) == "system-users"
    assert helpers.desired_group(["read"]) == "system-read-only"
    assert helpers.desired_group([]) == "system-read-only"


def test_local_only_for():
    assert helpers.local_only_for(True) is True
    assert helpers.local_only_for(False) is False


def test_rotation_due():
    assert helpers.rotation_due("2025-10-02T12:00:00+00:00", NOW) is False  # 364 days
    assert helpers.rotation_due("2025-10-01T12:00:00+00:00", NOW) is True   # 365 days
    assert helpers.rotation_due("", NOW) is True
    assert helpers.rotation_due(None, NOW) is True
    assert helpers.rotation_due("garbage", NOW) is True
    assert helpers.rotation_due("2026-09-30T12:00:00Z", NOW) is False


def test_token_names():
    assert helpers.owner_token_name("Kitchen", "abcdef123456") == "Pantry Raider (Kitchen abcdef)"
    assert helpers.owner_token_name("", "abcdef123456") == "Pantry Raider (install abcdef)"
    assert helpers.owner_token_name("Kitchen", "abcdef") != helpers.owner_token_name("Kitchen", "zzzzzz")
    assert helpers.dedicated_user_name("Kitchen") == "Pantry Raider (Kitchen)"
    assert helpers.dedicated_user_name(None) == "Pantry Raider (install)"
    assert helpers.dedicated_token_name(3) == "Pantry Raider 3"
    # None of them is the shared name older versions used for the owner token.
    assert "Pantry Raider" not in (helpers.owner_token_name("K", "e"),
                                   helpers.dedicated_user_name("K"),
                                   helpers.dedicated_token_name(1))


def test_legacy_owner_token_removable():
    one = [{"history": True, "token_kind": "dedicated"}]
    assert helpers.legacy_owner_token_removable(one) is True
    two = one + [{"history": True, "token_kind": "dedicated"}]
    assert helpers.legacy_owner_token_removable(two) is True
    mixed = one + [{"history": True, "token_kind": ""}]
    assert helpers.legacy_owner_token_removable(mixed) is False
    owner_path = one + [{"history": True, "token_kind": "owner"}]
    assert helpers.legacy_owner_token_removable(owner_path) is False
    no_history = one + [{"history": False, "token_kind": ""}]
    assert helpers.legacy_owner_token_removable(no_history) is True


def test_pasted_by_hand():
    shared = "Pantry Raider"
    assert helpers.pasted_by_hand(None, [], shared) is False
    assert helpers.pasted_by_hand({"id": ""}, [], shared) is False
    tok = {"id": "t1", "client_name": "Kitchen", "owner": True}
    assert helpers.pasted_by_hand(tok, [], shared) is True
    assert helpers.pasted_by_hand(tok, ["t1", None], shared) is False
    assert helpers.pasted_by_hand(dict(tok, client_name=shared), [], shared) is False
    assert helpers.pasted_by_hand(
        dict(tok, client_name=shared, owner=False), [], shared) is True


def test_plan_satellite_does_nothing():
    plan = _plan(is_server=False)
    assert plan["action"] == "none" and plan["mint"] is False and plan["repair"] is None


def test_plan_connect_back_off():
    assert _plan(connect_back=False)["action"] == "none"
    own = {"ha_token_id": "t1"}
    assert _plan(connect_back=False, options=own)["action"] == "disconnect_own"
    owner = {"ha_owner_token_id": "o1"}
    assert _plan(connect_back=False, options=owner)["action"] == "disconnect_own"
    # The old app's shared owner token was not minted by this entry.
    assert _plan(connect_back=False, state=OLD_APP,
                 options={"connect_done": True})["action"] == "none"


def test_plan_manual_token_is_left_alone():
    manual = _app(token_kind="manual")
    off = _plan(connect_back=False, state=manual, options={"ha_token_id": "t1"})
    assert off["action"] == "none" and off["mint"] is False
    on = _plan(state=manual, options={"ha_token_id": "t1", "ha_token_kind": "dedicated"})
    assert on["action"] == "none" and on["mint"] is False
    assert on["repair"] == "manual_token"


def test_plan_old_app():
    first = _plan(state=OLD_APP)
    assert first["action"] == "owner" and first["mint"] is True
    assert first["repair"] == "app_update_needed"
    done = _plan(state=OLD_APP, options={"connect_done": True})
    assert done["action"] == "owner" and done["mint"] is False
    assert done["repair"] == "app_update_needed"


def test_plan_app_rolled_back_after_a_limited_account():
    """The install went back to a version without limited accounts after
    this entry handed it one. That version sends probe alerts the way only an
    administrator may, so it gets an owner token named for this entry, once."""
    dedicated = {"connect_done": True, "ha_token_id": "t1", "ha_token_kind": "dedicated",
                 "ha_token_created": "2026-09-01T00:00:00+00:00"}
    plan = _plan(state=OLD_APP, options=dedicated)
    assert plan["action"] == "owner" and plan["mint"] is True
    # After that handover the entry records an owner token: no second one.
    handed = dict(dedicated, ha_token_kind="owner", ha_owner_token_id="o1")
    assert _plan(state=OLD_APP, options=handed)["mint"] is False


def test_plan_new_app_first_time():
    plan = _plan(state=_app(token_kind=""))
    assert plan["action"] == "dedicated"
    assert plan["mint"] is True and plan["rotate"] is False
    assert plan["group"] == "system-read-only"
    assert plan["local_only"] is True
    assert plan["repair"] is None


def test_plan_new_app_with_control_and_external_url():
    plan = _plan(state=_app(token_kind="", needs=["read", "control"]), used_internal_url=False)
    assert plan["group"] == "system-users"
    assert plan["local_only"] is False


def test_plan_needs_missing_keeps_control():
    state = _app(token_kind="")
    del state["ha_access"]["needs"]
    assert _plan(state=state)["group"] == "system-users"


def test_plan_settled_and_rotation_due():
    settled = {"ha_token_id": "t1", "ha_token_kind": "dedicated",
               "ha_token_created": "2026-09-01T00:00:00+00:00"}
    plan = _plan(options=settled)
    assert plan["action"] == "dedicated" and plan["mint"] is False and plan["rotate"] is False
    old = dict(settled, ha_token_created="2025-09-01T00:00:00+00:00")
    plan = _plan(options=old)
    assert plan["mint"] is True and plan["rotate"] is True


def test_plan_app_reports_owner_legacy_after_a_restore():
    settled = {"ha_token_id": "t1", "ha_token_kind": "dedicated",
               "ha_token_created": "2026-09-01T00:00:00+00:00"}
    plan = _plan(state=_app(token_kind="owner-legacy"), options=settled)
    assert plan["action"] == "dedicated"
    assert plan["mint"] is True and plan["rotate"] is True


def test_token_ref_for_id_matches_the_app():
    """The integration and the app hash the refresh token id the same way,
    so the app's ha_access.token_ref can be matched without either side
    showing the token or its id."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))
    from app.services import ha_client

    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    access = f"{seg({'alg': 'HS256'})}.{seg({'iss': 'refresh-id-1', 'exp': 1})}.sig"
    assert ha_client.token_ref(access) == helpers.token_ref_for_id("refresh-id-1")
    assert len(helpers.token_ref_for_id("refresh-id-1")) == 16
    assert "refresh-id-1" not in helpers.token_ref_for_id("refresh-id-1")
    assert helpers.token_ref_for_id("") == ""
    assert helpers.token_ref_for_id(None) == ""


def test_remember_removed_ref_is_capped_and_deduplicated():
    refs = []
    for i in range(12):
        refs = helpers.remember_removed_ref(refs, f"t{i}")
    assert len(refs) == helpers.MAX_REMOVED_REFS
    assert refs[-1] == helpers.token_ref_for_id("t11")
    assert helpers.token_ref_for_id("t0") not in refs
    assert helpers.remember_removed_ref(refs, "t11") == refs
    assert helpers.remember_removed_ref(None, "") == []


def test_plan_app_holding_a_removed_token_gets_a_new_one():
    """A handover whose reply timed out after the app saved the token, or a
    backup restored from before a yearly replacement: the app reports a
    token this entry removed, so its link is dead and it gets a new one."""
    settled = {"ha_token_id": "t2", "ha_token_kind": "dedicated",
               "ha_token_created": "2026-09-01T00:00:00+00:00",
               "ha_removed_refs": [helpers.token_ref_for_id("t1")]}
    plan = _plan(state=_app(token_ref=helpers.token_ref_for_id("t1")), options=settled)
    assert plan["action"] == "dedicated" and plan["mint"] is True
    # The token it was given, or one this entry knows nothing about (another
    # Home Assistant's), is left alone.
    for ref in (helpers.token_ref_for_id("t2"), helpers.token_ref_for_id("elsewhere"), ""):
        plan = _plan(state=_app(token_ref=ref), options=settled)
        assert plan["mint"] is False, ref


def test_plan_after_owner_path_moves_to_dedicated():
    plan = _plan(state=_app(token_kind="owner-legacy"),
                 options={"connect_done": True, "ha_owner_token_id": "o1",
                          "ha_token_kind": "owner"})
    assert plan["action"] == "dedicated" and plan["mint"] is True
    assert plan["rotate"] is False


def test_pairing_result():
    assert helpers.pairing_result({"status": "approved", "api_key": "k"}) == "approved"
    assert helpers.pairing_result({"status": "approved"}) == "approved_no_key"
    assert helpers.pairing_result({"status": "approved", "api_key": ""}) == "approved_no_key"
    assert helpers.pairing_result({"status": "pending"}) == "pending"
    assert helpers.pairing_result({"status": "denied"}) == "denied"
    assert helpers.pairing_result({"status": "expired"}) == "expired"
    assert helpers.pairing_result({}) == "expired"
    assert helpers.pairing_result(None) == "expired"


def test_auth_failure_action():
    assert helpers.auth_failure_action(False) == "reauth"
    assert helpers.auth_failure_action(True) == "warn"


def test_grace_issue_placeholders():
    state = {"auth": {"grace_until": "2026-10-15T08:00:00Z"}}
    assert helpers.grace_issue_placeholders(state) == {"date": "2026-10-15"}
    assert helpers.grace_issue_placeholders({"auth": {"grace_until": None}}) is None
    assert helpers.grace_issue_placeholders({}) is None
    assert helpers.grace_issue_placeholders(None) is None


@pytest.mark.parametrize("kind,expected", [
    ("name", "open_install_name"),
    ("proxied", "open_install_grace"),
    ("public", "open_install_grace"),
    (None, "open_install_grace"),
    ("missing", "open_install_grace"),
])
def test_grace_issue_id(kind, expected):
    auth = {"grace_until": "2026-10-15T08:00:00Z"}
    if kind != "missing":
        auth["grace_kind"] = kind
    assert helpers.grace_issue_id({"auth": auth}) == expected


def test_grace_issue_id_without_grace():
    assert helpers.grace_issue_id({"auth": {"grace_until": None, "grace_kind": "name"}}) is None
    assert helpers.grace_issue_id({}) is None


def test_probe_alert_event_data_is_an_equal_copy():
    data = {"device_id": "HA:SENSOR.GRILL", "probe": "1", "message": "done"}
    out = helpers.probe_alert_event_data(data)
    assert out == data and out is not data


# --- The rest of the integration, checked without Home Assistant ----------------

def _const_values() -> dict:
    tree = ast.parse((_COMPONENT / "const.py").read_text())
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant):
                out[target.id] = node.value.value
    return out


def test_helpers_constants_match_const():
    const = _const_values()
    for name in ("OPT_CONNECT_DONE", "OPT_TOKEN_ID", "OPT_TOKEN_CREATED", "OPT_TOKEN_KIND",
                 "OPT_OWNER_TOKEN_ID", "OPT_REMOVED_REFS", "ROTATE_AFTER_DAYS",
                 "ISSUE_APP_UPDATE",
                 "ISSUE_OPEN_GRACE", "ISSUE_OPEN_NAME", "ISSUE_MANUAL_TOKEN"):
        assert const[name] == getattr(helpers, name), name
    assert const["CONNECT_BACK_CLIENT_NAME"] == "Pantry Raider"
    assert const["DEDICATED_TOKEN_DAYS"] == 400
    assert const["OLD_TOKEN_GRACE_SECONDS"] == 3600
    assert const["CLIENT_CAPS"] == "pair1"
    assert const["SERVICE_PROBE_ALERT"] == "probe_alert"
    assert const["EVENT_PROBE_ALERT"] == "pantry_raider_probe_alert"


def test_every_integration_module_compiles(tmp_path):
    files = sorted(_COMPONENT.glob("*.py"))
    assert (_COMPONENT / "repairs.py") in files
    for path in files:
        py_compile.compile(str(path), cfile=str(tmp_path / (path.name + "c")), doraise=True)


_CALLBACKS = {"async_remove_refresh_token", "async_get_refresh_token",
              "async_create_access_token"}


def test_auth_callbacks_are_never_awaited_directly():
    """Current Home Assistant releases make these plain callbacks; awaiting
    one raises TypeError, which once made every token removal fail."""
    tree = ast.parse((_COMPONENT / "__init__.py").read_text())
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in _CALLBACKS:
                seen.add(node.func.attr)
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            func = node.value.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            assert name not in _CALLBACKS, f"{name} is awaited at line {node.lineno}"
    assert seen == _CALLBACKS


def _keys(tree, prefix=""):
    out = set()
    for key, value in tree.items():
        path = f"{prefix}.{key}" if prefix else key
        out.add(path)
        if isinstance(value, dict):
            out |= _keys(value, path)
    return out


def test_strings_and_translations_match():
    strings = json.loads((_COMPONENT / "strings.json").read_text())
    english = json.loads((_COMPONENT / "translations" / "en.json").read_text())
    assert _keys(strings) == _keys(english)
    for path in (_COMPONENT / "strings.json", _COMPONENT / "translations" / "en.json"):
        assert "\u2014" not in path.read_text()
    for issue in ("pair_needed", "pairing_off", "app_update_needed", "open_install_grace",
                  "open_install_name", "manual_token"):
        assert issue in strings["issues"], issue
    for issue in ("pair_needed", "pairing_off"):
        assert "confirm" in strings["issues"][issue]["fix_flow"]["step"]
    assert "reauth_confirm" in strings["config"]["step"]
    assert "pairing_key_missing" in strings["config"]["error"]
    assert "reauth_successful" in strings["config"]["abort"]
    assert "{host}" in strings["issues"]["open_install_grace"]["description"]
    assert "{date}" in strings["issues"]["open_install_name"]["description"]


def test_services_yaml_lists_every_service():
    services = yaml.safe_load((_COMPONENT / "services.yaml").read_text())
    assert set(services) >= {"camera_popup", "camera_detect", "probe_alert"}
    strings = json.loads((_COMPONENT / "strings.json").read_text())
    assert set(services) == set(strings["services"])


def test_http_url_brackets_an_ipv6_address():
    h = _load_helpers()
    assert h.http_url("192.168.1.20", 9284) == "http://192.168.1.20:9284"
    assert h.http_url("fd00::20", 9284) == "http://[fd00::20]:9284"
    assert h.http_url("[fd00::20]", 80) == "http://[fd00::20]:80"
    assert h.http_url("fd00::21") == "http://[fd00::21]"
    assert h.http_url("pantry.local") == "http://pantry.local"


def test_no_module_builds_an_http_url_by_hand():
    """Every install and Bandit address goes through helpers.http_url, so an
    IPv6 address is always bracketed."""
    for path in _COMPONENT.glob("*.py"):
        if path.name == "helpers.py":
            continue
        assert 'f"http://{' not in path.read_text(), path.name
