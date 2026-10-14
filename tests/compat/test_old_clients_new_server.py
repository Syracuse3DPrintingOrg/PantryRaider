"""Every v0.20.0 client request against this release.

tests/compat/fixtures/clients-v0.20.0.json holds every request the v0.20.0
clients make (satellites at 0.20.0 and 0.20.2, the Home Assistant
integration, the Stream Deck, the sensor reader, a 0.18 Bandit Cub, the YAML
examples, the kitchen screen and a curl script). baseline-v0.20.0.json holds
what the real v0.20.0 app answered to each, for every password state, peer
shape and credential. Both were captured at build time
(scripts/compat/capture-client-fixtures.py); nothing here reads git.

Each cell that passed against v0.20.0 is replayed against this release in the
state a fielded install is in after the update: upgraded, legacy loopback
trust on, the grace running. It must pass again, unless it is one of the
intended refusals in INTENDED, which are asserted exactly: status, detail and
code. A refusal that matches no row fails the test, and so does a row whose
answer is not exactly the one written down. New rows come through the
orchestrator (I-0).

The other scenarios a fielded fleet meets are replayed too: the grace over,
trust retired, a fresh install, a satellite, and the satellites' Grocy and
Mealie proxy requests.
"""
from __future__ import annotations

import html
import json
import secrets
import time
from collections import defaultdict

import pytest

from app.services import access

OUTSIDE = ("proxied", "public")
HOME_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal", ".localdomain",
                 ".intranet", ".private", ".corp")

# The intended refusals. Each row names the change that made it and the exact
# answer. 'none expected' rows list in NAMED_NATURAL the fixtures that hit
# them with the client's own peer and credential (the shapes these clients
# really use); every other hit comes from crossing a client with a peer or key
# it never uses.
INTENDED = {
    "a": ("A key on the admin surface (Settings, admin, tunnel, keys, pairing "
          "approval) gets 403 ADMIN_KEY_MSG from any origin, except plain loopback "
          "while legacy trust lasts, where it is served as legacy loopback and raises "
          "KEY_ADMIN_LOOPBACK_ITEM. Keys no longer change settings (I-2)."),
    "b": ("An anonymous proxied or public request to an install with no password, "
          "after the 14-day grace: 403 OPEN_REFUSAL_MSG, code open_mode_refused, or "
          "the refusal page with the setup-code form first (Dan's decision 2)."),
    "c": ("A hard-denied proxy path: 403 proxy_path_denied (I-15). None expected."),
    "d1": ("A loopback peer with a forwarding header (a reverse proxy on the same "
           "machine) on a password install: /ui paths redirect to /ui/login, other "
           "paths answer 401 Unauthorized, and a password-only sign-in answers 401 "
           "with the proxy second-factor copy and the setup-code link. On an install "
           "with no password: served in the grace, refused as in (b) after it. On "
           "either, pairing (POST /api/pairing/request, GET /api/pairing/status) "
           "answers 403 PAIRING_LAN_MSG, as it has for every proxied request since "
           "0.20.0."),
    "d2": ("A loopback peer with no forwarding header and a Host that is not "
           "localhost, 127.0.0.1, ::1 or one of the device's own names is proxied: "
           "the same outcomes as (d1). None expected."),
    "d3": ("A loopback peer with a local Host and no forwarding header that arrives "
           "over HTTP/1.0, or whose browser marks it same-origin or same-site (or "
           "sends no Sec-Fetch-Site) while its Origin or Referer names another host "
           "(a proxy on the same machine left on nginx's defaults): the same outcomes "
           "as (d1), and one same_host_proxy inbox item."),
    "e": ("A Home Assistant, notification, sensor reader or display call through "
          "Ingress with no key, on an install with no password: 401 Unauthorized. "
          "Ingress serves people through Home Assistant's own sign-in; those device "
          "calls never came that way and need a key there (I-2, open mode)."),
    "f": ("A Bandit Cub firmware image (GET /cub/firmware/<profile>.ota.bin) without "
          "a key on a password install: 401 Unauthorized. Since 0.20.2 (R1) only the "
          "manifest is open to the home network; firmware 0.19 and later takes its "
          "image from the published release, and older Cubs keep the gated path on "
          "installs with no password (Dan's recommendation for old Cubs)."),
    "g": ("An anonymous request on the home network to an install with no password, "
          "by a name that is not one of the home's names, after the grace: 403 "
          "NAME_REFUSAL_MSG, code open_mode_unknown_name, or the name-refused page "
          "(I-2, the DNS-rebinding guard)."),
}

