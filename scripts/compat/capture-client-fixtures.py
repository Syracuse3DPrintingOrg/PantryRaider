#!/usr/bin/env python3
"""Record what every first-party client of v0.20.0 sends, and what v0.20.0 answers.

Build-time only: run it from the repository root when the fixtures need to be
made again, then commit what it writes. It is the only part of the
compatibility harness that runs git; the tests under tests/compat read the
committed files and nothing else.

    python3 scripts/compat/capture-client-fixtures.py --ref v0.20.0 \\
        --r1-ref e95cf9b --out tests/compat/fixtures [--integration-ref c9517f9]
    python3 scripts/compat/capture-client-fixtures.py --check-vendored

It exports each revision with git archive into a temporary directory and
writes three files:

clients-v0.20.0.json
    Every request a first-party client makes, sorted, each with its client,
    method, path, headers (secrets replaced by placeholders such as
    <legacy-key>), body, the Host it sends and the source line it comes from.
    The satellite rows are recorded by running the release's own app as a
    satellite and catching each request it makes to its main server; the
    satellite-0.20.2 rows do the same with the 0.20.2 tree (--r1-ref), which
    public main runs untagged. The Home Assistant integration's rows come from
    driving its api.py at the release and at the first integration
    (--integration-ref). The Stream Deck, sensor reader, Bandit Cub, YAML,
    kitchen screen and curl rows are read from the release's sources.

baseline-v0.20.0.json
    Each request replayed against the release's real app (the 0.20.2 app for
    the satellite-0.20.2 rows) in a subprocess, for every password state,
    peer shape and credential, recording the status and a verdict:
    auth_refused for a 401, a redirect to /ui/login, or a 403 carrying one of
    the release's sign-in messages, passed for anything else.

responses-v0.20.0.json
    What the release answers to the requests newer clients make: a signed
    config pull for a known key and nonce, /ha/state, the 404s for the key
    exchange and the kiosk launch probe, an approved pairing status that still
    carries its key, the sensor reader's /gadgets/config, and the Stream Deck
    config.toml the release's host bridge writes for a deck with Home
    Assistant keys.

--check-vendored exits 1 when the v0.20.0 key rule copied into
tests/compat/test_rollback.py no longer matches the release's
Settings.valid_api_keys and main._api_key_ok.

The subprocess that runs an old app has no network (sockets refuse, DNS
fails, every HTTP client call is answered locally) and cannot start
programs, so no request can reach a real service or change this machine.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = Path(__file__).resolve()

KEY = "<legacy-key>"
NONCE = "<nonce>"
PASSWORD_SLOT = "<password>"

PRIMARY_KEY = "fake-key-primary-aaaaaa"
EXTRA_KEY = "fake-extra-key-aaaaaaaa"
PASSWORD = "fake-password-aaaaaaaa"
CONFIG_NONCE = "0123456789abcdef0123456789abcdef"
AI_KEY = "fake-gemini-key-aaaaaaa"
HA_TOKEN = "fake-ha-token-aaaaaaaa"
GROCY_KEY = "fake-grocy-key-aaaaaaaa"
PAIRED_KEY = "fake-paired-key-aaaaaaa"
DEVICE_ID = "compat-sat-0001"
OWN_NAME = "kitchen-pi"
DEVICE_IP = "192.168.1.71"

SERVER_HOST = "server.example:9284"
LAN_HOST = "192.168.1.50:9284"
CUB_HOST = "192.168.1.60:9284"
KIOSK_HOST = "localhost:9284"
HA_BASE = "http://192.168.1.40:8123"

UA_HTTPX = "python-httpx/0.28.1"
UA_HA = "HomeAssistant/2026.9.1 aiohttp/3.12.15 Python/3.13"
UA_ESPHOME = "ESPHome/2026.6.5 (https://esphome.io)"
UA_KIOSK = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36")
UA_CURL = "curl/8.5.0"

# Home Assistant's real Ingress headers (the same shape tests/conftest.py uses).
INGRESS_HEADERS = {
    "X-Ingress-Path": "/api/hassio_ingress/testtoken",
    "X-Hass-Source": "core.ingress",
    "X-Forwarded-For": "192.168.1.20, 172.30.32.1",
    "X-Forwarded-Host": "homeassistant.local:8123",
    "X-Forwarded-Proto": "http",
}

LOOPBACK = ["127.0.0.1", 50000]
LAN = ["192.168.1.50", 50000]

# The peer shapes every request is replayed from. host overrides the Host the
# client sends; headers are added last; http_version 1.0 goes through a small
# ASGI shim because the test client always speaks HTTP/1.1.
PEERS = [
    {"name": "loopback", "client": LOOPBACK},
    {"name": "lan", "client": LAN},
    {"name": "public", "client": ["203.0.113.9", 50000]},
    {"name": "ingress", "client": ["172.30.32.2", 50000], "headers": INGRESS_HEADERS,
     "addon": True},
    {"name": "lan_forwarded", "client": LAN, "headers": {"X-Forwarded-For": "203.0.113.9"}},
    {"name": "loopback_proxy", "client": LOOPBACK, "host": "pantry.example.com",
     "headers": {"X-Forwarded-For": "203.0.113.9"}},
    {"name": "loopback_own_name", "client": LOOPBACK, "host": f"{OWN_NAME}:9284"},
    {"name": "loopback_referer", "client": LOOPBACK, "host": "127.0.0.1:9284",
     "headers": {"Referer": "https://pantry.example.com/ui/"}},
    {"name": "loopback_http10", "client": LOOPBACK, "host": "127.0.0.1:9284",
     "http_version": "1.0"},
]
PASSWORDS = ["none", "set"]
CREDENTIALS = ["none", "legacy_primary", "legacy_extra"]
CREDENTIAL_VALUES = {"legacy_primary": PRIMARY_KEY, "legacy_extra": EXTRA_KEY}

ADMIN_PASSWORD_MSG = "This needs the admin password."
NO_PASSWORD_GATE_MSG = (
    "This device has no password yet, so it only allows this from its own "
    "screen. Add a password in Settings to use it from anywhere.")
RULE = {
    "auth_refused_statuses": [401],
    "login_redirect_path": "/ui/login",
    "auth_refusal_details": [ADMIN_PASSWORD_MSG, NO_PASSWORD_GATE_MSG],
    "text": ("auth_refused for a 401, a redirect to /ui/login, or a 403 whose detail "
             "is one of auth_refusal_details; passed for anything else"),
}

CLIENT_ORDER = ["satellite", "satellite-0.20.2", "integration", "streamdeck", "reader",
                "cub018", "yaml", "kiosk", "curl"]


def _git(*args: str, text: bool = True):
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True,
                          capture_output=True, text=text).stdout


def _export(ref: str, dest: Path) -> Path:
    """git archive <ref> | tar -x for the parts the capture reads."""
    dest.mkdir(parents=True, exist_ok=True)
    paths = ["service", "streamdeck", "gadgets", "homeassistant", "esphome",
             "scripts/image-build/foodassistant-host-bridge"]
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", ref, *paths],
                             check=True, capture_output=True).stdout
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive, check=True)
    return dest


# --- Request building and the verdict (shared with tests/compat/conftest.py) ---


def verdict(status: int, location: str, detail) -> str:
    if status in RULE["auth_refused_statuses"]:
        return "auth_refused"
    if 300 <= status < 400 and urlsplit(location or "").path.endswith(RULE["login_redirect_path"]):
        return "auth_refused"
    if status == 403 and detail in RULE["auth_refusal_details"]:
        return "auth_refused"
    return "passed"


def _subst(value, mapping: dict):
    if isinstance(value, str):
        for k, v in mapping.items():
            value = value.replace(k, v)
        return value
    if isinstance(value, dict):
        return {k: _subst(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [_subst(v, mapping) for v in value]
    return value


def build_request(fx: dict, peer: dict, credential: str) -> dict:
    """The concrete request one baseline cell sends."""
    mapping = {NONCE: CONFIG_NONCE, PASSWORD_SLOT: PASSWORD}
    headers = {k: _subst(v, mapping) for k, v in fx["headers"].items()
               if k.lower() != "x-api-key"}
    if credential != "none":
        headers["X-API-Key"] = CREDENTIAL_VALUES[credential]
    for k, v in (peer.get("headers") or {}).items():
        for existing in [h for h in headers if h.lower() == k.lower()]:
            del headers[existing]
        headers[k] = v
    host = peer.get("host") or fx["host"]
    return {"method": fx["method"], "url": f"http://{host}{fx['path']}",
            "headers": headers, "body": _subst(fx.get("body"), mapping)}


# --- Static client rows -------------------------------------------------------


def _line_of(tree: Path, rel: str, needle: str, ref: str) -> str:
    lines = (tree / rel).read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines, 1):
        if needle in line:
            return f"{ref}:{rel}:{i}"
    raise SystemExit(f"{ref}:{rel}: no line contains {needle!r}")


def _row(client, method, path, headers, body, host, source) -> dict:
    return {"client": client, "method": method, "path": path, "headers": headers,
            "body": body, "host": host, "source": source}


DECK_FILE = "streamdeck/foodassistant_streamdeck/actions.py"
DECK_CONTROLLER = "streamdeck/foodassistant_streamdeck/controller.py"
# Every request the Stream Deck makes to the app, listed by hand from
# actions.py and controller.py (each needle must still be in the file).
DECK = [
    ("POST", "/ui/login", {"form": {"password": PASSWORD_SLOT}}, DECK_FILE,
     'f"{base}/ui/login"'),
    ("GET", "/ui/weather/data", None, DECK_FILE, 'url = f"{base}/ui/weather/data"'),
    ("GET", "/expiring/summary", None, DECK_FILE, 'f"{base}/expiring/summary"'),
    ("GET", "/pending/count", None, DECK_FILE, 'f"{base}/pending/count"'),
    ("GET", "/mealie/shopping/count", None, DECK_FILE, 'f"{base}/mealie/shopping/count"'),
    ("GET", "/mealie/suggest/ready-count", None, DECK_FILE,
     'f"{base}/mealie/suggest/ready-count"'),
    ("GET", "/current-recipe/timer-suggestions", None, DECK_FILE,
     'f"{base}/current-recipe/timer-suggestions"'),
    ("GET", "/timers", None, DECK_FILE, 'client.get(f"{base}/timers")'),
    ("GET", "/mealie/mealplan", None, DECK_FILE, 'f"{base}/mealie/mealplan"'),
    ("GET", "/current-recipe", None, DECK_FILE, 'client.get(f"{base}/current-recipe")'),
    ("POST", "/mealie/cooked", {"json": {"slug": "pasta"}}, DECK_FILE,
     'f"{base}/mealie/cooked"'),
    ("GET", "/mealie/shopping", None, DECK_FILE, 'client.get(f"{base}/mealie/shopping")'),
    ("POST", "/mealie/shopping/items", {"json": {"list_id": "1", "note": "Milk",
                                                 "quantity": 1.0}},
     DECK_FILE, 'f"{base}/mealie/shopping/items",'),
    ("POST", "/events/confirm", {"json": {"message": "Timer started"}}, DECK_FILE,
     'f"{base}/events/confirm"'),
    ("PUT", "/mealie/shopping/items/1", {"json": {"id": "1", "note": "Milk",
                                                  "checked": True}},
     DECK_FILE, 'f"{base}/mealie/shopping/items/{item_id}"'),
    ("POST", "/timers", {"json": {"label": "Pasta", "seconds": 600}}, DECK_FILE,
     'client.post(f"{base}/timers"'),
    ("POST", "/current-recipe/timers/start", {"json": {"step_index": 0, "label": "Boil",
                                                       "seconds": 600}},
     DECK_FILE, 'f"{base}/current-recipe/timers/start"'),
    ("DELETE", "/timers/1", None, DECK_FILE, 'f"{base}/timers/{tid}"'),
    ("POST", "/timers/1/extend", {"json": {"seconds": 60}}, DECK_FILE,
     'f"{base}/timers/{tid}/extend"'),
    ("POST", "/current-recipe/scale", {"json": {"factor": 2}}, DECK_FILE,
     'f"{base}/current-recipe/scale"'),
    ("POST", "/pending/commit", {"json": {}}, DECK_FILE, 'f"{base}/pending/commit"'),
    ("POST", "/pending/scanner-mode/cycle", None, DECK_FILE,
     'f"{base}/pending/scanner-mode/cycle"'),
    ("GET", "/pending/scanner-mode", None, DECK_CONTROLLER,
     '/pending/scanner-mode", timeout=4.0'),
]

READER_DAEMON = "gadgets/foodassistant_gadgets/daemon.py"
READER_MODULE = "gadgets/foodassistant_gadgets/i2c/module.py"
READER = [
    ("GET", "/gadgets/config", None, READER_DAEMON, 'f"{self.cfg.base_url}/gadgets/config"'),
    ("GET", "/cub/summary", None, READER_DAEMON, 'f"{self.cfg.base_url}/cub/summary"'),
    ("POST", "/gadgets/readings", {"json": {"devices": [], "discovered": []}},
     READER_DAEMON, 'f"{self.cfg.base_url}/gadgets/readings"'),
    ("GET", "/gadgets/outputs", None, READER_DAEMON, 'f"{self.cfg.base_url}/gadgets/outputs"'),
    ("POST", "/pending/scan", {"json": {"barcode": "0012345678905", "mode": "inventory"}},
     READER_DAEMON, 'f"{self.cfg.base_url}/pending/scan"'),
    ("POST", "/pending/scanner-mode", {"json": {"mode": "consume", "source": "neokey"}},
     READER_MODULE, 'return "/pending/scanner-mode"'),
    ("POST", "/gadgets/key-action", {"json": {"action": "timers", "source": "neokey"}},
     READER_MODULE, 'return "/gadgets/key-action"'),
]

CUB_CPP = "esphome/components/pantry_raider/pantry_raider.cpp"
CUB_ROUTER = "service/app/routers/cub.py"


def deck_rows(tree: Path, ref: str) -> list:
    rows = []
    for method, path, body, rel, needle in DECK:
        source = _line_of(tree, rel, needle, ref)
        for host in ("127.0.0.1:9284", KIOSK_HOST):
            headers = {"User-Agent": UA_HTTPX, "X-API-Key": KEY}
            if path == "/ui/login":
                headers = {"User-Agent": UA_HTTPX}
            rows.append(_row("streamdeck", method, path, headers, body, host, source))
    return rows


def reader_rows(tree: Path, ref: str) -> list:
    return [_row("reader", m, p, {"User-Agent": UA_HTTPX, "X-API-Key": KEY}, b,
                 "127.0.0.1:9284", _line_of(tree, rel, needle, ref))
            for m, p, b, rel, needle in READER]


def cub_rows(tree: Path, ref: str) -> list:
    base = {"User-Agent": UA_ESPHOME}
    return [
        _row("cub018", "GET", "/cub/summary", dict(base, **{"X-Cub-Id": "cub-a1b2c3"}), None,
             CUB_HOST, _line_of(tree, CUB_CPP, 'fetch_("GET", "/cub/summary"', ref)),
        _row("cub018", "GET", "/cub/firmware/manifest.json?profile=tdisplay", dict(base), None,
             CUB_HOST, _line_of(tree, CUB_CPP, "/cub/firmware/manifest.json?profile=", ref)),
        _row("cub018", "GET", "/cub/firmware/tdisplay.ota.bin", dict(base), None, CUB_HOST,
             _line_of(tree, CUB_ROUTER, '@router.get("/firmware/{profile}.ota.bin")', ref)),
    ]


_YAML_URL = re.compile(r"^(\s*(?:-\s*)?)(resource|url)\s*:\s*(https?://[^\s'\"]+)")
_YAML_METHOD = re.compile(r"^\s*method\s*:\s*([A-Za-z]+)")
_YAML_PAYLOAD = re.compile(r"^\s*payload\s*:\s*'(\{.*\})'\s*$")
# Sample values for the Home Assistant templates a payload carries, by the
# template's first name; anything else becomes 1 (a number) or 'compat'.
_YAML_SAMPLES = {"barcode": "0012345678905", "seconds": "600", "label": "Pasta",
                 "message": "Dinner is ready", "title": "Home Assistant"}


def _yaml_block(lines: list, i: int, key_col: int) -> list:
    """The sibling keys of the url or resource on line i: the lines of its
    mapping at the key's own indentation, up to the next line indented less
    or the next url."""
    block = []
    for follow in lines[i + 1:]:
        stripped = follow.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(follow) - len(follow.lstrip())
        if indent < key_col or _YAML_URL.match(follow):
            break
        if indent == key_col:
            block.append(follow)
    return block


def _yaml_payload(raw: str):
    """A rest_command payload as Home Assistant would send it: the YAML
    single-quote escape undone and each template filled with a sample."""
    text = raw.replace("''", "'")

    def fill(m):
        name = (re.findall(r"[A-Za-z_]+", m.group(1)) or [""])[0]
        inside = text[:m.start()].count('"') % 2 == 1
        return _YAML_SAMPLES.get(name, "compat" if inside else "1")
    try:
        return {"json": json.loads(re.sub(r"\{\{(.*?)\}\}", fill, text))}
    except ValueError:
        raise SystemExit(f"a payload is not JSON once its templates are filled: {raw}")


def yaml_rows(tree: Path, ref: str) -> list:
    rows = []
    for rel in ("homeassistant/configuration.yaml", "homeassistant/espcontrol.yaml",
                "homeassistant/barcode-scanner.md"):
        lines = (tree / rel).read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            m = _YAML_URL.match(line)
            if not m:
                continue
            parts = urlsplit(m.group(3).replace("YOUR_HOST", "192.168.1.50"))
            method, body = "GET", None
            for follow in _yaml_block(lines, i, len(m.group(1))):
                mm = _YAML_METHOD.match(follow)
                if mm:
                    method = mm.group(1).upper()
                pm = _YAML_PAYLOAD.match(follow)
                if pm:
                    body = _yaml_payload(pm.group(1))
            if method in ("POST", "PUT") and body is None:
                body = {"json": {}}
            path = parts.path + (f"?{parts.query}" if parts.query else "")
            for keyed in (False, True):
                headers = {"User-Agent": UA_HA}
                if keyed:
                    headers["X-API-Key"] = KEY
                rows.append(_row("yaml", method, path, headers, body,
                                 parts.netloc or LAN_HOST, f"{ref}:{rel}:{i + 1}"))
    return rows


_FETCH = re.compile(r"""fetch\(\s*(['"`])([^'"`]*)\1""")


def _js_url(line: str) -> str:
    """The URL a fetch(...) builds: string pieces kept, every expression
    between them (an id, encodeURIComponent(x), ${...}) becomes '1'."""
    s = line[line.index("fetch(") + len("fetch("):]
    out, i, depth = [], 0, 0
    while i < len(s):
        c = s[i]
        if c in "'\"`":
            j = s.index(c, i + 1)
            out.append(re.sub(r"\$\{[^}]*\}", "1", s[i + 1:j]))
            i = j + 1
            continue
        if c in ",)" and depth == 0:
            break
        if c in " +\t":
            i += 1
            continue
        # An expression: consume it up to the next + , or ) at depth 0.
        depth = 0
        while i < len(s):
            c = s[i]
            if c == "(":
                depth += 1
            elif c == ")":
                if depth == 0:
                    break
                depth -= 1
            elif c in "+," and depth == 0:
                break
            i += 1
        out.append("1")
    return "".join(out)


_METHOD = re.compile(r"""method\s*:\s*['"]([A-Z]+)['"]""")
# The four fetches whose URL is a variable, resolved from the line that sets it.
_DYNAMIC_FETCHES = {
    ("update-notice.js", "fetch(url"): [("GET", "admin/update-notice")],
    ("timers-page.js", "fetch(path"): [("POST", "gadgets/devices"), ("POST", "gadgets/target")],
    ("manage-pantry.js", "fetch(endpoint"): [("POST", "analyze/receipt"),
                                             ("POST", "analyze/food")],
    ("kiosk-status.js", "fetch(buildUrl()"): [("GET", "kiosk/status")],
}


def _norm_js_path(raw: str) -> str:
    path = raw.split("?")[0]
    while path.startswith("../"):
        path = path[3:]
    if path.startswith("./"):
        path = path[2:]
    return "/" + path.lstrip("/")


def kiosk_rows(tree: Path, ref: str) -> list:
    nav = {"User-Agent": UA_KIOSK, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
           "Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
    rows = [_row("kiosk", "GET", page, dict(nav), None, KIOSK_HOST, src) for page, src in (
        ("/ui/", "page:/ui/?kiosk=1 (the kiosk URL)"),
        ("/ui/add", "page:/ui/add"),
        ("/ui/timers", "page:/ui/timers"),
        ("/setup", "page:/setup"),
    )]
    js = tree / "service" / "app" / "static" / "js"
    seen = set()
    for f in sorted(js.rglob("*.js")):
        rel = f.relative_to(tree).as_posix()
        if "/static/js/setup/" in rel or "/static/js/vendor/" in rel:
            continue
        lines = f.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if "fetch(" not in line:
                continue
            calls = []
            m = _FETCH.search(line)
            if m:
                # The options of this call only: up to the next fetch( if one
                # follows within the window.
                window = " ".join(lines[i:i + 6])
                window = window[window.find("fetch(") + len("fetch("):]
                if "fetch(" in window:
                    window = window[:window.find("fetch(")]
                mm = _METHOD.search(window)
                calls = [(mm.group(1) if mm else "GET", _js_url(line))]
            else:
                for (name, needle), resolved in _DYNAMIC_FETCHES.items():
                    if f.name == name and needle in line:
                        calls = resolved
                if not calls:
                    if re.search(r"\bfetch\(", line):
                        raise SystemExit(f"{rel}:{i + 1}: unresolved fetch: {line.strip()}")
                    continue
            for method, raw in calls:
                path = _norm_js_path(raw)
                key = (method, path)
                if key in seen:
                    continue
                seen.add(key)
                headers = {"User-Agent": UA_KIOSK, "Accept": "*/*",
                           "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors",
                           "Sec-Fetch-Dest": "empty"}
                body = None
                if method not in ("GET", "HEAD", "DELETE"):
                    headers["Origin"] = f"http://{KIOSK_HOST}"
                    body = {"json": {}}
                rows.append(_row("kiosk", method, path, headers, body, KIOSK_HOST,
                                 f"{ref}:{rel}:{i + 1}"))
    return rows


def curl_rows(ref: str) -> list:
    return [_row("curl", "GET", "/pending/count", {"User-Agent": UA_CURL}, None,
                 "127.0.0.1:9284", "manual:a shell script or cron job polling the review count")]


# --- The Home Assistant integration ---------------------------------------------


API_PATH = "homeassistant/custom_components/pantry_raider/api.py"
INTEGRATION_BASE = f"http://{LAN_HOST}"


def integration_rows(refs: list) -> list:
    import asyncio
    import inspect
    import types

    class _Response:
        def __init__(self, url):
            self.status = 200
            self._url = url

        async def json(self):
            if "/api/pairing/request" in self._url:
                return {"ok": True, "request_id": "req-1", "code": "1234"}
            if "/api/pairing/status/" in self._url:
                return {"status": "pending"}
            return {"ok": True}

    rows: list = []

    class _Session:
        def __init__(self, label, filename):
            self.label = label
            self.filename = filename

        def _record(self, method, url, headers=None, json=None, **_kw):
            line = 0
            for frame in inspect.stack():
                if frame.filename == self.filename:
                    line = frame.lineno
                    break
            hdrs = {"User-Agent": UA_HA}
            for k, v in (headers or {}).items():
                hdrs[k] = KEY if k.lower() == "x-api-key" else v
            path = url[len(INTEGRATION_BASE):] if url.startswith(INTEGRATION_BASE) else url
            rows.append(_row("integration", method, path, hdrs,
                             {"json": json} if json is not None else None, LAN_HOST,
                             f"{self.label}:{API_PATH}:{line}"))
            return _Response(url)

        async def get(self, url, headers=None, **kw):
            return self._record("GET", url, headers=headers, **kw)

        async def post(self, url, headers=None, json=None, **kw):
            return self._record("POST", url, headers=headers, json=json, **kw)

    class _Timeout:
        def __init__(self, *_a, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    saved = {n: sys.modules.get(n) for n in ("aiohttp", "async_timeout")}
    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientError = type("ClientError", (Exception,), {})
    aiohttp.ClientSession = object
    async_timeout = types.ModuleType("async_timeout")
    async_timeout.timeout = _Timeout
    sys.modules["aiohttp"] = aiohttp
    sys.modules["async_timeout"] = async_timeout
    try:
        for ref in refs:
            source = _git("show", f"{ref}:{API_PATH}")
            filename = f"<{ref}:{API_PATH}>"
            module = types.ModuleType(f"pr_compat_api_{re.sub(r'[^a-z0-9]', '_', ref)}")
            module.__file__ = filename
            if "from .helpers import" in source:
                helpers_src = _git("show", f"{ref}:homeassistant/custom_components/"
                                           "pantry_raider/helpers.py")
                helpers = types.ModuleType(module.__name__ + "_helpers")
                exec(compile(helpers_src, f"<{ref}:helpers.py>", "exec"), helpers.__dict__)
                source = source.replace("from .helpers import", "from __pr_helpers__ import")
                sys.modules["__pr_helpers__"] = helpers
            exec(compile(source, filename, "exec"), module.__dict__)
            sys.modules.pop("__pr_helpers__", None)

            calls = (
                ("async_get_state", ()),
                ("async_post_settings", ({"display_idle_timeout": 10},)),
                ("async_display_action", ("wake",)),
                ("async_notify", ("Dinner is ready", "Kitchen", "info", 0)),
                ("async_camera_popup", ("Front Door", 20)),
                ("async_camera_detect", ("Front Door", "person", 0)),
                ("async_connect", (HA_BASE, "<ha-token>")),
                ("async_request_pairing", ()),
                ("async_pairing_status", ("req-1",)),
            )
            # Every request method the client has must be driven, so a call
            # an older revision adds is never left out of the fixtures.
            driven = {c[0] for c in calls}
            undriven = sorted(
                n for n, v in vars(module.PantryRaiderClient).items()
                if n.startswith("async_") and inspect.iscoroutinefunction(v)
                and n not in driven)
            if undriven:
                raise SystemExit(f"{ref}:{API_PATH}: no drive step for {', '.join(undriven)}")

            async def drive():
                for key in (None, KEY):
                    client = module.PantryRaiderClient(_Session(ref, filename),
                                                       INTEGRATION_BASE, key)
                    for name, args in calls:
                        method = getattr(client, name, None)
                        if method is None:
                            continue
                        params = inspect.signature(method).parameters
                        await method(*args[:len(params)])
            asyncio.run(drive())
    finally:
        for n, mod in saved.items():
            if mod is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = mod
    return rows


# --- Running an old app ------------------------------------------------------------


def _driver_env(data_dir: Path) -> dict:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
           "HOME": os.environ.get("HOME", "/tmp"), "DATA_DIR": str(data_dir),
           "TZ": "UTC", "PYTHONDONTWRITEBYTECODE": "1"}
    for name in ("PYTHONUSERBASE", "VIRTUAL_ENV"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


def _run_driver(tree: Path, mode: str, work: Path, payload: dict) -> dict:
    data_dir = work / f"data-{mode}-{tree.name}"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True)
    inp = work / f"in-{mode}-{tree.name}.json"
    out = work / f"out-{mode}-{tree.name}.json"
    inp.write_text(json.dumps(payload))
    subprocess.run([sys.executable, str(SCRIPT), "--driver", mode, "--tree", str(tree),
                    "--in", str(inp), "--out-file", str(out)],
                   cwd=str(tree / "service"), env=_driver_env(data_dir), check=True,
                   timeout=3600)
    return json.loads(out.read_text())


def _sandbox() -> None:
    """No network and no programs for the old app."""
    import asyncio
    import socket

    def refuse(*_a, **_kw):
        raise ConnectionRefusedError(111, "compat capture: no network")

    def no_dns(*_a, **_kw):
        raise socket.gaierror(-2, "compat capture: no network")

    def no_programs(*_a, **_kw):
        raise OSError("compat capture: no programs")

    async def no_programs_async(*_a, **_kw):
        raise OSError("compat capture: no programs")

    # The documentation ranges stand in for internet addresses in every
    # fixture, and Python 3.12 calls them private (ipaddress.is_private), which
    # an old app would read as the home network. Make them what they stand for.
    import ipaddress
    docs = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24",
                                              "203.0.113.0/24")]
    private, public = ipaddress.IPv4Address.is_private, ipaddress.IPv4Address.is_global
    ipaddress.IPv4Address.is_private = property(
        lambda self: False if any(self in n for n in docs) else private.fget(self))
    ipaddress.IPv4Address.is_global = property(
        lambda self: True if any(self in n for n in docs) else public.fget(self))

    socket.socket.connect = refuse
    socket.socket.connect_ex = lambda *_a, **_kw: 111
    socket.getaddrinfo = no_dns
    socket.create_connection = refuse
    socket.gethostname = lambda: OWN_NAME
    subprocess.Popen = no_programs
    subprocess.run = no_programs
    subprocess.call = no_programs
    subprocess.check_call = no_programs
    subprocess.check_output = no_programs
    os.system = lambda *_a, **_kw: 1
    asyncio.create_subprocess_exec = no_programs_async
    asyncio.create_subprocess_shell = no_programs_async


def _httpx_answer(handler) -> None:
    """Every httpx request the app makes is handed to handler(request)."""
    import httpx

    def sync(self, request):
        return handler(request)

    async def asyn(self, request):
        return handler(request)

    httpx.HTTPTransport.handle_request = sync
    httpx.AsyncHTTPTransport.handle_async_request = asyn


def _set(settings, **values) -> None:
    for k, v in values.items():
        try:
            object.__setattr__(settings, k, v)
        except Exception:
            pass


def _server_settings(settings, password: str, hash_secret) -> None:
    _set(settings, deployment_mode="server", grocy_base_url="http://127.0.0.1:9",
         grocy_api_key=GROCY_KEY, mealie_base_url="", mealie_api_key="",
         api_key=PRIMARY_KEY, extra_api_keys=[EXTRA_KEY], extra_api_key_names=["Extra"],
         demo_mode=False, kiosk_pin="", device_hostname=OWN_NAME, totp_secret="",
         local_totp_enabled=False, remote_server_url="", upstream_api_key="",
         device_id="compat-server-0001", auth_required=password == "set",
         auth_password=hash_secret(PASSWORD) if password == "set" else "",
         viewer_password="")


def _reset_limits() -> None:
    try:
        from app.services import rate_limit
        for guard in vars(rate_limit).values():
            events = getattr(guard, "_events", None)
            if hasattr(events, "clear"):
                events.clear()
    except Exception:
        pass


def _reset_pairing() -> None:
    """Empty the pairing queue, so no cell meets the three-request ceiling an
    earlier cell filled (it answers 429 and hides whether pairing was allowed)."""
    try:
        from app.config import settings
        from app.services import pairing
        (Path(settings.data_dir) / "pairing.json").unlink(missing_ok=True)
        pairing._requests = {}
        pairing._mtime = None
    except Exception:
        pass


def _detail(resp):
    try:
        body = resp.json()
    except Exception:
        return None
    return body.get("detail") if isinstance(body, dict) else None


def _http10(app):
    async def shim(scope, receive, send):
        if scope.get("type") == "http":
            scope = dict(scope, http_version="1.0")
        await app(scope, receive, send)
    return shim


def _send(client, req: dict):
    kwargs = {"headers": req["headers"], "follow_redirects": False}
    body = req.get("body") or {}
    if "json" in body:
        kwargs["json"] = body["json"]
    elif "form" in body:
        kwargs["data"] = body["form"]
    return client.request(req["method"], req["url"], **kwargs)


def driver_baseline(payload: dict) -> dict:
    import httpx
    from starlette.testclient import TestClient

    def refuse(request):
        raise httpx.ConnectError("compat capture: no network", request=request)
    _httpx_answer(refuse)
    from app.main import app
    from app.config import settings
    from app.passwords import hash_secret
    results: dict = {}
    cache: dict = {}
    hashed = {"set": hash_secret(PASSWORD)}
    for password in PASSWORDS:
        for peer in PEERS:
            target = _http10(app) if peer.get("http_version") == "1.0" else app
            client = TestClient(target, client=tuple(peer["client"]),
                                raise_server_exceptions=False)
            if peer.get("addon"):
                os.environ["PANTRY_RAIDER_ADDON"] = "1"
            else:
                os.environ.pop("PANTRY_RAIDER_ADDON", None)
            for fx in payload["fixtures"]:
                for credential in CREDENTIALS:
                    req = build_request(fx, peer, credential)
                    sig = json.dumps([password, peer["name"], req], sort_keys=True)
                    if sig not in cache:
                        _server_settings(settings, password, lambda _p: hashed["set"])
                        _reset_limits()
                        _reset_pairing()
                        client.cookies.clear()
                        r = _send(client, req)
                        cache[sig] = [r.status_code, verdict(
                            r.status_code, r.headers.get("location", ""), _detail(r))]
                    results.setdefault(fx["id"], {})[
                        f"{password}|{peer['name']}|{credential}"] = cache[sig]
    return {"results": results}


def _walk_routes(routes, prefix=""):
    for r in routes:
        if hasattr(r, "original_router"):
            p = getattr(getattr(r, "include_context", None), "prefix", "") or ""
            yield from _walk_routes(r.original_router.routes, prefix + p)
        elif getattr(r, "methods", None):
            yield prefix + r.path, sorted(r.methods), getattr(r, "endpoint", None)


_FORWARD_PREFIXES = ("/pending", "/audit", "/current-recipe", "/timers", "/action-items",
                     "/mealie", "/recipes", "/gadgets", "/printing/label",
                     "/printing/decorative", "/printing/document", "/setup/pairing",
                     "/kiosk")


# Bodies for the forwarding routes that refuse an empty one before forwarding.
_DRIVE_BODIES = {
    "/setup/pairing/status": {"request_id": "req-compat-1"},
    "/gadgets/key-action": {"action": "timers", "source": "neokey"},
    "/gadgets/readings": {"devices": [], "discovered": []},
    "/pending/scan": {"barcode": "0012345678905"},
    "/pending/scanner-mode": {"mode": "consume"},
    "/timers": {"label": "Pasta", "seconds": 600},
}


def driver_satellite(payload: dict) -> dict:
    import signal
    import traceback

    import httpx
    from starlette.testclient import TestClient

    tree = Path(payload["tree"]).resolve()
    app_dir = str(tree / "service" / "app")
    recorded: list = []

    def answer(request):
        url = request.url
        source = ""
        for frame in reversed(traceback.extract_stack()):
            if frame.filename.startswith(app_dir):
                source = Path(frame.filename).relative_to(tree).as_posix() + f":{frame.lineno}"
                break
        recorded.append({"method": request.method, "url": str(url),
                         "headers": dict(request.headers), "content": request.content.decode(
                             "utf-8", "replace") if request.content else "",
                         "source": source})
        path = url.path
        if path == "/api/config/satellite":
            return httpx.Response(401, json={"detail": "Invalid API key."}, request=request)
        if path == "/api/pairing/request":
            return httpx.Response(200, json={"ok": True, "request_id": "req-compat-1",
                                             "code": "123456", "expires_in": 300},
                                  request=request)
        if path.startswith("/api/pairing/status/"):
            return httpx.Response(200, json={"ok": True, "status": "pending"}, request=request)
        return httpx.Response(200, json={}, request=request)
    _httpx_answer(answer)

    from app.main import app
    from app.config import settings
    from app.services import satellite
    satellite._local_ip = lambda: DEVICE_IP
    _set(settings, deployment_mode="pi_remote", remote_server_url=f"http://{SERVER_HOST}",
         upstream_api_key=PRIMARY_KEY, auth_required=False, auth_password="",
         device_id=DEVICE_ID, recipes_backend="native", device_hostname=OWN_NAME,
         remote_server_ip="", remote_server_host="")

    satellite._do_sync_from_upstream()

    class _Slow(Exception):
        pass

    def alarm(*_a):
        raise _Slow()
    signal.signal(signal.SIGALRM, alarm)
    client = TestClient(app, base_url=f"http://{KIOSK_HOST}", client=tuple(LOOPBACK),
                        raise_server_exceptions=False)
    skipped = []
    for path, methods, _endpoint in _walk_routes(app.router.routes):
        if "stream" in path or path.startswith("/static"):
            continue
        concrete = re.sub(r"\{[^}]+\}", "1", path)
        for method in methods:
            if method in ("HEAD", "OPTIONS"):
                continue
            if method != "GET" and not any(path == p or path.startswith(p + "/")
                                           for p in _FORWARD_PREFIXES):
                continue
            kwargs = {"follow_redirects": False}
            if method != "GET":
                kwargs["json"] = _DRIVE_BODIES.get(path, {})
            signal.alarm(20)
            try:
                client.request(method, concrete, **kwargs)
            except _Slow:
                skipped.append(f"{method} {path}")
            finally:
                signal.alarm(0)

    rows = []
    for rec in recorded:
        parts = urlsplit(rec["url"])
        if parts.netloc != SERVER_HOST:
            continue
        headers = {}
        for k, v in rec["headers"].items():
            lk = k.lower()
            if lk in ("host", "content-length", "accept", "accept-encoding", "connection"):
                continue
            name = "-".join(w.capitalize() for w in lk.split("-"))
            name = name.replace("-Pr-", "-PR-").replace("X-Api-Key", "X-API-Key")
            if lk == "x-api-key":
                v = KEY
            elif lk == "x-config-nonce":
                v = NONCE
            elif lk == "user-agent" and v.startswith("python-httpx/"):
                v = UA_HTTPX
            headers[name] = v
        ctype = rec["headers"].get("content-type", "")
        body = None
        if rec["content"]:
            if "json" in ctype:
                try:
                    body = {"json": json.loads(rec["content"])}
                except ValueError:
                    body = None
            elif "x-www-form-urlencoded" in ctype:
                body = {"form": dict(parse_qsl(rec["content"]))}
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        rows.append({"method": rec["method"], "path": path, "headers": headers,
                     "body": body, "host": SERVER_HOST, "source": rec["source"]})
    return {"rows": rows, "skipped": skipped}


def driver_responses(payload: dict) -> dict:
    import importlib.machinery
    import importlib.util

    import httpx
    from starlette.testclient import TestClient

    def refuse(request):
        raise httpx.ConnectError("compat capture: no network", request=request)
    _httpx_answer(refuse)
    from app.main import app
    from app.config import settings
    from app.passwords import hash_secret
    _server_settings(settings, "set", hash_secret)
    _set(settings, vision_provider="gemini", gemini_api_key=AI_KEY,
         streamdeck_ha_base_url=HA_BASE, streamdeck_ha_token=HA_TOKEN)
    lan = TestClient(app, base_url=f"http://{SERVER_HOST}", client=tuple(LAN),
                     raise_server_exceptions=False)
    loop = TestClient(app, base_url=f"http://{KIOSK_HOST}", client=tuple(LOOPBACK),
                      raise_server_exceptions=False)
    key = {"X-API-Key": PRIMARY_KEY}

    def rec(r, scrub=None):
        try:
            body = r.json()
        except Exception:
            body = None
        if scrub:
            body = scrub(body)
        return {"status": r.status_code, "json": body}

    out: dict = {}
    pull_headers = dict(key, **{"X-Device-Id": DEVICE_ID, "X-Device-Hostname": OWN_NAME,
                                "X-Device-Mode": "pi_remote",
                                "X-Device-Version": payload["version"],
                                "X-Device-Ip": DEVICE_IP, "X-Config-Nonce": CONFIG_NONCE})
    out["satellite_pull"] = dict(rec(lan.get("/api/config/satellite", headers=pull_headers)),
                                 key=PRIMARY_KEY, nonce=CONFIG_NONCE,
                                 request_headers=pull_headers)
    out["ha_state"] = rec(lan.get("/ha/state", headers=key))
    out["credentials_exchange"] = rec(lan.post("/api/credentials/exchange", headers=key,
                                               json={"kind": "ha"}))
    out["kiosk_launch_probe"] = rec(loop.get("/ui/kiosk/launch?probe=1",
                                             follow_redirects=False))
    out["gadgets_config"] = rec(TestClient(
        app, base_url="http://127.0.0.1:9284", client=tuple(LOOPBACK),
        raise_server_exceptions=False).get("/gadgets/config", headers=key))
    created = lan.post("/api/pairing/request", json={"hostname": "Home Assistant"}).json()
    rid = created["request_id"]
    loop.post("/api/pairing/approve", json={"request_id": rid})

    def scrub_pairing(body):
        body = dict(body or {})
        if body.get("api_key"):
            body["api_key"] = PAIRED_KEY
        if "request_id" in body:
            body["request_id"] = "<request-id>"
        return body
    out["pairing_status_approved"] = rec(lan.get(f"/api/pairing/status/{rid}"),
                                         scrub=scrub_pairing)

    tree = Path(payload["tree"])
    bridge_path = tree / "scripts" / "image-build" / "foodassistant-host-bridge"
    loader = importlib.machinery.SourceFileLoader("pr_compat_bridge", str(bridge_path))
    spec = importlib.util.spec_from_loader("pr_compat_bridge", loader)
    bridge = importlib.util.module_from_spec(spec)
    loader.exec_module(bridge)
    from app.services import satellite
    overrides = [
        {"slot": 0, "type": "ha_action", "entity_id": "lock.front_door",
         "service": "lock.unlock", "label": "Front door"},
        {"slot": 1, "type": "ha_action", "entity_id": "light.kitchen",
         "service": "light.toggle", "label": "Kitchen light"},
    ]
    base_cfg = {"base_url": "http://127.0.0.1:9284", "api_key": PRIMARY_KEY}
    merged = satellite._merge_streamdeck_settings(
        base_cfg, "Syracuse", "f", "pantryraider", ha_base_url=HA_BASE, ha_token=HA_TOKEN,
        key_overrides=overrides)
    out["streamdeck_config_toml"] = {"input": merged,
                                     "text": bridge._streamdeck_config_toml(merged)}
    return out


def driver_main(args) -> int:
    sys.path.insert(0, os.getcwd())
    _sandbox()
    from app.database import Base, engine
    from app.models import db_models  # noqa: F401 - registers the tables
    Base.metadata.create_all(bind=engine)
    try:
        from app.database import ensure_schema
        ensure_schema(engine)
    except ImportError:
        pass
    payload = json.loads(Path(args.inp).read_text())
    payload["tree"] = args.tree
    mode = {"baseline": driver_baseline, "satellite": driver_satellite,
            "responses": driver_responses}[args.driver]
    Path(args.out_file).write_text(json.dumps(mode(payload)))
    return 0


# --- Vendored rule check -----------------------------------------------------------


ROLLBACK_TEST = ROOT / "tests" / "compat" / "test_rollback.py"
VENDORED = (("valid_api_keys", "service/app/config.py"), ("_api_key_ok", "service/app/main.py"))


def _function_source(text: str, name: str) -> str:
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            lines = text.splitlines()[node.lineno - 1:node.end_lineno]
            return textwrap.dedent("\n".join(lines)).strip()
    raise SystemExit(f"{name} not found")


def _vendored_block(text: str, name: str, ref: str) -> str:
    begin = f"# BEGIN VENDORED {ref} {name}"
    end = f"# END VENDORED {ref} {name}"
    if begin not in text or end not in text:
        raise SystemExit(f"{ROLLBACK_TEST.relative_to(ROOT)}: no vendored block for {name}")
    block = text.split(begin, 1)[1].split(end, 1)[0]
    lines = [ln for ln in block.splitlines() if ln.strip()]
    return textwrap.dedent("\n".join(lines)).strip()


def check_vendored(ref: str) -> int:
    text = ROLLBACK_TEST.read_text(encoding="utf-8")
    bad = []
    for name, path in VENDORED:
        want = _function_source(_git("show", f"{ref}:{path}"), name)
        want = "\n".join(ln for ln in want.splitlines() if ln.strip())
        if _vendored_block(text, name, ref) != want:
            bad.append(f"{name} ({path})")
    if bad:
        print(f"the vendored {ref} rule differs from the tag: {', '.join(bad)}")
        return 1
    print(f"the vendored {ref} rule matches the tag")
    return 0


# --- Capture -----------------------------------------------------------------------


def _check_rule_messages(tree: Path) -> None:
    """The sign-in messages the verdict rule names are the release's own."""
    text = (tree / "service" / "app" / "main.py").read_text(encoding="utf-8")
    found = {node.value for node in ast.walk(ast.parse(text))
             if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    missing = [m for m in RULE["auth_refusal_details"] if m not in found]
    if missing:
        raise SystemExit(f"the release's main.py does not carry: {missing}")


def _version_of(tree: Path) -> str:
    text = (tree / "service" / "app" / "config.py").read_text(encoding="utf-8")
    return re.search(r'^APP_VERSION = "([^"]+)"', text, re.M).group(1)


def _vtuple(v: str):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def proxy_rows(client: str, version: str, natural: list, ref: str) -> list:
    """The R1 fleet proxy paths a satellite at this version sends, with the
    headers the recorded satellite really sent on a proxy request."""
    table = json.loads((ROOT / "tests" / "data" / "r1_fleet_proxy_client_paths.json").read_text())
    rows = []
    for entry in table:
        if entry["backend"] not in ("grocy", "mealie"):
            continue
        first = _vtuple(entry["first"])
        last = None if entry["last"] == "HEAD" else _vtuple(entry["last"])
        if _vtuple(version) < first or (last is not None and _vtuple(version) > last):
            continue
        prefix = f"/api/proxy/{entry['backend']}/"
        headers = next((dict(r["headers"]) for r in natural
                        if r["path"].startswith(prefix)), None)
        if headers is None:
            headers = {"User-Agent": UA_HTTPX, "X-API-Key": KEY}
        headers.pop("Content-Type", None)
        body = {"json": {}} if entry["method"] in ("POST", "PUT") else None
        if body:
            headers["Content-Type"] = "application/json"
        rows.append(_row(client, entry["method"], prefix + entry["path"], headers, body,
                         SERVER_HOST, f"{ref}:tests/data/r1_fleet_proxy_client_paths.json:"
                                      f"{entry['backend']} {entry['method']} {entry['template']}"))
    return rows


# Files whose literal upstream URLs a satellite sends to its main server
# from background work or from handlers the route drive cannot reach (the
# reader relay queue, a NeoKey timer action, the STEMMA mode poll).
STATIC_FORWARD_FILES = (
    "service/app/main.py", "service/app/services/satellite.py",
    "service/app/services/stemma.py", "service/app/services/gadgets_relay.py",
    "service/app/routers/pending.py", "service/app/routers/audit.py",
    "service/app/routers/current_recipe.py", "service/app/routers/action_items.py",
    "service/app/routers/gadgets.py", "service/app/routers/kiosk_status.py",
)
_UPSTREAM_URL = re.compile(
    r"""f["']\{(?:base|settings\.remote_server_url\.rstrip\(['"]/['"]\))\}(/[A-Za-z0-9_\-./]*)["']""")
_CALL_METHOD = re.compile(r"\.(get|post|put|delete|patch)\(|request\(\s*[\"'](GET|POST|PUT|DELETE|PATCH)")


def static_forward_rows(tree: Path, client: str, ref: str, have: set) -> list:
    rows = []
    for rel in STATIC_FORWARD_FILES:
        path = tree / rel
        if not path.exists():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            for m in _UPSTREAM_URL.finditer(line):
                url_path = m.group(1)
                method = "GET"
                for back in range(i, max(-1, i - 4), -1):
                    mm = _CALL_METHOD.search(lines[back])
                    if mm:
                        method = (mm.group(1) or mm.group(2)).upper()
                        break
                if (method, url_path) in have:
                    continue
                have.add((method, url_path))
                headers = {"User-Agent": UA_HTTPX, "X-API-Key": KEY}
                body = None
                if method in ("POST", "PUT", "PATCH"):
                    headers["Content-Type"] = "application/json"
                    body = {"json": {}}
                rows.append(_row(client, method, url_path, headers, body, SERVER_HOST,
                                 f"{ref}:{rel}:{i + 1}"))
    return rows


def satellite_rows(tree: Path, client: str, ref: str, work: Path) -> list:
    got = _run_driver(tree, "satellite", work, {})
    if got["skipped"]:
        print(f"{client}: skipped slow routes: {', '.join(got['skipped'])}")
    natural = []
    seen = set()
    for r in got["rows"]:
        key = (r["method"], r["path"], json.dumps(r["headers"], sort_keys=True),
               json.dumps(r["body"], sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        natural.append(_row(client, r["method"], r["path"], r["headers"], r["body"],
                            r["host"], f"{ref}:{r['source']}"))
    have = {(r["method"], r["path"]) for r in natural}
    extra = [r for r in proxy_rows(client, _version_of(tree), natural, ref)
             if (r["method"], r["path"]) not in have]
    have |= {(r["method"], r["path"]) for r in extra}
    return natural + extra + static_forward_rows(tree, client, ref, have)


def _sort_and_number(rows: list) -> list:
    def key(r):
        return (CLIENT_ORDER.index(r["client"]), r["method"], r["path"], r["host"],
                json.dumps(r["headers"], sort_keys=True), json.dumps(r["body"], sort_keys=True))
    out, seen, counts = [], {}, {}
    for r in sorted(rows, key=key):
        k = key(r)
        if k in seen:
            # The same request from another place (another revision of the
            # integration, another YAML file): one row, every source named.
            first = seen[k]
            if r["source"] not in first["source"].split(" | "):
                first["source"] += " | " + r["source"]
            continue
        counts[r["client"]] = counts.get(r["client"], 0) + 1
        row = {"id": f"{r['client']}-{counts[r['client']]:03d}", **r}
        seen[k] = row
        out.append(row)
    return out


def _dump_baseline(doc: dict, path: Path) -> None:
    head = {k: v for k, v in doc.items() if k != "results"}
    text = json.dumps(head, indent=1, sort_keys=False)[:-2]
    lines = [text + ',\n "results": {']
    items = list(doc["results"].items())
    for i, (fid, res) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        lines.append(f"  {json.dumps(fid)}: {json.dumps(res, separators=(',', ':'))}{comma}")
    lines.append(" }\n}\n")
    path.write_text("\n".join(lines))
    json.loads(path.read_text())


def _private_check(paths: list, work: Path) -> None:
    import socket
    real = {socket.gethostname(), str(work), str(Path.home())}
    for p in paths:
        text = p.read_text()
        for needle in real:
            if needle and len(needle) > 3 and needle in text:
                raise SystemExit(f"{p}: carries a private value ({needle!r}); not written")


def capture(args) -> int:
    out_dir = (ROOT / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="pr-compat-"))
    try:
        ref_tree = _export(args.ref, work / "ref")
        r1_tree = _export(args.r1_ref, work / "r1")
        version = _version_of(ref_tree)
        r1_version = _version_of(r1_tree)
        _check_rule_messages(ref_tree)
        print(f"{args.ref} is {version}; {args.r1_ref} is {r1_version}")

        rows = []
        rows += satellite_rows(ref_tree, "satellite", args.ref, work)
        rows += satellite_rows(r1_tree, "satellite-0.20.2", args.r1_ref, work)
        rows += integration_rows([args.integration_ref, args.ref])
        rows += deck_rows(ref_tree, args.ref)
        rows += reader_rows(ref_tree, args.ref)
        rows += cub_rows(ref_tree, args.ref)
        rows += yaml_rows(ref_tree, args.ref)
        rows += kiosk_rows(ref_tree, args.ref)
        rows += curl_rows(args.ref)
        fixtures = _sort_and_number(rows)
        counts = {c: sum(1 for f in fixtures if f["client"] == c) for c in CLIENT_ORDER}
        print("fixtures:", counts)

        captured_with = (f"scripts/compat/capture-client-fixtures.py --ref {args.ref} "
                         f"--r1-ref {args.r1_ref} --out {args.out} "
                         f"--integration-ref {args.integration_ref}")
        refs = {"ref": args.ref, "version": version, "r1_ref": args.r1_ref,
                "r1_version": r1_version, "integration_ref": args.integration_ref}
        clients_doc = {
            "ref": args.ref, "captured_with": captured_with,
            "note": ("Every request each first-party client of v0.20.0 makes. Secrets are "
                     "placeholders: <legacy-key> is the client's key, <nonce> a pull nonce, "
                     "<password> the install's password. The satellite-0.20.2 rows come from "
                     "the 0.20.2 tree, which public main runs untagged."),
            "refs": refs, "counts": counts, "fixtures": fixtures}

        main_fx = [f for f in fixtures if f["client"] != "satellite-0.20.2"]
        r1_fx = [f for f in fixtures if f["client"] == "satellite-0.20.2"]
        results = {}
        results.update(_run_driver(ref_tree, "baseline", work, {"fixtures": main_fx})["results"])
        results.update(_run_driver(r1_tree, "baseline", work, {"fixtures": r1_fx})["results"])
        cells = [f"{pw}|{peer['name']}|{cred}" for pw in PASSWORDS for peer in PEERS
                 for cred in CREDENTIALS]
        results = {f["id"]: [results[f["id"]][cell] for cell in cells] for f in fixtures}
        baseline_doc = {
            "ref": args.ref, "captured_with": captured_with,
            "note": ("Each client request replayed against the real v0.20.0 app (the "
                     "satellite-0.20.2 rows against the 0.20.2 app), for every password "
                     "state, peer shape and credential. Each fixture's results are "
                     "[status, verdict] pairs in the order of matrix.cells "
                     "(password|peer|credential). The public peer is a documentation "
                     "address standing in for an internet one; the old app ran with "
                     "Python told it is not private, as a real internet address is not."),
            "refs": refs,
            "measured_against": {"default": args.ref, "satellite-0.20.2": args.r1_ref},
            "rule": RULE,
            "matrix": {"cells": cells, "passwords": PASSWORDS, "peers": PEERS,
                       "credentials": CREDENTIALS,
                       "credential_values": CREDENTIAL_VALUES, "password": PASSWORD,
                       "nonce": CONFIG_NONCE, "own_name": OWN_NAME},
            "results": results}

        responses = _run_driver(ref_tree, "responses", work, {"version": version})
        responses_doc = {
            "ref": args.ref, "captured_with": captured_with,
            "note": ("What v0.20.0 answers to the requests newer clients make, measured "
                     "against its real app. The approved pairing status had a freshly "
                     "minted key; its value is replaced by a fixed fake one."),
            "refs": refs, **responses}

        targets = [out_dir / f"clients-{args.ref}.json", out_dir / f"baseline-{args.ref}.json",
                   out_dir / f"responses-{args.ref}.json"]
        tmp_out = work / "out"
        tmp_out.mkdir()
        staged = [tmp_out / t.name for t in targets]
        staged[0].write_text(json.dumps(clients_doc, indent=1) + "\n")
        _dump_baseline(baseline_doc, staged[1])
        staged[2].write_text(json.dumps(responses_doc, indent=1) + "\n")
        _private_check(staged, work)
        for src, dst in zip(staged, targets):
            shutil.copyfile(src, dst)
            print(f"wrote {dst}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ref", default="v0.20.0")
    ap.add_argument("--r1-ref", default="e95cf9b")
    ap.add_argument("--integration-ref", default="c9517f9")
    ap.add_argument("--out", default="tests/compat/fixtures")
    ap.add_argument("--check-vendored", action="store_true")
    ap.add_argument("--driver", help=argparse.SUPPRESS)
    ap.add_argument("--tree", help=argparse.SUPPRESS)
    ap.add_argument("--in", dest="inp", help=argparse.SUPPRESS)
    ap.add_argument("--out-file", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.driver:
        return driver_main(args)
    if args.check_vendored:
        return check_vendored(args.ref)
    return capture(args)


if __name__ == "__main__":
    raise SystemExit(main())
