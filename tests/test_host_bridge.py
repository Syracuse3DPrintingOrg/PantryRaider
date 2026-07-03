"""Unit tests for the Pantry Raider host bridge helpers.

The bridge is a plain python http.server script with no .py extension, so it is
loaded here from its source path. These tests cover the pure helpers that gate
the background Mealie start (FoodAssistant-5wc): the install/start tracking map
and the compose environment used to invoke docker compose.

Run: python -m pytest tests/test_host_bridge.py -q
"""
from __future__ import annotations

import importlib.util
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
    captured = {}
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
    assert 'ATTRS{name}=="ADS7846 Touchscreen"' in written
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


def test_reboot_command_prefers_systemctl():
    cmd = bridge._reboot_command(which=lambda n: n == "systemctl")
    assert cmd == ["systemctl", "reboot"]


def test_reboot_command_falls_back_to_reboot():
    cmd = bridge._reboot_command(which=lambda n: False)
    assert cmd == ["reboot"]


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
