import copy
import os
import sys
from pathlib import Path

import pytest

# Make `app` importable the same way the container does (workdir /app == service/)
sys.path.insert(0, str(Path(__file__).parent.parent / "service"))

# Make the Stream Deck controller package importable for its pure-logic tests.
sys.path.insert(0, str(Path(__file__).parent.parent / "streamdeck"))

# Same for the Bluetooth thermometer reader's pure decoders (gadgets/).
sys.path.insert(0, str(Path(__file__).parent.parent / "gadgets"))

# Parallel runs (pytest -n auto): give every xdist worker its own fresh data
# dir before the app is imported, so workers never share the SQLite database
# or the state files (timers, scanner mode, auth state) under data_dir. An
# explicit DATA_DIR becomes the parent of the per-worker dirs.
#
# A serial run with no DATA_DIR gets a fresh temp dir too. Without it the app
# fell back to its default /app/data, so on a machine where that path exists
# the suite shared (and wrote to) a real on-disk database with every other
# run, and two concurrent runs could block each other on its SQLite lock
# (FoodAssistant-uddro).
_XDIST_WORKER = os.environ.get("PYTEST_XDIST_WORKER")
if _XDIST_WORKER or not os.environ.get("DATA_DIR"):
    import atexit
    import shutil
    import tempfile

    _parent = os.environ.get("DATA_DIR") or None
    if _parent:
        os.makedirs(_parent, exist_ok=True)
    _worker_dir = tempfile.mkdtemp(prefix=f"pytest-{_XDIST_WORKER or 'serial'}-", dir=_parent)
    os.environ["DATA_DIR"] = _worker_dir
    atexit.register(shutil.rmtree, _worker_dir, ignore_errors=True)


# Never advertise over real mDNS from a test. Every `with TestClient(app)`
# runs the app's lifespan, which registers a zeroconf service on the LAN; the
# probe and goodbye packets cost half a second or more per client, which across
# the suite was most of its run time. A test that exercises discovery itself
# patches AsyncZeroconf with its own fake, which still wins over this one.
class _QuietZeroconf:
    def __init__(self, *args, **kwargs):
        pass

    async def async_register_service(self, *args, **kwargs):
        return None

    async def async_unregister_service(self, *args, **kwargs):
        return None

    async def async_close(self):
        return None


try:
    import zeroconf.asyncio as _zc_asyncio
except Exception:  # zeroconf is optional; discovery skips itself without it
    _zc_asyncio = None
if _zc_asyncio is not None:
    _zc_asyncio.AsyncZeroconf = _QuietZeroconf

# Create the app's tables on its own engine up front. A few tests exercise the
# real SessionLocal (not an in-memory override), and on a clean database (CI,
# or a fresh checkout) those tables would not exist yet. create_all is
# idempotent, so this is a no-op when the tables already exist.
from app.database import engine, ensure_schema, Base  # noqa: E402
from app.models import db_models  # noqa: E402,F401 - registers models with Base

Base.metadata.create_all(bind=engine)
# A pre-existing local database may predate post-release column additions
# (create_all never alters an existing table); backfill them like startup does.
ensure_schema(engine)


@pytest.fixture
def anyio_backend():
    """Run @pytest.mark.anyio async tests on asyncio only (no trio dependency)."""
    return "asyncio"


# --- Cross-test isolation -------------------------------------------------
#
# The app keeps a single global `settings` object, and many test modules
# mutate it, some with plain `settings.x = ...` or `settings.apply(...)`
# with no restore at all. Historically that made the suite order dependent
# (for example, test_restore.py leaked gemini_api_key="live-key", and any
# later test that hit /health then tried to build a real Gemini provider).
# The pair of autouse fixtures below snapshots the settings state and the
# working directory at module and at test granularity and restores both, so
# no module can leak state into another and no test can leak into the next
# test regardless of collection order.
#
# The root logger gets the same treatment for the diagnostics file handler:
# app startup (any TestClient(app)) installs the tagged rotating handler for
# that test's data_dir and never removes it, which used to make a later
# configure_file_logging() call reuse a handler pointed at a dead tmp dir.

def _snapshot_settings():
    from app.config import settings

    return copy.deepcopy(settings.__dict__)


def _snapshot_log_handlers():
    import logging

    from app.services import diagnostics

    root = logging.getLogger()
    tagged = {h for h in root.handlers if getattr(h, diagnostics._HANDLER_TAG, False)}
    return tagged, root.level


def _restore_log_handlers(snapshot) -> None:
    import logging

    from app.services import diagnostics

    tagged, level = snapshot
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, diagnostics._HANDLER_TAG, False) and h not in tagged:
            root.removeHandler(h)
            h.close()
    root.setLevel(level)