# The pairing routes' own refusal for a request that did not come straight
# from the home network (routers/pairing.py, unchanged since 0.20.0).
PAIRING_LAN_MSG = "Pairing only works from the local network."
PAIRING_PATHS = ("/api/pairing/request", "/api/pairing/status/")

# Rows marked 'none expected', with the fixtures that hit them in their
# clients' own shapes. Any other natural hit fails the test.
NAMED_NATURAL = {"a": set(), "c": set(), "d2": set()}

# The peers each client really uses, for NAMED_NATURAL.
NATURAL_PEERS = {
    "satellite": ("lan",), "satellite-0.20.2": ("lan",), "integration": ("lan",),
    "yaml": ("lan",), "cub018": ("lan",), "streamdeck": ("loopback",),
    "reader": ("loopback",), "kiosk": ("loopback",), "curl": ("loopback", "lan"),
}


def _header(fx: dict, name: str):
    for k, v in fx["headers"].items():
        if k.lower() == name.lower():
            return v
    return None


def expected_origin(compat, fx: dict, peer: dict) -> str:
    """Where this release must place a cell, from I-2's rules written out
    again for the peer shapes (deliberately not access.classify_origin)."""
    name = peer["name"]
    if name == "ingress":
        return "ingress"
    if name == "lan":
        return "lan_direct"
    if name == "public":
        return "public"
    if name in ("lan_forwarded", "loopback_proxy", "loopback_http10"):
        return "proxied"
    host = compat.host_only(peer.get("host") or fx["host"])
    if host not in compat.LOCAL_HOSTS:
        return "proxied"
    sfs = _header(fx, "Sec-Fetch-Site")
    if fx["method"] not in ("GET", "HEAD", "OPTIONS") and sfs not in (None, "same-origin"):
        return "proxied"
    if name == "loopback_referer" and sfs in (None, "same-origin", "same-site"):
        return "proxied"
    return "plain_loopback"


def loopback_row(compat, fx: dict, peer: dict):
    """d1, d2 or d3 for a loopback peer this release now treats as proxied."""
    if peer["client"][0] != "127.0.0.1" or expected_origin(compat, fx, peer) != "proxied":
        return None
    if peer["name"] == "loopback_proxy":
        return "d1"
    if peer["name"] in ("loopback_referer", "loopback_http10"):
        return "d3"
    return "d2"


def _is_home(compat, host: str) -> bool:
    import ipaddress
    name = compat.host_only(host)
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return (name == "localhost" or "." not in name or name.endswith(HOME_SUFFIXES)
            or name in compat.OWN_NAMES)


# --- What each row's answer must be -------------------------------------------


def _json(resp):
    try:
        return resp.json()
    except Exception:
        return None


def signin(resp, fx):
    if fx["path"].startswith("/ui"):
        ok = (resp.status_code == 303 and resp.headers.get("location", "").split("?")[0]
              .endswith("/ui/login"))
        return ok, "a redirect to /ui/login"
    return (resp.status_code == 401 and _json(resp) == {"detail": "Unauthorized"},
            "401 {'detail': 'Unauthorized'}")


def unauthorized(resp, fx):
    return (resp.status_code == 401 and _json(resp) == {"detail": "Unauthorized"},
            "401 {'detail': 'Unauthorized'}")


def admin_key(resp, fx):
    return (resp.status_code == 403 and _json(resp) == {"detail": access.ADMIN_KEY_MSG},
            "403 ADMIN_KEY_MSG")


def outside_login(proxy: bool):
    def check(resp, fx):
        from app.routers import ui
        msg = ui._PROXY_2FA_MSG if proxy else ui._LOCAL_INTERNET_MSG
        text = html.unescape(resp.text)
        return (resp.status_code == 401 and msg in text and "ui/setup-code" in text,
                f"401 with {'the proxy' if proxy else 'the outside'} second-factor copy "
                "and the setup-code link")
    return check


def _page_or_json(resp, msg, code):
    if "text/html" in resp.headers.get("content-type", ""):
        text = html.unescape(resp.text)
        return resp.status_code == 403 and msg in text and "setup-code" in text
    return resp.status_code == 403 and _json(resp) == {"detail": msg, "code": code}


