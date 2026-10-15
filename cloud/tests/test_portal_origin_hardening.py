"""Hardening against same-site kitchen pages: reserved kitchen addresses, the
__Host- session cookie, and the same-origin guard on the remaining cookie
POSTs.

A kitchen's remote-access address is a subdomain of the Forager domain, so
its pages are same-site with the portal. These tests pin the three defenses
that keep one from passing itself off as Forager or riding a member's
session."""
import pytest
from fastapi.testclient import TestClient

from app import tunnel as alloc
from app import tunnel_client
from app.config import settings
from app.database import SessionLocal
from app.deps import SECURE_SESSION_COOKIE, SESSION_COOKIE
from app.main import app
from app.models import AuthSession
from app.security import token_hash

EVIL = {"Origin": "https://evil.forager.pantryraider.app",
        "Referer": "https://evil.forager.pantryraider.app/x"}


def wg_key(tag: str) -> str:
    return tag + "A" * (43 - len(tag)) + "="


@pytest.fixture
def stub_agent(monkeypatch):
    monkeypatch.setattr(tunnel_client, "add_peer", lambda *a, **k: None)
    monkeypatch.setattr(tunnel_client, "remove_peer", lambda pk: None)


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# --- Reserved kitchen addresses ------------------------------------------------

@pytest.mark.parametrize("name", ["login", "Billing", "www", "admin", "api",
                                  "support", "forager", "status"])
def test_reserved_subdomain_cannot_be_chosen(client, instance_token, stub_agent,
                                             name):
    r = client.post("/v1/tunnel/enable", headers=_auth(instance_token),
                    json={"public_key": wg_key("K"), "subdomain": name})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "subdomain_reserved"
    assert not alloc.is_reserved_subdomain(detail["suggestion"])


def test_reserved_hostname_hint_gets_a_readable_address(client, instance_token,
                                                        stub_agent):
    r = client.post("/v1/tunnel/enable", headers=_auth(instance_token),
                    json={"public_key": wg_key("K"), "hostname_hint": "admin"})
    assert r.status_code == 200
    assert r.json()["dns_name"] == "admin-kitchen.forager.pantryraider.app"


def test_subdomain_available_reports_reserved(client, instance_token):
    body = client.get("/v1/tunnel/subdomain-available?name=login",
                      headers=_auth(instance_token)).json()
    assert body["available"] is False
    assert body["reserved"] is True
    assert body["suggestion"] == "login-kitchen"


def test_ordinary_names_are_not_reserved():
    for name in ("kitchen", "pr", "dans-pi", "kitchen-2"):
        assert not alloc.is_reserved_subdomain(name)


# --- The __Host- session cookie ------------------------------------------------

@pytest.fixture
def tls(monkeypatch):
    """Behave as production does behind TLS: Secure cookies on, and a client
    speaking https so the browser-side cookie jar sends them back."""
    monkeypatch.setattr(settings, "cookie_secure", True)
    return TestClient(app, base_url="https://testserver")


def _signup(c, email):
    r = c.post("/signup", data={"email": email, "password": "abcdefghijk1",
                                "confirm_password": "abcdefghijk1"},
               follow_redirects=False)
    assert r.status_code == 303
    return r


def _set_cookies(resp):
    return resp.headers.get_list("set-cookie")


def test_login_sets_a_host_prefixed_cookie(tls):
    resp = _signup(tls, "alice@example.com")
    cookies = _set_cookies(resp)
    host = [c for c in cookies if c.startswith(f"{SECURE_SESSION_COOKIE}=")]
    assert host, cookies
    attrs = host[0].lower()
    assert "secure" in attrs and "path=/" in attrs and "domain=" not in attrs
    assert "httponly" in attrs


def test_plain_name_cookie_cannot_shadow_the_real_session(tls):
    victim = _signup(tls, "victim@example.com").cookies.get(SECURE_SESSION_COOKIE)
    tls.cookies.clear()
    attacker = _signup(tls, "attacker@example.com").cookies.get(
        SECURE_SESSION_COOKIE)
    tls.cookies.clear()
    # What a browser sends after a kitchen page tossed a parent-domain cookie
    # under the plain name: both, the planted one included.
    r = tls.get("/account", headers={
        "Cookie": f"{SESSION_COOKIE}={attacker}; "
                  f"{SECURE_SESSION_COOKIE}={victim}"})
    assert r.status_code == 200
    assert "victim@example.com" in r.text
    assert "attacker@example.com" not in r.text


