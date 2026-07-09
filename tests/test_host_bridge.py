"""Unit tests for the Pantry Raider host bridge helpers.

The bridge is a plain python http.server script with no .py extension, so it is
loaded here from its source path. These tests cover the pure helpers that gate
the background Mealie start (FoodAssistant-5wc): the install/start tracking map
and the compose environment used to invoke docker compose.

Run: python -m pytest tests/test_host_bridge.py -q
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BRIDGE = REPO / "scripts" / "image-build" / "foodassistant-host-bridge"


def _load_bridge():
    spec = importlib.util.spec_from_loader(
        "foodassistant_host_bridge",
        importlib.machinery.SourceFileLoader("foodassistant_host_bridge", str(BRIDGE)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


import importlib.machinery  # noqa: E402

bridge = _load_bridge()


def test_installing_false_when_no_proc():
    bridge._INSTALL_PROCS.pop("mealie", None)
    assert bridge._installing("mealie") is False


def test_installing_true_while_running_then_false_when_done():
    class FakeProc:
        def __init__(self):
            self._done = False

        def poll(self):
            return None if not self._done else 0

    p = FakeProc()
    bridge._INSTALL_PROCS["mealie"] = p
    try:
        assert bridge._installing("mealie") is True
        p._done = True
        assert bridge._installing("mealie") is False
    finally:
        bridge._INSTALL_PROCS.pop("mealie", None)


def test_compose_env_defaults_repo_dir(monkeypatch):
    # With REPO_DIR unset and no provisioner found, fall back to the same
    # default the appliance compose file uses for its build context.
    monkeypatch.delenv("REPO_DIR", raising=False)
    monkeypatch.setattr(bridge, "_find_firstboot", lambda: (None, None))
    env = bridge._compose_env()
    assert env["REPO_DIR"] == "/home/foodassistant/FoodAssistant"


def test_compose_env_honors_existing_repo_dir(monkeypatch):
    monkeypatch.setenv("REPO_DIR", "/custom/repo")
    env = bridge._compose_env()
    assert env["REPO_DIR"] == "/custom/repo"


def test_compose_env_uses_provisioner_repo_dir(monkeypatch):
    monkeypatch.delenv("REPO_DIR", raising=False)
    monkeypatch.setattr(
        bridge, "_find_firstboot", lambda: ("/x/scripts/image-build/firstboot.sh", "/x")
    )
    env = bridge._compose_env()
    assert env["REPO_DIR"] == "/x"


# Touch provisioning: write the ADS7846 SPI overlay on a running Pi when the
# display type was chosen in the wizard after first boot (FoodAssistant-vbfp).


def test_config_has_active_ignores_comments():
    text = "dtparam=audio=on\n#dtparam=spi=on\n  # dtoverlay=ads7846\n"
    assert bridge._config_has_active(text, "dtparam=audio=on") is True
    assert bridge._config_has_active(text, "dtparam=spi=on") is False
    assert bridge._config_has_active(text, "dtoverlay=ads7846") is False


def test_provision_touch_ads7846_writes_spi_and_overlay(tmp_path):
    cfg = tmp_path / "config.txt"
    cfg.write_text("dtparam=audio=on\ndtoverlay=vc4-kms-v3d\n")
    changed, needs_reboot = bridge._provision_touch("ads7846", str(cfg))
    assert changed is True and needs_reboot is True
    body = cfg.read_text()
    assert "dtparam=spi=on" in body
    assert "dtoverlay=ads7846," in body
    assert "[all]" in body


def test_provision_touch_ads7846_is_idempotent(tmp_path):
    cfg = tmp_path / "config.txt"
    cfg.write_text("dtparam=audio=on\n")
    bridge._provision_touch("ads7846", str(cfg))
    first = cfg.read_text()
    changed, needs_reboot = bridge._provision_touch("ads7846", str(cfg))
    assert changed is False and needs_reboot is False
    assert cfg.read_text() == first          # no duplicate lines
    assert first.count("dtoverlay=ads7846,") == 1


def test_provision_touch_only_adds_missing_overlay(tmp_path):
    cfg = tmp_path / "config.txt"
    cfg.write_text("dtparam=spi=on\n")        # SPI already on, overlay missing
    changed, _ = bridge._provision_touch("ads7846", str(cfg))
    assert changed is True
    body = cfg.read_text()
    assert body.count("dtparam=spi=on") == 1  # not added again
    assert "dtoverlay=ads7846," in body


def test_provision_touch_non_ads7846_is_noop(tmp_path):
    cfg = tmp_path / "config.txt"
    cfg.write_text("dtparam=audio=on\n")
    for driver in ("usb", "generic", "none"):
        assert bridge._provision_touch(driver, str(cfg)) == (False, False)
    assert cfg.read_text() == "dtparam=audio=on\n"


def test_provision_touch_missing_config_raises(monkeypatch):
    import pytest
    monkeypatch.setattr(bridge, "_pi_config_txt", lambda: "")  # not a Pi
    with pytest.raises(OSError):
        bridge._provision_touch("ads7846")       # no config found


# Mealie readiness probe (FoodAssistant-28z)


def test_http_serving_true_on_2xx(monkeypatch):
    class FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(bridge.urllib.request, "urlopen", lambda *a, **k: FakeResp())
    assert bridge._http_serving("http://127.0.0.1:9285/") is True


def test_http_serving_true_on_http_error(monkeypatch):
    def raise_http_error(*a, **k):
        raise bridge.urllib.error.HTTPError("u", 401, "no", {}, None)
    monkeypatch.setattr(bridge.urllib.request, "urlopen", raise_http_error)
    # A 401 still means the server answered, so it is serving.
    assert bridge._http_serving("http://127.0.0.1:9285/") is True


def test_http_serving_false_on_connection_refused(monkeypatch):
    def raise_conn(*a, **k):
        raise ConnectionRefusedError("refused")
    monkeypatch.setattr(bridge.urllib.request, "urlopen", raise_conn)
    assert bridge._http_serving("http://127.0.0.1:9285/") is False


def test_http_serving_false_on_5xx(monkeypatch):
    class FakeResp:
        status = 502
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(bridge.urllib.request, "urlopen", lambda *a, **k: FakeResp())
    assert bridge._http_serving("http://127.0.0.1:9285/") is False


# Install/start log tailing (FoodAssistant-59z)


def test_tail_log_unknown_name_returns_empty():
    assert bridge._tail_log("nope") == []


def test_tail_log_missing_file_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setitem(bridge._LOG_PATHS, "mealie", str(tmp_path / "absent.log"))
    assert bridge._tail_log("mealie") == []


def test_tail_log_reads_lines_and_drops_blank(monkeypatch, tmp_path):
    p = tmp_path / "mealie.log"
    p.write_text("pulling image\n\nstarting container\n")
    monkeypatch.setitem(bridge._LOG_PATHS, "mealie", str(p))
    assert bridge._tail_log("mealie") == ["pulling image", "starting container"]


def test_tail_log_caps_bytes_and_drops_partial_first_line(monkeypatch, tmp_path):
    p = tmp_path / "kiosk.log"
    # Three lines; cap below the full size so we seek into the middle of line 1.
    p.write_text("AAAAAAAAAA\nBBBBBBBBBB\nCCCCCCCCCC\n")
    monkeypatch.setitem(bridge._LOG_PATHS, "kiosk", str(p))
    lines = bridge._tail_log("kiosk", max_bytes=20)
    # The partial leading line is dropped; only whole trailing lines remain.
    assert "AAAAAAAAAA" not in lines
    assert lines[-1] == "CCCCCCCCCC"


def test_tail_log_grocy_points_at_firstboot_log():
    # The Grocy/stack install runs inside firstboot (deploy_stack), so the
    # setup wizard's live Grocy install window tails that log
    # (FoodAssistant-n5ky).
    assert bridge._LOG_PATHS["grocy"] == "/var/log/foodassistant-firstboot.log"


def test_tail_log_grocy_reads_firstboot_output(monkeypatch, tmp_path):
    p = tmp_path / "firstboot.log"
    p.write_text("[firstboot] deploy_stack\npulling grocy image\n")
    monkeypatch.setitem(bridge._LOG_PATHS, "grocy", str(p))
    assert bridge._tail_log("grocy") == [
        "[firstboot] deploy_stack", "pulling grocy image",
    ]


def test_tail_log_handles_non_utf8(monkeypatch, tmp_path):
    p = tmp_path / "sd.log"
    p.write_bytes(b"ok line\n\xff\xfe bad bytes\n")
    monkeypatch.setitem(bridge._LOG_PATHS, "streamdeck", str(p))
    # Decodes with replacement instead of raising.
    lines = bridge._tail_log("streamdeck")
    assert lines[0] == "ok line"
    assert len(lines) == 2


# Wi-Fi parsing helpers (FoodAssistant-cqw)


def test_nmcli_split_plain():
    assert bridge._nmcli_split("wlan0:wifi:connected") == ["wlan0", "wifi", "connected"]


def test_nmcli_split_unescapes_colon_and_backslash():
    # nmcli escapes ':' as '\:' inside a field value (for example an SSID).
    assert bridge._nmcli_split(r"My\:Net:80:WPA2") == ["My:Net", "80", "WPA2"]
    assert bridge._nmcli_split(r"a\\b:1") == ["a\\b", "1"]


def test_parse_wifi_device_finds_wifi():
    out = "eth0:ethernet:connected\nwlan0:wifi:disconnected\nlo:loopback:unmanaged\n"
    assert bridge._parse_wifi_device(out) == ("wlan0", "disconnected")


def test_parse_wifi_device_none_when_no_wifi():
    out = "eth0:ethernet:connected\nlo:loopback:unmanaged\n"
    assert bridge._parse_wifi_device(out) == (None, None)


def test_parse_wifi_device_reports_unmanaged_state():
    out = "wlan0:wifi:unmanaged\n"
    assert bridge._parse_wifi_device(out) == ("wlan0", "unmanaged")


def test_parse_active_ssid():
    out = "no:OtherNet\nyes:HomeNet\nno:Guest\n"
    assert bridge._parse_active_ssid(out) == "HomeNet"


def test_parse_active_ssid_none_connected():
    out = "no:OtherNet\nno:Guest\n"
    assert bridge._parse_active_ssid(out) == ""


def test_parse_active_ssid_with_escaped_colon():
    out = r"yes:Home\:Net" + "\n"
    assert bridge._parse_active_ssid(out) == "Home:Net"


def test_parse_wifi_scan_sorts_and_dedupes():
    out = (
        "HomeNet:42:WPA2\n"
        "HomeNet:88:WPA2\n"   # stronger duplicate wins
        "Cafe:55:--\n"
        ":30:WPA2\n"          # hidden / blank SSID dropped
    )
    nets = bridge._parse_wifi_scan(out)
    assert nets == [
        {"ssid": "HomeNet", "signal": 88, "security": "WPA2"},
        {"ssid": "Cafe", "signal": 55, "security": "--"},
    ]


def test_parse_wifi_scan_handles_bad_signal():
    out = "Net:xx:WPA2\n"
    nets = bridge._parse_wifi_scan(out)
    assert nets == [{"ssid": "Net", "signal": 0, "security": "WPA2"}]


def test_parse_wifi_scan_empty():
    assert bridge._parse_wifi_scan("") == []


# AP fallback flag (FoodAssistant-ac7)


def test_ap_status_inactive_when_no_flag(monkeypatch):
    monkeypatch.setattr(
        bridge.Path, "exists", lambda self: False
    )
    assert bridge.Path(bridge._AP_FLAG).exists() is False


def test_ap_status_active_when_flag_present(monkeypatch):
    monkeypatch.setattr(
        bridge.Path, "exists", lambda self: True
    )
    assert bridge.Path(bridge._AP_FLAG).exists() is True


# Attached-hardware detection from sysfs (FoodAssistant-92e.3)


def _make_drm(root, connectors):
    """Create <root>/<name>/status files. connectors maps name -> status."""
    for name, status in connectors.items():
        d = root / name
        d.mkdir(parents=True)
        (d / "status").write_text(status + "\n")


def test_drm_connected_returns_connected_names(tmp_path):
    root = tmp_path / "drm"
    _make_drm(root, {
        "card1-HDMI-A-1": "connected",
        "card1-HDMI-A-2": "disconnected",
        "card1-DP-1": "connected",
    })
    assert bridge._drm_connected(str(root)) == ["card1-DP-1", "card1-HDMI-A-1"]


def test_drm_connected_empty_when_all_disconnected(tmp_path):
    root = tmp_path / "drm"
    _make_drm(root, {"card1-HDMI-A-1": "disconnected"})
    assert bridge._drm_connected(str(root)) == []


def test_drm_connected_missing_root_returns_empty(tmp_path):
    assert bridge._drm_connected(str(tmp_path / "nope")) == []


def _make_usb_device(root, name, vendor, product=None):
    d = root / name
    d.mkdir(parents=True)
    (d / "idVendor").write_text(vendor + "\n")
    if product is not None:
        (d / "product").write_text(product + "\n")


def test_streamdeck_info_present_with_product(tmp_path):
    root = tmp_path / "usb"
    _make_usb_device(root, "1-1", "1d6b")  # a hub, not Elgato
    _make_usb_device(root, "1-2", "0fd9", "Stream Deck MK.2")
    assert bridge._streamdeck_info(str(root)) == (True, "Stream Deck MK.2")


def test_streamdeck_info_present_without_product(tmp_path):
    root = tmp_path / "usb"
    _make_usb_device(root, "1-2", "0FD9")  # vendor match is case-insensitive
    assert bridge._streamdeck_info(str(root)) == (True, "")


def test_streamdeck_info_absent(tmp_path):
    root = tmp_path / "usb"
    _make_usb_device(root, "1-1", "1d6b")
    assert bridge._streamdeck_info(str(root)) == (False, "")


def test_streamdeck_info_missing_root_returns_absent(tmp_path):
    assert bridge._streamdeck_info(str(tmp_path / "nope")) == (False, "")


def test_hardware_status_shape(monkeypatch):
    monkeypatch.setattr(bridge, "_drm_connected", lambda *a, **k: ["card1-HDMI-A-1"])
    monkeypatch.setattr(bridge, "_streamdeck_info", lambda *a, **k: (True, "Stream Deck XL"))
    monkeypatch.setattr(bridge, "_streamdeck_keycount", lambda *a, **k: 32)
    assert bridge._hardware_status() == {
        "ok": True,
        "display": {"present": True, "connectors": ["card1-HDMI-A-1"]},
        "streamdeck": {"present": True, "model": "Stream Deck XL", "key_count": 32},
    }


def test_hardware_status_nothing_attached(monkeypatch):
    monkeypatch.setattr(bridge, "_drm_connected", lambda *a, **k: [])
    monkeypatch.setattr(bridge, "_streamdeck_info", lambda *a, **k: (False, ""))
    monkeypatch.setattr(bridge, "_streamdeck_keycount", lambda *a, **k: None)
    assert bridge._hardware_status() == {
        "ok": True,
        "display": {"present": False, "connectors": []},
        "streamdeck": {"present": False, "model": "", "key_count": None},
    }


def test_calibration_recompose_for_rotation(tmp_path, monkeypatch):
    # The stored fit follows a rotation change by composing the rotation delta
    # (FoodAssistant-9ohp). Identity at the same rotation; a real rotation at a
    # different one; and 90 then 270 round-trips back.
    import json
    store = tmp_path / "cal.json"
    monkeypatch.setattr(bridge, "_CALIB_STORE", str(store))

    store.write_text(json.dumps({"matrix": "1 0 0 0 1 0", "rotation": 0}))
    # An identity fit at delta 0 composes to the identity: no rule needed.
    assert bridge._matrix_for_rotation(0) is None
    assert bridge._matrix_for_rotation(90) == "0 -1 1 1 0 0"        # Rot90 . identity

    # A fit taken at 90, asked for 90, is unchanged (delta 0).
    store.write_text(json.dumps({"matrix": "1.2 0 0 0 1.3 0", "rotation": 90}))
    assert bridge._matrix_for_rotation(90) == "1.2 0 0 0 1.3 0"


def test_rotation_matrix_without_a_stored_fit(tmp_path, monkeypatch):
    # A panel that was never calibrated (the 7-inch DSI screen) has no store.
    # Rotating the display must still counter-rotate touch: the compositor
    # transforms only the output, so without a matrix touch stays in
    # panel-native orientation (FoodAssistant-mox4). The base is treated as an
    # identity fit at rotation 0, so the pure rotation matrix is produced.
    monkeypatch.setattr(bridge, "_CALIB_STORE", str(tmp_path / "missing.json"))
    assert bridge._matrix_for_rotation(90) == "0 -1 1 1 0 0"
    assert [float(v) for v in bridge._matrix_for_rotation(180).split()] == [-1, 0, 1, 0, -1, 1]
    assert bridge._matrix_for_rotation(270) == "0 1 0 -1 0 1"
    # Back at 0 the composed matrix is the identity: no rule (any prior pure
    # rotation rule gets removed by the caller).
    assert bridge._matrix_for_rotation(0) is None


def test_rotation_matrix_with_corrupt_store_falls_back_to_identity(tmp_path, monkeypatch):
    store = tmp_path / "cal.json"
    monkeypatch.setattr(bridge, "_CALIB_STORE", str(store))
    store.write_text("not json")
    assert bridge._matrix_for_rotation(90) == "0 -1 1 1 0 0"
    store.write_text('{"matrix": "1 2 3", "rotation": 0}')
    assert bridge._matrix_for_rotation(90) == "0 -1 1 1 0 0"


def test_compose_affine_identity():
    fit = [1.2, 0.1, -0.05, 0.05, 1.3, -0.1]
    assert bridge._compose_affine(bridge._ROT_AFFINE[0], fit) == fit
    # 90 then 270 is a full turn back to the original.
    r90 = bridge._compose_affine(bridge._ROT_AFFINE[90], fit)
    back = bridge._compose_affine(bridge._ROT_AFFINE[270], r90)
    assert [round(v, 6) for v in back] == [round(v, 6) for v in fit]


def test_touch_device_name_finds_ads7846(tmp_path):
    # The ADS7846 reports PROP=0 but its name carries "touch"/"ads7846", so the
    # name-hint match finds it for the calibration rule (FoodAssistant-mox4).
    devs = tmp_path / "devices"
    devs.write_text(
        'I: Bus=001c Vendor=0000 Product=1ea6\n'
        'N: Name="ADS7846 Touchscreen"\n'
        'H: Handlers=mouse0 event1\n'
        'B: PROP=0\nB: EV=b\nB: ABS=1000003\n'
        '\n'
        'N: Name="vc4-hdmi-0"\nH: Handlers=kbd event2\nB: PROP=20\n'
    )
    assert bridge._touch_device_name(str(devs)) == "ADS7846 Touchscreen"


def test_touch_device_name_none_when_no_touch(tmp_path):
    devs = tmp_path / "devices"
    devs.write_text('N: Name="vc4-hdmi-0"\nH: Handlers=kbd event2\nB: PROP=20\nB: REL=3\n')
    assert bridge._touch_device_name(str(devs)) == ""


def test_write_calibration_rule_targets_named_device(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "_touch_device_name", lambda *a, **k: "ADS7846 Touchscreen")
    monkeypatch.setattr(bridge.os, "makedirs", lambda *a, **k: None)
    monkeypatch.setattr(bridge.subprocess, "run", lambda *a, **k: None)
    rule_file = tmp_path / "rule"

    real_open = open

    def _fake_open(path, *a, **k):
        if str(path).endswith("99-foodassistant-touch.rules"):
            return real_open(rule_file, *a, **k)
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", _fake_open)
    target = bridge._write_calibration_rule("1 0 0 0 1 0")
    assert target == "ADS7846 Touchscreen"
    written = rule_file.read_text()
    # Matched by a prefix glob so a volatile "(NN)" name suffix cannot break it.
    assert 'ATTRS{name}=="ADS7846 Touchscreen*"' in written
    assert 'ENV{LIBINPUT_CALIBRATION_MATRIX}="1 0 0 0 1 0"' in written


def test_streamdeck_keycount_from_sysfs_fallback(tmp_path, monkeypatch):
    # When lsusb is unavailable, the key count is read from the Elgato device's
    # sysfs idProduct (Pantry Raider): an MK.2 (006d) maps to 15 keys.
    def _raise(*a, **k):
        raise FileNotFoundError("lsusb not installed")
    monkeypatch.setattr(bridge.subprocess, "run", _raise)
    dev = tmp_path / "1-1"
    dev.mkdir()
    (dev / "idVendor").write_text("0fd9\n")
    (dev / "idProduct").write_text("006d\n")
    assert bridge._streamdeck_keycount(usb_root=str(tmp_path)) == 15


# System health: power / thermal / disk warnings (FoodAssistant-me1)
# ------------------------------------------------------------------

def test_parse_throttled_all_clear():
    assert bridge._parse_throttled(0) == []


def test_parse_throttled_live_undervoltage():
    out = bridge._parse_throttled(0x1)
    assert out == [{"key": "undervoltage", "message": "Under-voltage detected", "live": True}]


def test_parse_throttled_sticky_and_live():
    # 0x50005 = bits 0, 2 (live undervoltage + throttled) and 16, 18 (sticky).
    out = bridge._parse_throttled(0x50005)
    keys_live = {(w["key"], w["live"]) for w in out}
    assert ("undervoltage", True) in keys_live
    assert ("throttled", True) in keys_live
    assert ("undervoltage", False) in keys_live
    assert ("throttled", False) in keys_live


def test_read_throttled_word_parses_hex(monkeypatch):
    class FakeRun:
        stdout = "throttled=0x50005\n"
    monkeypatch.setattr(bridge.subprocess, "run", lambda *a, **k: FakeRun())
    assert bridge._read_throttled_word() == 0x50005


def test_read_throttled_word_none_when_unparseable(monkeypatch):
    class FakeRun:
        stdout = "command not found"
    monkeypatch.setattr(bridge.subprocess, "run", lambda *a, **k: FakeRun())
    assert bridge._read_throttled_word() is None


def test_read_throttled_word_none_on_exception(monkeypatch):
    def boom(*a, **k):
        raise OSError("no vcgencmd")
    monkeypatch.setattr(bridge.subprocess, "run", boom)
    assert bridge._read_throttled_word() is None


# Continuous monitoring for GET /system/warnings (FoodAssistant-y06w)
# --------------------------------------------------------------------

def test_collapse_throttle_flags_prefers_live_over_sticky():
    # 0x50005: undervoltage and throttled both live AND sticky; the collapsed
    # list carries each condition once, with the live wording winning.
    flags = bridge._parse_throttled(0x50005)
    out = bridge._collapse_throttle_flags(flags)
    assert [(w["key"], w["live"]) for w in out] == [
        ("undervoltage", True), ("throttled", True)]


def test_collapse_throttle_flags_keeps_sticky_only():
    # 0x50000: only the since-boot bits; the collapsed list keeps them, marked
    # not-live, so a past brownout still surfaces (OctoPrint-style).
    out = bridge._collapse_throttle_flags(bridge._parse_throttled(0x50000))
    assert [(w["key"], w["live"]) for w in out] == [
        ("undervoltage", False), ("throttled", False)]


def test_warnings_snapshot_all_clear():
    snap = bridge._warnings_snapshot(0, 45.0, 40, 10.0, checked_epoch=123.0)
    assert snap["ok"] is True
    assert snap["warnings"] == []
    assert snap["checked_epoch"] == 123.0


def test_warnings_snapshot_combines_power_heat_and_disk():
    snap = bridge._warnings_snapshot(0x1, 85.0, 95, 1.2, checked_epoch=1.0)
    keys = [w["key"] for w in snap["warnings"]]
    assert keys == ["undervoltage", "temperature", "disk"]
    assert all(w["live"] for w in snap["warnings"])
    # Thresholds mirror _system_health: 80C and 90%.
    assert "85C" in snap["warnings"][1]["message"]
    assert "95%" in snap["warnings"][2]["message"]


def test_warnings_snapshot_unknown_probes_stay_quiet():
    # No vcgencmd, no thermal zone, no disk info: an unknown state must not
    # fabricate warnings (a non-Pi host would otherwise cry wolf forever).
    snap = bridge._warnings_snapshot(None, None, None, None, checked_epoch=1.0)
    assert snap["warnings"] == []
    assert snap["throttled"] is None


def test_warnings_snapshot_below_thresholds():
    snap = bridge._warnings_snapshot(0, 79.9, 89, 3.0, checked_epoch=1.0)
    assert snap["warnings"] == []


def test_system_warnings_exempt_get():
    assert "/system/warnings" in bridge.TOKEN_EXEMPT_GET


def test_read_cpu_temp(tmp_path):
    f = tmp_path / "temp"
    f.write_text("48312\n")
    assert bridge._read_cpu_temp(str(f)) == 48.3


def test_read_cpu_temp_missing_returns_none(tmp_path):
    assert bridge._read_cpu_temp(str(tmp_path / "nope")) is None


def test_system_health_all_clear(monkeypatch):
    monkeypatch.setattr(bridge, "_read_throttled_word", lambda: 0)
    monkeypatch.setattr(bridge, "_read_cpu_temp", lambda *a, **k: 45.0)
    monkeypatch.setattr(bridge, "_disk_usage", lambda *a, **k: (40, 20.0))
    health = bridge._system_health()
    assert health["ok"] is True
    assert health["warnings"] == []
    assert health["temp_c"] == 45.0
    assert health["disk_percent"] == 40


def test_system_health_flags_hot_and_full(monkeypatch):
    monkeypatch.setattr(bridge, "_read_throttled_word", lambda: 0x1)
    monkeypatch.setattr(bridge, "_read_cpu_temp", lambda *a, **k: 82.0)
    monkeypatch.setattr(bridge, "_disk_usage", lambda *a, **k: (95, 1.2))
    health = bridge._system_health()
    keys = {w["key"] for w in health["warnings"]}
    assert "undervoltage" in keys
    assert "temperature" in keys
    assert "disk" in keys


def test_system_health_unknown_throttle_is_not_false_clear(monkeypatch):
    # vcgencmd unavailable -> throttled is None and contributes no flags, but the
    # other probes still run.
    monkeypatch.setattr(bridge, "_read_throttled_word", lambda: None)
    monkeypatch.setattr(bridge, "_read_cpu_temp", lambda *a, **k: None)
    monkeypatch.setattr(bridge, "_disk_usage", lambda *a, **k: (None, None))
    health = bridge._system_health()
    assert health["throttled"] is None
    assert health["warnings"] == []


# Stream Deck key-count detection (FoodAssistant-dcrh)
# ---------------------------------------------------

def test_streamdeck_keycount_xl(monkeypatch):
    class FakeRun:
        stdout = "Bus 001 Device 005: ID 0fd9:006c Elgato Systems Stream Deck XL\n"
    monkeypatch.setattr(bridge.subprocess, "run", lambda *a, **k: FakeRun())
    assert bridge._streamdeck_keycount() == 32


def test_streamdeck_keycount_mini(monkeypatch):
    class FakeRun:
        stdout = "Bus 001 Device 004: ID 0fd9:0063 Elgato Systems Stream Deck Mini\n"
    monkeypatch.setattr(bridge.subprocess, "run", lambda *a, **k: FakeRun())
    assert bridge._streamdeck_keycount() == 6


def test_streamdeck_keycount_unknown_product(monkeypatch):
    class FakeRun:
        stdout = "Bus 001 Device 004: ID 0fd9:ffff Elgato Systems Future Deck\n"
    monkeypatch.setattr(bridge.subprocess, "run", lambda *a, **k: FakeRun())
    assert bridge._streamdeck_keycount() is None


def test_streamdeck_keycount_no_deck(monkeypatch):
    class FakeRun:
        stdout = "Bus 001 Device 002: ID 1d6b:0002 Linux Foundation 2.0 root hub\n"
    monkeypatch.setattr(bridge.subprocess, "run", lambda *a, **k: FakeRun())
    assert bridge._streamdeck_keycount() is None


# -- display + activity coordination (FoodAssistant-otiy) -------------------

def test_should_blank_respects_disabled_and_blanked():
    # idle_minutes 0 means the feature is off, never blank.
    assert bridge._should_blank(1000.0, 0.0, 0, False) is False
    # already blanked, do not re-blank.
    assert bridge._should_blank(1000.0, 0.0, 5, True) is False


def test_should_blank_threshold():
    now = 1000.0
    # 5 min timeout: not yet at 4m59s, yes at exactly 5m.
    assert bridge._should_blank(now, now - 299, 5, False) is False
    assert bridge._should_blank(now, now - 300, 5, False) is True
    assert bridge._should_blank(now, now - 600, 5, False) is True


def test_display_power_commands_prefers_vcgencmd():
    # Only vcgencmd present (no compositor helper).
    cmds = bridge._display_power_commands(False, which=lambda n: n == "vcgencmd")
    assert cmds == [["vcgencmd", "display_power", "0"]]
    cmds_on = bridge._display_power_commands(True, which=lambda n: n == "vcgencmd")
    assert cmds_on == [["vcgencmd", "display_power", "1"]]


def test_display_power_commands_prefers_compositor_helper():
    # The compositor-aware helper must come first so blanking does not drop a
    # cage kiosk to the console (FoodAssistant-8khi).
    cmds = bridge._display_power_commands(False, which=lambda n: True)
    assert cmds[0] == ["foodassistant-display-power", "off"]
    cmds_on = bridge._display_power_commands(True, which=lambda n: True)
    assert cmds_on[0] == ["foodassistant-display-power", "on"]


def test_display_power_commands_orders_vcgencmd_then_xset():
    cmds = bridge._display_power_commands(False, which=lambda n: True)
    assert ["vcgencmd", "display_power", "0"] in cmds
    assert ["xset", "dpms", "force", "off"] in cmds
    # Compositor helper precedes the firmware/X11 fallbacks.
    assert cmds.index(["foodassistant-display-power", "off"]) < cmds.index(["vcgencmd", "display_power", "0"])


def test_display_power_commands_empty_when_no_tools():
    assert bridge._display_power_commands(True, which=lambda n: False) == []


def test_kiosk_restart_cmd_prefers_transient_unit():
    kind, cmd = bridge._kiosk_restart_cmd(2, token="123", which=lambda n: n == "systemd-run")
    assert kind == "transient"
    assert cmd[0] == "systemd-run"
    assert "--on-active=2s" in cmd
    assert "--unit=fa-kiosk-restart-123" in cmd
    assert cmd[-3:] == ["systemctl", "restart", "foodassistant-kiosk.service"]


def test_kiosk_restart_cmd_shell_fallback():
    kind, cmd = bridge._kiosk_restart_cmd(3, which=lambda n: None)
    assert kind == "shell"
    assert cmd[:2] == ["/bin/sh", "-c"]
    assert "sleep 3" in cmd[2]
    assert "systemctl restart foodassistant-kiosk.service" in cmd[2]


def test_schedule_kiosk_restart_uses_transient_unit(monkeypatch):
    # With systemd-run available and succeeding, no shell process is spawned:
    # the restart is a detached transient unit (FoodAssistant-9ext).
    calls = []

    class Ok:
        returncode = 0

    monkeypatch.setattr(bridge.shutil, "which", lambda n: n == "systemd-run")
    monkeypatch.setattr(bridge.subprocess, "run",
                        lambda cmd, **k: calls.append(("run", cmd)) or Ok())
    monkeypatch.setattr(bridge.subprocess, "Popen",
                        lambda cmd, **k: calls.append(("popen", cmd)))
    assert bridge._schedule_kiosk_restart() is True
    assert [c[0] for c in calls] == ["run"]
    assert calls[0][1][0] == "systemd-run"


def test_schedule_kiosk_restart_falls_back_to_detached_shell(monkeypatch):
    # systemd-run refusing (e.g. a leftover transient unit) falls back to a
    # detached sleep+restart rather than blocking or failing.
    spawned = []

    class Refused:
        returncode = 1

    monkeypatch.setattr(bridge.shutil, "which", lambda n: n == "systemd-run")
    monkeypatch.setattr(bridge.subprocess, "run", lambda cmd, **k: Refused())
    monkeypatch.setattr(bridge.subprocess, "Popen",
                        lambda cmd, **k: spawned.append(cmd))
    assert bridge._schedule_kiosk_restart() is True
    assert spawned and spawned[0][:2] == ["/bin/sh", "-c"]


def test_schedule_kiosk_restart_false_when_nothing_works(monkeypatch):
    def boom(*a, **k):
        raise OSError("no shell")
    monkeypatch.setattr(bridge.shutil, "which", lambda n: None)
    monkeypatch.setattr(bridge.subprocess, "Popen", boom)
    assert bridge._schedule_kiosk_restart() is False


def test_set_rotation_noop_when_unchanged_skips_restart(monkeypatch):
    # Re-pushing the rotation the display is already at must NOT restart the
    # kiosk (FoodAssistant-b0by): a spurious re-push (a settings save, a
    # display re-detect) was flashing the boot splash on a loop.
    monkeypatch.setattr(bridge, "_read_kiosk_rotation", lambda: 270)
    restarts = []
    monkeypatch.setattr(bridge, "_schedule_kiosk_restart",
                        lambda *a, **k: restarts.append(k.get("reason")) or True)

    def _no_helper(name):
        raise AssertionError("set-rotation ran on an unchanged rotation")
    monkeypatch.setattr(bridge, "_ensure_helper", _no_helper)
    sent = {}

    class FakeHandler:
        _set_rotation = bridge._Handler._set_rotation

        def _body(self):
            return {"degrees": 270}

        def _send(self, code, body):
            sent["code"] = code
            sent["body"] = body

    FakeHandler()._set_rotation()
    assert sent["code"] == 200
    assert sent["body"].get("unchanged") is True
    assert restarts == []   # no kiosk restart scheduled for a no-op


def test_set_rotation_changed_proceeds_past_the_guard(monkeypatch):
    # A genuine rotation change must NOT be short-circuited by the no-op guard;
    # it proceeds to run the helper (here mocked missing, so it 500s, which
    # proves it got past the guard rather than returning "unchanged").
    monkeypatch.setattr(bridge, "_read_kiosk_rotation", lambda: 0)
    monkeypatch.setattr(bridge, "_ensure_helper", lambda name: "")  # helper missing
    sent = {}

    class FakeHandler:
        _set_rotation = bridge._Handler._set_rotation

        def _body(self):
            return {"degrees": 270}

        def _send(self, code, body):
            sent["code"] = code
            sent["body"] = body

    FakeHandler()._set_rotation()
    assert sent["code"] == 500
    assert sent["body"].get("unchanged") is not True


def test_touch_match_expr_strips_volatile_name_suffix():
    # An ft5x06 panel appends a volatile "(NN)" to its name and re-enumerated
    # from (00) to (79) across the mode switch, breaking an exact-name match so
    # the calibration matrix stopped applying and touch was un-rotated
    # (FoodAssistant-ly82 follow-up). Match by a prefix glob instead.
    assert bridge._touch_match_expr("10-0038 generic ft5x06 (79)") == \
        'ATTRS{name}=="10-0038 generic ft5x06*"'
    assert bridge._touch_match_expr("10-0038 generic ft5x06 (00)") == \
        'ATTRS{name}=="10-0038 generic ft5x06*"'
    # A name without a suffix still globs (the trailing * is harmless).
    assert bridge._touch_match_expr("Goodix Capacitive TouchScreen") == \
        'ATTRS{name}=="Goodix Capacitive TouchScreen*"'


def test_touch_match_expr_falls_back_to_property_without_name():
    # A USB HID panel reports a vendor name, not a controller name; with no
    # detected name, match any touchscreen by its udev property.
    for empty in ("", None):
        assert bridge._touch_match_expr(empty) == 'ENV{ID_INPUT_TOUCHSCREEN}=="1"'


def test_reboot_command_prefers_systemctl():
    cmd = bridge._reboot_command(which=lambda n: n == "systemctl")
    assert cmd == ["systemctl", "reboot"]


def test_reboot_command_falls_back_to_reboot():
    cmd = bridge._reboot_command(which=lambda n: False)
    assert cmd == ["reboot"]


def test_reboot_calendar_nightly():
    assert bridge._reboot_calendar("03:30", "nightly") == "*-*-* 03:30:00"


def test_reboot_calendar_legacy_time_only_means_nightly():
    # An older app posts just {"time": ...}; the timer stays nightly.
    assert bridge._reboot_calendar("04:00") == "*-*-* 04:00:00"
    assert bridge._reboot_calendar("04:00", "") == "*-*-* 04:00:00"


def test_reboot_calendar_weekly_days():
    assert bridge._reboot_calendar("03:30", "weekly", 0) == "Sun *-*-* 03:30:00"
    assert bridge._reboot_calendar("03:30", "weekly", 1) == "Mon *-*-* 03:30:00"
    assert bridge._reboot_calendar("03:30", "weekly", 6) == "Sat *-*-* 03:30:00"


def test_reboot_calendar_weekly_bad_day_defaults_to_sunday():
    assert bridge._reboot_calendar("03:30", "weekly", 9) == "Sun *-*-* 03:30:00"
    assert bridge._reboot_calendar("03:30", "weekly", "x") == "Sun *-*-* 03:30:00"
    assert bridge._reboot_calendar("03:30", "weekly", None) == "Sun *-*-* 03:30:00"


def test_persist_idle_minutes_roundtrip(tmp_path):
    p = tmp_path / "display-idle"
    assert bridge._write_persisted_idle_minutes(15, path=str(p)) is True
    assert bridge._read_persisted_idle_minutes(path=str(p)) == 15


def test_read_persisted_idle_minutes_defaults_to_zero(tmp_path):
    assert bridge._read_persisted_idle_minutes(path=str(tmp_path / "missing")) == 0


def test_record_activity_wakes_when_blanked(monkeypatch):
    calls = []
    monkeypatch.setattr(bridge, "_set_display_power", lambda on: calls.append(on) or True)
    with bridge._activity_lock:
        bridge._activity_state["display_blanked"] = True
    woke = bridge._record_activity()
    assert woke is True
    assert calls == [True]  # powered the display back on
    with bridge._activity_lock:
        assert bridge._activity_state["display_blanked"] is False


def test_record_activity_noop_when_awake(monkeypatch):
    calls = []
    monkeypatch.setattr(bridge, "_set_display_power", lambda on: calls.append(on) or True)
    with bridge._activity_lock:
        bridge._activity_state["display_blanked"] = False
    woke = bridge._record_activity()
    assert woke is False
    assert calls == []  # no power command when already awake


# --- wake on motion (FoodAssistant-fr5) -------------------------------------

def test_persist_wake_on_motion_roundtrip(tmp_path):
    p = tmp_path / "wake-on-motion"
    assert bridge._write_persisted_wake_on_motion("off", path=str(p)) is True
    assert bridge._read_persisted_wake_on_motion(path=str(p)) == "off"


def test_read_persisted_wake_on_motion_defaults_to_auto(tmp_path):
    assert bridge._read_persisted_wake_on_motion(path=str(tmp_path / "missing")) == "auto"
    # A garbage value on disk also falls back to auto rather than raising.
    p = tmp_path / "wake-on-motion"
    p.write_text("sometimes\n")
    assert bridge._read_persisted_wake_on_motion(path=str(p)) == "auto"


def test_motion_wake_enabled_truth_table():
    # off always disables, even with the sensor present.
    assert bridge._motion_wake_enabled("off", True) is False
    assert bridge._motion_wake_enabled("off", False) is False
    # auto and on both follow the hardware: no sensor, no phantom wakes.
    assert bridge._motion_wake_enabled("auto", True) is True
    assert bridge._motion_wake_enabled("auto", False) is False
    assert bridge._motion_wake_enabled("on", True) is True
    assert bridge._motion_wake_enabled("on", False) is False


def test_motion_exceeds_detects_a_nudge_not_rest():
    at_rest = (0.01, 0.02, 1.00)
    # Sensor noise around the gravity vector stays below the threshold.
    assert bridge._motion_exceeds(at_rest, (0.02, 0.01, 0.99)) is False
    # A real bump or tilt moves the vector well past it.
    assert bridge._motion_exceeds(at_rest, (0.15, 0.02, 0.95)) is True
    # Identical samples are never motion.
    assert bridge._motion_exceeds(at_rest, at_rest) is False


def test_motion_exceeds_safe_on_missing_or_bad_samples():
    assert bridge._motion_exceeds(None, (0.0, 0.0, 1.0)) is False
    assert bridge._motion_exceeds((0.0, 0.0, 1.0), None) is False
    assert bridge._motion_exceeds((0.0, 0.0), (0.0, 0.0, 1.0)) is False
    assert bridge._motion_exceeds(("x", 0.0, 1.0), (0.0, 0.0, 1.0)) is False


def test_motion_exceeds_honours_custom_threshold():
    prev = (0.0, 0.0, 1.0)
    cur = (0.05, 0.0, 1.0)
    assert bridge._motion_exceeds(prev, cur, threshold=0.04) is True
    assert bridge._motion_exceeds(prev, cur, threshold=0.06) is False


def test_accel_iio_device_finds_lsm6_by_name(tmp_path):
    dev = tmp_path / "iio:device0"
    dev.mkdir()
    (dev / "name").write_text("lsm6dsx_accel\n")
    other = tmp_path / "iio:device1"
    other.mkdir()
    (other / "name").write_text("cpu_thermal\n")
    assert bridge._accel_iio_device(iio_root=str(tmp_path)) == str(dev)


def test_accel_iio_device_empty_when_absent(tmp_path):
    assert bridge._accel_iio_device(iio_root=str(tmp_path)) == ""


def test_read_iio_accel_converts_raw_to_g(tmp_path):
    dev = tmp_path / "iio:device0"
    dev.mkdir()
    # scale is m/s^2 per LSB; raw * scale / 9.80665 gives g.
    (dev / "in_accel_scale").write_text("0.000598\n")
    (dev / "in_accel_x_raw").write_text("0\n")
    (dev / "in_accel_y_raw").write_text("0\n")
    (dev / "in_accel_z_raw").write_text("16400\n")
    sample = bridge._read_iio_accel(str(dev))
    assert sample is not None
    x, y, z = sample
    assert abs(x) < 0.001 and abs(y) < 0.001
    assert 0.98 <= z <= 1.02  # one gravity on the Z axis


def test_read_iio_accel_none_on_missing_files(tmp_path):
    assert bridge._read_iio_accel(str(tmp_path / "iio:device9")) is None


# --- Full-stack restore source helpers (FoodAssistant-h18b) ----------------

def test_classify_restore_source_absolute_path():
    assert bridge._classify_restore_source("/srv/foodassistant-20260626.tar.gz") == (
        "path", "/srv/foodassistant-20260626.tar.gz"
    )


def test_classify_restore_source_strips_whitespace():
    assert bridge._classify_restore_source("  /a/b.tar.gz  ") == ("path", "/a/b.tar.gz")


def test_classify_restore_source_rclone_prefix():
    assert bridge._classify_restore_source("rclone:remote:bucket/snap.tar.gz") == (
        "rclone", "remote:bucket/snap.tar.gz"
    )


def test_classify_restore_source_rclone_trims_remote():
    assert bridge._classify_restore_source("rclone:  remote:x  ") == ("rclone", "remote:x")


def test_classify_restore_source_rclone_empty_is_invalid():
    kind, _ = bridge._classify_restore_source("rclone:")
    assert kind == "invalid"


def test_classify_restore_source_empty_is_invalid():
    kind, _ = bridge._classify_restore_source("")
    assert kind == "invalid"
    kind, _ = bridge._classify_restore_source("   ")
    assert kind == "invalid"


def test_classify_restore_source_relative_is_invalid():
    kind, _ = bridge._classify_restore_source("backups/snap.tar.gz")
    assert kind == "invalid"


def test_rclone_pull_cmd_default_binary():
    assert bridge._rclone_pull_cmd("remote:x", "/tmp/out.tar.gz") == [
        "rclone", "copyto", "remote:x", "/tmp/out.tar.gz"
    ]


def test_rclone_pull_cmd_custom_binary():
    assert bridge._rclone_pull_cmd("remote:x", "/tmp/out.tar.gz", rclone="/usr/bin/rclone") == [
        "/usr/bin/rclone", "copyto", "remote:x", "/tmp/out.tar.gz"
    ]


# --- Fallback AP connectivity gate (FoodAssistant-xt9b) ---------------------
# The setup-mode banner must never show on a device with real connectivity, so
# GET /ap/status trusts the flag file only when the device has no default route
# and no wired link. These cover the pure decision helpers behind that gate.

def test_is_wired_iface_accepts_real_nics():
    for name in ("eth0", "eth1", "end0", "enp1s0", "eno1", "usb0"):
        assert bridge._is_wired_iface(name) is True, name


def test_is_wired_iface_rejects_loopback_wifi_and_virtual():
    for name in ("lo", "wlan0", "wlan1", "docker0", "br-abc123", "veth1a2b",
                 "tun0", "tap0", "virbr0"):
        assert bridge._is_wired_iface(name) is False, name


def test_connectivity_default_route_wins():
    # Any gateway (wired or Wi-Fi) means the device is on a network.
    route = "default via 192.168.1.1 dev eth0 proto dhcp metric 100"
    assert bridge._connectivity_from(route, []) is True


def test_connectivity_wired_up_with_ip():
    assert bridge._connectivity_from("", [("eth0", True, True)]) is True


def test_connectivity_wired_up_without_ip_is_not_enough():
    # Carrier without an address (cable in, DHCP not done) is not connectivity.
    assert bridge._connectivity_from("", [("eth0", True, False)]) is False


def test_connectivity_wired_down_is_not_enough():
    assert bridge._connectivity_from("", [("eth0", False, False)]) is False


def test_connectivity_virtual_ifaces_do_not_count():
    # Docker bridges and veths always have carrier + IP; they must not mask a
    # genuinely stranded device.
    ifaces = [("docker0", True, True), ("br-1f2e3d", True, True),
              ("veth99", True, True), ("virbr0", True, True)]
    assert bridge._connectivity_from("", ifaces) is False


def test_connectivity_stranded_device_stays_false():
    # AP mode proper: no default route, wlan0 holds the static AP address,
    # ethernet unplugged. The hotspot must still report active here.
    assert bridge._connectivity_from("", [("eth0", False, False)]) is False
    assert bridge._connectivity_from("\n", []) is False


def test_ap_status_stands_down_when_flag_set_but_connected(tmp_path, monkeypatch):
    # Wire the handler's decision path end to end without HTTP: flag present
    # plus live connectivity must stand the AP down and report inactive.
    flag = tmp_path / "foodassistant-ap-active"
    flag.write_text("")
    monkeypatch.setattr(bridge, "_AP_FLAG", str(flag))
    monkeypatch.setattr(bridge, "_has_lan_connectivity", lambda: True)
    stood_down = []
    monkeypatch.setattr(bridge, "_ap_stand_down", lambda: stood_down.append(True))

    sent = {}

    class FakeHandler:
        _ap_status = bridge._Handler._ap_status

        def _send(self, code, body):
            sent["code"] = code
            sent["body"] = body

    FakeHandler()._ap_status()
    assert sent["code"] == 200
    assert sent["body"]["active"] is False
    assert stood_down == [True]


def test_ap_status_active_when_flag_set_and_no_connectivity(tmp_path, monkeypatch):
    flag = tmp_path / "foodassistant-ap-active"
    flag.write_text("")
    monkeypatch.setattr(bridge, "_AP_FLAG", str(flag))
    monkeypatch.setattr(bridge, "_has_lan_connectivity", lambda: False)

    sent = {}

    class FakeHandler:
        _ap_status = bridge._Handler._ap_status

        def _send(self, code, body):
            sent["code"] = code
            sent["body"] = body

    FakeHandler()._ap_status()
    assert sent["code"] == 200
    assert sent["body"]["active"] is True
    assert flag.exists()
# --- Helper self-heal (FoodAssistant-jppi / FoodAssistant-9la0) -------------
# A device imaged before a helper script existed has nothing under
# /usr/local/bin for it; the bridge reinstalls it from the source checkout.


def test_helper_source_dirs_honors_repo_dir(monkeypatch):
    monkeypatch.setenv("REPO_DIR", "/custom/repo")
    dirs = bridge._helper_source_dirs()
    assert dirs[0] == "/custom/repo/scripts/image-build"
    assert "/opt/foodassistant-src/scripts/image-build" in dirs


def test_helper_source_dirs_without_repo_dir(monkeypatch):
    monkeypatch.delenv("REPO_DIR", raising=False)
    dirs = bridge._helper_source_dirs()
    assert dirs[0] == "/opt/foodassistant-src/scripts/image-build"


def test_find_helper_source_prefers_first_dir(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "foodassistant-display-power").write_text("#!/bin/sh\necho a\n")
    (b / "foodassistant-display-power").write_text("#!/bin/sh\necho b\n")
    found = bridge._find_helper_source(
        "foodassistant-display-power", src_dirs=[str(a), str(b)])
    assert found == str(a / "foodassistant-display-power")


def test_find_helper_source_none_when_absent(tmp_path):
    assert bridge._find_helper_source(
        "foodassistant-display-power", src_dirs=[str(tmp_path)]) is None


def test_ensure_helper_returns_existing_install(tmp_path, monkeypatch):
    dst = tmp_path / "foodassistant-set-rotation"
    dst.write_text("#!/bin/sh\n")
    # The checkout lookup must not even be consulted when the install exists.
    monkeypatch.setattr(
        bridge, "_find_helper_source",
        lambda name: (_ for _ in ()).throw(AssertionError("looked up source")))
    assert bridge._ensure_helper(
        "foodassistant-set-rotation", bin_dir=str(tmp_path)) == str(dst)


def test_ensure_helper_installs_from_checkout(tmp_path, monkeypatch):
    src_dir = tmp_path / "checkout"
    bin_dir = tmp_path / "bin"
    src_dir.mkdir()
    bin_dir.mkdir()
    src = src_dir / "foodassistant-display-power"
    src.write_text("#!/bin/sh\necho hi\n")
    monkeypatch.setattr(
        bridge, "_find_helper_source", lambda name: str(src))
    dst = bridge._ensure_helper(
        "foodassistant-display-power", bin_dir=str(bin_dir))
    assert dst == str(bin_dir / "foodassistant-display-power")
    installed = bin_dir / "foodassistant-display-power"
    assert installed.read_text() == "#!/bin/sh\necho hi\n"
    assert installed.stat().st_mode & 0o755 == 0o755


def test_ensure_helper_none_when_no_source(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "_find_helper_source", lambda name: None)
    assert bridge._ensure_helper(
        "foodassistant-display-power", bin_dir=str(tmp_path)) is None


def test_ensure_helper_none_when_install_fails(tmp_path, monkeypatch):
    src = tmp_path / "foodassistant-display-power"
    src.write_text("#!/bin/sh\n")
    monkeypatch.setattr(bridge, "_find_helper_source", lambda name: str(src))
    missing_bin = tmp_path / "no-such-dir"
    assert bridge._ensure_helper(
        "foodassistant-display-power", bin_dir=str(missing_bin)) is None


def test_display_power_commands_uses_self_healed_helper_path():
    # PATH does not see the helper, but the self-heal installed it: the full
    # path is used and still precedes the firmware fallback.
    cmds = bridge._display_power_commands(
        False, which=lambda n: n == "vcgencmd",
        helper_path="/usr/local/bin/foodassistant-display-power")
    assert cmds[0] == ["/usr/local/bin/foodassistant-display-power", "off"]
    assert cmds[1] == ["vcgencmd", "display_power", "0"]


def test_display_power_commands_path_helper_wins_over_healed_path():
    # When PATH already resolves the helper the bare name is kept (existing
    # behaviour); the healed path is not added twice.
    cmds = bridge._display_power_commands(
        True, which=lambda n: True, helper_path="/usr/local/bin/x")
    assert cmds[0] == ["foodassistant-display-power", "on"]
    assert ["/usr/local/bin/x", "on"] not in cmds
# --- Mealie install persistence + resume (FoodAssistant-nqpb) ---------------


def test_env_profiles_empty_text():
    assert bridge._env_profiles("") == []
    assert bridge._env_profiles(None) == []


def test_env_profiles_parses_csv():
    text = "TZ=UTC\nCOMPOSE_PROFILES=with-mealie,with-ollama\nFOO=bar\n"
    assert bridge._env_profiles(text) == ["with-mealie", "with-ollama"]


def test_env_profiles_strips_quotes_and_spaces():
    assert bridge._env_profiles('COMPOSE_PROFILES=" with-mealie , with-ollama "') == [
        "with-mealie", "with-ollama"
    ]


def test_add_env_profile_appends_line_when_missing():
    out = bridge._add_env_profile("TZ=UTC\n", "with-mealie")
    assert "COMPOSE_PROFILES=with-mealie" in out
    assert out.startswith("TZ=UTC\n")
    assert out.endswith("\n")


def test_add_env_profile_extends_existing_line():
    out = bridge._add_env_profile("COMPOSE_PROFILES=with-ollama\n", "with-mealie")
    assert bridge._env_profiles(out) == ["with-ollama", "with-mealie"]


def test_add_env_profile_idempotent():
    text = "TZ=UTC\nCOMPOSE_PROFILES=with-mealie\n"
    assert bridge._add_env_profile(text, "with-mealie") == text


def test_add_env_profile_empty_env():
    assert bridge._add_env_profile("", "with-mealie") == "COMPOSE_PROFILES=with-mealie\n"


def test_persist_compose_profile_creates_file(tmp_path):
    path = tmp_path / ".env"
    assert bridge._persist_compose_profile("with-mealie", path=str(path)) is True
    assert bridge._env_profiles(path.read_text()) == ["with-mealie"]


def test_persist_compose_profile_preserves_existing_keys(tmp_path):
    path = tmp_path / ".env"
    path.write_text("TZ=UTC\nFOODASSISTANT_TAG=latest\n")
    assert bridge._persist_compose_profile("with-mealie", path=str(path)) is True
    text = path.read_text()
    assert "TZ=UTC" in text and "FOODASSISTANT_TAG=latest" in text
    assert bridge._env_profiles(text) == ["with-mealie"]


def test_persist_compose_profile_second_call_is_noop(tmp_path):
    path = tmp_path / ".env"
    bridge._persist_compose_profile("with-mealie", path=str(path))
    before = path.read_text()
    bridge._persist_compose_profile("with-mealie", path=str(path))
    assert path.read_text() == before


def test_should_resume_mealie_when_requested_and_absent():
    assert bridge._should_resume_mealie(["with-mealie"], False, False) is True


def test_should_resume_mealie_skips_when_container_up():
    assert bridge._should_resume_mealie(["with-mealie"], True, False) is False


def test_should_resume_mealie_skips_when_install_in_flight():
    assert bridge._should_resume_mealie(["with-mealie"], False, True) is False


def test_should_resume_mealie_skips_when_never_requested():
    assert bridge._should_resume_mealie([], False, False) is False
    assert bridge._should_resume_mealie(["with-ollama"], False, False) is False


def test_resume_gives_up_when_docker_never_answers(monkeypatch):
    # No docker daemon: the resume loop must give up quietly, never raise.
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        raise OSError("no docker")

    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    assert bridge._resume_mealie_install(retries=3, delay=0, sleep=lambda _: None) is False
    assert len(calls) == 3


# --- One-image mode switch: park / resume the local stack (FoodAssistant-dzx9)

_APPLIANCE_COMPOSE = """\
services:
  service:
    image: ghcr.io/syracuse3dprintingorg/pantryraider:latest
  grocy:
    image: lscr.io/linuxserver/grocy:4.6.0
  mealie:
    image: ghcr.io/mealie-recipes/mealie:v3.19.2
    profiles: ["with-mealie"]
  ollama:
    image: ollama/ollama:0.30.8
    profiles: ["with-ollama"]