def pairing_refused(resp, fx):
    return (resp.status_code == 403
            and _json(resp) == {"ok": False, "error": PAIRING_LAN_MSG},
            "403 {'ok': False, 'error': PAIRING_LAN_MSG}")


def open_refused(resp, fx):
    return (_page_or_json(resp, access.OPEN_REFUSAL_MSG, "open_mode_refused"),
            "403 OPEN_REFUSAL_MSG open_mode_refused (or the refusal page)")


def name_refused(resp, fx):
    return (_page_or_json(resp, access.NAME_REFUSAL_MSG, "open_mode_unknown_name"),
            "403 NAME_REFUSAL_MSG open_mode_unknown_name (or the name-refused page)")


# --- Which row a cell falls under, per state ------------------------------------


def expect_password(compat, fx, peer, cred):
    """Password install, upgraded, legacy trust on."""
    method, path = fx["method"], compat.path_only(fx["path"])
    origin = expected_origin(compat, fx, peer)
    lp = loopback_row(compat, fx, peer)
    if method == "POST" and path == "/ui/login":
        if lp:
            return lp, outside_login(True)
        return None
    if lp and path.startswith(PAIRING_PATHS):
        return lp, pairing_refused
    fam = access.route_family(method, path)
    if fam == "public":
        return None
    if cred == "none":
        if origin == "plain_loopback":
            return None
        if lp:
            return lp, signin
        if (method == "GET" and path.startswith("/cub/firmware/")
                and path != "/cub/firmware/manifest.json"):
            return "f", unauthorized
        return None
    if fam == "admin" and origin != "plain_loopback":
        return "a", admin_key
    return None


def expect_open(compat, fx, peer, cred, *, grace: bool):
    """No password, upgraded, legacy trust on; the grace running or over."""
    method, path = fx["method"], compat.path_only(fx["path"])
    origin = expected_origin(compat, fx, peer)
    lp = loopback_row(compat, fx, peer)
    if lp and path.startswith(PAIRING_PATHS):
        return lp, pairing_refused
    fam = access.route_family(method, path)
    if fam == "public":
        return None
    if cred != "none":
        if fam == "admin" and origin in OUTSIDE:
            return "a", admin_key
        return None
    if fam in access.FAMILIES and origin == "ingress":
        return "e", unauthorized
    if grace:
        return None
    if origin in OUTSIDE:
        return (lp or "b"), open_refused
    if origin == "lan_direct" and not _is_home(compat, fx["host"]):
        return "g", name_refused
    return None


# --- The replay ------------------------------------------------------------------


def _natural(fx, peer, cred) -> bool:
    keyed = any(k.lower() == "x-api-key" for k in fx["headers"])
    return (peer["name"] in NATURAL_PEERS[fx["client"]]
            and cred == ("legacy_primary" if keyed else "none"))


def replay(compat, sender, password: str, expect, credentials=None) -> dict:
    """Replay every cell that passed against v0.20.0; return the row hits.
    credentials limits the replay to those credential columns."""
    failures: list = []
    hits: dict = defaultdict(list)
    natural: dict = defaultdict(set)
    cache: dict = {}
    for fx in compat.fixtures():
        cells = compat.baseline(fx["id"])
        for peer in compat.MATRIX["peers"]:
            for cred in credentials or compat.MATRIX["credentials"]:
                status0, verdict0 = cells[f"{password}|{peer['name']}|{cred}"]
                if verdict0 != "passed":
                    continue
                req = compat.build_request(fx, peer, cred)
                sig = json.dumps([peer["name"], req], sort_keys=True)
                if sig not in cache:
                    cache[sig] = sender.send(req, peer)
                resp = cache[sig]
                where = (f"{fx['id']} {fx['method']} {fx['path']} [{peer['name']}, {cred}] "
                         f"was {status0}, now {resp.status_code}")
                body = _json(resp) if isinstance(_json(resp), dict) else {}
                if body.get("code") in ("proxy_path_denied", "proxy_path_not_allowed",
                                        "proxy_path_invalid"):
                    hits["c"].append(fx["id"])
                    if _natural(fx, peer, cred):
                        natural["c"].add(fx["id"])
                    failures.append(f"{where}: proxy refusal {body}")
                    continue
                row = expect(fx, peer, cred)
                if row is None:
                    if compat.newly_refused(resp, status0):
                        failures.append(f"{where}: refused by no intended row "
                                        f"({resp.text[:160]!r})")
                    continue
                rid, check = row
                ok, want = check(resp, fx)
                if not ok:
                    failures.append(f"{where}: row ({rid}) expects {want}, got "
                                    f"{resp.text[:160]!r}")
                    continue
                hits[rid].append(fx["id"])
                if _natural(fx, peer, cred):
                    natural[rid].add(fx["id"])
    assert not failures, (f"{len(failures)} cells broke against this release:\n"
                          + "\n".join(failures[:60]))
    for rid, named in NAMED_NATURAL.items():
        assert natural.get(rid, set()) == named, (
            f"row ({rid}) is hit by real client shapes: {sorted(natural.get(rid, set()))}")
    return hits


