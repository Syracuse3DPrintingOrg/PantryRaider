"""Sign in with your own identity provider (OpenID Connect).

A fake provider (a real RSA key, ID tokens signed here, every provider call
through an httpx.MockTransport) stands in for Pocket ID or any other
provider, so the flow runs end to end through /setup/oidc, /auth/oidc and
/ui/login with no network.
"""
import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.passwords import hash_secret  # noqa: E402
from app.services import access  # noqa: E402
from app.services import oidc  # noqa: E402

_PW = "kitchen-secret"
_HOST = "kitchen.example.com"
_ORIGIN = f"https://{_HOST}"
_ISSUER = "https://id.example.org"
_CLIENT = "client-123"
_SECRET = "s3cret/with+odd=chars"
_SUB = "user-abc"


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class FakeProvider:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "k1"
        self.sub = _SUB
        self.claims_override: dict = {}
        self.sign_with = None
        self.last_auth: dict = {}
        self.token_calls = 0
        self.discovery_issuer = _ISSUER

    def jwks(self):
        n = self.key.public_key().public_numbers()
        return {"keys": [{"kty": "RSA", "use": "sig", "alg": "RS256", "kid": self.kid,
                          "n": _b64u(n.n.to_bytes((n.n.bit_length() + 7) // 8, "big")),
                          "e": _b64u(n.e.to_bytes(3, "big"))}]}

    def id_token(self, nonce: str) -> str:
        now = int(time.time())
        claims = {"iss": _ISSUER, "aud": _CLIENT, "sub": self.sub, "nonce": nonce,
                  "iat": now, "exp": now + 300, "preferred_username": "dan"}
        claims.update(self.claims_override)
        head = {"alg": "RS256", "kid": self.kid, "typ": "JWT"}
        signing = _b64u(json.dumps(head).encode()) + "." + _b64u(json.dumps(claims).encode())
        key = self.sign_with or self.key
        sig = key.sign(signing.encode(), padding.PKCS1v15(), hashes.SHA256())
        return signing + "." + _b64u(sig)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == _ISSUER + "/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": self.discovery_issuer,
                "authorization_endpoint": _ISSUER + "/authorize",
                "token_endpoint": _ISSUER + "/api/oidc/token",
                "jwks_uri": _ISSUER + "/.well-known/jwks.json"})
        if url == _ISSUER + "/.well-known/jwks.json":
            return httpx.Response(200, json=self.jwks())
        if url == _ISSUER + "/api/oidc/token":
            self.token_calls += 1
            form = parse_qs(request.content.decode())
            basic = base64.b64encode(
                f"{_CLIENT}:{'s3cret%2Fwith%2Bodd%3Dchars'}".encode()).decode()
            if request.headers.get("authorization") != "Basic " + basic:
                return httpx.Response(401, json={"error": "invalid_client"})
            verifier = form["code_verifier"][0]
            challenge = _b64u(hashlib.sha256(verifier.encode()).digest())
            if (form["code"][0] != "good-code"
                    or challenge != self.last_auth.get("code_challenge")
                    or form["redirect_uri"][0] != self.last_auth.get("redirect_uri")):
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"id_token": self.id_token(self.last_auth["nonce"]),
                                             "access_token": "a", "token_type": "Bearer"})
        return httpx.Response(404)

    def authorize(self, location: str) -> dict:
        parts = urlsplit(location)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == _ISSUER + "/authorize"
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        assert q["code_challenge_method"] == "S256"
        assert q["response_type"] == "code"
        assert q["client_id"] == _CLIENT
        self.last_auth = q
        return q


@pytest.fixture
def provider(monkeypatch):
    p = FakeProvider()
    monkeypatch.setattr(oidc, "transport", httpx.MockTransport(p.handler))
    oidc.reset_cache()
    yield p
    oidc.reset_cache()


@pytest.fixture
def client(monkeypatch, tmp_path, provider):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    for name, value in {
        "data_dir": str(tmp_path), "deployment_mode": "server",
        "grocy_base_url": "http://grocy.test", "grocy_api_key": "gk",
        "auth_password": hash_secret(_PW), "viewer_password": "", "auth_required": True,
        "local_totp_enabled": False, "local_totp_secret": "", "totp_secret": "",
        "local_totp_recovery": [], "local_passkeys": [], "tunnel_enabled": False,
        "cloud_instance_token": "", "home_host_names": [_HOST],
        "oidc_issuer": "", "oidc_client_id": "", "oidc_client_secret": "", "oidc_name": "",
        "oidc_subject": "", "oidc_account": "", "oidc_host": "", "oidc_only": False,
    }.items():
        monkeypatch.setattr(settings, name, value, raising=False)
    from app.main import app
    try:
        yield TestClient(app, base_url=_ORIGIN)
    finally:
        os.chdir(cwd)


