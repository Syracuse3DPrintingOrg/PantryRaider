"""Loaders and scenario builders for the mixed-version tests.

Everything here reads the fixtures committed under tests/compat/fixtures,
which scripts/compat/capture-client-fixtures.py wrote at build time from the
v0.20.0 tag, the 0.20.2 tree and the first Home Assistant integration. No
test in this directory reads git, a tag, the network or Docker.

The scenario builders put this release into the state a fielded install is in
after the update: upgraded with legacy loopback trust and a grace running
(the default the top-level conftest gives every test), trust retired, a fresh
install, the grace over, or a satellite. They reuse the top-level conftest's
peers, Ingress headers and its fake bridge token, and Z02's credential
vectors.
"""
from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
VECTORS = json.loads((HERE.parent / "fixtures" / "local_credentials_vectors.json").read_text())


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


CLIENTS = load("clients-v0.20.0.json")
BASELINE = load("baseline-v0.20.0.json")
RESPONSES = load("responses-v0.20.0.json")

MATRIX = BASELINE["matrix"]
RULE = BASELINE["rule"]
PEERS = {p["name"]: p for p in MATRIX["peers"]}
FIXTURES_BY_ID = {f["id"]: f for f in CLIENTS["fixtures"]}
PASSWORD = MATRIX["password"]
NONCE = MATRIX["nonce"]
OWN_NAME = MATRIX["own_name"]
OWN_NAMES = frozenset({OWN_NAME, OWN_NAME + ".local"})
CREDENTIAL_VALUES = MATRIX["credential_values"]
PRIMARY_KEY = CREDENTIAL_VALUES["legacy_primary"]
EXTRA_KEY = CREDENTIAL_VALUES["legacy_extra"]
GROCY_KEY = "fake-grocy-key-aaaaaaaa"

KEY_SLOT = "<legacy-key>"
NONCE_SLOT = "<nonce>"
PASSWORD_SLOT = "<password>"

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"}) | OWN_NAMES


CELLS = MATRIX["cells"]


def fixtures(client: str | None = None) -> list:
    return [f for f in CLIENTS["fixtures"] if client is None or f["client"] == client]


def baseline(fid: str) -> dict:
    """{'password|peer|credential': [status, verdict]} for one fixture."""
    return dict(zip(CELLS, BASELINE["results"][fid]))


def host_only(host: str) -> str:
    host = (host or "").strip().lower()
    if host.startswith("["):
        return host[1:host.index("]")] if "]" in host else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def path_only(path: str) -> str:
    return path.split("?", 1)[0]


# --- The capture's request builder and rule --------------------------------------


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


def build_request(fx: dict, peer: dict, credential: str, key: str | None = None) -> dict:
    """The concrete request for one cell: the same steps as the capture script.

    credential is none, legacy_primary or legacy_extra (the baseline's), or
    'key' with the secret in key (a key minted on this release)."""
    mapping = {NONCE_SLOT: NONCE, PASSWORD_SLOT: PASSWORD}
    headers = {k: _subst(v, mapping) for k, v in fx["headers"].items()
               if k.lower() != "x-api-key"}
    if credential == "key":
        headers["X-API-Key"] = key
    elif credential != "none":
        headers["X-API-Key"] = CREDENTIAL_VALUES[credential]
    for k, v in (peer.get("headers") or {}).items():
        for existing in [h for h in headers if h.lower() == k.lower()]:
            del headers[existing]
        headers[k] = v
    host = peer.get("host") or fx["host"]
    return {"method": fx["method"], "url": f"http://{host}{fx['path']}", "host": host,
            "path": fx["path"], "headers": headers, "body": _subst(fx.get("body"), mapping)}


def detail_of(resp):
    try:
        body = resp.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def is_login_redirect(resp) -> bool:
    from urllib.parse import urlsplit
    return (300 <= resp.status_code < 400 and urlsplit(
        resp.headers.get("location", "")).path.endswith(RULE["login_redirect_path"]))


def capture_verdict(resp) -> str:
    """The rule the baseline was measured with, unchanged."""
    if resp.status_code in RULE["auth_refused_statuses"]:
        return "auth_refused"
    if is_login_redirect(resp):
        return "auth_refused"
    body = detail_of(resp) or {}
    if resp.status_code == 403 and body.get("detail") in RULE["auth_refusal_details"]:
        return "auth_refused"
    return "passed"


def _r2_refusals() -> tuple:
    from app.services import access
    details = {access.OPEN_REFUSAL_MSG, access.ADMIN_KEY_MSG, access.SCOPE_MSG,
               access.VIEWER_MSG, access.SATELLITE_GATE_MSG, access.NAME_REFUSAL_MSG,
               access.CROSS_SITE_MSG, access.SETUP_CODE_MSG}
    codes = {"open_mode_refused", "open_mode_unknown_name", "setup_code_required",
             "cross_site", "proxy_path_denied", "proxy_path_not_allowed",
             "proxy_path_invalid"}
    return details, codes


