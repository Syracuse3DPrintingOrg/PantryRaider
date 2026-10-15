"""One rule for "from outside" on the sign-in page and the setup code.

A password alone signs in from home and is refused from outside while the
kitchen has no two-factor; the refusal offers the setup code, and the setup
code page must then take the code and that password. The two pages used to
judge "outside" differently: the sign-in by the peer's address, the setup
code by the access rules (which also count Home networks and the networks the
device is attached to). A peer one called home and the other called outside
sent the owner from the sign-in page to the setup code and straight back.

These walk every kind of peer through both pages and check they agree, that
a refused sign-in only links to a setup code page that answers, and that the
setup code then signs the owner in.
"""
from __future__ import annotations

import html
import ipaddress
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.passwords import hash_secret
from app.services import access, lan, setup_code

SERVICE = Path(__file__).resolve().parents[1] / "service"
_PW = "kitchen-secret-1"
_LINK = 'href="ui/setup-code"'
_INGRESS_HEADERS = {
    "X-Ingress-Path": "/api/hassio_ingress/abc123",
    "X-Hass-Source": "core.ingress",
    "X-Forwarded-For": "192.168.1.20, 172.30.32.1",
    "X-Forwarded-Host": "homeassistant.local:8123",
    "X-Forwarded-Proto": "http",
}
_GATEWAY = "172.18.0.1"


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "gk", raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret(_PW), raising=False)
    monkeypatch.setattr(settings, "viewer_password", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "local_totp_enabled", False, raising=False)
    monkeypatch.setattr(settings, "local_totp_secret", "", raising=False)
    monkeypatch.setattr(settings, "totp_secret", "", raising=False)
    monkeypatch.setattr(settings, "tunnel_enabled", False, raising=False)
    monkeypatch.setattr(settings, "qr_public_url", "", raising=False)
    monkeypatch.setattr(settings, "cloud_instance_token", "", raising=False)
    monkeypatch.setattr(settings, "lan_cidrs", "", raising=False)
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True, raising=False)
    monkeypatch.delenv("PANTRY_RAIDER_ADDON", raising=False)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    # Nothing from this test machine's own network may leak into the answers.
    monkeypatch.setattr(lan, "onlink_networks", lambda proc_root="/proc": [])
    monkeypatch.setattr(lan, "default_gateways", lambda proc_root="/proc": set())
    monkeypatch.setattr(lan, "in_docker_bridge", lambda *a, **k: False)
    from app.main import app as fastapi_app
    return fastapi_app


def _http10(asgi_app):
    async def shim(scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope, http_version="1.0")
        await asgi_app(scope, receive, send)
    return shim


def _lan_cidrs(monkeypatch):
    monkeypatch.setattr(settings, "lan_cidrs", "100.64.0.0/10", raising=False)


def _onlink(monkeypatch):
    # A home network with global IPv6 addresses, which the device is attached
    # to (it is not in the fixed private ranges).
    nets = [ipaddress.ip_network("2001:db8:1::/64")]
    monkeypatch.setattr(lan, "onlink_networks", lambda proc_root="/proc": list(nets))


def _addon(monkeypatch):
    monkeypatch.setenv("PANTRY_RAIDER_ADDON", "1")


def _tunnel(monkeypatch):
    monkeypatch.setattr(settings, "tunnel_enabled", True, raising=False)
    monkeypatch.setattr(settings, "qr_public_url", "https://kitchen.example.org", raising=False)


def _docker_gateway(monkeypatch):
    monkeypatch.setattr(lan, "default_gateways", lambda proc_root="/proc": {_GATEWAY})
    monkeypatch.setattr(lan, "in_docker_bridge", lambda *a, **k: True)