def _restore_settings(snapshot) -> None:
    from app.config import settings

    if settings.__dict__ == snapshot:
        return
    settings.__dict__.clear()
    settings.__dict__.update(copy.deepcopy(snapshot))
    # Cached providers may have been built from the leaked settings; drop
    # them so the next test builds from the restored state.
    from app import dependencies

    dependencies.reset_providers()


@pytest.fixture(autouse=True, scope="module")
def _module_settings_isolation():
    """Undo settings/cwd/log-handler changes made by module-scoped fixtures."""
    snapshot = _snapshot_settings()
    logs = _snapshot_log_handlers()
    cwd = os.getcwd()
    yield
    _restore_settings(snapshot)
    _restore_log_handlers(logs)
    os.chdir(cwd)


@pytest.fixture(autouse=True)
def _test_settings_isolation():
    """Undo settings/cwd changes made by an individual test.

    This snapshots AFTER module-scoped fixtures have run, so per-module
    client fixtures that configure settings keep working for every test
    in their module; only per-test drift is rolled back.
    """
    snapshot = _snapshot_settings()
    logs = _snapshot_log_handlers()
    cwd = os.getcwd()
    yield
    _restore_settings(snapshot)
    _restore_log_handlers(logs)
    os.chdir(cwd)


def _reset_module_caches() -> None:
    """Empty every process-wide cache the app holds in module globals."""
    from app.services.ttl_cache import TTLCache

    for name, module in list(sys.modules.items()):
        if module is None or not name.startswith("app."):
            continue
        try:
            values = list(vars(module).values())
        except TypeError:                             # not a real module object
            continue
        for value in values:
            if isinstance(value, TTLCache):
                value.invalidate()
    # Two caches predate TTLCache and keep their own shape.
    start_page = sys.modules.get("app.services.start_page")
    if start_page is not None:
        start_page._catalog_cache["at"] = 0.0
        start_page._catalog_cache["value"] = None
    mealie = sys.modules.get("app.services.mealie")
    if mealie is not None:
        mealie.reset_cache()


@pytest.fixture(autouse=True)
def _cache_isolation():
    """Drop every module-level cache before each test.

    Routers and services hold caches in module globals that outlive a
    TestClient: the kiosk status health probe, the pending and action-item
    forward counts, the Start Page deck catalog, the Mealie recipe and food
    maps. Their TTLs are seconds to minutes, so a value stored by one test was
    still being served to the next one, whose settings often describe a
    completely different install. That makes a suite pass in order and fail
    when a single file runs alone. Individual files that also invalidate a
    cache by hand are left as they are; belt and braces is fine here.
    """
    _reset_module_caches()
    yield


@pytest.fixture(autouse=True)
def _readiness_isolation():
    """Reset the first-boot readiness gate's module state around every test.

    The gate keeps sticky flags (answered / dismissed / provisioned) in a
    module-level dict, so a test that dismissed the gate used to silently turn
    it off for every later test in the process. That masked real failures for
    weeks: settings-page tests rendering a pi_hosted install with no inventory
    key only passed because SOME earlier test had left the gate dismissed, and
    re-ordering the suite (or running a file alone) surfaced them
    (FoodAssistant-6v9q ship). Every test now starts with a pristine gate; a
    fixture that wants the gate out of the way says so honestly by setting
    grocy_api_key, exactly like a real configured appliance.
    """
    from app.services import readiness
    readiness.reset()
    yield
    readiness.reset()


# --- Access (0.21) ------------------------------------------------------------
#
# Every TestClient built without client= gets a LAN peer. Starlette's default
# peer is the string 'testclient', which is not an address, and the access
# rules treat a non-address peer as the internet (as production must).
# Plain loopback (the device's own programs) needs client=('127.0.0.1', 50000)
# and base_url 'http://localhost'; off-LAN tests use ('203.0.113.9', 50000).

DEFAULT_TEST_PEER = ("192.168.1.50", 50000)
LOOPBACK_PEER = ("127.0.0.1", 50000)
PUBLIC_PEER = ("203.0.113.9", 50000)
INGRESS_PEER = ("172.30.32.2", 50000)
INGRESS_HEADERS = {
    "X-Ingress-Path": "/api/hassio_ingress/testtoken",
    "X-Hass-Source": "core.ingress",
    "X-Forwarded-For": "192.168.1.20, 172.30.32.1",
    "X-Forwarded-Host": "homeassistant.local:8123",
    "X-Forwarded-Proto": "http",
}
TEST_BRIDGE_TOKEN = "test-bridge-token-not-a-secret"


def _wrap_testclient() -> None:
    from starlette.testclient import TestClient as _TC

    if getattr(_TC.__init__, "_pr_wrapped", False):
        return
    original = _TC.__init__

    def __init__(self, *args, **kwargs):
        if "client" not in kwargs:
            kwargs["client"] = DEFAULT_TEST_PEER
        original(self, *args, **kwargs)

    __init__._pr_wrapped = True
    _TC.__init__ = __init__


_wrap_testclient()


