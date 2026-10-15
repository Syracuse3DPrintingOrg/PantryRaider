"""The app-side recovery-hotspot config: /setup/ap/config proxy routes and the
Network pane card.

The bridge owns the actual files (see test_host_bridge.py); these cover the
app's validators, the proxy's error degradation off a Pi and with no bridge,
and that the settings card renders on Pi shapes only.

Run: python -m pytest tests/test_ap_hotspot.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(_SERVICE))

from app.config import settings  # noqa: E402


class _Resp:
    def __init__(self, status, data=None):
        self.status_code = status
        self._data = data or {}

    def json(self):
        return self._data


class _FakeBridge:
    """Async-context httpx stand-in dispatching bridge calls by URL suffix."""

    def __init__(self, routes, calls=None):
        self._routes = routes
        self.calls = calls if calls is not None else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _match(self, url):
        for suffix, resp in self._routes.items():
            if url.endswith(suffix):
                return resp
        return _Resp(404)

    async def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self._match(url)

    async def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self._match(url)


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app

    cwd = os.getcwd()
    os.chdir(_SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "auth_password", "")
    # Grocy configured so the setup-redirect middleware stays out of the way.
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test")
    monkeypatch.setattr(settings, "grocy_api_key", "k")
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


# --- pure validators ---------------------------------------------------------

def test_validate_ap_ssid_truth_table():
    from app.routers.setup import validate_ap_ssid

    ok = ["FoodAssistant", "Kitchen Pi", "a", "x" * 32]
    bad = ["", "   ", " edge", "edge ", "x" * 33, "two\nlines", "tab\tname",
           "café" + "x" * 29]
    for ssid in ok:
        assert validate_ap_ssid(ssid) == "", ssid
    for ssid in bad:
        assert validate_ap_ssid(ssid) != "", ssid


def test_validate_ap_passphrase_truth_table():
    from app.routers.setup import validate_ap_passphrase

    ok = ["goodpass", "x" * 63, "with spaces ok", "punct!@#$%"]
    bad = ["", "x" * 7, "x" * 64, "café-password", "line\nbreak-pw"]
    for pw in ok:
        assert validate_ap_passphrase(pw) == "", pw
    for pw in bad:
        assert validate_ap_passphrase(pw) != "", pw


# --- proxy routes ------------------------------------------------------------

def test_ap_config_get_off_pi_degrades_cleanly(client):
    with patch("app.routers.setup.is_raspberry_pi", return_value=False):
        r = client.get("/setup/ap/config")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is False and "platform" in d["error"]


def test_ap_config_get_proxies_the_bridge(client):
    routes = {"/ap/config": _Resp(200, {
        "ok": True, "active": False, "ssid": "Kitchen Pi",
        "default_ssid": "FoodAssistant", "generated": True,
        "passphrase": "abcd-efgh-jkmn",
    })}
    with patch("app.routers.setup.is_raspberry_pi", return_value=True), \
         patch("app.routers.setup.bridge_client",
               side_effect=lambda *a, **k: _FakeBridge(routes)):
        r = client.get("/setup/ap/config")
    d = r.json()
    assert d["ok"] is True and d["ssid"] == "Kitchen Pi"
    assert d["passphrase"] == "abcd-efgh-jkmn"


def test_ap_config_get_bridge_down_reports_error(client):
    class _Boom:
        async def __aenter__(self):
            raise RuntimeError("bridge down")

        async def __aexit__(self, *a):
            return False

    with patch("app.routers.setup.is_raspberry_pi", return_value=True), \
         patch("app.routers.setup.bridge_client",
               side_effect=lambda *a, **k: _Boom()):
        r = client.get("/setup/ap/config")
    d = r.json()
    assert d["ok"] is False and "bridge down" in d["error"]


def test_ap_config_post_validates_before_proxying(client):
    calls = []
    routes = {"/ap/config": _Resp(200, {"ok": True})}
    with patch("app.routers.setup.is_raspberry_pi", return_value=True), \
         patch("app.routers.setup.bridge_client",
               side_effect=lambda *a, **k: _FakeBridge(routes, calls)):
        r1 = client.post("/setup/ap/config", json={"ssid": "x" * 33})
        r2 = client.post("/setup/ap/config", json={"passphrase": "short"})
        r3 = client.post("/setup/ap/config", json={})
    assert r1.status_code == 400 and "32 bytes" in r1.json()["error"]
    assert r2.status_code == 400 and "8 to 63" in r2.json()["error"]
    assert r3.status_code == 400  # nothing to change
    assert calls == [], "invalid payloads must never reach the bridge"


def test_ap_config_post_forwards_valid_payload(client):
    calls = []
    routes = {"/ap/config": _Resp(200, {
        "ok": True, "active": False, "ssid": "Kitchen Pi", "generated": False,
    })}
    with patch("app.routers.setup.is_raspberry_pi", return_value=True), \
         patch("app.routers.setup.bridge_client",
               side_effect=lambda *a, **k: _FakeBridge(routes, calls)):
        r = client.post("/setup/ap/config",
                        json={"ssid": "Kitchen Pi", "passphrase": "new-pass-99"})
    assert r.json()["ok"] is True
    assert len(calls) == 1
    method, url, kw = calls[0]
    assert method == "POST" and url.endswith("/ap/config")
    assert kw["json"] == {"ssid": "Kitchen Pi", "passphrase": "new-pass-99"}


def test_ap_config_post_empty_ssid_resets_and_regenerate_wins(client):
    calls = []
    routes = {"/ap/config": _Resp(200, {"ok": True})}
    with patch("app.routers.setup.is_raspberry_pi", return_value=True), \
         patch("app.routers.setup.bridge_client",
               side_effect=lambda *a, **k: _FakeBridge(routes, calls)):
        client.post("/setup/ap/config", json={"ssid": ""})
        client.post("/setup/ap/config",
                    json={"regenerate": True, "passphrase": "ignored-pw"})
    # An empty ssid is forwarded as the reset-to-default signal.
    assert calls[0][2]["json"] == {"ssid": ""}
    # regenerate wins over a simultaneous passphrase.
    assert calls[1][2]["json"] == {"regenerate": True}


def test_ap_config_post_off_pi_degrades_cleanly(client):
    with patch("app.routers.setup.is_raspberry_pi", return_value=False):
        r = client.post("/setup/ap/config", json={"ssid": "Anything"})
    d = r.json()
    assert d["ok"] is False and "platform" in d["error"]


# --- pane render -------------------------------------------------------------

def _render_setup(client, is_pi):
    with patch.object(type(settings), "is_configured", lambda self: True), \
         patch("app.services.readiness.gate_possible", return_value=False), \
         patch("app.routers.setup.is_raspberry_pi", return_value=is_pi), \
         patch("app.templating.is_raspberry_pi", return_value=is_pi):
        r = client.get("/setup")
    assert r.status_code == 200
    return r.text


def test_hotspot_card_renders_on_pi_only(client, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted")
    html = _render_setup(client, is_pi=True)
    assert "Recovery hotspot" in html
    assert 'id="ap_ssid"' in html
    assert 'id="ap_passphrase"' in html
    assert 'id="ap-config-status"' in html
    # Honest copy: saving while joined through the hotspot drops the session.
    assert "saving drops your connection" in html

    monkeypatch.setattr(settings, "deployment_mode", "server")
    html = _render_setup(client, is_pi=False)
    assert 'id="ap_ssid"' not in html
    assert 'id="ap-config-status"' not in html


def test_hotspot_card_renders_on_pi_remote_too(client, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    html = _render_setup(client, is_pi=True)
    assert 'id="ap_ssid"' in html
    assert 'id="ap_passphrase"' in html


# --- the bridge's SD card copy (I-19) ----------------------------------------
# The owner can turn off the copy of the hotspot details kept on the SD card.
# These run the bridge's own code, loaded from its source like
# test_host_bridge.py does, with every path pointed into tmp.

import importlib.machinery  # noqa: E402
import importlib.util  # noqa: E402

_BRIDGE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "image-build" / "foodassistant-host-bridge"


def _load_bridge():
    loader = importlib.machinery.SourceFileLoader("foodassistant_host_bridge_ap", str(_BRIDGE_PATH))
    spec = importlib.util.spec_from_loader("foodassistant_host_bridge_ap", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


bridge = _load_bridge()

_CARD_TEXT = (
    "Pantry Raider recovery hotspot\n"
    "Network name: Kitchen Pi\n"
    "Password: pw-123456\n"
    "This copy lets you find the password by putting the card in a computer. "
    "Anyone who reads this card can join your hotspot. To stop keeping it here, "
    "turn off Keep a copy on the SD card in Settings, Network.\n"
)


@pytest.fixture
def ap(monkeypatch, tmp_path):
    """Every hotspot path in tmp, a current identity on disk, the AP down,
    and subprocess.run recorded instead of run."""
    conf = tmp_path / "hostapd.conf"
    conf.write_text(bridge._render_hostapd_conf("Kitchen Pi", "pw-123456"))
    boot_a = tmp_path / "boot-firmware"
    boot_b = tmp_path / "boot"
    boot_a.mkdir()
    boot_b.mkdir()
    flag = tmp_path / "ap-boot-copy"
    monkeypatch.setattr(bridge, "_HOSTAPD_CONF", str(conf))
    monkeypatch.setattr(bridge, "_AP_SSID_FILE", str(tmp_path / "ap-ssid"))
    monkeypatch.setattr(bridge, "_AP_PASSPHRASE_FILE", str(tmp_path / "ap-passphrase"))
    monkeypatch.setattr(bridge, "_AP_BOOT_DIRS", (str(boot_a), str(boot_b)))
    monkeypatch.setattr(bridge, "_AP_FLAG", str(tmp_path / "ap-active"))
    monkeypatch.setattr(bridge, "_AP_BOOT_COPY_FLAG", str(flag))
    monkeypatch.setattr(bridge, "_EXPECTED_TOKEN", "secret")
    calls = []
    monkeypatch.setattr(bridge.subprocess, "run", lambda *a, **k: calls.append(a[0]))
    return {"conf": conf, "boot": (boot_a, boot_b), "flag": flag, "calls": calls,
            "tmp": tmp_path}


def _post(body):
    class Dummy:
        headers = {"X-Bridge-Token": "secret"}
        sent = None
        _has_valid_token = bridge._Handler._has_valid_token
        _ap_config_get = bridge._Handler._ap_config_get
        _ap_config_set = bridge._Handler._ap_config_set

        def _body(self):
            return body

        def _send(self, code, data):
            self.sent = (code, data)

    return Dummy()


def _mirror(boot):
    return boot / "pantry-raider-hotspot.txt"


def test_card_text_is_exactly_four_lines():
    text = bridge._render_hotspot_mirror("Kitchen Pi", "pw-123456")
    assert text == _CARD_TEXT
    assert len(text.splitlines()) == 4 and text.endswith("\n")
    assert chr(0x2014) not in text


def test_boot_copy_flag_truth_table(tmp_path):
    flag = tmp_path / "ap-boot-copy"
    assert bridge._ap_boot_copy_enabled(str(flag)) is True      # missing
    for content, expected in (("0\n", False), ("0", False), ("1\n", True),
                              ("", True), ("garbage\n", True), ("off\n", True),
                              ("00\n", True), ("1\n0\n", True)):
        flag.write_text(content)
        assert bridge._ap_boot_copy_enabled(str(flag)) is expected, content
    # Bytes that are not text are unrecognised, so the copy stays on.
    flag.write_bytes(b"\xff\xfe\x00")
    assert bridge._ap_boot_copy_enabled(str(flag)) is True


def test_boot_copy_set_off_then_on(ap):
    boot_a, boot_b = ap["boot"]
    _mirror(boot_a).write_text("old copy\n")
    _mirror(boot_b).write_text("old copy\n")
    bridge._ap_boot_copy_set(False)
    assert ap["flag"].read_text() == "0\n"
    assert (ap["flag"].stat().st_mode & 0o777) == 0o644
    assert not _mirror(boot_a).exists() and not _mirror(boot_b).exists()

    bridge._ap_boot_copy_set(True)
    assert ap["flag"].read_text() == "1\n"
    assert (ap["flag"].stat().st_mode & 0o777) == 0o644
    assert _mirror(boot_a).read_text() == _CARD_TEXT
    assert ap["calls"] == [], "the SD card switch never touches hostapd"


def test_write_with_the_flag_off_removes_a_stale_copy(ap):
    boot_a, boot_b = ap["boot"]
    _mirror(boot_a).write_text("Pantry Raider recovery hotspot\nSSID: Old\nPassword: old\n")
    ap["flag"].write_text("0\n")
    bridge._ap_config_write("Kitchen Pi", "pw-123456")
    assert not _mirror(boot_a).exists() and not _mirror(boot_b).exists()
    assert bridge._parse_hostapd_conf(ap["conf"].read_text()) == ("Kitchen Pi", "pw-123456")
    # And with the flag on, the copy comes back.
    ap["flag"].write_text("1\n")
    bridge._ap_config_write("Kitchen Pi", "pw-123456")
    assert _mirror(boot_a).read_text() == _CARD_TEXT


def test_boot_copy_only_post_never_touches_hostapd(ap):
    # The hotspot is broadcasting: an identity change would restart hostapd.
    (ap["tmp"] / "ap-active").write_text("")
    before = ap["conf"].read_bytes()
    mtime = ap["conf"].stat().st_mtime_ns
    _mirror(ap["boot"][0]).write_text("old copy\n")
    d = _post({"boot_copy": False})
    d._ap_config_set()
    code, body = d.sent
    assert code == 200 and body["ok"] is True and body["boot_copy"] is False
    assert body["active"] is True and body["ssid"] == "Kitchen Pi"
    assert ap["conf"].read_bytes() == before
    assert ap["conf"].stat().st_mtime_ns == mtime
    assert ap["calls"] == [], "no systemctl: the owner's connection never drops"
    assert ap["flag"].read_text() == "0\n"
    assert not _mirror(ap["boot"][0]).exists()

    d = _post({"boot_copy": True})
    d._ap_config_set()
    assert d.sent[0] == 200 and d.sent[1]["boot_copy"] is True
    assert ap["conf"].read_bytes() == before
    assert ap["calls"] == []
    assert _mirror(ap["boot"][0]).read_text() == _CARD_TEXT


def test_boot_copy_with_the_current_identity_never_touches_hostapd(ap):
    # A settings form that posts every field along with the switch names the
    # name and password already in force: still only an SD card change.
    (ap["tmp"] / "ap-active").write_text("")
    before = ap["conf"].read_bytes()
    mtime = ap["conf"].stat().st_mtime_ns
    d = _post({"ssid": "Kitchen Pi", "passphrase": "pw-123456", "boot_copy": False})
    d._ap_config_set()
    assert d.sent[0] == 200 and d.sent[1]["boot_copy"] is False
    assert ap["conf"].read_bytes() == before
    assert ap["conf"].stat().st_mtime_ns == mtime
    assert ap["calls"] == []
    assert ap["flag"].read_text() == "0\n"
    # A real change of name still goes on the air.
    d = _post({"ssid": "Pantry", "boot_copy": False})
    d._ap_config_set()
    assert d.sent[0] == 200
    assert bridge._parse_hostapd_conf(ap["conf"].read_text())[0] == "Pantry"
    assert ap["calls"] == [["systemctl", "restart", "hostapd"]]


def test_boot_copy_must_be_a_boolean(ap):
    for value in ("0", 0, "false", None, [True]):
        d = _post({"boot_copy": value})
        d._ap_config_set()
        if value is None:
            # Treated as absent: nothing to change, the identity is rewritten
            # as today and the flag is left alone.
            assert d.sent[0] == 200
            assert not ap["flag"].exists()
        else:
            assert d.sent[0] == 400, value
            assert not ap["flag"].exists()


def test_post_without_boot_copy_leaves_the_flag(ap):
    ap["flag"].write_text("0\n")
    d = _post({"ssid": "Pantry", "passphrase": "new-pass-99"})
    d._ap_config_set()
    assert d.sent[0] == 200 and d.sent[1]["boot_copy"] is False
    assert ap["flag"].read_text() == "0\n"
    assert not _mirror(ap["boot"][0]).exists()
    assert bridge._parse_hostapd_conf(ap["conf"].read_text()) == ("Pantry", "new-pass-99")


def test_post_with_identity_and_boot_copy_applies_both(ap):
    ap["flag"].write_text("0\n")
    d = _post({"ssid": "Pantry", "boot_copy": True})
    d._ap_config_set()
    assert d.sent[0] == 200 and d.sent[1]["boot_copy"] is True
    assert ap["flag"].read_text() == "1\n"
    assert "Network name: Pantry\n" in _mirror(ap["boot"][0]).read_text()


def test_get_returns_boot_copy(ap):
    d = _post({})
    d._ap_config_get()
    assert d.sent[0] == 200 and d.sent[1]["boot_copy"] is True
    ap["flag"].write_text("0\n")
    d._ap_config_get()
    assert d.sent[1]["boot_copy"] is False