"""

_REMOTE_COMPOSE = """\
services:
  service:
    image: ghcr.io/syracuse3dprintingorg/pantryraider:latest
    ports:
      - "80:9284"
"""


def test_compose_backend_services_appliance():
    assert bridge._compose_backend_services(_APPLIANCE_COMPOSE) == [
        "grocy", "mealie", "ollama"]


def test_compose_backend_services_never_includes_the_app_itself():
    assert "service" not in bridge._compose_backend_services(_APPLIANCE_COMPOSE)


def test_compose_backend_services_remote_compose_is_empty():
    # The Pi Remote compose defines no backend services, so a device flashed as
    # a plain satellite is never treated as a parked hosted stack.
    assert bridge._compose_backend_services(_REMOTE_COMPOSE) == []


def test_compose_backend_services_empty_and_missing_text():
    assert bridge._compose_backend_services("") == []
    assert bridge._compose_backend_services(None) == []


def test_compose_backend_services_ignores_deeper_mentions():
    # A grocy mention that is not a two-space service key (e.g. a volume path
    # or comment) must not count as a service.
    text = "services:\n  service:\n    volumes:\n      - ./grocy:/config\n# grocy: notes\n"
    assert bridge._compose_backend_services(text) == []


def test_stack_stop_cmd_enables_all_profiles():
    cmd = bridge._stack_stop_cmd(["grocy", "mealie"])
    assert cmd[:2] == ["docker", "compose"]
    assert "--profile" in cmd and "with-mealie" in cmd and "with-ollama" in cmd
    assert cmd[-3:] == ["stop", "grocy", "mealie"]


def test_stack_up_cmd_is_a_plain_up():
    # Profiles come from the persisted COMPOSE_PROFILES in the stack's .env, so
    # up must not force them: an appliance without Mealie must not gain it.
    assert bridge._stack_up_cmd() == ["docker", "compose", "up", "-d"]


def test_read_stack_compose_missing_file(tmp_path):
    assert bridge._read_stack_compose(str(tmp_path / "nope.yml")) == ""


def test_read_stack_compose_reads_text(tmp_path):
    p = tmp_path / "docker-compose.yml"
    p.write_text(_APPLIANCE_COMPOSE)
    assert bridge._read_stack_compose(str(p)) == _APPLIANCE_COMPOSE
# Kiosk auto-enable (FoodAssistant-92e.3)
# ---------------------------------------

def test_unit_installed_true_when_unit_file_exists(tmp_path):
    (tmp_path / "foodassistant-kiosk.service").write_text("[Unit]\n")
    assert bridge._unit_installed("foodassistant-kiosk.service", str(tmp_path)) is True


def test_unit_installed_false_when_missing(tmp_path):
    assert bridge._unit_installed("foodassistant-kiosk.service", str(tmp_path)) is False


def test_config_env_value_reads_key(tmp_path):
    cfg = tmp_path / "config.env"
    cfg.write_text("# comment\nENABLE_KIOSK=false\n")
    assert bridge._config_env_value("ENABLE_KIOSK", [str(cfg)]) == "false"


def test_config_env_value_later_assignment_wins(tmp_path):
    cfg = tmp_path / "config.env"
    cfg.write_text("ENABLE_KIOSK=true\nENABLE_KIOSK=false\n")
    assert bridge._config_env_value("ENABLE_KIOSK", [str(cfg)]) == "false"


def test_config_env_value_handles_export_and_quotes(tmp_path):
    cfg = tmp_path / "config.env"
    cfg.write_text('export ENABLE_KIOSK="auto"\n')
    assert bridge._config_env_value("ENABLE_KIOSK", [str(cfg)]) == "auto"


def test_config_env_value_first_existing_file_wins(tmp_path):
    # Mirrors firstboot: it sources only the first candidate that exists, so
    # a later candidate must not be consulted even when the key is unset.
    first = tmp_path / "first.env"
    second = tmp_path / "second.env"
    first.write_text("OTHER=1\n")
    second.write_text("ENABLE_KIOSK=false\n")
    missing = tmp_path / "missing.env"
    assert bridge._config_env_value(
        "ENABLE_KIOSK", [str(missing), str(first), str(second)]
    ) == ""


def test_config_env_value_no_files_returns_empty(tmp_path):
    assert bridge._config_env_value("ENABLE_KIOSK", [str(tmp_path / "nope")]) == ""


def test_config_env_value_ignores_commented_assignment(tmp_path):
    cfg = tmp_path / "config.env"
    cfg.write_text("# ENABLE_KIOSK=auto\n")
    assert bridge._config_env_value("ENABLE_KIOSK", [str(cfg)]) == ""


def _decide(**kw):
    args = dict(
        display_connected=True, installed=False, installing=False, active=False,
        enable_flag="", attempted_install=False, attempted_start=False,
    )
    args.update(kw)
    return bridge._kiosk_autoenable_action(**args)


def test_autoenable_installs_when_display_and_never_provisioned():
    assert _decide() == "install"


def test_autoenable_nothing_without_display():
    assert _decide(display_connected=False) is None


def test_autoenable_respects_explicit_opt_out():
    for flag in ("false", "FALSE", "0", "no", "off"):
        assert _decide(enable_flag=flag) is None


def test_autoenable_auto_and_true_flags_allow_install():
    assert _decide(enable_flag="auto") == "install"
    assert _decide(enable_flag="true") == "install"
    assert _decide(enable_flag="") == "install"


def test_autoenable_waits_while_installing():
    assert _decide(installing=True) is None


def test_autoenable_installs_once_per_run():
    assert _decide(attempted_install=True) is None


def test_autoenable_starts_installed_but_stopped_kiosk():
    assert _decide(installed=True) == "start"


def test_autoenable_starts_once_per_connection():
    assert _decide(installed=True, attempted_start=True) is None


def test_autoenable_noop_when_running():
    assert _decide(installed=True, active=True) is None


# --- USB flash-drive backup helpers (FoodAssistant-ch6d) ----------------------

_USB_MOUNTS = (
    "proc /proc proc rw,nosuid 0 0\n"
    "/dev/mmcblk0p2 / ext4 rw,noatime 0 0\n"
    "/dev/mmcblk0p1 /boot/firmware vfat rw,relatime 0 0\n"
    "/dev/sda1 /media/pi/PANTRY\\040USB vfat rw,nosuid 0 0\n"
    "/dev/sdb1 /mnt/readonly ext4 ro,relatime 0 0\n"
)


def test_usb_parse_mounts_decodes_escapes_and_skips_short_lines():
    entries = bridge._usb_parse_mounts(_USB_MOUNTS + "garbage\n")
    sda = next(e for e in entries if e[0] == "/dev/sda1")
    assert sda[1] == "/media/pi/PANTRY USB"
    assert all(len(e) == 4 for e in entries)


def test_usb_disk_for_matches_partition_suffixes_only():
    assert bridge._usb_disk_for("sda1", ["sda"]) == "sda"
    assert bridge._usb_disk_for("nvme0n1p2", ["nvme0n1"]) == "nvme0n1"
    assert bridge._usb_disk_for("sdab1", ["sda"]) is None


def test_usb_mount_candidates_filters_ro_root_and_boot():
    mounts = bridge._usb_parse_mounts(_USB_MOUNTS)
    assert bridge._usb_mount_candidates(["sda", "sdb"], mounts) == [
        ("/dev/sda1", "/media/pi/PANTRY USB")
    ]
    # A Pi booted from a removable USB SSD never gets its system disk offered.
    sys_mounts = bridge._usb_parse_mounts(
        "/dev/sda2 / ext4 rw 0 0\n/dev/sda1 /boot/firmware vfat rw 0 0\n"
    )
    assert bridge._usb_mount_candidates(["sda"], sys_mounts) == []


def test_usb_pick_mount_prefers_automount_paths():
    cands = [("/dev/sdb1", "/srv/x"), ("/dev/sda1", "/media/pi/STICK")]
    assert bridge._usb_pick_mount(cands) == ("/dev/sda1", "/media/pi/STICK")
    assert bridge._usb_pick_mount([]) is None


def test_usb_removable_disks_reads_sysfs(tmp_path):
    (tmp_path / "sda").mkdir()
    (tmp_path / "sda" / "removable").write_text("1\n")
    (tmp_path / "mmcblk0").mkdir()
    (tmp_path / "mmcblk0" / "removable").write_text("0\n")
    assert bridge._usb_removable_disks(str(tmp_path)) == ["sda"]
    assert bridge._usb_removable_disks(str(tmp_path / "nope")) == []


def test_usb_rotation_keeps_newest_14_and_ignores_foreign_files():
    names = ["foodassistant-usb-202601%02d-000000.tar.gz" % d for d in range(1, 18)]
    names += ["holiday.jpg", "backup.tar.gz"]
    victims = bridge._usb_rotation_victims(names, keep=14)
    assert victims == sorted(names[:17])[:3]
    assert "holiday.jpg" not in victims and "backup.tar.gz" not in victims
    assert bridge._usb_rotation_victims(names[:14], keep=14) == []


def test_usb_data_dirs_appliance_layout_then_repo_fallback(tmp_path):
    # Appliance layout: whatever exists among data/grocy/mealie. A Pi Remote
    # only has data/, so its backup is naturally the device config.
    (tmp_path / "data").mkdir()
    assert bridge._usb_data_dirs(str(tmp_path)) == ["data"]
    (tmp_path / "grocy").mkdir()
    (tmp_path / "mealie").mkdir()
    assert bridge._usb_data_dirs(str(tmp_path)) == ["data", "grocy", "mealie"]
    # Repo-style checkout: the app data lives under service/data instead.
    repo = tmp_path / "repo"
    (repo / "service" / "data").mkdir(parents=True)
    (repo / "grocy" / "config").mkdir(parents=True)
    assert bridge._usb_data_dirs(str(repo)) == ["service/data", "grocy"]
    # Nothing found: nothing to back up.
    empty = tmp_path / "empty"
    empty.mkdir()
    assert bridge._usb_data_dirs(str(empty)) == []
# -- Stream Deck config TOML serializer -------------------------------------
# The bridge rewrites /opt/foodassistant/config.toml from the posted dict; the
# controller reads it back with tomllib. The round trip must preserve custom
# key overrides exactly, including a macro key's list of action names
# (previously stringified, which broke every macro after a save).

def _roundtrip(cfg):
    import tomllib
    return tomllib.loads(bridge._streamdeck_config_toml(cfg))


def test_streamdeck_config_toml_roundtrips_scalars_and_lists():
    cfg = {
        "base_url": "http://127.0.0.1:9284",
        "brightness": 60,
        "rotation": 0,
        "keys": ["expiring", "blank", "commit"],
        "poll_seconds": 30,
    }
    assert _roundtrip(cfg) == cfg


def test_streamdeck_config_toml_roundtrips_override_tables():
    cfg = {
        "key_overrides": [
            {"slot": 20, "type": "shopping_add", "item": "Milk", "label": ""},
            {"slot": 3, "type": "timer", "minutes": 12},
        ],
    }
    out = _roundtrip(cfg)
    assert out["key_overrides"][0]["slot"] == 20
    assert out["key_overrides"][0]["item"] == "Milk"
    assert out["key_overrides"][1]["minutes"] == 12


def test_streamdeck_config_toml_keeps_macro_action_list():
    cfg = {
        "key_overrides": [
            {"slot": 1, "type": "macro", "actions": ["commit", "timer_1"]},
        ],
    }
    out = _roundtrip(cfg)
    assert out["key_overrides"][0]["actions"] == ["commit", "timer_1"]


def test_streamdeck_config_toml_escapes_quotes_and_backslashes():
    cfg = {"weather_location": 'say "hi" \\ there'}
    assert _roundtrip(cfg) == cfg


# --- Screensaver photo helpers (FoodAssistant-5w4m) --------------------------
# The bridge serves slideshow images from a pictures/ or photos/ directory at
# the drive root, read-only. Listing must be extension- and size-filtered,
# and the per-file lookup must fail closed on anything that tries to escape
# that directory.

def test_usb_photos_dir_matches_case_insensitively(tmp_path):
    (tmp_path / "Music").mkdir()
    (tmp_path / "PICTURES").mkdir()
    assert bridge._usb_photos_dir(str(tmp_path)) == str(tmp_path / "PICTURES")


def test_usb_photos_dir_accepts_photos_name_too(tmp_path):
    (tmp_path / "Photos").mkdir()
    assert bridge._usb_photos_dir(str(tmp_path)) == str(tmp_path / "Photos")


def test_usb_photos_dir_ignores_files_and_missing(tmp_path):
    # A FILE named photos does not count, and no match at all is None.
    (tmp_path / "photos").write_text("not a dir")
    assert bridge._usb_photos_dir(str(tmp_path)) is None
    assert bridge._usb_photos_dir(str(tmp_path / "nope")) is None


def test_usb_photo_names_filters_extension_hidden_and_size(tmp_path):
    (tmp_path / "b.JPG").write_bytes(b"x")
    (tmp_path / "a.png").write_bytes(b"x")
    (tmp_path / "c.webp").write_bytes(b"x")
    (tmp_path / "notes.txt").write_bytes(b"x")
    (tmp_path / "movie.mp4").write_bytes(b"x")
    (tmp_path / ".hidden.jpg").write_bytes(b"x")
    (tmp_path / "big.jpg").write_bytes(b"x" * 100)
    (tmp_path / "sub").mkdir()  # directories never listed, even sub.jpg-alikes
    assert bridge._usb_photo_names(str(tmp_path), max_bytes=50) == \
        ["a.png", "b.JPG", "c.webp"]


def test_usb_photo_names_empty_on_missing_dir(tmp_path):
    assert bridge._usb_photo_names(str(tmp_path / "gone")) == []


def test_usb_photo_safe_path_accepts_plain_image_names(tmp_path):
    (tmp_path / "pic.jpeg").write_bytes(b"x")
    assert bridge._usb_photo_safe_path(str(tmp_path), "pic.jpeg") == \
        os.path.realpath(str(tmp_path / "pic.jpeg"))


def test_usb_photo_safe_path_rejects_traversal_and_separators(tmp_path):
    (tmp_path / "pic.jpg").write_bytes(b"x")
    outside = tmp_path.parent / "outside.jpg"
    outside.write_bytes(b"x")
    for bad in ("", ".", "..", "../outside.jpg", "sub/pic.jpg",
                "..\\outside.jpg", "/etc/passwd", ".hidden.jpg"):
        assert bridge._usb_photo_safe_path(str(tmp_path), bad) is None


def test_usb_photo_safe_path_rejects_non_image_and_missing(tmp_path):
    (tmp_path / "notes.txt").write_text("x")
    assert bridge._usb_photo_safe_path(str(tmp_path), "notes.txt") is None
    assert bridge._usb_photo_safe_path(str(tmp_path), "gone.jpg") is None


def test_usb_photo_safe_path_rejects_symlink_escaping_the_dir(tmp_path):
    photos = tmp_path / "photos"
    photos.mkdir()
    secret = tmp_path / "secret.jpg"
    secret.write_bytes(b"x")
    (photos / "link.jpg").symlink_to(secret)
    assert bridge._usb_photo_safe_path(str(photos), "link.jpg") is None


# --- HEIC/HEIF transcode (FoodAssistant-hulb) -----------------------------

def test_usb_photo_names_and_safe_path_accept_heic(tmp_path):
    (tmp_path / "iphone.HEIC").write_bytes(b"x")
    (tmp_path / "iphone2.heif").write_bytes(b"x")
    assert bridge._usb_photo_names(str(tmp_path)) == ["iphone.HEIC", "iphone2.heif"]
    assert bridge._usb_photo_safe_path(str(tmp_path), "iphone.HEIC") == \
        os.path.realpath(str(tmp_path / "iphone.HEIC"))


def test_usb_photo_is_heic_matches_extension_only():
    assert bridge._usb_photo_is_heic("photo.heic") is True
    assert bridge._usb_photo_is_heic("photo.HEIF") is True
    assert bridge._usb_photo_is_heic("/some/dir/IMG_0001.Heic") is True
    assert bridge._usb_photo_is_heic("photo.jpg") is False
    assert bridge._usb_photo_is_heic("noext") is False


def test_heif_convert_cmd_is_plain_arg_vector():
    assert bridge._heif_convert_cmd("/a b/in.heic", "/tmp/out.jpg") == \
        ["heif-convert", "/a b/in.heic", "/tmp/out.jpg"]


def test_heic_cache_key_is_a_safe_flat_segment():
    key = bridge._heic_cache_key("/mnt/usb/photos/IMG 1.heic", 123.5)
    assert "/" not in key and "\\" not in key
    assert key.startswith("heic-") and key.endswith(".jpg")


def test_heic_cache_key_varies_by_path_and_mtime():
    a = bridge._heic_cache_key("/x/a.heic", 1)
    b = bridge._heic_cache_key("/x/b.heic", 1)
    c = bridge._heic_cache_key("/x/a.heic", 2)
    assert a != b and a != c
    # Stable for the same inputs.
    assert a == bridge._heic_cache_key("/x/a.heic", 1)


def _fake_heif_runner(jpeg_bytes=b"JPEGDATA", returncode=0, calls=None):
    """A stand-in for heif-convert: writes the destination file (cmd[2]) with
    the given bytes and reports the given return code. Records each call."""
    class _R:
        def __init__(self, rc):
            self.returncode = rc

    def run(cmd, capture_output=False, timeout=None):
        if calls is not None:
            calls.append(cmd)
        if returncode == 0:
            with open(cmd[2], "wb") as f:
                f.write(jpeg_bytes)
        return _R(returncode)
    return run


def test_heic_transcode_runs_convert_and_returns_jpeg(tmp_path):
    src = tmp_path / "img.heic"
    src.write_bytes(b"heic-bytes")
    cache = tmp_path / "cache"
    calls = []
    runner = _fake_heif_runner(b"JPEGDATA", calls=calls)
    body = bridge._heic_transcode(str(src), runner=runner, cache_dir=str(cache))
    assert body == b"JPEGDATA"
    assert len(calls) == 1 and calls[0][0] == "heif-convert"


def test_heic_transcode_serves_from_cache_without_reconverting(tmp_path):
    src = tmp_path / "img.heic"
    src.write_bytes(b"heic-bytes")
    cache = tmp_path / "cache"
    calls = []
    runner = _fake_heif_runner(b"JPEGDATA", calls=calls)
    first = bridge._heic_transcode(str(src), runner=runner, cache_dir=str(cache))
    second = bridge._heic_transcode(str(src), runner=runner, cache_dir=str(cache))
    assert first == second == b"JPEGDATA"
    assert len(calls) == 1  # second call hit the cache


def test_heic_transcode_reconverts_when_source_mtime_changes(tmp_path):
    src = tmp_path / "img.heic"
    src.write_bytes(b"heic-bytes")
    cache = tmp_path / "cache"
    calls = []
    runner = _fake_heif_runner(b"JPEGDATA", calls=calls)
    bridge._heic_transcode(str(src), runner=runner, cache_dir=str(cache))
    os.utime(str(src), (10000, 10000))  # simulate an edit in place
    bridge._heic_transcode(str(src), runner=runner, cache_dir=str(cache))
    assert len(calls) == 2


def test_heic_transcode_returns_none_when_tool_missing(tmp_path):
    src = tmp_path / "img.heic"
    src.write_bytes(b"heic-bytes")

    def missing(cmd, capture_output=False, timeout=None):
        raise FileNotFoundError("heif-convert")

    assert bridge._heic_transcode(
        str(src), runner=missing, cache_dir=str(tmp_path / "c")) is None


def test_heic_transcode_returns_none_on_convert_failure(tmp_path):
    src = tmp_path / "img.heic"
    src.write_bytes(b"heic-bytes")
    runner = _fake_heif_runner(returncode=1)
    assert bridge._heic_transcode(
        str(src), runner=runner, cache_dir=str(tmp_path / "c")) is None


def test_heic_transcode_returns_none_on_empty_output(tmp_path):
    src = tmp_path / "img.heic"
    src.write_bytes(b"heic-bytes")
    runner = _fake_heif_runner(jpeg_bytes=b"", calls=None)
    assert bridge._heic_transcode(
        str(src), runner=runner, cache_dir=str(tmp_path / "c")) is None


def test_heic_transcode_returns_none_for_missing_source(tmp_path):
    runner = _fake_heif_runner()
    assert bridge._heic_transcode(
        str(tmp_path / "gone.heic"), runner=runner,
        cache_dir=str(tmp_path / "c")) is None


def test_heic_cache_prune_keeps_newest(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    for i in range(5):
        p = cache / ("f%d.jpg" % i)
        p.write_bytes(b"x")
        os.utime(str(p), (1000 + i, 1000 + i))
    bridge._heic_cache_prune(str(cache), keep=2)
    remaining = sorted(os.listdir(str(cache)))
    assert remaining == ["f3.jpg", "f4.jpg"]


# --- Shared-token handshake (FoodAssistant-pxcm) ---------------------------

def test_token_gate_exempt_get_ok_without_token():
    for path in sorted(bridge.TOKEN_EXEMPT_GET):
        assert bridge._token_gate("GET", path, "", "secret", False) == "ok"


def test_token_gate_exempt_is_get_only():
    # POST /activity changes state (wakes the display) so it is not exempt.
    assert bridge._token_gate("POST", "/activity", "", "secret", False) == "deny"


def test_token_gate_query_string_does_not_dodge_the_check():
    assert bridge._token_gate("GET", "/usb/photo?path=x.jpg", "", "secret", False) == "deny"


def test_token_gate_correct_token_ok():
    assert bridge._token_gate("POST", "/reboot", "secret", "secret", False) == "ok"


def test_token_gate_missing_token_grace_allows():
    assert bridge._token_gate("POST", "/reboot", "", "secret", True) == "grace"


def test_token_gate_missing_token_denied_after_grace():
    assert bridge._token_gate("POST", "/reboot", "", "secret", False) == "deny"


def test_token_gate_wrong_token_denied_even_in_grace():
    assert bridge._token_gate("POST", "/reboot", "nope", "secret", True) == "deny"


def test_token_gate_no_expected_token_degrades_to_grace():
    # The bridge could not write its token file: never lock everyone out.
    assert bridge._token_gate("POST", "/reboot", "", "", False) == "grace"
    assert bridge._token_gate("POST", "/reboot", "stale", "", False) == "grace"


def test_load_or_create_token_creates_dir_and_file(tmp_path):
    path = tmp_path / "data" / "bridge-token"
    token = bridge._load_or_create_token(str(path))
    assert len(token) == 64 and all(c in "0123456789abcdef" for c in token)
    assert path.read_text().strip() == token
    # 644: the venv app and the deck run as the primary user, not root.
    assert (path.stat().st_mode & 0o777) == 0o644


def test_load_or_create_token_reuses_existing(tmp_path):
    path = tmp_path / "bridge-token"
    path.write_text("existing-token\n")
    assert bridge._load_or_create_token(str(path)) == "existing-token"


def test_authorized_grace_allows_and_warns(monkeypatch, capsys):
    monkeypatch.setattr(bridge, "_EXPECTED_TOKEN", "secret")
    monkeypatch.setattr(bridge, "GRACE_MODE", True)

    class Dummy:
        command = "POST"
        path = "/reboot"
        headers = {}
        sent = None

        def _send(self, code, data):
            self.sent = (code, data)

    d = Dummy()
    assert bridge._Handler._authorized(d) is True
    assert d.sent is None
    assert "grace mode" in capsys.readouterr().out


def test_authorized_denies_wrong_token_with_401(monkeypatch):
    monkeypatch.setattr(bridge, "_EXPECTED_TOKEN", "secret")
    monkeypatch.setattr(bridge, "GRACE_MODE", True)

    class Dummy:
        command = "POST"
        path = "/reboot"
        headers = {"X-Bridge-Token": "wrong"}
        sent = None

        def _send(self, code, data):
            self.sent = (code, data)

    d = Dummy()
    assert bridge._Handler._authorized(d) is False
    assert d.sent[0] == 401
# --- Support bundle helpers (FoodAssistant-w7mb) ---------------------------

def test_run_capture_notes_a_missing_command():
    out = bridge._run_capture(["definitely-not-a-command-xyz"])
    assert out.startswith("note: definitely-not-a-command-xyz is not installed")


def test_support_unit_names_parses_list_unit_files():
    def fake_run(cmd, timeout=15):
        return ("foodassistant-host-bridge.service enabled enabled\n"
                "foodassistant-kiosk.service enabled enabled\n"
                "somethingelse.service static -\n")
    assert bridge._support_unit_names(fake_run) == [
        "foodassistant-host-bridge.service", "foodassistant-kiosk.service"]


def test_support_units_reports_enabled_and_active():
    def fake_run(cmd, timeout=15):
        if cmd[:2] == ["systemctl", "list-unit-files"]:
            return "foodassistant-kiosk.service enabled enabled"
        if cmd[:2] == ["systemctl", "is-enabled"]:
            return "enabled"
        if cmd[:2] == ["systemctl", "is-active"]:
            return "active"
        return ""
    out = bridge._support_units(fake_run)
    assert out == "foodassistant-kiosk.service: enabled=enabled active=active"


def test_support_units_notes_when_none_found():
    out = bridge._support_units(lambda cmd, timeout=15: "")
    assert out.startswith("note: no foodassistant-* systemd units")


def test_support_dropins_reads_unit_files_and_dropins(tmp_path):
    (tmp_path / "foodassistant-kiosk.service").write_text("[Unit]\nkiosk\n")
    d = tmp_path / "foodassistant-kiosk.service.d"
    d.mkdir()
    (d / "override.conf").write_text("[Service]\noverride\n")
    (tmp_path / "unrelated.service").write_text("nope")
    out = bridge._support_dropins(str(tmp_path))
    assert "foodassistant-kiosk.service =====" in out
    assert "kiosk" in out
    assert "override" in out
    assert "nope" not in out


def test_support_helper_hashes(tmp_path):
    import hashlib
    p = tmp_path / "foodassistant-update"
    p.write_bytes(b"#!/bin/sh\necho hi\n")
    (tmp_path / "other-tool").write_bytes(b"x")
    out = bridge._support_helper_hashes(str(tmp_path))
    expected = hashlib.sha1(p.read_bytes()).hexdigest()
    assert out == f"{expected}  {p}"


def test_support_drm_lists_every_connector(tmp_path):
    c1 = tmp_path / "card0-HDMI-A-1"
    c1.mkdir()
    (c1 / "status").write_text("connected\n")
    c2 = tmp_path / "card0-DSI-1"
    c2.mkdir()
    (c2 / "status").write_text("disconnected\n")
    out = bridge._support_drm(str(tmp_path))
    assert "card0-HDMI-A-1: connected" in out
    assert "card0-DSI-1: disconnected" in out


def test_read_file_or_note_missing_and_tail(tmp_path):
    assert bridge._read_file_or_note(str(tmp_path / "gone")).startswith("note: ")
    big = tmp_path / "big.log"
    big.write_text("x" * 100)
    out = bridge._read_file_or_note(str(big), max_bytes=10)
    assert out.startswith("note: showing the last 10 bytes of 100")


def test_support_bundle_has_every_section_and_never_raises():
    result = bridge._support_bundle(lambda cmd, timeout=15: "stub output")
    assert result["ok"] is True
    sections = result["sections"]
    for key in ("systemd-units", "systemd-unit-files", "boot-cmdline",
                "helper-sha1", "bridge-journal", "input-devices",
                "drm-connectors", "disk-usage", "throttled", "update-log"):
        assert key in sections, key
        assert isinstance(sections[key], str) and sections[key]
# -- update channel (FoodAssistant-wkwx) --------------------------------------

def test_write_update_channel_persists_valid_channels(tmp_path):
    f = tmp_path / "etc" / "update-channel"  # parent dir is created on demand
    for ch in ("stable", "main"):
        ok, err = bridge._write_update_channel(ch, path=str(f))
        assert ok is True and err == ""
        assert f.read_text() == ch + "\n"


def test_write_update_channel_rejects_unknown_values(tmp_path):
    f = tmp_path / "update-channel"
    for bad in ("", "nightly", "Main", "stable\n"):
        ok, err = bridge._write_update_channel(bad, path=str(f))
        assert ok is False and "stable" in err
    assert not f.exists()


# --- USB photo mount fixes (FoodAssistant-l3op) ---

def test_usb_photo_candidates_keep_read_only_mounts():
    # Photos only read, so a ro-mounted stick (which backups must skip) counts.
    mounts = [
        ("/dev/sda1", "/media/pi/PHOTOS", "vfat", "ro,noatime"),
        ("/dev/sdb1", "/media/pi/BACKUP", "vfat", "rw,noatime"),
    ]
    got = bridge._usb_photo_mount_candidates(["sda", "sdb"], mounts)
    assert ("/dev/sda1", "/media/pi/PHOTOS") in got   # ro kept, unlike backups
    assert ("/dev/sdb1", "/media/pi/BACKUP") in got
    # The backup candidate list still drops the ro one.
    assert bridge._usb_mount_candidates(["sda", "sdb"], mounts) == [
        ("/dev/sdb1", "/media/pi/BACKUP")]


def test_usb_photo_candidates_skip_system_disks():
    mounts = [("/dev/sda1", "/", "ext4", "rw"),
              ("/dev/sda2", "/boot/firmware", "vfat", "rw")]
    assert bridge._usb_photo_mount_candidates(["sda"], mounts) == []


def test_usb_partitions_for_lists_partitions(tmp_path):
    disk = tmp_path / "sda"
    for p in ("sda1", "sda2", "queue", "sdaX"):
        (disk / p).mkdir(parents=True)
    assert bridge._usb_partitions_for("sda", str(tmp_path)) == [
        "/dev/sda1", "/dev/sda2"]


def test_usb_partitions_for_whole_disk_when_no_partition_table(tmp_path):
    (tmp_path / "sdb" / "queue").mkdir(parents=True)
    assert bridge._usb_partitions_for("sdb", str(tmp_path)) == ["/dev/sdb"]


def test_usb_unmounted_partitions_finds_the_unmounted_stick(tmp_path):
    for p in ("sda1",):
        (tmp_path / "sda" / p).mkdir(parents=True)
    (tmp_path / "sdb" / "sdb1").mkdir(parents=True)
    mounts = [("/dev/sda1", "/media/x", "vfat", "rw")]   # sda mounted, sdb not
    assert bridge._usb_unmounted_partitions(
        ["sda", "sdb"], mounts, str(tmp_path)) == ["/dev/sdb1"]


def test_usb_photos_status_mounts_on_demand_when_nothing_mounted(monkeypatch, tmp_path):
    # No removable drive is mounted; the bridge should mount an unmounted
    # partition read-only and find the photos folder there.
    photos = tmp_path / "mnt" / "photos"
    photos.mkdir(parents=True)
    (photos / "a.jpg").write_bytes(b"x")
    monkeypatch.setattr(bridge, "_usb_removable_disks", lambda *a, **k: ["sda"])
    monkeypatch.setattr(bridge, "_usb_unmounted_partitions",
                        lambda *a, **k: ["/dev/sda1"])
    monkeypatch.setattr(bridge, "_usb_try_mount_ro",
                        lambda dev, mountpoint=None: str(tmp_path / "mnt"))
    monkeypatch.setattr("builtins.open",
                        lambda *a, **k: __import__("io").StringIO(""))
    pdir, reason = bridge._usb_photos_status()
    assert pdir == str(photos) and reason == ""


# --- kiosk screensaver state (FoodAssistant-qh8p) ---------------------------


def test_screensaver_post_tracks_state_without_bumping_activity(monkeypatch):
    import io
    # Freeze last_activity to an old value so we can prove the report never
    # advances it (a screensaver report is not activity, FoodAssistant-ofip).
    with bridge._activity_lock:
        bridge._activity_state["last_activity"] = 100.0
        bridge._activity_state["screensaver_active"] = False

    class FakeHandler:
        headers = {"Content-Length": "16"}
        rfile = io.BytesIO(b'{"active": true}')
        sent = None

        def _send(self, code, data):
            self.sent = (code, data)

        _body = bridge._Handler._body
        _screensaver_post = bridge._Handler._screensaver_post

    h = FakeHandler()
    h._screensaver_post()
    assert h.sent[0] == 200
    assert h.sent[1]["screensaver_active"] is True
    with bridge._activity_lock:
        assert bridge._activity_state["screensaver_active"] is True
        # The activity epoch is untouched: no new wake source.
        assert bridge._activity_state["last_activity"] == 100.0


def test_activity_get_reports_screensaver_active(monkeypatch):
    import io
    with bridge._activity_lock:
        bridge._activity_state["screensaver_active"] = True

    class FakeHandler:
        sent = None

        def _send(self, code, data):
            self.sent = (code, data)

        _activity_get = bridge._Handler._activity_get

    h = FakeHandler()
    h._activity_get()
    assert h.sent[0] == 200
    # The deck polls this to decide the display-off logo on a soft sleep.
    assert h.sent[1]["screensaver_active"] is True
    with bridge._activity_lock:
        bridge._activity_state["screensaver_active"] = False


# Optional print stack (FoodAssistant-gyri): the .env marker the OTA update
# helper and the print-setup helper share to know printing was turned on.


def test_printing_enabled_true_forms():
    for val in ("1", "true", "TRUE", "yes", "on", "On"):
        assert bridge._printing_enabled(f"TZ=x\nPRINTING_ENABLED={val}\n") is True


def test_printing_enabled_false_forms():
    for val in ("0", "false", "no", "off", ""):
        assert bridge._printing_enabled(f"PRINTING_ENABLED={val}\n") is False


def test_printing_enabled_absent_marker():
    assert bridge._printing_enabled("TZ=x\nCOMPOSE_PROFILES=with-mealie\n") is False


def test_printing_enabled_empty_and_none():
    assert bridge._printing_enabled("") is False
    assert bridge._printing_enabled(None) is False


def test_printing_enabled_ignores_quotes():
    assert bridge._printing_enabled('PRINTING_ENABLED="1"\n') is True


# -- Printer discovery + add validation (FoodAssistant-r9a4) ----------------
# The queue name and device URI flow into lpadmin, run as root. These pin the
# pure validators and argv builders that keep unsanitized input out of the
# command (defense in depth: the app validates the same way).


def test_valid_printer_name_accepts_safe_rejects_unsafe():
    for good in ("KitchenLaser", "Zebra_ZD-410", "a1"):
        assert bridge._valid_printer_name(good) is True
    for bad in ("has space", "a/b", "a;rm -rf", "a$(x)", "a`b`", "a|b",
                "a&b", "", "   "):
        assert bridge._valid_printer_name(bad) is False


def test_valid_printer_connection_requires_scheme_and_safe_chars():
    assert bridge._valid_printer_connection("ipp://192.168.1.5/ipp/print") is True
    assert bridge._valid_printer_connection("socket://10.0.0.9:9100") is True
    assert bridge._valid_printer_connection("dnssd://Brother%20HL._ipp._tcp.local/") is True
    for bad in ("192.168.1.5", "socket://h;reboot", "ipp://h/`id`",
                "socket://h /x", ""):
        assert bridge._valid_printer_connection(bad) is False


def test_lpadmin_add_args_driverless_and_socket():
    assert bridge._lpadmin_add_args(
        "Brother", "ipp://192.168.1.50/ipp/print", "everywhere") == [
        "lpadmin", "-p", "Brother", "-E", "-v",
        "ipp://192.168.1.50/ipp/print", "-m", "everywhere"]
    # Empty model falls back to a raw queue.
    assert bridge._lpadmin_add_args("Zebra", "socket://h:9100", "") == [
        "lpadmin", "-p", "Zebra", "-E", "-v", "socket://h:9100", "-m", "raw"]
    # A Zebra ZPL label printer adds with the bundled PPD (-P), not -m.
    assert bridge._lpadmin_add_args("Zebra_ZM400", "socket://h:9100", "zebra-zpl") == [
        "lpadmin", "-p", "Zebra_ZM400", "-E", "-v", "socket://h:9100",
        "-P", bridge._PPD_DRIVERS["zebra-zpl"]]


def test_lpadmin_add_args_rejects_unsafe():
    import pytest
    with pytest.raises(ValueError):
        bridge._lpadmin_add_args("bad name", "ipp://h/ipp/print", "everywhere")
    with pytest.raises(ValueError):
        bridge._lpadmin_add_args("Good", "socket://h;reboot", "raw")


def test_lpadmin_remove_args_validates_name():
    import pytest
    assert bridge._lpadmin_remove_args("Brother") == ["lpadmin", "-x", "Brother"]
    with pytest.raises(ValueError):
        bridge._lpadmin_remove_args("bad; rm -rf /")


LPINFO_L = """\
Device: uri = dnssd://Brother%20HL-L8360CDW._ipp._tcp.local./?uuid=abc
        class = network
        info = Brother HL-L8360CDW
        make-and-model = Brother HL-L8360CDW
