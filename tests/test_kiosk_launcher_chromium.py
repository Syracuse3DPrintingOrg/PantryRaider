"""The kiosk launcher's debugging pipe broker against a real Chromium.

tests/test_kiosk_launcher_helper.py covers the launcher with a stub browser.
This one starts the real launcher with a real Chromium against a local test
page, waits for the screen's page to load, asks the launcher over nav.sock to
open /ui/add and checks that the browser fetched it.

Finding a browser: PR_TEST_CHROMIUM, else chromium, chromium-browser,
google-chrome or google-chrome-stable on PATH, else the newest Playwright
Chromium under ~/.cache/ms-playwright, else Playwright's chrome-headless-shell.
A host missing the browser's shared libraries can pass them in through
LD_LIBRARY_PATH (the environment goes through to the browser).

It skips without a usable browser, and on GitHub's runners unless
PR_CHROMIUM_TEST=1. With PR_CHROMIUM_REQUIRED=1 (the release gate) every skip
is a failure instead.
"""
from __future__ import annotations

import glob
import http.server
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "image-build" / "foodassistant-kiosk-launch"
REQUIRED = os.environ.get("PR_CHROMIUM_REQUIRED") == "1"

STUB_SYSTEMCTL = """#!{python}
import sys
args = sys.argv[1:]
if args[:1] == ["show"] and "LoadState" in args:
    print("not-found")
"""


def _skip(reason):
    if REQUIRED:
        pytest.fail(f"PR_CHROMIUM_REQUIRED=1 but the real-browser check cannot run: {reason}")
    pytest.skip(reason)


def find_browser():
    env = os.environ.get("PR_TEST_CHROMIUM")
    if env:
        return env
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        path = shutil.which(name)
        if path:
            return path
    cache = Path.home() / ".cache" / "ms-playwright"

    def newest(pattern):
        found = sorted(glob.glob(str(cache / pattern)),
                       key=lambda p: int("".join(c for c in Path(p).parts[-3] if c.isdigit()) or 0))
        return found[-1] if found else ""

    return (newest("chromium-*/chrome-linux*/chrome")
            or newest("chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"))


def _headless_flags(browser):
    return [] if "headless-shell" in os.path.basename(browser) else ["--headless=new"]


@pytest.fixture(scope="module")
def browser():
    if os.environ.get("GITHUB_ACTIONS") and os.environ.get("PR_CHROMIUM_TEST") != "1":
        _skip("the real-browser check does not run on GitHub's runners unless PR_CHROMIUM_TEST=1")
    if not hasattr(socket, "AF_UNIX") or not hasattr(os, "posix_spawnp"):
        _skip("needs Unix sockets and posix_spawn")
    path = find_browser()
    if not path or not os.path.exists(path):
        _skip("no Chromium found (set PR_TEST_CHROMIUM)")
    cmd = [path, *_headless_flags(path), "--no-sandbox", "--disable-gpu",
           "--disable-dev-shm-usage", "--dump-dom", "about:blank"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=dict(os.environ))
    except (OSError, subprocess.TimeoutExpired) as e:
        _skip(f"{path} did not start: {e}")
    if r.returncode != 0 or "<html" not in r.stdout:
        _skip(f"{path} did not start: {(r.stderr or '').strip()[-300:]}")
    return path


class _App:
    """A stand-in app: /ui/kiosk/launch answers 404 (an older app, so the
    launcher keeps the plain URL), everything else a small page."""

    def __init__(self):
        self.paths = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.paths.append(self.path)
                if self.path.startswith("/ui/kiosk/launch"):
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = b"<!doctype html><title>test</title><p>ok</p>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def saw(self, path, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if path in self.paths:
                return True
            time.sleep(0.1)
        return False

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _ask(sock_path, message):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(20)
    s.connect(str(sock_path))
    s.sendall(json.dumps(message).encode() + b"\n")
    data = b""
    while not data.endswith(b"\n"):
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
    s.close()
    return json.loads(data)


def _until(check, timeout, step=0.2):
    """Poll check() until it returns something truthy or timeout passes; the
    last value is returned. Generous limits keep a loaded machine (pytest -n
    auto alongside other work) from failing on time alone."""
    end = time.monotonic() + timeout
    while True:
        value = check()
        if value or time.monotonic() >= end:
            return value
        time.sleep(step)


def _ping(sock_path):
    # The launcher puts nav.sock in place only once it listens, so a refused
    # connection is a real failure. A slow answer on a busy machine is not.
    try:
        return _ask(sock_path, {"op": "ping"})
    except (FileNotFoundError, socket.timeout):
        return None


def _navigate(sock_path, path):
    # One CDP step has a few seconds; a starved browser can miss that, and
    # the launcher then answers ok false. Ask again rather than fail on time.
    try:
        reply = _ask(sock_path, {"op": "navigate", "path": path})
    except (ConnectionRefusedError, socket.timeout):
        return None
    return reply if reply == {"ok": True} else None


def test_real_chromium_navigates_over_the_pipe(browser, tmp_path):
    # nav.sock has to fit a Unix socket path (about 107 bytes), so a long
    # TMPDIR (a sandbox's scratch folder, say) would leave the launcher
    # without its socket and the test waiting it out. /tmp keeps it short.
    base = None if len(tempfile.gettempdir()) < 40 or not os.path.isdir("/tmp") else "/tmp"
    short = Path(tempfile.mkdtemp(prefix="klc-", dir=base))
    app = _App()
    proc = None
    try:
        run = short / "run"
        run.mkdir(mode=0o711)
        sysctl = tmp_path / "systemctl"
        sysctl.write_text(STUB_SYSTEMCTL.format(python=sys.executable))
        sysctl.chmod(0o755)
        env = dict(os.environ)
        env.update({
            "KIOSK_RUN_DIR": str(run),
            "DECK_PKG_DIR": str(tmp_path / "deck"),
            "FA_DEVTOOLS_FLAG": str(tmp_path / "devtools"),
            "SYSTEMCTL": str(sysctl),
        })
        log = tmp_path / "launcher.log"
        cmd = [sys.executable, str(LAUNCHER), browser, *_headless_flags(browser),
               "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--no-first-run",
               f"--user-data-dir={tmp_path / 'profile'}",
               f"http://127.0.0.1:{app.port}/ui/?kiosk=1"]
        with open(log, "wb") as errf:
            proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=errf,
                                    start_new_session=True)

        sock = run / "nav.sock"
        assert _until(sock.exists, 90), log.read_text(errors="replace")[-2000:]
        assert _until(lambda: _ping(sock), 60) == {"ok": True, "version": 1}, \
            log.read_text(errors="replace")[-2000:]
        assert app.saw("/ui/?kiosk=1", 90), (app.paths, log.read_text(errors="replace")[-2000:])

        reply = _until(lambda: _navigate(sock, "/ui/add"), 60, step=1.0)
        assert reply == {"ok": True}, log.read_text(errors="replace")[-2000:]
        assert app.saw("/ui/add", 60), app.paths
        assert "9222" not in log.read_text(errors="replace")

        os.kill(proc.pid, signal.SIGTERM)
        proc.wait(timeout=15)
    finally:
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait(timeout=10)
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)  # any browser helpers left over
            except OSError:
                pass
        app.close()
        shutil.rmtree(short, ignore_errors=True)
