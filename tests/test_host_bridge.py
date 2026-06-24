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