# name, peer, base_url, headers, setup, http10, outside
CASES = [
    ("lan", ("192.168.1.20", 50000), "http://testserver", {}, None, False, False),
    ("loopback", ("127.0.0.1", 50000), "http://localhost:9284", {}, None, False, False),
    ("home networks", ("100.101.102.103", 50000), "http://testserver", {}, _lan_cidrs,
     False, False),
    # Tailscale and carrier-grade NAT addresses keep the sign-in they had in
    # 0.20 (a password alone), with or without Home networks.
    ("tailscale", ("100.101.102.103", 50000), "http://testserver", {}, None, False, False),
    ("attached network", ("2001:db8:1::7", 50000), "http://testserver", {}, _onlink,
     False, False),
    ("ingress", ("172.30.32.2", 50000), "http://testserver", _INGRESS_HEADERS, _addon,
     False, False),
    ("public", ("203.0.113.9", 50000), "http://testserver", {}, None, False, True),
    ("proxied", ("192.168.1.5", 50000), "http://testserver",
     {"X-Forwarded-For": "203.0.113.9"}, None, False, True),
    ("tunnel name", ("192.168.1.5", 50000), "http://kitchen.example.org", {}, _tunnel,
     False, True),
    # nginx on its defaults in front of a Docker bridge install: the app sees
    # Docker's gateway, the rewritten Host and HTTP/1.0.
    ("docker gateway proxy", (_GATEWAY, 50000), "http://127.0.0.1:9284", {}, _docker_gateway,
     True, True),
    # A program on the Docker host reaching the published port is still home.
    ("docker gateway host program", (_GATEWAY, 50000), "http://localhost:9284", {},
     _docker_gateway, False, False),
]


def _client(app, peer, base_url, headers, http10):
    target = _http10(app) if http10 else app
    return TestClient(target, client=peer, base_url=base_url, headers=dict(headers))


def _login(client):
    return client.post("/ui/login", data={"mode": "local", "password": _PW},
                       follow_redirects=False)


@pytest.mark.parametrize("name,peer,base_url,headers,setup,http10,outside", CASES,
                         ids=[c[0] for c in CASES])
def test_the_sign_in_and_the_setup_code_agree(app, monkeypatch, name, peer, base_url,
                                              headers, setup, http10, outside):
    if setup is not None:
        setup(monkeypatch)
    client = _client(app, peer, base_url, headers, http10)
    r = _login(client)
    page = _client(app, peer, base_url, headers, http10).get("/ui/setup-code",
                                                              follow_redirects=False)
    if outside:
        assert r.status_code == 401, name
        assert "second factor" in html.unescape(r.text), name
        # The refusal links to the setup code, and that page takes it.
        assert _LINK in r.text, name
        assert page.status_code == 200, name
        assert access.SETUP_CODE_INTRO_RESCUE in html.unescape(page.text), name
        code = setup_code.current()
        assert code, name
        done = client.post("/ui/setup-code", data={"code": code, "password": _PW},
                           follow_redirects=False)
        assert done.status_code == 303, name
        assert done.headers["location"].endswith("/setup?twofactor=needed#pane-security"), name
    else:
        assert r.status_code == 303, (name, r.text[:300])
        assert r.headers["location"].endswith("/ui/"), name
        # From home the setup code page has nothing to offer: it is the
        # normal sign-in.
        assert page.status_code == 303, name
        assert page.headers["location"].endswith("/ui/login"), name
    # Settings agrees with both pages about where this visitor is.
    state = client.get("/setup/security/state")
    assert state.status_code == 200, (name, state.text[:200])
    assert state.json()["sign_in_outside"] is outside, name


def test_the_link_is_only_offered_while_the_setup_code_page_answers(app, monkeypatch):
    """With two-factor on there is no setup-code way in, so a refused sign-in
    (here a viewer password, which never completes from outside) offers no
    link that would lead back to the sign-in page."""
    from app import totp as local_totp
    monkeypatch.setattr(settings, "local_totp_secret", local_totp.generate_totp_secret(),
                        raising=False)
    monkeypatch.setattr(settings, "local_totp_enabled", True, raising=False)
    monkeypatch.setattr(settings, "viewer_password", hash_secret("viewer-secret-1"),
                        raising=False)
    client = TestClient(app, client=("203.0.113.9", 50000))
    r = client.post("/ui/login", data={"mode": "local", "password": "viewer-secret-1"},
                    follow_redirects=False)
    assert r.status_code == 401 and _LINK not in r.text
    page = client.get("/ui/setup-code", follow_redirects=False)
    assert page.status_code == 303


def test_the_outside_rule_is_one_function(app):
    """The sign-in page, the setup code and the Security pane all ask access."""
    from app.routers import setup_code as setup_code_router
    from app.routers import ui as ui_router
    src = (SERVICE / "app" / "routers" / "ui.py").read_text()
    assert "access.sign_in_from_outside(request)" in src
    assert "is_internet_origin(" not in src
    sc = (SERVICE / "app" / "routers" / "setup_code.py").read_text()
    assert "access.sign_in_from_outside(request)" in sc
    sec = (SERVICE / "app" / "routers" / "security.py").read_text()
    assert "access.sign_in_from_outside(request)" in sec
    del setup_code_router, ui_router
