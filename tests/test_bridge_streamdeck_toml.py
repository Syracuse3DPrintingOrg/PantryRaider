"""Stream Deck config.toml written by the host bridge.

The bridge turns the posted Stream Deck settings into
/opt/foodassistant/config.toml, which the deck controller loads with tomllib on
every start. These tests cover three things about that file:

- the TOML is always valid: strings escape newlines, tabs and other control
  characters, and keys that TOML cannot hold bare are dropped, so a value
  synced from a main server can never break the deck or inject extra keys;
- a result that still fails to parse is refused with a 400 before anything is
  written;
- the file holds the Home Assistant token and camera credentials, so it is
  written 0600 for the deck's user, atomically through os.replace, and an
  existing readable file is tightened when the bridge starts.

Run: python -m pytest tests/test_bridge_streamdeck_toml.py -q
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
import stat
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BRIDGE = REPO / "scripts" / "image-build" / "foodassistant-host-bridge"


def _load_bridge():
    spec = importlib.util.spec_from_loader(
        "foodassistant_host_bridge_sdtoml",
        importlib.machinery.SourceFileLoader("foodassistant_host_bridge_sdtoml", str(BRIDGE)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bridge = _load_bridge()


def _roundtrip(cfg):
    return tomllib.loads(bridge._streamdeck_config_toml(cfg))


# --- Serializer: always valid TOML -------------------------------------------

def test_theme_with_newline_roundtrips():
    cfg = {"theme": "dark\nmode", "brightness": 60}
    assert _roundtrip(cfg) == cfg


def test_override_label_with_tab_and_newline_roundtrips():
    cfg = {
        "key_overrides": [
            {"slot": 4, "type": "shopping_add", "item": "Milk",
             "label": "Buy\tmilk\nnow"},
        ],
    }
    out = _roundtrip(cfg)
    assert out["key_overrides"][0]["label"] == "Buy\tmilk\nnow"
    assert out["key_overrides"][0]["slot"] == 4


def test_other_control_characters_roundtrip():
    s = "a\rb\x00c\x01d\x1fe\x7ff\x08g\x0ch"
    cfg = {"weather_location": s, "keys": ["x\ny", "z\x7f"]}
    assert _roundtrip(cfg) == cfg


def test_crafted_value_cannot_inject_top_level_keys():
    # A value that closes the string and starts a new line must stay one
    # string: no extra key and no table header may appear.
    evil = 'x"\nha_token = "attacker"\n[[key_overrides]]\nslot = 1\n#'
    cfg = {"theme": evil}
    out = _roundtrip(cfg)
    assert out == {"theme": evil}
    assert "ha_token" not in out
    assert "key_overrides" not in out


def test_bad_top_level_key_is_dropped():
    cfg = {"brightness": 60, "bad key\nx = 1": "v", "": 3, "ok_key-2": "fine"}
    out = _roundtrip(cfg)
    assert out == {"brightness": 60, "ok_key-2": "fine"}


def test_bad_key_inside_table_entry_is_dropped():
    cfg = {
        "key_overrides": [
            {"slot": 2, "type": "timer", "minutes": 5,
             'label"\n[evil]\nx': "boom"},
        ],
    }
    out = _roundtrip(cfg)
    assert out == {"key_overrides": [{"slot": 2, "type": "timer", "minutes": 5}]}


def test_key_ending_in_newline_is_dropped():
    # "$" in a regex also matches just before a trailing newline, so a key
    # like "label\n" must still be refused, at the top level and in a table.
    cfg = {
        "theme\n": "x",
        "brightness": 60,
        "key_overrides": [{"slot": 1, "label\n": "boom", "type": "timer"}],
        "keys\n": [{"slot": 2}],
    }
    out = _roundtrip(cfg)
    assert out == {"brightness": 60,
                   "key_overrides": [{"slot": 1, "type": "timer"}]}


def test_bad_table_name_is_dropped():
    cfg = {"bad name": [{"slot": 1}], "brightness": 10}
    assert _roundtrip(cfg) == {"brightness": 10}


def test_existing_shapes_still_roundtrip():
    cfg = {
        "base_url": "http://127.0.0.1:9284",
        "brightness": 60,
        "keys": ["expiring", "blank", "commit"],
        "weather_location": 'say "hi" \\ there',
        "key_overrides": [
            {"slot": 1, "type": "macro", "actions": ["commit", "timer_1"]},
        ],
    }
    assert _roundtrip(cfg) == cfg


# --- POST /streamdeck/config handler ------------------------------------------

class _Harness:
    def __init__(self, payload):
        raw = json.dumps(payload).encode()
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.sent = None

    def _send(self, code, data):
        self.sent = (code, data)

    _body = bridge._Handler._body
    _streamdeck_set_config = bridge._Handler._streamdeck_set_config
    _streamdeck_get_config = bridge._Handler._streamdeck_get_config


@pytest.fixture
def chowns(monkeypatch):
    # Unit tests do not run as root; record the chown instead of doing it.
    calls = []
    monkeypatch.setattr(bridge.os, "chown",
                        lambda p, uid, gid: calls.append((p, uid, gid)))
    monkeypatch.setattr(bridge, "_primary_user_ids", lambda: (1000, 1000))
    return calls


@pytest.fixture
def cfg_path(tmp_path, monkeypatch, chowns):
    path = tmp_path / "config.toml"
    monkeypatch.setattr(bridge, "STREAMDECK_CONFIG_PATH", str(path))
    return path


def test_set_config_writes_0600_through_os_replace(cfg_path, chowns, monkeypatch):
    replaced = []
    real_replace = os.replace

    def spy_replace(src, dst):
        replaced.append((src, dst))
        # The temp file is already locked down before it becomes config.toml.
        assert stat.S_IMODE(os.stat(src).st_mode) == 0o600
        real_replace(src, dst)

    monkeypatch.setattr(bridge.os, "replace", spy_replace)
    h = _Harness({"config": {"theme": "dark\nmode", "ha_token": "secret"}})
    h._streamdeck_set_config()
    assert h.sent == (200, {"ok": True})
    assert len(replaced) == 1
    src, dst = replaced[0]
    assert dst == str(cfg_path)
    assert os.path.dirname(src) == os.path.dirname(str(cfg_path))
    assert not os.path.exists(src)
    assert stat.S_IMODE(os.stat(cfg_path).st_mode) == 0o600
    assert tomllib.loads(cfg_path.read_text()) == {
        "theme": "dark\nmode", "ha_token": "secret"}
    # Handed to the deck's user, not left to root.
    assert chowns and chowns[0][1:] == (1000, 1000)
    # No stray temp files left beside it.
    assert sorted(p.name for p in cfg_path.parent.iterdir()) == ["config.toml"]


def test_write_is_utf8_under_an_ascii_locale(tmp_path):
    # The deck reads the file as UTF-8 bytes, so a bridge started under a
    # plain C locale must still write a non-ASCII city name, not fail.
    import subprocess
    import sys
    path = tmp_path / "config.toml"
    script = (
        "import importlib.machinery, importlib.util, os, sys\n"
        "loader = importlib.machinery.SourceFileLoader('b', sys.argv[1])\n"
        "spec = importlib.util.spec_from_loader('b', loader)\n"
        "b = importlib.util.module_from_spec(spec); loader.exec_module(b)\n"
        "b._primary_user_ids = lambda: None\n"
        "b.os.chown = lambda *a: None\n"
        "b._write_streamdeck_config("
        "b._streamdeck_config_toml({'weather_location': 'Montr\\u00e9al'}), sys.argv[2])\n"
    )
    env = dict(os.environ, LC_ALL="C", LANG="C", PYTHONCOERCECLOCALE="0",
               PYTHONUTF8="0")
    r = subprocess.run([sys.executable, "-X", "utf8=0", "-c", script,
                        str(BRIDGE), str(path)],
                       env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert tomllib.loads(path.read_bytes().decode("utf-8")) == {
        "weather_location": "Montréal"}


def test_set_config_tightens_an_existing_readable_file(cfg_path):
    cfg_path.write_text('theme = "old"\n')
    os.chmod(cfg_path, 0o644)
    h = _Harness({"config": {"theme": "new"}})
    h._streamdeck_set_config()
    assert h.sent[0] == 200
    assert stat.S_IMODE(os.stat(cfg_path).st_mode) == 0o600
    assert tomllib.loads(cfg_path.read_text()) == {"theme": "new"}


def test_set_config_refuses_toml_that_does_not_parse(cfg_path, monkeypatch):
    cfg_path.write_text('theme = "keep"\n')
    monkeypatch.setattr(bridge, "_streamdeck_config_toml",
                        lambda cfg: 'theme = "broken\n')
    h = _Harness({"config": {"theme": "x"}})
    h._streamdeck_set_config()
    assert h.sent[0] == 400
    assert h.sent[1]["ok"] is False
    assert h.sent[1]["error"]
    # The deck's current file is untouched.
    assert cfg_path.read_text() == 'theme = "keep"\n'


def test_set_config_refuses_non_object(cfg_path):
    h = _Harness({"config": ["not", "a", "table"]})
    h._streamdeck_set_config()
    assert h.sent[0] == 400
    assert not cfg_path.exists()


def test_failed_write_leaves_no_temp_file(cfg_path, monkeypatch):
    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(bridge.os, "replace", boom)
    h = _Harness({"config": {"theme": "x"}})
    h._streamdeck_set_config()
    assert h.sent[0] == 500
    assert list(cfg_path.parent.iterdir()) == []


def test_write_keeps_existing_owner_when_no_primary_user(cfg_path, chowns, monkeypatch):
    cfg_path.write_text('theme = "old"\n')
    monkeypatch.setattr(bridge, "_primary_user_ids", lambda: None)
    st = os.stat(cfg_path)
    bridge._write_streamdeck_config('theme = "new"\n', str(cfg_path))
    assert chowns[-1][1:] == (st.st_uid, st.st_gid)
    assert stat.S_IMODE(os.stat(cfg_path).st_mode) == 0o600


# --- Startup tightening --------------------------------------------------------

def test_startup_tightens_readable_config(cfg_path, chowns):
    cfg_path.write_text('ha_token = "secret"\n')
    os.chmod(cfg_path, 0o644)
    bridge._secure_streamdeck_config(str(cfg_path))
    assert stat.S_IMODE(os.stat(cfg_path).st_mode) == 0o600
    # A file already owned by the deck's user keeps its owner.
    assert chowns == []


def test_startup_leaves_private_config_alone(cfg_path, monkeypatch):
    cfg_path.write_text('theme = "x"\n')
    os.chmod(cfg_path, 0o600)
    calls = []
    monkeypatch.setattr(bridge.os, "chmod", lambda *a: calls.append(a))
    bridge._secure_streamdeck_config(str(cfg_path))
    assert calls == []


def test_startup_ignores_missing_config(tmp_path):
    bridge._secure_streamdeck_config(str(tmp_path / "nope.toml"))


def test_startup_hands_root_owned_config_to_deck_user(tmp_path, monkeypatch):
    # A root-owned 0644 file (created by an older bridge) was readable by the
    # deck only through the world bit; tightening it must not lock the deck
    # out, so it goes to the deck's user at the same time.
    path = tmp_path / "config.toml"
    path.write_text('theme = "x"\n')
    os.chmod(path, 0o644)
    real_stat = os.stat

    class _St:
        def __init__(self, st):
            self.st_mode = st.st_mode
            self.st_uid = 0
            self.st_gid = 0

    monkeypatch.setattr(bridge.os, "stat",
                        lambda p, *a, **k: _St(real_stat(p, *a, **k)))
    chowns = []
    monkeypatch.setattr(bridge.os, "chown",
                        lambda p, uid, gid: chowns.append((p, uid, gid)))
    monkeypatch.setattr(bridge, "_primary_user_ids", lambda: (1000, 1000))
    bridge._secure_streamdeck_config(str(path))
    assert chowns == [(str(path), 1000, 1000)]
    assert stat.S_IMODE(real_stat(path).st_mode) == 0o600


# --- tomllib fallback ------------------------------------------------------------

def test_tomllib_loader_prefers_stdlib():
    assert bridge._tomllib() is tomllib


def test_tomllib_loader_falls_back_to_tomli(monkeypatch):
    import builtins
    import sys
    import types

    fake = types.ModuleType("tomli")
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "tomllib":
            raise ModuleNotFoundError("No module named 'tomllib'")
        return real_import(name, *a, **k)

    monkeypatch.setitem(sys.modules, "tomli", fake)
    monkeypatch.delitem(sys.modules, "tomllib", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert bridge._tomllib() is fake


def test_get_config_reads_written_file(cfg_path, monkeypatch):
    cfg_path.write_text('theme = "dark\\nmode"\n')
    monkeypatch.setattr(bridge.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no venv")))
    h = _Harness({})
    h._streamdeck_get_config()
    assert h.sent == (200, {"ok": True, "config": {"theme": "dark\nmode"}})