def test_plain_name_cookie_with_a_new_session_is_ignored(tls):
    attacker = _signup(tls, "attacker@example.com").cookies.get(
        SECURE_SESSION_COOKIE)
    tls.cookies.clear()
    r = tls.get("/account", headers={"Cookie": f"{SESSION_COOKIE}={attacker}"},
                follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def _age_session(token):
    db = SessionLocal()
    try:
        sess = db.query(AuthSession).filter_by(token_hash=token_hash(token)).one()
        sess.created_at = "2026-09-01T00:00:00+00:00"
        db.commit()
    finally:
        db.close()


def test_old_plain_name_session_is_honored_once_and_moved(tls):
    token = _signup(tls, "dana@example.com").cookies.get(SECURE_SESSION_COOKIE)
    tls.cookies.clear()
    _age_session(token)
    r = tls.get("/account", headers={"Cookie": f"{SESSION_COOKIE}={token}"})
    assert r.status_code == 200
    assert "dana@example.com" in r.text
    cookies = _set_cookies(r)
    assert any(c.startswith(f"{SECURE_SESSION_COOKIE}={token}") for c in cookies)
    # The plain-name copy is expired in the same response.
    assert any(c.startswith(f"{SESSION_COOKIE}=") and "max-age=0" in c.lower()
               for c in cookies)


def test_signing_out_with_an_old_cookie_does_not_reissue_it(tls):
    token = _signup(tls, "eli@example.com").cookies.get(SECURE_SESSION_COOKIE)
    tls.cookies.clear()
    _age_session(token)
    r = tls.post("/logout", headers={"Cookie": f"{SESSION_COOKIE}={token}"},
                 follow_redirects=False)
    assert r.status_code == 303
    for c in _set_cookies(r):
        if "forager_session=" in c:
            assert "max-age=0" in c.lower(), c
    db = SessionLocal()
    try:
        assert db.query(AuthSession).filter_by(
            token_hash=token_hash(token)).first() is None
    finally:
        db.close()


def test_plain_http_keeps_the_plain_name(client):
    resp = _signup(client, "fay@example.com")
    assert SESSION_COOKIE in resp.cookies
    assert SECURE_SESSION_COOKIE not in resp.cookies


# --- Same-origin guard on the remaining cookie POSTs ---------------------------

@pytest.mark.parametrize("path,data", [
    ("/recipes/upload", {"method": "manual", "title": "x",
                         "ingredients": "1 thing", "steps": "Do it"}),
    ("/recipes/upload/confirm", {"title": "Planted by a stranger",
                                 "ingredients": "1 thing", "steps": "Do it",
                                 "attribution": "someone"}),
    ("/account/shares/sometoken/revoke", {}),
    ("/r/sometoken/save", {}),
    ("/r/sometoken/report", {}),
])
def test_cookie_posts_refuse_a_cross_origin_form(client, path, data):
    _signup(client, "gus@example.com")
    r = client.post(path, headers=EVIL, data=data, follow_redirects=False)
    assert r.status_code == 403


def test_upload_confirm_from_a_kitchen_page_saves_nothing(client, instance_token):
    from app.models import CommunityRecipe
    client.get("/v1/instance/me", headers=_auth(instance_token))
    r = client.post("/login", data={"email": "dan@example.com",
                                    "password": "hunter2222"},
                    follow_redirects=False)
    client.cookies.set(SESSION_COOKIE, r.cookies.get(SESSION_COOKIE))
    r = client.post("/recipes/upload/confirm", headers=EVIL, data={
        "title": "Planted by a stranger", "ingredients": "1 thing",
        "steps": "Do it", "attribution": "someone"}, follow_redirects=False)
    assert r.status_code == 403
    db = SessionLocal()
    try:
        assert db.query(CommunityRecipe).filter_by(
            title="Planted by a stranger").first() is None
    finally:
        db.close()


def test_same_origin_save_still_works(client):
    _signup(client, "hal@example.com")
    r = client.post("/r/sometoken/save",
                    headers={"Origin": "http://testserver"},
                    follow_redirects=False)
    assert r.status_code != 403


def test_short_lived_sign_in_cookies_use_the_host_prefix_over_tls(monkeypatch):
    # Like the session cookie, a kitchen subdomain must not be able to plant
    # a two-factor, Google or passkey cookie that steers someone's sign-in.
    from app.deps import delete_flow_cookie, flow_cookie
    from starlette.responses import Response
    monkeypatch.setattr(settings, "cookie_secure", True)
    assert flow_cookie("forager_totp_pending") == "__Host-forager_totp_pending"
    resp = Response()
    delete_flow_cookie(resp, "forager_oauth")
    header = resp.headers["set-cookie"]
    assert header.startswith("__Host-forager_oauth=")
    assert "Secure" in header and "Path=/" in header
    monkeypatch.setattr(settings, "cookie_secure", False)
    assert flow_cookie("forager_oauth") == "forager_oauth"