def _same_host_items(raised_items) -> list:
    return [i for i in raised_items if i["kind"] == "same_host_proxy"]


def test_fixture_coverage(compat):
    """At least 100 fixtures and every client represented."""
    rows = compat.fixtures()
    assert len(rows) >= 100
    clients = {r["client"] for r in rows}
    assert clients == set(NATURAL_PEERS)
    assert set(compat.BASELINE["results"]) == {r["id"] for r in rows}
    for fid, res in compat.BASELINE["results"].items():
        assert len(res) == len(compat.CELLS), fid
    assert set(INTENDED) >= set(NAMED_NATURAL)


def test_password_install_replay(compat, sender, monkeypatch, tmp_path, raised_items):
    compat.configure_server(monkeypatch, tmp_path, password="set")
    hits = replay(compat, sender, "set",
                  lambda fx, peer, cred: expect_password(compat, fx, peer, cred))
    for rid in ("a", "d1", "d2", "d3", "f"):
        assert hits.get(rid), f"row ({rid}) is never exercised"
    assert len(_same_host_items(raised_items)) == 1
    assert any(i["kind"] == "key_admin_loopback" for i in raised_items)


def test_open_install_replay_in_grace(compat, sender, monkeypatch, tmp_path, raised_items):
    compat.configure_server(monkeypatch, tmp_path, password="none")
    hits = replay(compat, sender, "none",
                  lambda fx, peer, cred: expect_open(compat, fx, peer, cred, grace=True))
    assert hits.get("a") and hits.get("e")
    assert len(_same_host_items(raised_items)) == 1


def test_open_install_replay_after_grace(compat, sender, monkeypatch, tmp_path):
    """After the grace only requests without a key change: a key never had a
    grace, so a satellite or Home Assistant that reaches an install with no
    password through a proxy with its key keeps working. The keyless column
    and the primary key's are replayed; the extra key's column runs in the
    other states."""
    compat.configure_server(monkeypatch, tmp_path, password="none")
    compat.set_grace("over")
    hits = replay(compat, sender, "none",
                  lambda fx, peer, cred: expect_open(compat, fx, peer, cred, grace=False),
                  credentials=["none", "legacy_primary"])
    for rid in ("b", "d1", "d2", "d3", "e", "g"):
        assert hits.get(rid), f"row ({rid}) is never exercised"


# --- Loopback shapes that must keep passing --------------------------------------


def _loopback_peer(compat, host):
    return {"name": f"loopback@{host}", "client": ["127.0.0.1", 50000], "host": host}


def test_own_name_is_served_like_localhost(compat, sender, monkeypatch, tmp_path):
    """Host kitchen-pi:9284 from loopback is plain loopback, exactly like
    localhost:9284, under legacy trust (round-1 critic high)."""
    compat.configure_server(monkeypatch, tmp_path, password="set")
    for fx in compat.fixtures("kiosk") + compat.fixtures("streamdeck"):
        for cred in ("none", "legacy_primary"):
            a = sender.send(compat.build_request(fx, _loopback_peer(compat, "localhost:9284"),
                                                 cred), {"name": "l", "client": ["127.0.0.1", 1]})
            b = sender.send(compat.build_request(
                fx, _loopback_peer(compat, f"{compat.OWN_NAME}:9284"), cred),
                {"name": "o", "client": ["127.0.0.1", 1]})
            assert (a.status_code, compat.r2_verdict(a)) == (b.status_code, compat.r2_verdict(b)), (
                fx["id"], cred)
            assert compat.r2_verdict(b) == "passed", fx["id"]