def _password_sign_in(client):
    r = client.post("/ui/login", data={"mode": "local", "password": _PW}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/ui/"), r.text[:400]


def _signed_in(client) -> bool:
    r = client.get("/setup/oidc", follow_redirects=False)
    return r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json")


def _configure(client):
    r = client.post("/setup/oidc/config", json={"issuer": _ISSUER + "/", "client_id": _CLIENT,
                                                "client_secret": _SECRET, "name": "Pocket ID"})
    assert r.status_code == 200 and r.json()["configured"], r.text
    return r.json()


def _link(client, provider):
    _password_sign_in(client)
    _configure(client)
    r = client.post("/setup/oidc/link", json={"credential": _PW})
    assert r.status_code == 200 and r.json()["ok"], r.text
    q = provider.authorize(r.json()["url"])
    assert q["redirect_uri"] == _ORIGIN + "/auth/oidc/callback"
    r = client.get("/auth/oidc/callback", params={"code": "good-code", "state": q["state"],
                                                  "iss": _ISSUER}, follow_redirects=False)
    assert r.status_code == 303, r.text[:400]
    assert r.headers["location"] == "/setup?oidc=linked#pane-security"
    assert settings.oidc_subject == _SUB
    assert settings.oidc_host == _HOST
    assert settings.oidc_account == "dan"


def _start(client, provider):
    r = client.get("/auth/oidc/start", follow_redirects=False)
    assert r.status_code == 303, r.text[:400]
    return provider.authorize(r.headers["location"])


def _callback(client, state, code="good-code", **extra):
    return client.get("/auth/oidc/callback", params={"code": code, "state": state, **extra},
                      follow_redirects=False)


def _sign_out(client):
    client.post("/ui/logout", follow_redirects=False)
    assert not _signed_in(client)


def test_nothing_changes_until_it_is_set_up(client):
    page = client.get("/ui/login").text
    assert "auth/oidc/start" not in page
    assert 'id="login-password"' in page
    r = client.get("/auth/oidc/start", follow_redirects=False)
    assert r.headers["location"].endswith("/ui/login")
    _password_sign_in(client)
    assert _signed_in(client)


def test_config_checks_the_provider(client, provider):
    _password_sign_in(client)
    r = client.post("/setup/oidc/config", json={"issuer": "http://id.example.org",
                                                "client_id": _CLIENT, "client_secret": "x"})
    assert r.status_code == 400 and "https://" in r.json()["error"]
    provider.discovery_issuer = "https://someone-else.example"
    r = client.post("/setup/oidc/config", json={"issuer": _ISSUER, "client_id": _CLIENT,
                                                "client_secret": "x"})
    assert r.status_code == 400
    assert settings.oidc_issuer == ""
    provider.discovery_issuer = _ISSUER
    state = _configure(client)
    assert state["issuer"] == _ISSUER and state["has_secret"] and not state["linked"]
    assert state["callback"] == _ORIGIN + "/auth/oidc/callback"
    assert "client_secret" not in json.dumps(state).replace("has_secret", "")
    # A blank secret keeps the saved one.
    r = client.post("/setup/oidc/config", json={"issuer": _ISSUER, "client_id": _CLIENT,
                                                "client_secret": "", "name": "Pocket ID"})
    assert r.status_code == 200 and settings.oidc_client_secret == _SECRET


def test_linking_needs_the_password(client, provider):
    _password_sign_in(client)
    _configure(client)
    r = client.post("/setup/oidc/link", json={"credential": "wrong"})
    assert r.status_code == 401
    assert settings.oidc_subject == ""


def test_link_then_sign_in(client, provider):
    _link(client, provider)
    _sign_out(client)
    page = client.get("/ui/login").text
    assert 'href="auth/oidc/start"' in page and "Sign in with Pocket ID" in page
    assert 'id="login-password"' in page  # the other ways in stay
    q = _start(client, provider)
    r = _callback(client, q["state"], iss=_ISSUER)
    assert r.status_code == 303 and r.headers["location"] == "/ui/"
    assert _signed_in(client)


def test_the_state_is_single_use_and_bound_to_the_browser(client, provider):
    _link(client, provider)
    _sign_out(client)
    q = _start(client, provider)
    other = TestClient(client.app, base_url=_ORIGIN)
    assert _callback(other, q["state"]).status_code == 400
    assert not _signed_in(other)
    q = _start(client, provider)
    assert _callback(client, q["state"]).status_code == 303
    _sign_out(client)
    client.cookies.set(oidc.STATE_COOKIE, q["state"], domain=_HOST, path="/auth/oidc")
    assert _callback(client, q["state"]).status_code == 400
    assert not _signed_in(client)


@pytest.mark.parametrize("change", [
    {"sub": "someone-else"},
    {"aud": "another-client"},
    {"iss": "https://evil.example"},
    {"exp": int(time.time()) - 3600},
    {"nonce": "not-the-nonce"},
    {"aud": [_CLIENT, "other"], "azp": "other"},
    "bad-signature",
    "wrong-issuer-param",
    "bad-code",
])
def test_bad_answers_never_sign_in(client, provider, change):
    _link(client, provider)
    _sign_out(client)
    q = _start(client, provider)
    extra = {}
    code = "good-code"
    if change == "bad-signature":
        provider.sign_with = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    elif change == "wrong-issuer-param":
        extra["iss"] = "https://evil.example"
    elif change == "bad-code":
        code = "stolen"
    else:
        provider.claims_override = change
    r = _callback(client, q["state"], code=code, **extra)
    assert r.status_code == 401
    assert not _signed_in(client)


def test_provider_refusal_is_shown(client, provider):
    _link(client, provider)
    _sign_out(client)
    q = _start(client, provider)
    r = client.get("/auth/oidc/callback", params={"state": q["state"], "error": "access_denied"})
    assert r.status_code == 401 and "did not let that account in" in r.text
    assert not _signed_in(client)


def test_a_new_issuer_or_client_unlinks(client, provider):
    _link(client, provider)
    r = client.post("/setup/oidc/only", json={"on": True})
    assert r.json()["only"]
    _configure(client)  # same provider and client: still linked
    assert settings.oidc_subject == _SUB and settings.oidc_only
    r = client.post("/setup/oidc/config", json={"issuer": _ISSUER, "client_id": "other-client",
                                                "client_secret": "", "name": ""})
    assert r.status_code == 200
    assert settings.oidc_subject == "" and not settings.oidc_only


def test_linking_needs_the_same_signed_in_browser(client, provider):
    _password_sign_in(client)
    _configure(client)
    r = client.post("/setup/oidc/link", json={"credential": _PW})
    q = provider.authorize(r.json()["url"])
    # Signed out before coming back: the link does not happen.
    client.post("/ui/logout")
    r = _callback(client, q["state"])
    assert r.status_code == 401
    assert settings.oidc_subject == ""


def test_only_switch_needs_a_linked_account(client, provider):
    _password_sign_in(client)
    _configure(client)
    r = client.post("/setup/oidc/only", json={"on": True})
    assert r.status_code == 400 and not settings.oidc_only


def test_only_mode_refuses_the_other_ways_in(client, provider, monkeypatch):
    _link(client, provider)
    client.post("/setup/oidc/only", json={"on": True})
    _sign_out(client)
    monkeypatch.setattr(access, "sign_in_from_outside", lambda request: True)
    page = client.get("/ui/login").text
    assert 'href="auth/oidc/start"' in page
    assert 'id="login-password"' not in page
    r = client.post("/ui/login", data={"mode": "local", "password": _PW}, follow_redirects=False)
    assert r.status_code == 403
    r = client.post("/ui/login", data={"mode": "passkey_begin"})
    assert r.status_code == 403
    r = client.post("/ui/login", data={"mode": "forager", "email": "a@b.c", "fpass": "x"})
    assert r.status_code == 403
    assert client.get("/ui/login?google=start", follow_redirects=False).status_code == 403
    assert not _signed_in(client)
    q = _start(client, provider)
    assert _callback(client, q["state"]).status_code == 303
    assert _signed_in(client)


def test_only_mode_keeps_the_password_from_home(client, provider):
    _link(client, provider)
    client.post("/setup/oidc/only", json={"on": True})
    _sign_out(client)
    page = client.get("/ui/login").text
    assert "Can&#39;t reach Pocket ID?" in page or "Can't reach Pocket ID?" in page
    assert 'id="login-password"' in page
    _password_sign_in(client)
    assert _signed_in(client)


def test_button_only_on_the_linked_address(client, provider):
    _link(client, provider)
    _sign_out(client)
    lan = TestClient(client.app, base_url="http://192.168.1.50:9284")
    page = lan.get("/ui/login").text
    assert 'href="auth/oidc/start"' not in page
    assert f"https://{_HOST}/ui/login" in page
    r = lan.get("/auth/oidc/start", follow_redirects=False)
    assert r.status_code == 400
    assert f"https://{_HOST}" in r.text


def test_unlink(client, provider):
    _link(client, provider)
    r = client.post("/setup/oidc/unlink")
    assert r.status_code == 200 and not r.json()["linked"]
    _sign_out(client)
    assert "auth/oidc/start" not in client.get("/ui/login").text
