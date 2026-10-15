"""Every route has a reviewed family, and each key scope reaches what its table says.

tests/data/route_families.json pins one entry per real route
({"METHOD /path/{param}": family}). A new route fails here until its line is
reviewed and added; an entry whose route has not landed yet is not an error.
"""
from __future__ import annotations

import json
import re
import warnings
from pathlib import Path

import pytest

from app.services import access

ROUTES_JSON = Path(__file__).parent / "data" / "route_families.json"
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _norm(path: str) -> str:
    return re.sub(r"\{([^}:]+):[^}]+\}", r"{\1}", path)


def collect_routes(app) -> set:
    ops = set()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        spec = app.openapi()
    for path, item in spec["paths"].items():
        for method in item:
            if method.upper() in METHODS:
                ops.add((method.upper(), _norm(path)))

    def walk(routes):
        for route in routes:
            inner = getattr(route, "original_router", None)
            if inner is not None:
                walk(inner.routes)
                continue
            path = getattr(route, "path", None)
            for method in getattr(route, "methods", None) or ():
                if method in METHODS and path:
                    ops.add((method, _norm(path)))

    walk(app.router.routes)
    return ops


@pytest.fixture(scope="module")
def routes():
    from app.main import app
    return collect_routes(app)


@pytest.fixture(scope="module")
def pinned():
    return json.loads(ROUTES_JSON.read_text())


def test_the_collector_sees_the_whole_app(routes):
    assert len(routes) >= 350
    assert sum(1 for _m, p in routes if "{" in p) >= 50


def test_every_route_has_a_reviewed_family(routes, pinned):
    missing = sorted(f"{m} {p}" for m, p in routes if f"{m} {p}" not in pinned)
    assert not missing, "Review and add to tests/data/route_families.json:\n" + "\n".join(
        f'  "{k}": "{access.route_family(*k.split(" ", 1))}",' for k in missing)


def test_the_pinned_families_match_the_rules(pinned):
    wrong = {k: (v, access.route_family(*k.split(" ", 1))) for k, v in pinned.items()
             if access.route_family(*k.split(" ", 1)) != v}
    assert not wrong, wrong


def test_the_routes_later_zones_add_are_pre_listed(pinned):
    assert pinned["POST /api/fleet/ai"] == "fleet"
    assert pinned["POST /api/fleet/recipes-external"] == "fleet"
    assert pinned["POST /api/fleet/camera-ai"] == "fleet"
    assert pinned["POST /ha/disconnect"] == "ha"
    assert pinned["GET /setup/update/state"] == "admin"


def test_every_always_public_entry_is_public():
    from app import main
    for path in main._ALWAYS_PUBLIC:
        for method in ("GET", "POST"):
            assert access.route_family(method, path) == "public", path
    for prefix in main._ALWAYS_PUBLIC_PREFIXES:
        assert access.route_family("GET", prefix + "abc") == "public"
    assert "/api/config/satellite" not in main._ALWAYS_PUBLIC
    assert "/api/config/satellite" in main._SETUP_BYPASS


@pytest.mark.parametrize("method,path", [
    ("POST", "/setup/save"), ("POST", "/api/pairing/approve"), ("POST", "/admin/restore"),
    ("POST", "/tunnel/start"), ("POST", "/printing/install"), ("GET", "/api/credentials"),
    ("GET", "/openapi.json"), ("POST", "/api/devices/scan"), ("GET", "/ui/camera/preview"),
])
def test_legacy_keys_never_reach_the_admin_surface(method, path):
    assert access.route_family(method, path) == "admin"
    assert not access.allows_raw("legacy", method, path, kiosk_is_admin=True)
    assert access.allows_raw("admin", method, path, kiosk_is_admin=True)


@pytest.mark.parametrize("method,path", [
    ("POST", "/ha/connect"), ("GET", "/api/config/satellite"), ("GET", "/api/proxy/grocy/api/stock"),
    ("POST", "/api/credentials/exchange"), ("GET", "/timers"), ("POST", "/events/notify"),
])
def test_legacy_keys_keep_everything_else(method, path):
    assert access.allows_raw("legacy", method, path, kiosk_is_admin=True)


SCOPE_TABLE = [
    # method, path, satellite, viewer, ha, ha_rest, display
    ("GET", "/timers", True, True, False, True, False),
    ("POST", "/timers", True, True, False, True, False),
    ("DELETE", "/timers/abc", True, True, False, True, True),
    ("POST", "/timers/abc/extend", True, True, False, True, True),
    ("POST", "/pending/scan", True, True, False, True, False),
    ("POST", "/inventory/consume", True, True, False, False, False),
    ("GET", "/ha/state", True, True, True, True, False),
    ("POST", "/ha/connect", False, False, True, False, False),
    ("POST", "/ha/settings", True, True, True, False, False),
    ("POST", "/events/notify", True, True, True, True, False),
    ("POST", "/events/confirm", True, True, False, False, False),
    ("GET", "/cub/summary", True, True, False, False, True),
    ("GET", "/cub/firmware/tdisplay.ota.bin", True, True, False, True, True),
    ("POST", "/gadgets/esp-action", True, True, False, True, True),
    ("GET", "/gadgets/esp-screen", True, True, False, True, True),
    ("GET", "/api/config/satellite", True, True, False, False, False),
    ("GET", "/api/proxy/grocy/api/stock", True, True, False, False, False),
    ("POST", "/setup/save", False, False, False, False, False),
]


@pytest.mark.parametrize("row", SCOPE_TABLE, ids=lambda r: f"{r[0]} {r[1]}")
def test_narrow_scopes(row):
    method, path, sat, viewer, ha, ha_rest, display = row
    for scope, want in (("satellite", sat), ("viewer", viewer), ("ha", ha),
                        ("ha_rest", ha_rest), ("display", display)):
        assert access.allows_raw(scope, method, path, kiosk_is_admin=True) is want, (scope, row)


def test_head_counts_as_get():
    assert access.route_family("HEAD", "/health") == "public"
    assert access.allows_raw("ha_rest", "HEAD", "/timers", kiosk_is_admin=True)


def test_anonymous_is_never_allowed():
    snap = access.AccessSnapshot()
    assert not access.allows(access.ANONYMOUS("lan_direct"), "GET", "/timers", snap)
