"""foodassistant-kiosk-launch: the kitchen screen's launcher (I-5 ticket
step, I-6 debugging pipe broker and navigation socket).

The browser here is a stub, a small Python script that reads NUL-ended JSON
on fd 3, answers on fd 4 like Chromium's debugging pipe and records what it
received and how it was started. tests/test_kiosk_launcher_chromium.py runs
the same launcher against a real Chromium.
"""
from __future__ import annotations

import asyncio
import http.server
import importlib.machinery
import importlib.util
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

import pytest

from foodassistant_streamdeck import kiosk_nav

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX") or not hasattr(os, "posix_spawnp"),
                                reason="needs Unix sockets and posix_spawn")

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "image-build" / "foodassistant-kiosk-launch"
KIOSK_NAV = ROOT / "streamdeck" / "foodassistant_streamdeck" / "kiosk_nav.py"
OLD_DECK = json.loads((ROOT / "tests" / "data" / "streamdeck_v0_20_0_files.json").read_text())
VECTORS = json.loads((ROOT / "tests" / "fixtures" / "local_credentials_vectors.json").read_text())
TICKET = VECTORS["tickets"][0]["ticket"]
LEGACY = "--remote-debugging-port=9222"
PIPE = "--remote-debugging-pipe"


def _load():
    loader = importlib.machinery.SourceFileLoader("foodassistant_kiosk_launch", str(LAUNCHER))
    spec = importlib.util.spec_from_loader("foodassistant_kiosk_launch", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


kl = _load()

STUB_BROWSER = r'''#!{python}
import json, os, signal, sys, time
rec = os.environ["STUB_RECORD"]
with open(rec + ".argv", "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")

def _term(signum, frame):
    with open(rec + ".signals", "a") as f:
        f.write("%d\n" % signum)
    os._exit(int(os.environ.get("STUB_TERM_CODE", "0")))

signal.signal(signal.SIGTERM, _term)
try:
    os.fstat(3)
    os.fstat(4)
    piped = "--remote-debugging-pipe" in sys.argv
except OSError:
    piped = False
if not piped or os.environ.get("STUB_MODE") == "silent":
    while True:
        time.sleep(1)
buf = b""
while True:
    chunk = os.read(3, 65536)
    if not chunk:
        break
    buf += chunk
    while b"\0" in buf:
        raw, buf = buf.split(b"\0", 1)
        msg = json.loads(raw.decode("utf-8"))
        with open(rec + ".msgs", "a") as f:
            f.write(json.dumps(msg) + "\n")
        m = msg.get("method")
        if m == "Target.getTargets":
            result = {{"targetInfos": [{{"targetId": "W1", "type": "service_worker"}},
                                       {{"targetId": "P1", "type": "page", "url": "x"}}]}}
        elif m == "Target.attachToTarget":
            result = {{"sessionId": "S1"}}
        elif m == "Page.navigate":
            result = {{"frameId": "F1", "loaderId": "L1"}}
        else:
            result = {{}}
        os.write(4, json.dumps({{"method": "Target.targetInfoChanged", "params": {{}}}}).encode() + b"\0")
        os.write(4, json.dumps({{"id": msg["id"], "result": result}}).encode() + b"\0")
while True:
    time.sleep(1)
'''

STUB_SYSTEMCTL = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
data = json.loads(os.environ.get("STUB_SYSTEMCTL_JSON", "{{}}"))
if args and args[0] == "show" and "-p" in args:
    print(data.get(args[args.index("-p") + 1], ""))
'''


@pytest.fixture
def short_dir():
    """A short directory: a Unix socket path must stay under 108 bytes."""
    d = tempfile.mkdtemp(prefix="kl-")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def rig(tmp_path, short_dir):
    run = short_dir / "run"
    run.mkdir(mode=0o711)
    stub = tmp_path / "browser"
    stub.write_text(STUB_BROWSER.format(python=sys.executable))
    stub.chmod(0o755)
    sysctl = tmp_path / "systemctl"
    sysctl.write_text(STUB_SYSTEMCTL.format(python=sys.executable))
    sysctl.chmod(0o755)
    record = tmp_path / "rec"
    env = dict(os.environ)
    env.update({
        "KIOSK_RUN_DIR": str(run),
        "DECK_PKG_DIR": str(tmp_path / "deck"),
        "FA_DEVTOOLS_FLAG": str(tmp_path / "devtools"),
        "SYSTEMCTL": str(sysctl),
        "STUB_SYSTEMCTL_JSON": json.dumps({"LoadState": "not-found"}),
        "STUB_RECORD": str(record),
        "FA_PIPE_CHECK_SECONDS": "20",
        "FA_PROBE_RETRY_SECONDS": "0.1",
    })
    procs = []

    class Rig:
        run_dir = run
        sock = run / "nav.sock"
        browser = stub
        environ = env
        log = tmp_path / "launcher.log"

        def start(self, *args, **extra):
            e = dict(env)
            e.update(extra)
            with open(self.log, "ab") as errf:
                p = subprocess.Popen([sys.executable, str(LAUNCHER), str(stub), *args],
                                     env=e, stdout=subprocess.DEVNULL, stderr=errf,
                                     start_new_session=True)
            procs.append(p)
            return p

        def argvs(self):
            p = Path(str(record) + ".argv")
            return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

        def msgs(self):
            p = Path(str(record) + ".msgs")
            return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

        def signals(self):
            p = Path(str(record) + ".signals")
            return p.read_text().split() if p.exists() else []

        def wait_for(self, cond, timeout=15.0):
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if cond():
                    return True
                time.sleep(0.05)
            return False

    yield Rig()
    for p in procs:
        if p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            p.wait(timeout=5)


def _ask(sock_path, message, raw=None):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(str(sock_path))
    s.sendall(raw if raw is not None else json.dumps(message).encode() + b"\n")
    data = b""
    while not data.endswith(b"\n"):
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
    s.close()
    return json.loads(data)


# --- pure helpers -----------------------------------------------------------

def test_kiosk_url_detection_and_origin_join():
    args = ["--kiosk", "--app=http://ignored", "http://a/x", "https://localhost:9284/ui/?kiosk=1"]
    assert kl.kiosk_url_index(args) == 3
    assert kl.kiosk_url_index(["--kiosk"]) == -1
    assert kl.origin_of("http://user:pw@localhost:9284/ui/?kiosk=1") == "http://localhost:9284"
    assert kl.origin_of("HTTPS://[::1]/ui/") == "https://[::1]"
    assert kl.origin_of("file:///run/x.html") == ""
    assert kl.join_origin("http://localhost:9284", "/ui/camera?cam=Front%20Door") == \
        "http://localhost:9284/ui/camera?cam=Front%20Door"


def test_is_local_kiosk_host():
    for host in ("127.0.0.1", "localhost", "LOCALHOST", "::1", "[::1]", "kitchen-pi"):
        assert kl.is_local_kiosk_host(host, hostname="kitchen-pi"), host
    for host in ("kitchen-pi.local", "pantry.example.com", "kitchen-pi.lan", "192.168.1.20", ""):
        assert not kl.is_local_kiosk_host(host, hostname="kitchen-pi"), host
    # A dotted host name never counts as this device.
    assert not kl.is_local_kiosk_host("kitchen-pi.lan", hostname="kitchen-pi.lan")


def _reason(**kw):
    base = dict(deck_loaded=False, deck_has_nav=False, deck_user="", kiosk_user="kiosk",
                deck_groups=None, socket_gid=1000, socket_ok=True, devtools=False)
    base.update(kw)
    return kl.legacy_port_reason(**base)


def test_legacy_port_decision_table():
    assert _reason() == ""
    assert "older Stream Deck" in _reason(deck_loaded=True, deck_has_nav=False)
    assert _reason(deck_loaded=True, deck_has_nav=True, deck_user="kiosk") == ""
    assert "cannot open" in _reason(deck_loaded=True, deck_has_nav=True, deck_user="pi",
                                     deck_groups=[1001])
    assert "cannot open" in _reason(deck_loaded=True, deck_has_nav=True, deck_user="ghost",
                                     deck_groups=None)
    assert _reason(deck_loaded=True, deck_has_nav=True, deck_user="pi",
                   deck_groups=[1001, 1000]) == ""
    assert _reason(deck_loaded=True, deck_has_nav=True, deck_user="root") == ""
    assert "debugging switch" in _reason(devtools=True)
    assert "socket could not be created" in _reason(socket_ok=False)


def test_old_deck_package_opens_9222_and_the_repo_package_does_not(tmp_path, monkeypatch):
    old = tmp_path / "old_deck"
    old.mkdir()
    for name in OLD_DECK["files"]:
        (old / name).write_text("")
    for name in OLD_DECK["dirs"]:
        (old / name).mkdir()
    assert "kiosk_nav.py" not in OLD_DECK["files"]
    monkeypatch.setattr(kl, "_systemctl_value",
                        lambda prop, unit: {"LoadState": "loaded", "User": ""}.get(prop, ""))
    monkeypatch.setenv("FA_DEVTOOLS_FLAG", str(tmp_path / "no-flag"))
    monkeypatch.setenv("DECK_PKG_DIR", str(old))
    assert "older Stream Deck" in kl.host_legacy_reason("kiosk", 1000, True)
    monkeypatch.setenv("DECK_PKG_DIR", str(ROOT / "streamdeck" / "foodassistant_streamdeck"))
    assert kl.host_legacy_reason("kiosk", 1000, True) == ""
    (tmp_path / "flag").write_text("")
    monkeypatch.setenv("FA_DEVTOOLS_FLAG", str(tmp_path / "flag"))
    assert "debugging switch" in kl.host_legacy_reason("kiosk", 1000, True)


def test_no_answer_from_systemd_goes_by_the_deck_package(tmp_path, monkeypatch):
    monkeypatch.setattr(kl, "_systemctl_value", lambda prop, unit: None)
    monkeypatch.setenv("FA_DEVTOOLS_FLAG", str(tmp_path / "no-flag"))
    old = tmp_path / "old_deck"
    old.mkdir()
    for name in OLD_DECK["files"]:
        (old / name).write_text("")
    monkeypatch.setenv("DECK_PKG_DIR", str(old))
    assert "older Stream Deck" in kl.host_legacy_reason("kiosk", 1000, True)
    monkeypatch.setenv("DECK_PKG_DIR", str(tmp_path / "no-deck"))
    assert kl.host_legacy_reason("kiosk", 1000, True) == ""
    monkeypatch.setenv("DECK_PKG_DIR", str(ROOT / "streamdeck" / "foodassistant_streamdeck"))
    assert kl.host_legacy_reason("kiosk", 1000, True) == ""


def test_flags_keep_the_url_last():
    args = ["/usr/bin/chromium", "--kiosk", "http://localhost:9284/ui/?kiosk=1"]
    out = kl.with_flags(args, [PIPE, LEGACY])
    assert out == ["/usr/bin/chromium", "--kiosk", PIPE, LEGACY, "http://localhost:9284/ui/?kiosk=1"]
    assert kl.with_flags(out, [LEGACY]) == out
    # The local launch page that stands in for the kiosk URL stays last too.
    page = ["/usr/bin/chromium", "--kiosk", "file:///run/foodassistant-kiosk/launch.html"]
    assert kl.with_flags(page, [PIPE])[-2:] == [PIPE, page[-1]]


def test_ticket_shape():
    assert kl.valid_ticket(TICKET)
    for bad in ("", "v2" + TICKET[2:], TICKET.upper(), TICKET + ".x", "v1.abc.0.0",
                TICKET.replace(".", "-")):
        assert not kl.valid_ticket(bad), bad


def test_launch_page_escapes_and_hides_the_referrer():
    target = kl.launch_target("http://localhost:9284", TICKET, "http://localhost:9284/ui/?kiosk=1")
    assert target == ("http://localhost:9284/ui/kiosk/launch?ticket=" + TICKET
                      + "&next=%2Fui%2F%3Fkiosk%3D1")
    page = kl.launch_page(target)
    assert '<meta name="referrer" content="no-referrer">' in page
    assert "#212529" in page
    assert "&amp;next=" in page  # HTML-escaped in the attribute
    assert "\\u0026next=" in page  # JSON in the script, with no raw & or <
    evil = kl.launch_page('http://x/"></script><script>alert(1)</script>')
    assert "</script><script>" not in evil


def test_handle_request_codes():
    ok = lambda path: True  # noqa: E731
    ready = lambda: True  # noqa: E731
    assert kl.handle_request(b'{"op": "ping"}', ok, ready) == {"ok": True, "version": 1}
    for path in ("/ui/add", "/ui/camera?cam=Front%20Door"):
        assert kl.handle_request(json.dumps({"op": "navigate", "path": path}).encode(),
                                 ok, ready) == {"ok": True}
    for path in ("ui/add", "//evil.example/x", "/a\\b", "/a\x01", "", "/" + "a" * 2048, None):
        assert kl.handle_request(json.dumps({"op": "navigate", "path": path}).encode(),
                                 ok, ready) == {"ok": False, "error": "bad_path"}, path
    for raw in (b"not json", b"[1]", b'{"op": "reboot"}', b'{"op": "ping"}' + b" " * 4096):
        assert kl.handle_request(raw, ok, ready) == {"ok": False, "error": "bad_request"}
    nav = json.dumps({"op": "navigate", "path": "/ui/add"}).encode()
    assert kl.handle_request(nav, ok, lambda: False) == {"ok": False, "error": "not_ready"}
    assert kl.handle_request(nav, lambda p: False, ready) == {"ok": False, "error": "navigate_failed"}


# --- the nav path rule is shared byte for byte -------------------------------

def _nav_block(text):
    start = text.index("# --- nav path rule (shared with the Stream Deck; keep byte-identical) ---")
    end = text.index("# --- end nav path rule ---") + len("# --- end nav path rule ---")
    return text[start:end]


def test_nav_path_block_is_byte_identical_to_the_decks():
    assert _nav_block(LAUNCHER.read_text(encoding="utf-8")) == \
        _nav_block(KIOSK_NAV.read_text(encoding="utf-8"))
    assert kl.NAV_PATH_MAX == 2048


class _FakePipe:
    closed = False

    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send(self, method, params=None, session_id=None, timeout=3.0):
        self.sent.append((method, params, session_id))
        if self.fail:
            raise kl.CDPError("boom")
        if method == "Target.getTargets":
            return {"targetInfos": [{"targetId": "P1", "type": "page"}]}
        if method == "Target.attachToTarget":
            return {"sessionId": "S1"}
        return {}


def test_deck_client_against_the_real_socket_server(short_dir):
    path = str(short_dir / "nav.sock")
    server = kl.NavServer(path, "http://localhost:9284", os.environ.get("USER") or "x")
    server.create()
    server.serve()
    try:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o660
        assert asyncio.run(kiosk_nav.ping(path)) is True
        # Not started yet.
        assert asyncio.run(kiosk_nav.navigate("/ui/add", path)) == (False, "not_ready")
        server.pipe = _FakePipe()
        assert asyncio.run(kiosk_nav.navigate("/ui/add", path)) == (True, "")
        assert server.pipe.sent[2] == ("Page.navigate", {"url": "http://localhost:9284/ui/add"}, "S1")
        assert asyncio.run(kiosk_nav.navigate("ui/add", path)) == (False, "bad_path")
        assert asyncio.run(kiosk_nav.navigate(kiosk_nav.to_nav_path("ui/add"), path)) == (True, "")
        assert _ask(path, None, raw=b"{nope\n") == {"ok": False, "error": "bad_request"}
        assert _ask(path, None, raw=b"x" * 5000) == {"ok": False, "error": "bad_request"}
        # At most 4096 bytes in all, the newline included.
        ping = b'{"op": "ping"'
        fits = ping + b" " * (4096 - len(ping) - 2) + b"}\n"
        assert len(fits) == 4096
        assert _ask(path, None, raw=fits) == {"ok": True, "version": 1}
        over = ping + b" " * (4097 - len(ping) - 2) + b"}\n"
        assert _ask(path, None, raw=over) == {"ok": False, "error": "bad_request"}
        assert _ask(path, {"op": "dance"}) == {"ok": False, "error": "bad_request"}
        server.pipe = _FakePipe(fail=True)
        assert asyncio.run(kiosk_nav.navigate("/ui/add", path)) == (False, "navigate_failed")
    finally:
        server.close()
    assert not os.path.exists(path)


def _launcher_os(**replace):
    """A copy of os for the launcher's eyes only, with some functions
    replaced; the test's own os stays the real one."""
    fake = types.ModuleType("os")
    fake.__dict__.update(os.__dict__)
    fake.__dict__.update(replace)
    return fake


def test_nav_socket_listens_before_its_path_appears(short_dir, monkeypatch):
    """The Stream Deck connects as soon as it finds nav.sock, so the path
    must never stand for a socket that would refuse it. After every os call
    create() makes, a client tries the path: it is either not there yet or
    answering, and never refusing while the mode and group are still being
    set. A socket an earlier run left behind does not count as there."""
    path = short_dir / "nav.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))  # an earlier run's socket, no longer listening
    stale.close()
    seen = []

    def client_view():
        if not path.exists():
            return "absent"
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(str(path))
        except ConnectionRefusedError:
            return "refused"
        finally:
            s.close()
        return "listening"

    def watched(name, fn):
        def call(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            finally:
                seen.append((name, client_view()))
        return call

    gid = os.getgid()
    calls = {name: watched(name, fn) for name, fn in os.__dict__.items()
             if isinstance(fn, types.BuiltinFunctionType)}
    # The nav group is one the kiosk user is in, so the group step runs too.
    calls["getgrouplist"] = watched("getgrouplist", lambda user, group: [group])
    monkeypatch.setattr(kl, "os", _launcher_os(**calls))
    monkeypatch.setattr(kl, "grp", types.SimpleNamespace(
        getgrnam=lambda name: types.SimpleNamespace(gr_gid=gid)))
    monkeypatch.setattr(kl, "pwd", types.SimpleNamespace(
        getpwnam=lambda name: types.SimpleNamespace(pw_gid=gid)))

    server = kl.NavServer(str(path), "http://localhost:9284", "kiosk")
    try:
        assert server.create() == gid
        names = [name for name, _ in seen]
        assert "chmod" in names and "chown" in names, names
        assert "refused" not in [view for _, view in seen], seen
        assert seen[-1][1] == "listening"
        st = os.stat(path)
        assert stat.S_ISSOCK(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o660
        assert st.st_gid == gid
        # Nothing but the socket itself is left in the directory.
        assert [p.name for p in short_dir.iterdir()] == ["nav.sock"]
        server.serve()
        assert _ask(path, {"op": "ping"}) == {"ok": True, "version": 1}
    finally:
        server.close()
    assert not path.exists()


def test_nav_socket_that_cannot_be_finished_leaves_nothing_behind(short_dir, monkeypatch):
    """A step after the bind that fails closes the socket and removes its
    file, so a failed start leaves neither a stray file nor a nav.sock that
    would refuse the deck. The launcher then starts the browser with the
    debugging port, as before."""
    def no_chmod(*args, **kwargs):
        raise PermissionError("chmod refused")

    monkeypatch.setattr(kl, "os", _launcher_os(chmod=no_chmod))
    server = kl.NavServer(str(short_dir / "nav.sock"), "http://localhost:9284", "kiosk")
    with pytest.raises(OSError):
        server.create()
    assert server.sock is None
    assert list(short_dir.iterdir()) == []


# --- the ticket step --------------------------------------------------------

class _ProbeServer:
    def __init__(self, status):
        outer = self
        self.paths = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.paths.append(self.path)
                self.send_response(status)
                if status in (301, 302, 303):
                    self.send_header("Location", "/setup")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def probe_server():
    servers = []

    def make(status):
        s = _ProbeServer(status)
        servers.append(s)
        return s
    yield make
    for s in servers:
        s.close()


def _step(run_dir, monkeypatch, url, ticket=TICKET):
    monkeypatch.setenv("KIOSK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("FA_PROBE_RETRY_SECONDS", "0.05")
    if ticket is not None:
        (run_dir / "ticket").write_text(ticket)
    args = ["/usr/bin/chromium", "--kiosk", url]
    return args, kl.ticket_step(list(args), hostname="kitchen-pi")


def test_ticket_step_on_204_writes_the_launch_page(short_dir, monkeypatch, probe_server):
    srv = probe_server(204)
    url = f"http://127.0.0.1:{srv.port}/ui/?kiosk=1"
    args, (out, page) = _step(short_dir, monkeypatch, url)
    assert srv.paths == ["/ui/kiosk/launch?probe=1"]
    assert out[:-1] == args[:-1] and out[-1] == "file://" + page
    assert TICKET not in " ".join(out)
    assert not (short_dir / "ticket").exists()
    html = Path(page).read_text()
    assert TICKET in html and f"http://127.0.0.1:{srv.port}/ui/kiosk/launch?ticket=" in html
    assert stat.S_IMODE(os.stat(page).st_mode) == 0o600


@pytest.mark.parametrize("status", [404, 302])
def test_older_or_unconfigured_app_keeps_the_plain_url(short_dir, monkeypatch, probe_server, status):
    srv = probe_server(status)
    url = f"http://127.0.0.1:{srv.port}/ui/?kiosk=1"
    args, (out, page) = _step(short_dir, monkeypatch, url)
    assert out == args and page == ""
    assert len(srv.paths) == 1  # redirects are not followed


def test_refused_probe_keeps_the_plain_url(short_dir, monkeypatch):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    args, (out, page) = _step(short_dir, monkeypatch, f"http://127.0.0.1:{port}/ui/?kiosk=1")
    assert out == args and page == ""


def test_missing_or_malformed_ticket_keeps_the_plain_url(short_dir, monkeypatch, probe_server):
    srv = probe_server(204)
    url = f"http://127.0.0.1:{srv.port}/ui/?kiosk=1"
    args, (out, page) = _step(short_dir, monkeypatch, url, ticket=None)
    assert out == args and page == ""
    args, (out, page) = _step(short_dir, monkeypatch, url, ticket="v1.123.short.abc")
    assert out == args and page == ""
    assert not (short_dir / "ticket").exists()


def test_non_local_kiosk_url_never_probes_and_drops_the_ticket(short_dir, monkeypatch):
    called = []
    monkeypatch.setattr(kl, "probe", lambda *a, **k: called.append(a) or True)
    for url in ("https://pantry.example.com/ui/?kiosk=1", "http://kitchen-pi.local:9284/ui/?kiosk=1"):
        args, (out, page) = _step(short_dir, monkeypatch, url)
        assert out == args and page == ""
        assert not (short_dir / "ticket").exists()
    assert called == []


def test_bare_host_name_kiosk_url_is_signed_in(short_dir, monkeypatch):
    monkeypatch.setattr(kl, "probe", lambda origin, **k: origin == "http://kitchen-pi:9284")
    args, (out, page) = _step(short_dir, monkeypatch, "http://kitchen-pi:9284/ui/?kiosk=1")
    assert page and out[-1] == "file://" + page


# --- an exception before the start ------------------------------------------

def test_exception_before_start_execs_the_original_args_with_the_port(monkeypatch):
    def boom(self):
        raise RuntimeError("no pipes today")
    calls = []
    resets = []
    monkeypatch.setattr(kl.Launcher, "start", boom)
    monkeypatch.setattr(kl.os, "execvp", lambda f, a: calls.append((f, a)))
    # Never touch the test process's own signal handling.
    monkeypatch.setattr(kl.signal, "signal", lambda sig, handler: resets.append((sig, handler)))
    argv = ["/usr/bin/chromium", "--kiosk", "http://localhost:9284/ui/?kiosk=1"]
    kl.main(list(argv))
    assert calls == [("/usr/bin/chromium",
                      ["/usr/bin/chromium", "--kiosk", LEGACY, "http://localhost:9284/ui/?kiosk=1"])]
    # The browser does not inherit Python's ignored SIGPIPE.
    assert (signal.SIGPIPE, signal.SIG_DFL) in resets


# --- with a stub browser ----------------------------------------------------

def test_broker_navigates_over_the_pipe_and_forwards_sigterm(rig):
    url = "https://pantry.example.com/ui/?kiosk=1"
    p = rig.start("--kiosk", url, STUB_TERM_CODE="7", FA_PIPE_CHECK_SECONDS="3")
    assert rig.wait_for(rig.sock.exists), rig.log.read_text()
    assert stat.S_IMODE(os.stat(rig.sock).st_mode) == 0o660
    assert rig.wait_for(lambda: rig.argvs())
    argv = rig.argvs()[0]
    assert argv == ["--kiosk", PIPE, url]  # no legacy port, URL last
    assert _ask(rig.sock, {"op": "ping"}) == {"ok": True, "version": 1}
    assert _ask(rig.sock, {"op": "navigate", "path": "/ui/add"}) == {"ok": True}
    methods = [(m["method"], m.get("params"), m.get("sessionId")) for m in rig.msgs()]
    # The self-check asked for targets first; the navigation follows.
    nav = methods[methods.index(("Target.attachToTarget", {"targetId": "P1", "flatten": True}, None)) - 1:]
    assert nav[0][0] == "Target.getTargets"
    assert nav[1] == ("Target.attachToTarget", {"targetId": "P1", "flatten": True}, None)
    assert nav[2] == ("Page.navigate", {"url": "https://pantry.example.com/ui/add"}, "S1")
    assert nav[3] == ("Target.detachFromTarget", {"sessionId": "S1"}, None)
    time.sleep(3.5)
    assert len(rig.argvs()) == 1  # a browser that answers is never restarted
    os.kill(p.pid, signal.SIGTERM)
    assert p.wait(timeout=15) == 7
    assert rig.signals() == [str(int(signal.SIGTERM))]
    assert not rig.sock.exists()


def test_ticket_reaches_the_launch_page_and_never_argv(rig, probe_server):
    srv = probe_server(204)
    (rig.run_dir / "ticket").write_text(TICKET)
    url = f"http://127.0.0.1:{srv.port}/ui/?kiosk=1"
    p = rig.start("--kiosk", url)
    assert rig.wait_for(lambda: rig.argvs()), rig.log.read_text()
    argv = rig.argvs()[0]
    page = rig.run_dir / "launch.html"
    assert argv[-1] == f"file://{page}"
    assert TICKET not in json.dumps(argv)
    assert TICKET in page.read_text()
    assert not (rig.run_dir / "ticket").exists()
    # The broker still steers the ORIGINAL kiosk origin, never the file page.
    assert _ask(rig.sock, {"op": "navigate", "path": "/ui/add"}) == {"ok": True}
    navs = [m for m in rig.msgs() if m["method"] == "Page.navigate"]
    assert navs[-1]["params"]["url"] == f"http://127.0.0.1:{srv.port}/ui/add"
    os.kill(p.pid, signal.SIGTERM)
    assert p.wait(timeout=15) == 0
    assert TICKET not in rig.log.read_text()


def test_old_deck_or_devtools_keeps_the_legacy_port(rig, tmp_path):
    deck = tmp_path / "deck"
    deck.mkdir()
    for name in OLD_DECK["files"]:
        (deck / name).write_text("")
    p = rig.start("--kiosk", "https://pantry.example.com/ui/?kiosk=1",
                  STUB_SYSTEMCTL_JSON=json.dumps({"LoadState": "loaded", "User": ""}))
    assert rig.wait_for(lambda: rig.argvs()), rig.log.read_text()
    assert rig.argvs()[0] == ["--kiosk", PIPE, LEGACY, "https://pantry.example.com/ui/?kiosk=1"]
    assert "older Stream Deck controller" in rig.log.read_text()
    os.kill(p.pid, signal.SIGTERM)
    p.wait(timeout=15)


def test_silent_browser_is_restarted_with_the_legacy_port(rig):
    url = "https://pantry.example.com/ui/?kiosk=1"
    p = rig.start("--kiosk", "--no-first-run", url, STUB_MODE="silent",
                  FA_PIPE_CHECK_SECONDS="2")
    assert rig.wait_for(rig.sock.exists)
    assert rig.wait_for(lambda: len(rig.argvs()) == 2, timeout=30), rig.log.read_text()
    first, second = rig.argvs()
    assert PIPE in first
    assert second == ["--kiosk", "--no-first-run", LEGACY, url]
    assert "did not answer on its debugging pipe" in rig.log.read_text()
    assert not rig.sock.exists()
    assert rig.signals() == [str(int(signal.SIGTERM))]  # the first stub was stopped
    # The launcher became the browser: its pid now runs the restarted stub.
    assert p.poll() is None
    os.kill(p.pid, signal.SIGTERM)
    p.wait(timeout=15)


def test_silent_browser_restart_drops_the_launch_page(rig, probe_server):
    srv = probe_server(204)
    (rig.run_dir / "ticket").write_text(TICKET)
    url = f"http://127.0.0.1:{srv.port}/ui/?kiosk=1"
    p = rig.start("--kiosk", url, STUB_MODE="silent", FA_PIPE_CHECK_SECONDS="2")
    assert rig.wait_for(lambda: len(rig.argvs()) == 2, timeout=30), rig.log.read_text()
    first, second = rig.argvs()
    assert first[-1].startswith("file://")
    # Restarted the old way: the plain kiosk URL, and the page with the
    # ticket in it is gone.
    assert second == ["--kiosk", LEGACY, url]
    assert not (rig.run_dir / "launch.html").exists()
    os.kill(p.pid, signal.SIGTERM)
    p.wait(timeout=15)


# --- foodassistant-kiosk-ticket ---------------------------------------------

TICKET_HELPER = ROOT / "scripts" / "image-build" / "foodassistant-kiosk-ticket"
DEVTOOLS = ROOT / "scripts" / "image-build" / "foodassistant-kiosk-devtools"


def _load_ticket():
    loader = importlib.machinery.SourceFileLoader("foodassistant_kiosk_ticket", str(TICKET_HELPER))
    spec = importlib.util.spec_from_loader("foodassistant_kiosk_ticket", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_ticket_mint_equals_the_vectors():
    kt = _load_ticket()
    for v in VECTORS["tickets"]:
        assert kt.mint(VECTORS["token"], v["t"], v["n"]) == v["ticket"]
        assert kt.mint(VECTORS["token"], str(v["t"]), v["n"]) == v["ticket"]


def test_ticket_helper_writes_0600_and_never_prints(tmp_path):
    install = tmp_path / "opt"
    (install / "data").mkdir(parents=True)
    (install / "data" / "bridge-token").write_text(VECTORS["token"] + "\n")
    run = tmp_path / "run"
    run.mkdir(mode=0o711)
    env = dict(os.environ, INSTALL_DIR=str(install), KIOSK_RUN_DIR=str(run))
    r = subprocess.run([sys.executable, str(TICKET_HELPER)], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    ticket = (run / "ticket").read_text()
    assert kl.valid_ticket(ticket)
    assert abs(int(ticket.split(".")[1]) - time.time()) < 60
    assert stat.S_IMODE((run / "ticket").stat().st_mode) == 0o600
    assert ticket not in r.stdout + r.stderr
    # No token: nothing written, a stale ticket goes, still exit 0.
    (install / "data" / "bridge-token").write_text("")
    r = subprocess.run([sys.executable, str(TICKET_HELPER)], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and not (run / "ticket").exists()
    # No run directory: exit 0.
    env["KIOSK_RUN_DIR"] = str(tmp_path / "missing")
    r = subprocess.run([sys.executable, str(TICKET_HELPER)], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0


def test_ticket_ownership_is_set_on_the_open_file(tmp_path, monkeypatch):
    """Run as root, the helper hands the ticket to the directory's owner. The
    directory belongs to the kiosk user, who could swap the file's name for a
    link, so the owner is set through the open file, never by path."""
    kt = _load_ticket()
    run = tmp_path / "run"
    run.mkdir()
    calls = []

    def _fchown(fd, uid, gid):
        calls.append((uid, gid))
        os.fchown(fd, uid, gid)

    def _chown(*a, **k):
        raise AssertionError("chown by path")

    # The helper's own view of os only: root, with the two calls recorded.
    fake_os = types.ModuleType("os")
    fake_os.__dict__.update(os.__dict__)
    fake_os.geteuid = lambda: 0
    fake_os.fchown = _fchown
    fake_os.chown = _chown
    monkeypatch.setattr(kt, "os", fake_os)
    kt.write_ticket(str(run), "v1.ticket")
    st = run.stat()
    assert calls == [(st.st_uid, st.st_gid)]
    assert (run / "ticket").read_text() == "v1.ticket"
    assert stat.S_IMODE((run / "ticket").stat().st_mode) == 0o600
    assert [p.name for p in run.iterdir()] == ["ticket"]


# --- foodassistant-kiosk-devtools -------------------------------------------

def test_devtools_switch(tmp_path):
    if not shutil.which("bash"):
        pytest.skip("needs bash")
    calls = tmp_path / "calls"
    stub = tmp_path / "systemctl"
    stub.write_text(f'#!/bin/sh\necho "$*" >> "{calls}"\n')
    stub.chmod(0o755)
    flag = tmp_path / "devtools"
    env = dict(os.environ, FA_DEVTOOLS_FLAG=str(flag), SYSTEMCTL=str(stub))

    def devtools(arg):
        return subprocess.run(["bash", str(DEVTOOLS), arg], env=env,
                              capture_output=True, text=True, timeout=30)

    r = devtools("on")
    assert r.returncode == 0 and flag.exists()
    r = devtools("off")
    assert r.returncode == 0 and not flag.exists()
    assert calls.read_text().splitlines() == ["restart foodassistant-kiosk.service"] * 2
    r = devtools("status")
    assert r.returncode == 0
    assert r.stdout.strip() in ("The browser debugging port 9222 is open.",
                                "The browser debugging port 9222 is closed.")
    assert devtools("bogus").returncode == 2