def test_kiosk_exit_return_and_cross_site_probe_still_pass(compat, sender, monkeypatch,
                                                         tmp_path, raised_items):
    """A cross-site navigation back from a store page (the kiosk-exit return)
    and a cross-site GET from a page probing localhost stay plain loopback."""
    compat.configure_server(monkeypatch, tmp_path, password="set")
    peer = {"name": "lb", "client": ["127.0.0.1", 50000]}
    nav = sender.send({"method": "GET", "url": "http://localhost:9284/ui/", "body": None,
                       "headers": {"User-Agent": "Mozilla/5.0", "Accept": "text/html",
                                   "Sec-Fetch-Site": "cross-site",
                                   "Sec-Fetch-Mode": "navigate",
                                   "Referer": "https://shop.example.com/cart"}}, peer)
    probe = sender.send({"method": "GET", "url": "http://localhost:9284/pending/count",
                         "body": None,
                         "headers": {"User-Agent": "Mozilla/5.0", "Sec-Fetch-Site": "cross-site",
                                     "Origin": "https://evil.example"}}, peer)
    assert compat.r2_verdict(nav) == "passed", nav.status_code
    assert compat.r2_verdict(probe) == "passed", probe.status_code
    assert _same_host_items(raised_items) == []


def test_kiosk_url_through_another_address_raises_the_item(compat, monkeypatch, tmp_path,
                                                         raised_items):
    """Z02's kiosk URL check, from a bridge answer whose kiosk opens another
    address (url_local False)."""
    import asyncio

    from app import main
    from app.services import local_agents

    compat.configure_server(monkeypatch, tmp_path, password="set")
    hw = {"kiosk": {"installed": True, "url_local": False, "url_host": "pantry.example.com"}}

    async def status():
        return hw
    monkeypatch.setattr(local_agents, "bridge_present", lambda: True)
    monkeypatch.setattr(local_agents, "bridge_hardware_status", status)
    asyncio.run(main._access_housekeeping_pass(time.time()))
    items = [i for i in raised_items if i["kind"] == "kiosk_url_not_local"]
    assert len(items) == 1
    assert items[0]["title"] == access.KIOSK_URL_ITEM_TITLE
    assert "pantry.example.com" in items[0]["body"]


# --- Retired trust ----------------------------------------------------------------


def test_retired_trust(compat, sender, monkeypatch, tmp_path, bridge_token, kiosk_login):
    """After forced retirement: the deck and the reader with their agent keys
    pass, keyless programs on this device get 401 or the sign-in page, the
    kitchen screen signed in with a ticket passes, and an older key on the
    admin surface from loopback is refused."""
    compat.configure_server(monkeypatch, tmp_path, password="set")
    compat.set_trust("retired")
    agents = compat.VECTORS["agent_keys"]
    failures = []
    peer = {"name": "loopback", "client": ["127.0.0.1", 50000]}
    for client, agent in (("streamdeck", "streamdeck"), ("reader", "gadgets")):
        for fx in compat.fixtures(client):
            fam = access.route_family(fx["method"], compat.path_only(fx["path"]))
            keyed = sender.send(compat.build_request(fx, peer, "key", agents[agent]), peer)
            if compat.r2_verdict(keyed) != "passed" and fx["path"] != "/ui/login":
                failures.append(f"{fx['id']} {fx['path']} with its agent key: "
                                f"{keyed.status_code}")
            bare = sender.send(compat.build_request(fx, peer, "none"), peer)
            if fam != "public":
                ok, _ = signin(bare, fx)
                if not ok:
                    failures.append(f"{fx['id']} {fx['path']} keyless: {bare.status_code}")
    assert not failures, "\n".join(failures)

    # The kitchen screen, signed in once with a launch ticket.
    from fastapi.testclient import TestClient
    from app.main import app
    screen = TestClient(app, client=("127.0.0.1", 50000), base_url="http://localhost:9284",
                        raise_server_exceptions=False)
    kiosk_login(screen)
    for fx in compat.fixtures("kiosk"):
        req = compat.build_request(fx, peer, "none")
        kwargs = {"headers": req["headers"], "follow_redirects": False}
        if (req.get("body") or {}).get("json") is not None:
            kwargs["json"] = req["body"]["json"]
        compat.reset_limits()
        r = screen.request(req["method"], req["url"], **kwargs)
        assert compat.r2_verdict(r) == "passed", (fx["id"], r.status_code, r.text[:200])

    # An older key on the admin surface from this device: refused now.
    setup = next(f for f in compat.fixtures("kiosk") if f["path"] == "/setup")
    r = sender.send(compat.build_request(setup, peer, "legacy_primary"), peer)
    assert r.status_code == 403 and r.json() == {"detail": access.ADMIN_KEY_MSG}