def r2_verdict(resp) -> str:
    """The capture's rule, widened by every refusal this release added: its
    access messages and codes, a refusal page, and the setup-code redirect.
    Wider means stricter: anything this release turns away is caught."""
    if capture_verdict(resp) == "auth_refused":
        return "auth_refused"
    from urllib.parse import urlsplit
    if 300 <= resp.status_code < 400 and urlsplit(
            resp.headers.get("location", "")).path.endswith("/ui/setup-code"):
        return "auth_refused"
    if resp.status_code == 403:
        if "text/html" in resp.headers.get("content-type", ""):
            return "auth_refused"
        details, codes = _r2_refusals()
        body = detail_of(resp) or {}
        if body.get("detail") in details or body.get("code") in codes:
            return "auth_refused"
    return "passed"


def newly_refused(resp, status0: int) -> bool:
    """True when this release turns a cell away that v0.20.0 answered with
    status0: r2_verdict, or a 401 or 403 that v0.20.0 did not give (a route's
    own refusal, such as pairing's {ok: false, error: ...}, has no detail or
    code for r2_verdict to read)."""
    return (r2_verdict(resp) != "passed"
            or (resp.status_code in (401, 403) and resp.status_code != status0))


# --- Sending -----------------------------------------------------------------------


def _http10(app):
    async def shim(scope, receive, send):
        if scope.get("type") == "http":
            scope = dict(scope, http_version="1.0")
        await app(scope, receive, send)
    return shim


class Sender:
    """Sends cells to this release's app in-process, one HTTP client per peer
    shape, all on one event loop (a test client per request costs a thread
    each, and the replay sends thousands). Cookies are cleared before every
    request so no cell signs the next one in."""

    def __init__(self, monkeypatch):
        import asyncio
        self.monkeypatch = monkeypatch
        self.loop = asyncio.new_event_loop()
        self._clients: dict = {}

    def close(self) -> None:
        for client in self._clients.values():
            self.loop.run_until_complete(client.aclose())
        self.loop.close()

    def _client(self, peer: dict):
        import httpx
        from app.main import app
        key = (tuple(peer["client"]), peer.get("http_version") == "1.0")
        if key not in self._clients:
            target = _http10(app) if key[1] else app
            transport = httpx.ASGITransport(app=target, client=tuple(peer["client"]),
                                            raise_app_exceptions=False)
            self._clients[key] = httpx.AsyncClient(transport=transport)
        return self._clients[key]

    def send(self, req: dict, peer: dict):
        if peer.get("addon"):
            self.monkeypatch.setenv("PANTRY_RAIDER_ADDON", "1")
        else:
            self.monkeypatch.delenv("PANTRY_RAIDER_ADDON", raising=False)
        client = self._client(peer)
        client.cookies.clear()
        reset_limits()
        reset_pairing()
        kwargs = {"headers": req["headers"], "follow_redirects": False}
        body = req.get("body") or {}
        if "json" in body:
            kwargs["json"] = body["json"]
        elif "form" in body:
            kwargs["data"] = body["form"]
        return self.loop.run_until_complete(
            client.request(req["method"], req["url"], **kwargs))


def reset_limits() -> None:
    from app.routers import credentials as credentials_router
    from app.services.rate_limit import login_guard, pairing_guard, pin_guard
    for guard in (login_guard, pin_guard, pairing_guard, credentials_router.exchange_guard):
        guard._events.clear()


def reset_pairing() -> None:
    """Empty the pairing queue, as the capture did for every cell: three
    waiting requests fill it, and a full queue answers 429 to everyone."""
    from app.services import pairing
    pairing.reset()


# --- Scenario builders ----------------------------------------------------------


def configure_server(monkeypatch, tmp_path, *, password: str, **extra) -> None:
    """A configured server install shaped like the baseline's: Grocy that
    refuses connections, the two older full-access keys, and a password or
    none (password 'set' or 'none')."""
    from app.config import settings
    from app.passwords import hash_secret
    values = dict(
        data_dir=str(tmp_path), deployment_mode="server",
        grocy_base_url="http://127.0.0.1:9", grocy_api_key=GROCY_KEY,
        mealie_base_url="", mealie_api_key="", api_key=PRIMARY_KEY,
        extra_api_keys=[EXTRA_KEY], extra_api_key_names=["Extra"], demo_mode=False,
        kiosk_pin="", device_hostname=OWN_NAME, totp_secret="", local_totp_enabled=False,
        remote_server_url="", upstream_api_key="", viewer_password="",
        auth_required=password == "set",
        auth_password=hash_secret(PASSWORD) if password == "set" else "",
        require_key_families={}, lan_cidrs="", home_host_names="",
        kiosk_screen_is_admin=True, legacy_mirror=True)
    values.update(extra)
    for k, v in values.items():
        monkeypatch.setattr(settings, k, v, raising=False)