Device: uri = socket
        class = network
        info = AppSocket/HP JetDirect
Device: uri = socket://192.168.1.77:9100
        class = network
        info = Zebra ZM400
        make-and-model = Zebra ZM400
"""


def test_bridge_parse_lpinfo_v_skips_bare_scheme_and_classifies():
    devices = bridge._parse_lpinfo_v(LPINFO_L)
    uris = [d["uri"] for d in devices]
    assert "socket" not in uris
    assert uris == [
        "dnssd://Brother%20HL-L8360CDW._ipp._tcp.local./?uuid=abc",
        "socket://192.168.1.77:9100",
    ]
    by_uri = {d["uri"]: d for d in devices}
    assert by_uri["socket://192.168.1.77:9100"]["kind"] == "socket"
    assert by_uri["dnssd://Brother%20HL-L8360CDW._ipp._tcp.local./?uuid=abc"]["kind"] == "driverless"
    # Suggested names are always valid queue names.
    for d in devices:
        assert bridge._valid_printer_name(d["name"]) is True


def test_bridge_parse_lpstat_v_collects_uris():
    text = ("device for OfficeLaser: ipp://192.168.1.50/ipp/print\n"
            "device for Raw: socket://10.0.0.9:9100\n")
    assert bridge._parse_lpstat_v(text) == {
        "ipp://192.168.1.50/ipp/print", "socket://10.0.0.9:9100"}