# --- A fresh install ----------------------------------------------------------------


def test_fresh_install(compat, sender, monkeypatch, tmp_path, fresh_install):
    """Ingress page loads pass; keyless integration and YAML calls to the
    Home Assistant and notification routes get 401, and pass with an ha
    (integration) or ha_rest (YAML) key."""
    from app.services import credentials
    compat.configure_server(monkeypatch, tmp_path, password="none")
    ingress = compat.PEERS["ingress"]
    for fx in compat.fixtures("kiosk"):
        if fx["method"] == "GET" and fx["path"] in ("/ui/", "/ui/add", "/ui/timers"):
            r = sender.send(compat.build_request(fx, ingress, "none"), ingress)
            assert compat.r2_verdict(r) == "passed", (fx["id"], r.status_code)
    _, ha_key = credentials.mint("ha", "Home Assistant", "manual")
    _, rest_key = credentials.mint("ha_rest", "Home Assistant sensors", "manual")
    lan = compat.PEERS["lan"]
    seen = set()
    for client, key in (("integration", ha_key), ("yaml", rest_key)):
        for fx in compat.fixtures(client):
            fam = access.route_family(fx["method"], compat.path_only(fx["path"]))
            keyed = sender.send(compat.build_request(fx, lan, "key", key), lan)
            assert compat.r2_verdict(keyed) == "passed", (fx["id"], keyed.status_code,
                                                         keyed.text[:200])
            if fam in access.FAMILIES:
                seen.add(client)
                bare = sender.send(compat.build_request(fx, lan, "none"), lan)
                assert bare.status_code == 401, (fx["id"], bare.status_code)
                assert bare.json() == {"detail": "Unauthorized"}
    assert seen == {"integration", "yaml"}


# --- A satellite ----------------------------------------------------------------------


@pytest.mark.parametrize("origin", ["upgraded", "fresh"])
def test_satellite_serves_home_assistant_with_an_unknown_key_or_none(
        compat, sender, monkeypatch, tmp_path, origin):
    """Home Assistant reaches a Bandit with the main server's key, which the
    Bandit cannot check yet, so on a satellite with no password /ha/state and
    the notification routes are served with that key or with none (Z02)."""
    compat.configure_satellite(monkeypatch, tmp_path)
    compat.set_origin(origin)
    if origin == "fresh":
        compat.set_trust("retired")
        compat.set_grace("none")
    lan = compat.PEERS["lan"]
    unknown = "compat-unknown-key-" + secrets.token_hex(4)
    checked = 0
    for fx in compat.fixtures("integration"):
        path = compat.path_only(fx["path"])
        if not (path == "/ha/state" or path.startswith("/events/")):
            continue
        for cred, key in (("key", unknown), ("none", None)):
            r = sender.send(compat.build_request(fx, lan, cred, key), lan)
            assert r.status_code not in (401, 403), (fx["id"], cred, r.status_code, r.text[:200])
            assert compat.r2_verdict(r) == "passed", (fx["id"], cred, r.status_code)
            checked += 1
    assert checked >= 4


# --- The satellites' Grocy and Mealie proxy requests ------------------------------


def test_satellite_proxy_requests_are_never_refused(compat, sender, monkeypatch, tmp_path):
    """Every recorded Grocy and Mealie request from a 0.20.0 or 0.20.2
    satellite, with its older key, passes the proxy policy (moved here from
    Z05)."""
    from app.config import settings
    compat.configure_server(monkeypatch, tmp_path, password="set")
    assert settings.proxy_path_policy == "enforce"
    lan = compat.PEERS["lan"]
    rows = [f for f in compat.fixtures() if f["client"].startswith("satellite")
            and (f["path"].startswith("/api/proxy/grocy/")
                 or f["path"].startswith("/api/proxy/mealie/"))]
    assert len(rows) >= 40
    bad = []
    for fx in rows:
        r = sender.send(compat.build_request(fx, lan, "legacy_primary"), lan)
        body = _json(r) if isinstance(_json(r), dict) else {}
        if r.status_code in (401, 403) or str(body.get("code", "")).startswith("proxy_path"):
            bad.append(f"{fx['id']} {fx['method']} {fx['path']}: {r.status_code} {body}")
    assert not bad, "\n".join(bad)
