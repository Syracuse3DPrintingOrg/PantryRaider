"""foodassistant-agent-key: the Stream Deck's and the thermometer reader's own
key for the app on this device (I-5, host side).

The helper decides from the unit as systemd sees it (systemctl show, here a
stub on PATH) and from the config file, never from its own environment, and
never takes the place of a key the owner set.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "image-build" / "foodassistant-agent-key"
BRIDGE = ROOT / "scripts" / "image-build" / "foodassistant-host-bridge"
VECTORS = json.loads((ROOT / "tests" / "fixtures" / "local_credentials_vectors.json").read_text())


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


ak = _load("foodassistant_agent_key", HELPER)

STUB = r'''#!{python}
import json, os, sys
data = json.load(open(os.environ["STUB_SYSTEMCTL_DATA"]))
args = sys.argv[1:]
with open(os.environ["STUB_SYSTEMCTL_DATA"] + ".log", "a") as f:
    f.write(" ".join(args) + "\n")
if not args or args[0] != "show":
    sys.exit(0)
if data.get("_fail"):
    sys.exit(1)
prop = args[args.index("-p") + 1]
unit = args[-1]
u = data.get(unit, {{}})
if prop == "EnvironmentFiles":
    for p in u.get("EnvironmentFiles", []):
        print("EnvironmentFiles=%s (ignore_errors=yes)" % p)
elif prop == "ExecStart":
    argv = u.get("ExecStart", "")
    if argv:
        print("ExecStart={{ path=%s ; argv[]=%s ; ignore_errors=no ; start_time=[n/a] ; "
              "stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }}" % (argv.split()[0], argv))
elif "--value" in args:
    print(u.get(prop, ""))
else:
    print("%s=%s" % (prop, u.get(prop, "")))
'''


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A fake host: install dir with a bridge token, an agent env dir, and a
    systemctl stub on PATH whose show output comes from a JSON file."""
    install = tmp_path / "opt"
    (install / "data").mkdir(parents=True)
    (install / "data" / "bridge-token").write_text(VECTORS["token"] + "\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "systemctl"
    stub.write_text(STUB.format(python=sys.executable))
    stub.chmod(0o755)
    data = tmp_path / "systemctl.json"
    data.write_text("{}")
    envdir = tmp_path / "run" / "foodassistant-agents"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SYSTEMCTL", "systemctl")
    monkeypatch.setenv("STUB_SYSTEMCTL_DATA", str(data))
    monkeypatch.setenv("INSTALL_DIR", str(install))
    monkeypatch.setenv("AGENT_ENV_DIR", str(envdir))
    for name in ("FOODASSISTANT_API_KEY", "FOODASSISTANT_BASE_URL",
                 "FOODASSISTANT_STREAMDECK_CONFIG", "FOODASSISTANT_GADGETS_CONFIG"):
        monkeypatch.delenv(name, raising=False)

    class Host:
        root = tmp_path
        install_dir = install
        env_dir = envdir

        def units(self, units):
            data.write_text(json.dumps(units))

        def env_file(self, agent):
            return envdir / f"{agent}.env"

        def key_written(self, agent):
            p = self.env_file(agent)
            return p.exists() and p.read_text() == \
                f"FOODASSISTANT_API_KEY={VECTORS['agent_keys'][agent]}\n"

    return Host()


def _deck_unit(environment="FOODASSISTANT_BASE_URL=http://localhost:9284",
               files=(), config="/etc/foodassistant/streamdeck.toml"):
    return {"Environment": environment, "EnvironmentFiles": list(files),
            "ExecStart": ("/opt/foodassistant/venv/bin/python -m foodassistant_streamdeck"
                          + (f" --config {config}" if config else ""))}


# --- the key itself ---------------------------------------------------------

@pytest.mark.parametrize("agent", ["streamdeck", "gadgets"])
def test_key_equals_the_vectors(agent):
    assert ak.agent_key(VECTORS["token"], agent) == VECTORS["agent_keys"][agent]


# --- loopback base URLs are written -----------------------------------------

def test_unit_environment_loopback_writes_with_tight_modes(host):
    # A Bandit's deck unit: Environment=FOODASSISTANT_BASE_URL=http://localhost:80.
    host.units({"foodassistant-streamdeck.service": _deck_unit(
        "FOODASSISTANT_BASE_URL=http://localhost:80", config=None)})
    assert ak.main(["streamdeck"]) == 0
    assert host.key_written("streamdeck")
    assert stat.S_IMODE(host.env_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(host.env_file("streamdeck").stat().st_mode) == 0o600


def test_gadgets_env_environment_file_case(host, tmp_path):
    genv = tmp_path / "gadgets.env"
    genv.write_text("# written by gadgets-setup\nFOODASSISTANT_BASE_URL=http://127.0.0.1:9284\n")
    host.units({"foodassistant-gadgets.service": {
        "Environment": "FOODASSISTANT_BASE_URL=http://192.168.1.20:9284",
        "EnvironmentFiles": [str(genv)],
        "ExecStart": "/opt/foodassistant/venv/bin/python -m foodassistant_gadgets"
                     f" --config {tmp_path / 'missing.toml'}"}})
    ak.main(["gadgets"])
    # The EnvironmentFile value wins over Environment=, as in systemd.
    assert host.key_written("gadgets")


def test_environment_file_lan_url_overrides_a_loopback_environment(host, tmp_path):
    genv = tmp_path / "gadgets.env"
    genv.write_text('FOODASSISTANT_BASE_URL="http://192.168.1.20:9284"\n')
    host.units({"foodassistant-gadgets.service": {
        "Environment": "FOODASSISTANT_BASE_URL=http://localhost:9284",
        "EnvironmentFiles": [str(genv)], "ExecStart": ""}})
    ak.main(["gadgets"])
    assert not host.env_file("gadgets").exists()


def test_default_base_url_is_loopback(host):
    host.units({"foodassistant-streamdeck.service": {"Environment": "", "ExecStart": ""}})
    ak.main(["streamdeck"])
    assert host.key_written("streamdeck")


# --- never over an owner key, never off this device -------------------------

def test_lan_base_url_is_not_written_and_a_stale_file_goes(host, capsys):
    host.env_dir.mkdir(parents=True)
    host.env_file("streamdeck").write_text("FOODASSISTANT_API_KEY=stale\n")
    host.units({"foodassistant-streamdeck.service": _deck_unit(
        "FOODASSISTANT_BASE_URL=http://192.168.1.20:9284", config=None)})
    ak.main(["streamdeck"])
    assert not host.env_file("streamdeck").exists()
    assert "agent key for streamdeck: not used (not a loopback address)" in capsys.readouterr().err


def test_owner_key_in_unit_environment(host, capsys):
    host.units({"foodassistant-streamdeck.service": _deck_unit(
        "FOODASSISTANT_BASE_URL=http://localhost:9284 FOODASSISTANT_API_KEY=owner-key-123",
        config=None)})
    ak.main(["streamdeck"])
    assert not host.env_file("streamdeck").exists()
    err = capsys.readouterr().err
    assert "not used (an owner key is set)" in err
    assert "owner-key-123" not in err


def test_owner_key_in_a_foreign_environment_file(host, tmp_path):
    owner = tmp_path / "streamdeck.env"
    owner.write_text("FOODASSISTANT_API_KEY='owner-key-123'\n")
    host.units({"foodassistant-streamdeck.service": _deck_unit(files=[f"-{owner}"],
                                                               config=None)})
    ak.main(["streamdeck"])
    assert not host.env_file("streamdeck").exists()


def test_owner_key_in_streamdeck_config(host, tmp_path):
    cfg = tmp_path / "streamdeck.toml"
    cfg.write_text('base_url = "http://127.0.0.1:9284"\napi_key = "owner-key-123"\n')
    host.units({"foodassistant-streamdeck.service": _deck_unit("", config=str(cfg))})
    ak.main(["streamdeck"])
    assert not host.env_file("streamdeck").exists()


def test_owner_key_in_gadgets_toml(host, tmp_path):
    cfg = tmp_path / "gadgets.toml"
    cfg.write_text("api_key = 'owner-key-123'\n")
    host.units({"foodassistant-gadgets.service": {
        "Environment": "FOODASSISTANT_BASE_URL=http://localhost:9284",
        "ExecStart": f"/opt/foodassistant/venv/bin/python -m foodassistant_gadgets --config {cfg}"}})
    ak.main(["gadgets"])
    assert not host.env_file("gadgets").exists()


def test_config_from_the_environment_variable_and_install_dir_default(host, tmp_path):
    cfg = tmp_path / "other.toml"
    cfg.write_text('api_key = "owner-key-123"\n')
    host.units({"foodassistant-gadgets.service": {
        "Environment": f"FOODASSISTANT_GADGETS_CONFIG={cfg}", "ExecStart": ""}})
    ak.main(["gadgets"])
    assert not host.env_file("gadgets").exists()
    # With nothing named, $INSTALL_DIR/config.toml is read.
    (host.install_dir / "config.toml").write_text('base_url = "http://192.168.1.20:9284"\n')
    host.units({"foodassistant-streamdeck.service": {"Environment": "", "ExecStart": ""}})
    ak.main(["streamdeck"])
    assert not host.env_file("streamdeck").exists()


def test_a_pra1_api_key_in_config_counts_as_ours(host, tmp_path):
    cfg = tmp_path / "streamdeck.toml"
    cfg.write_text(f'api_key = "{VECTORS["agent_keys"]["streamdeck"]}"\n')
    host.units({"foodassistant-streamdeck.service": _deck_unit(config=str(cfg))})
    ak.main(["streamdeck"])
    assert host.key_written("streamdeck")


def test_second_run_with_our_own_file_loaded_still_writes(host, monkeypatch):
    """Critic 2: systemd loads our own env file before ExecStartPre, so the
    key from the last start is in os.environ and in EnvironmentFiles. It must
    not count as an owner key, or the key would flip off at every restart."""
    own = host.env_file("streamdeck")
    host.units({"foodassistant-streamdeck.service": _deck_unit(
        files=[f"-{own}"], config=None)})
    ak.main(["streamdeck"])
    assert host.key_written("streamdeck")
    first = own.stat()
    monkeypatch.setenv("FOODASSISTANT_API_KEY", VECTORS["agent_keys"]["streamdeck"])
    monkeypatch.setenv("FOODASSISTANT_BASE_URL", "http://192.168.1.20:9284")
    ak.main(["streamdeck"])
    assert host.key_written("streamdeck")
    # Same content: not rewritten.
    assert own.stat().st_ino == first.st_ino and own.stat().st_mtime_ns == first.st_mtime_ns


def test_os_environ_is_never_read(host, monkeypatch):
    monkeypatch.setenv("FOODASSISTANT_API_KEY", "owner-key-from-somewhere-else")
    host.units({"foodassistant-streamdeck.service": _deck_unit(config=None)})
    ak.main(["streamdeck"])
    assert host.key_written("streamdeck")


def test_no_token_removes_and_logs(host, capsys):
    (host.install_dir / "data" / "bridge-token").write_text("  \n")
    host.env_dir.mkdir(parents=True)
    host.env_file("gadgets").write_text("FOODASSISTANT_API_KEY=old\n")
    host.units({"foodassistant-gadgets.service": _deck_unit(config=None)})
    assert ak.main(["gadgets"]) == 0
    assert not host.env_file("gadgets").exists()
    assert "agent key for gadgets: not used (no bridge token)" in capsys.readouterr().err


def test_unreadable_unit_never_writes(host, capsys):
    """When systemctl show gives no answer, an owner key in the unit could go
    unseen and our file, loaded after the owner's settings, would replace it."""
    host.env_dir.mkdir(parents=True)
    host.env_file("streamdeck").write_text("FOODASSISTANT_API_KEY=old\n")
    host.units({"_fail": True})
    assert ak.main(["streamdeck"]) == 0
    assert not host.env_file("streamdeck").exists()
    assert "agent key for streamdeck: not used" in capsys.readouterr().err


def test_unknown_agent_exits_zero(host):
    assert ak.main(["kiosk"]) == 0
    assert ak.main([]) == 0
    assert not host.env_dir.exists()


def test_key_never_printed(host, capsys):
    host.units({"foodassistant-streamdeck.service": _deck_unit(config=None)})
    ak.main(["streamdeck"])
    out = capsys.readouterr()
    assert "agent key for streamdeck: written" in out.err
    key = VECTORS["agent_keys"]["streamdeck"]
    assert key not in out.err and key not in out.out


# --- TOML without tomllib ---------------------------------------------------

def test_line_reader_reads_the_bridges_config_without_tomllib(tmp_path, monkeypatch):
    bridge = _load("foodassistant_host_bridge_for_agent_key", BRIDGE)
    cfg = {
        "base_url": "http://192.168.1.20:9284",
        "api_key": 'owner"key\\with\ttab',
        "brightness": 60,
        "key_overrides": [{"slot": 1, "type": "page", "base_url": "http://other"}],
    }
    path = tmp_path / "config.toml"
    path.write_text(bridge._streamdeck_config_toml(cfg), encoding="utf-8")
    monkeypatch.setitem(sys.modules, "tomllib", None)
    monkeypatch.setitem(sys.modules, "tomli", None)
    got = ak.read_config_strings(str(path))
    assert got == {"base_url": "http://192.168.1.20:9284", "api_key": 'owner"key\\with\ttab'}


def test_line_reader_literal_strings_and_tables():
    text = ("# comment\nbase_url = 'http://localhost:80'  # trailing\n"
            'api_key = "a\\u0041b"\n[section]\napi_key = "nope"\n')
    assert ak.read_toml_strings_fallback(text) == {"base_url": "http://localhost:80",
                                                   "api_key": "aAb"}
    assert ak.read_toml_strings_fallback('api_key = "unterminated\n') == {}


def test_unreadable_config_counts_as_unset(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("api_key = [unclosed\n")
    assert ak.read_config_strings(str(bad)) == {}
    assert ak.read_config_strings(str(tmp_path / "missing.toml")) == {}
    assert ak.read_config_strings(str(tmp_path)) == {}  # a directory


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
def test_a_fifo_in_place_of_the_config_never_blocks(tmp_path):
    # The config can be writable by the helper's own account; a FIFO there
    # must not hold up the unit's start.
    fifo = tmp_path / "config.toml"
    os.mkfifo(fifo)
    done = []
    t = threading.Thread(target=lambda: done.append(ak.read_config_strings(str(fifo))),
                         daemon=True)
    t.start()
    t.join(5)
    assert done == [{}]


def test_systemctl_show_parsers():
    assert ak.parse_environment_value('A=1 "B=two words" C=') == {"A": "1", "B": "two words", "C": ""}
    assert ak.parse_environment_files(
        "EnvironmentFiles=/etc/a.env (ignore_errors=yes)\nEnvironmentFiles=/etc/b env (ignore_errors=no)\n"
    ) == ["/etc/a.env", "/etc/b env"]
    assert ak.config_from_execstart(
        "ExecStart={ path=/x ; argv[]=/x -m mod --config /etc/c.toml ; ignore_errors=no }"
    ) == "/etc/c.toml"
    assert ak.config_from_execstart("") == ""


def test_loopback_hosts():
    assert ak.is_loopback_url("http://LOCALHOST:80")
    assert ak.is_loopback_url("http://[::1]:9284")
    assert not ak.is_loopback_url("http://kitchen-pi:9284")
    assert not ak.is_loopback_url("http://127.0.1.1:9284")
