"""Unit tests for the FoodAssistant host bridge helpers.

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
    assert bridge._hardware_status() == {
        "ok": True,
        "display": {"present": True, "connectors": ["card1-HDMI-A-1"]},
        "streamdeck": {"present": True, "model": "Stream Deck XL"},
    }


def test_hardware_status_nothing_attached(monkeypatch):
    monkeypatch.setattr(bridge, "_drm_connected", lambda *a, **k: [])
    monkeypatch.setattr(bridge, "_streamdeck_info", lambda *a, **k: (False, ""))
    assert bridge._hardware_status() == {
        "ok": True,
        "display": {"present": False, "connectors": []},
        "streamdeck": {"present": False, "model": ""},
    }


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
    # Only vcgencmd present.
    cmds = bridge._display_power_commands(False, which=lambda n: n == "vcgencmd")
    assert cmds == [["vcgencmd", "display_power", "0"]]
    cmds_on = bridge._display_power_commands(True, which=lambda n: n == "vcgencmd")
    assert cmds_on == [["vcgencmd", "display_power", "1"]]


def test_display_power_commands_orders_vcgencmd_then_xset():
    cmds = bridge._display_power_commands(False, which=lambda n: True)
    assert cmds[0][0] == "vcgencmd"
    assert ["xset", "dpms", "force", "off"] in cmds


def test_display_power_commands_empty_when_no_tools():
    assert bridge._display_power_commands(True, which=lambda n: False) == []


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