def _reset_access_state() -> None:
    from app.routers import credentials as credentials_router
    from app.services import (access, auth_state, credentials, lan, local_agents,
                              setup_code)
    from app.services.rate_limit import login_guard, pairing_guard, pin_guard

    for guard in (login_guard, pin_guard, pairing_guard, credentials_router.exchange_guard):
        guard._events.clear()
    setup_code.reset(persist=False)
    auth_state.reset(auth_state.test_state(), persist=False)
    credentials.reset(persist=False)
    local_agents.reset(persist=False)
    lan.clear_cache()
    access.clear_name_cache()
    access.clear_raised_once()


_REAL_RAISE_ITEM: dict = {}


@pytest.fixture(autouse=True)
def _access_isolation(request):
    """Each test starts as an upgraded install with legacy loopback trust and
    a grace running from now, no keys minted, nothing on disk, and the
    sign-in, PIN, pairing and setup-code limiters empty.

    The inbox items the access rules raise (services/access.raise_item) are
    kept in a list (the raised_items fixture) instead of the shared test
    database, where they would show up in later tests' counts. A test that
    needs the real rows marks itself @pytest.mark.real_access_items."""
    # No monkeypatch here on purpose: an autouse fixture that pulls in
    # monkeypatch makes every test's monkeypatch undo run after the settings
    # restore below it, which breaks tests that patch what the restore calls.
    from app.services import access
    _reset_access_state()
    raised = []
    # The real function, captured once: a test's own monkeypatch of it may be
    # undone after this fixture, so never trust the current attribute.
    original = _REAL_RAISE_ITEM.setdefault("fn", access.raise_item)
    if request.node.get_closest_marker("real_access_items") is None:
        access.raise_item = (lambda kind, title, body, dedupe, level="info":
                             raised.append({"kind": kind, "title": title, "body": body,
                                            "dedupe": dedupe, "level": level}))
    request.node._pr_raised_items = raised
    try:
        yield
    finally:
        access.raise_item = original
        _reset_access_state()


@pytest.fixture
def raised_items(request):
    """The inbox items the access rules raised during this test."""
    return request.node._pr_raised_items


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "real_access_items: let access inbox items reach the database")


@pytest.fixture
def fresh_install():
    """A fresh install: trust for local programs retired, no grace."""
    from app.services import auth_state
    import time as _time

    now = _time.time()
    state = auth_state.test_state(
        origin="fresh",
        loopback_trust={"state": "retired", "since": auth_state.iso(now), "by": "fresh",
                        "retrust_until": None, "confirm_needed": False},
        open_mode={"grace_until": None})
    auth_state.reset(state, persist=False)
    return state


@pytest.fixture
def grace_over():
    """An upgraded install whose 14-day grace ended yesterday (legacy trust on)."""
    from app.services import auth_state
    import time as _time

    now = _time.time()
    state = auth_state.test_state(open_mode={"grace_until": auth_state.iso(now - 86400)})
    auth_state.reset(state, persist=False)
    return state


@pytest.fixture
def own_screen():
    """The host bridge reports a kitchen screen installed on this device, so
    the old trust for its programs (legacy loopback) applies on a server
    install too. Without it a server keeps no such trust."""
    from app.services import local_agents

    local_agents._state["hw"] = {"kiosk": {"installed": True},
                                 "streamdeck": {"installed": False},
                                 "gadgets": {"installed": False}}
    yield
    local_agents._state["hw"] = None


@pytest.fixture
def lan_client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app, client=DEFAULT_TEST_PEER)


@pytest.fixture
def loopback_client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app, client=LOOPBACK_PEER, base_url="http://localhost")


@pytest.fixture
def public_client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app, client=PUBLIC_PEER)


@pytest.fixture
def ingress_client(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setenv("PANTRY_RAIDER_ADDON", "1")
    return TestClient(app, client=INGRESS_PEER, headers=dict(INGRESS_HEADERS))


@pytest.fixture
def bridge_token(tmp_path, monkeypatch):
    """A bridge token in a temporary data_dir (the fake value from the vectors)."""
    from app.config import settings
    from app.services import bridge

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    (tmp_path / "bridge-token").write_text(TEST_BRIDGE_TOKEN + "\n")
    bridge.invalidate_bridge_token()
    yield TEST_BRIDGE_TOKEN
    bridge.invalidate_bridge_token()


@pytest.fixture
def kiosk_login(bridge_token):
    """kiosk_login(client): sign a loopback client in as the kitchen screen."""
    import secrets as _secrets
    import time as _time

    from app.services import local_agents

    def _login(client):
        ticket = local_agents.make_ticket(bridge_token, int(_time.time()), _secrets.token_hex(16))
        r = client.get("/ui/kiosk/launch", params={"ticket": ticket, "next": "/ui/"},
                       follow_redirects=False)
        assert r.status_code == 303, r.text
        return r

    return _login
