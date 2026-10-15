"""Every path each first-party client calls is allowed under the key it holds.

The paths are read from the committed client sources, so a client that
starts calling something new fails here until the scope tables cover it (or
the change is deliberate). None of them is on the admin surface: no
first-party client calls Settings with a key.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.services import access

ROOT = Path(__file__).resolve().parents[1]


def _sub(path: str) -> str:
    """Placeholders become one concrete segment."""
    path = re.sub(r"\{[^}]*\}", "x1", path)
    return path.split("?")[0]


def _f_string_calls(text: str, base_pattern: str) -> set:
    """(METHOD, path) from client.<method>(f"{base}/path...") style calls."""
    out = set()
    pat = re.compile(r"\.(get|post|put|delete|patch)\(\s*f[\"']\{" + base_pattern +
                     r"\}(/[A-Za-z0-9_/{}.\-]*)", re.S)
    for m in pat.finditer(text):
        out.add((m.group(1).upper(), _sub(m.group(2))))
    return out


def deck_calls() -> set:
    calls = set()
    for name in ("actions.py", "controller.py"):
        text = (ROOT / "streamdeck" / "foodassistant_streamdeck" / name).read_text()
        calls |= _f_string_calls(text, r"base")
    # /system/health is the host bridge's own route (a different base).
    calls.discard(("GET", "/system/health"))
    calls.discard(("GET", "/api/camera_proxy/x1"))
    return calls


def reader_calls() -> set:
    calls = set()
    daemon = (ROOT / "gadgets" / "foodassistant_gadgets" / "daemon.py").read_text()
    calls |= _f_string_calls(daemon, r"self\.cfg\.base_url")
    module = (ROOT / "gadgets" / "foodassistant_gadgets" / "i2c" / "module.py").read_text()
    for m in re.finditer(r'return\s+"(/[a-z/_-]+)",', module):
        calls.add(("POST", m.group(1)))             # press_request: posted by the daemon
    return calls


def cub_calls() -> set:
    src = (ROOT / "esphome" / "components" / "pantry_raider" / "pantry_raider.cpp").read_text()
    calls = set()
    for m in re.finditer(r'fetch_\("(GET|POST|DELETE|PUT)",\s*([^,]+),', src):
        # "/timers/" + timer_id + "/extend": literals kept, variables become x1.
        parts = []
        for token in m.group(2).split("+"):
            token = token.strip()
            parts.append(token[1:-1] if token.startswith('"') else "x1")
        path = "".join(parts)
        if path.startswith("/"):
            calls.add((m.group(1), _sub(path)))
    # The public update manifest the stock ESPHome update component fetches.
    calls.add(("GET", "/cub/firmware/manifest.json"))
    return calls


def integration_calls() -> set:
    src = (ROOT / "homeassistant" / "custom_components" / "pantry_raider" / "api.py").read_text()
    calls = set()
    lines = src.splitlines()
    for i, line in enumerate(lines):
        m = re.search(r'url = f"\{self\._base_url\}(/[^"]+)"', line)
        if m:
            window = "\n".join(lines[i:i + 6])
            method = re.search(r"\._session\.(get|post|put|delete)\(", window)
            calls.add(((method.group(1) if method else "get").upper(), _sub(m.group(1))))
    for m in re.finditer(r'"(/events/[a-z-]+)"', src):
        calls.add(("POST", m.group(1)))
    # The key exchange is made only while the entry still holds a shared
    # legacy key (it swaps that key for an 'ha' key), so it is checked under
    # the legacy scope in its own test below.
    calls.discard(INTEGRATION_EXCHANGE)
    return calls


INTEGRATION_EXCHANGE = ("POST", "/api/credentials/exchange")


def satellite_calls() -> set:
    from app import main
    calls = {("GET", "/api/config/satellite"), ("POST", "/api/pairing/request"),
             ("GET", "/api/pairing/status/x1")}
    for prefix in main._UPSTREAM_FORWARD_PREFIXES + main._SATELLITE_RECIPE_PREFIXES + \
            main._SATELLITE_GADGET_PREFIXES:
        calls.add(("GET", prefix))
        calls.add(("POST", prefix + "/x1"))
    for path in main._SATELLITE_PRINT_PATHS:
        calls.add(("POST", path))
    upstream_files = [ROOT / "service" / "app" / p for p in (
        "services/stemma.py", "services/gadgets_relay.py", "routers/current_recipe.py",
        "routers/audit.py", "routers/action_items.py", "routers/pending.py")]
    for f in upstream_files:
        if f.exists():
            for m in re.finditer(r"\}(/(?:pending|timers|audit|action-items|gadgets|current-recipe)"
                                 r"[A-Za-z0-9_/{}-]*)[\"']", f.read_text()):
                calls.add(("GET", _sub(m.group(1))))
                calls.add(("POST", _sub(m.group(1))))
    rows = json.loads((ROOT / "tests" / "data" / "r1_fleet_proxy_client_paths.json").read_text())
    for row in rows:
        calls.add((row["method"].upper(), f"/api/proxy/{row['backend']}/{row['path']}"))
    return calls


def yaml_calls() -> set:
    calls = set()
    for name in ("configuration.yaml", "espcontrol.yaml", "barcode-scanner.md"):
        lines = (ROOT / "homeassistant" / name).read_text().splitlines()
        for i, line in enumerate(lines):
            m = re.search(r"(resource|url):\s*\"?https?://[^/\s]+:9284(/[^\s\"?]*)", line)
            if not m:
                continue
            if m.group(1) == "resource":
                calls.add(("GET", m.group(2)))
                continue
            method = "GET"
            for nxt in lines[i + 1:i + 6]:
                mm = re.search(r"method:\s*\"?([A-Za-z]+)", nxt)
                if mm:
                    method = mm.group(1).upper()
                    break
                if re.match(r"\s*\w+:\s*$", nxt) and "headers" not in nxt and "payload" not in nxt:
                    break
            if m.group(2).startswith("/ui/"):
                continue          # a page link in the docs, opened by a browser
            calls.add((method, m.group(2)))
    return calls


def esp_calls() -> set:
    text = (ROOT / "docs" / "esp-devices.md").read_text()
    calls = set()
    for m in re.finditer(r"http_request\.(post|get):\s*\n\s*url:\s*\"?https?://[^/\s]+(/[^\s\"]+)", text):
        calls.add((m.group(1).upper(), m.group(2)))
    for m in re.finditer(r"`(GET|POST) (/gadgets/[a-z-]+)`", text):
        calls.add((m.group(1), m.group(2)))
    return calls


CLIENTS = [
    ("deck", deck_calls, ["deck"], 15),
    ("reader", reader_calls, ["reader"], 7),
    ("cub", cub_calls, ["display"], 5),
    ("integration", integration_calls, ["ha"], 9),
    ("satellite", satellite_calls, ["satellite"], 20),
    ("yaml", yaml_calls, ["ha_rest"], 8),
    ("esp", esp_calls, ["ha_rest", "display"], 2),
]


def _allowed(scope, method, path) -> bool:
    if access.route_family(method, path) == "public":
        return True
    return access.allows_raw(scope, method, path, kiosk_is_admin=False)


@pytest.mark.parametrize("name,collect,scopes,minimum", CLIENTS, ids=[c[0] for c in CLIENTS])
def test_every_client_path_is_allowed(name, collect, scopes, minimum):
    calls = collect()
    assert len(calls) >= minimum, (name, sorted(calls))
    for scope in scopes:
        refused = sorted(c for c in calls if not _allowed(scope, *c))
        assert not refused, (name, scope, refused)
    on_admin = sorted(c for c in calls if access.route_family(*c) == "admin")
    assert not on_admin, (name, on_admin)


def test_integration_exchanges_its_legacy_key():
    src = (ROOT / "homeassistant" / "custom_components" / "pantry_raider" / "api.py").read_text()
    assert 'f"{self._base_url}/api/credentials/exchange"' in src
    init = (ROOT / "homeassistant" / "custom_components" / "pantry_raider" / "__init__.py").read_text()
    # Called only while the entry still holds a legacy key.
    assert 'auth.get("credential") != "legacy"' in init
    assert _allowed("legacy", *INTEGRATION_EXCHANGE)
    assert access.route_family(*INTEGRATION_EXCHANGE) == "kitchen"


def test_esp_shapes_are_the_button_and_the_screen():
    assert {("POST", "/gadgets/esp-action"), ("GET", "/gadgets/esp-screen")} <= esp_calls()


def test_the_esp_page_names_the_scope_labels_exactly():
    text = (ROOT / "docs" / "esp-devices.md").read_text()
    assert access.SCOPE_LABELS["ha_rest"] in text
    assert access.SCOPE_LABELS["display"] in text