def configure_satellite(monkeypatch, tmp_path, **extra) -> None:
    """An R2 Bandit (pi_remote) linked to a main server, with no password."""
    values = dict(deployment_mode="pi_remote", remote_server_url="http://server.example:9284",
                  upstream_api_key="fake-upstream-key-aaaaa", api_key="",
                  extra_api_keys=[], extra_api_key_names=[])
    values.update(extra)
    configure_server(monkeypatch, tmp_path, password="none", **values)


def set_trust(state: str) -> None:
    """Legacy loopback trust 'legacy' (upgraded default) or 'retired'."""
    import time
    from app.services import auth_state
    doc = auth_state.load()
    now = auth_state.iso(time.time())
    doc["loopback_trust"] = {"state": state, "since": now,
                             "by": "auto" if state == "retired" else "fresh",
                             "retrust_until": None, "confirm_needed": False}
    auth_state.reset(doc, persist=False)


def set_grace(state: str) -> None:
    """The open-mode grace: 'running' (from now), 'over' (ended yesterday)
    or 'none' (a fresh install has none)."""
    import time
    from app.services import auth_state
    doc = auth_state.load()
    now = time.time()
    until = {"running": auth_state.iso(now + 14 * 86400),
             "over": auth_state.iso(now - 86400), "none": None}[state]
    doc["open_mode"] = dict(doc.get("open_mode") or {}, grace_until=until, seen={},
                            seen_other={"count": 0, "last": None})
    auth_state.reset(doc, persist=False)


def set_origin(origin: str) -> None:
    """The install origin: 'upgraded' or 'fresh'."""
    from app.services import auth_state
    doc = auth_state.load()
    doc["origin"] = origin
    auth_state.reset(doc, persist=False)


@pytest.fixture(scope="session")
def compat():
    """This module: the loaders, the request builder, the rules and the
    scenario builders, for the tests in this directory."""
    return sys.modules[__name__]


@pytest.fixture(autouse=True)
def _service_cwd(monkeypatch):
    """The app finds its templates relative to service/, as in the container."""
    monkeypatch.chdir(HERE.parent.parent / "service")


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch):
    """A private in-memory database for every test here: the replays send
    thousands of writes (pending scans, inbox items, commits), and none may
    land in the database the rest of the suite shares."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import app.main  # noqa: F401 - every module that holds SessionLocal is loaded
    from app import database
    from app.models import db_models  # noqa: F401 - registers the tables

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    database.Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    original_session, original_engine = database.SessionLocal, database.engine
    for name, module in list(sys.modules.items()):
        if module is None or not (name == "app" or name.startswith("app.")):
            continue
        if getattr(module, "SessionLocal", None) is original_session:
            monkeypatch.setattr(module, "SessionLocal", session)
        if getattr(module, "engine", None) is original_engine:
            monkeypatch.setattr(module, "engine", engine)
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Only this machine's loopback: no test here may reach anything else."""
    real_connect = socket.socket.connect
    real_gai = socket.getaddrinfo

    def connect(self, address):
        if self.family == getattr(socket, "AF_UNIX", -1):
            return real_connect(self, address)
        host = address[0] if isinstance(address, tuple) else str(address)
        if host in ("127.0.0.1", "::1", "localhost") or host.startswith("127."):
            return real_connect(self, address)
        raise ConnectionRefusedError(111, "tests/compat: no network")

    def getaddrinfo(host, *args, **kwargs):
        if host in (None, "localhost", "127.0.0.1", "::1") or str(host).startswith("127."):
            return real_gai(host, *args, **kwargs)
        raise socket.gaierror(-2, "tests/compat: no network")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.fixture
def no_upstream(monkeypatch):
    """Every outbound HTTP call the app makes fails at once (Grocy, Mealie,
    weather, Home Assistant): the replay is about who gets in, and a refused
    backend answers the same way it did when the baseline was measured."""
    import httpx

    def refuse(self, request):
        raise httpx.ConnectError("tests/compat: no network", request=request)

    async def refuse_async(self, request):
        raise httpx.ConnectError("tests/compat: no network", request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse_async)


@pytest.fixture
def own_names(monkeypatch):
    """This device's own names, fixed: kitchen-pi and kitchen-pi.local."""
    from app.services import access
    monkeypatch.setattr(access, "own_loopback_names", lambda: OWN_NAMES)
    access.clear_name_cache()
    return OWN_NAMES


@pytest.fixture
def sender(monkeypatch, no_upstream, own_names):
    s = Sender(monkeypatch)
    yield s
    s.close()
