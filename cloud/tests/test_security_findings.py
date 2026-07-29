"""Regression tests for audited cloud security findings that do not fit a
single feature file: the anonymous-reporter pepper (FoodAssistant-1k1b) and the
same-site CSRF guard on state-changing portal and admin POSTs
(FoodAssistant-cuvh)."""
import pytest

import app.config as config
from app.config import report_ip_pepper, settings
from app.security import hash_ip


# --- FoodAssistant-1k1b: anonymous-reporter pepper ------------------------

def test_hash_ip_refuses_an_empty_pepper():
    # An unsalted sha256 of an IPv4 address is trivially reversible from a
    # database dump, so hashing without a pepper is refused outright.
    with pytest.raises(ValueError):
        hash_ip("203.0.113.9", "")
    # A real pepper hashes deterministically.
    assert hash_ip("203.0.113.9", "p") == hash_ip("203.0.113.9", "p")


def test_report_ip_pepper_is_never_empty(monkeypatch):
    # A configured value wins.
    monkeypatch.setattr(settings, "report_ip_pepper", "configured-pepper")
    assert report_ip_pepper() == "configured-pepper"
    # Unset: a strong random pepper is generated (never empty) and stays stable
    # for the life of the process, so dedupe is consistent and the hash is
    # always salted.
    monkeypatch.setattr(settings, "report_ip_pepper", "")
    monkeypatch.setattr(config, "_generated_pepper", None)
    first = report_ip_pepper()
    assert first and len(first) >= 16
    assert report_ip_pepper() == first
    # And it is usable as a real pepper (hash_ip does not reject it).
    assert hash_ip("203.0.113.9", first)


def test_report_path_salts_without_a_configured_pepper(client, session_token,
                                                       monkeypatch):
    # End to end: reporting a share with no configured pepper still stores a
    # peppered "ip:" hash, never the raw address, so the finding's fix does not
    # break reporting and the stored key is not brute-forceable.
    from app.database import SessionLocal
    from app.models import SharedRecipe, SharedRecipeReport
    monkeypatch.setattr(settings, "report_ip_pepper", "")
    monkeypatch.setattr(config, "_generated_pepper", None)

    db = SessionLocal()
    try:
        from app.models import Account
        owner = db.query(Account).first()
        share = SharedRecipe(token="sharetoken123", owner_account_id=owner.id,
                             title="Soup", created_at="2026-01-01T00:00:00+00:00")
        db.add(share)
        db.commit()
    finally:
        db.close()

    resp = client.post("/r/sharetoken123/report", follow_redirects=False,
                       headers={"x-forwarded-for": "203.0.113.9"})
    assert resp.status_code in (303, 200)
    db = SessionLocal()
    try:
        row = db.query(SharedRecipeReport).first()
        assert row is not None
        assert row.reporter_key.startswith("ip:")
        assert "203.0.113.9" not in row.reporter_key  # the raw address is never stored
    finally:
        db.close()


# --- FoodAssistant-cuvh: same-site CSRF -----------------------------------
#
# TestClient serves requests on the host "testserver", so a same-origin request
# names that host and a cross-origin one names anything else. A real kitchen
# subdomain is the same site (shares pantryraider.app) but a different origin,
# which is exactly what a mismatched Origin models here.
_FOREIGN_ORIGIN = "https://evilkitchen.forager.pantryraider.app"
_SAME_ORIGIN = "http://testserver"


def _portal_session(client):
    """Sign up so the client holds a portal session cookie."""
    client.post("/signup",
                data={"email": "csrf@example.com", "password": "hunter2222",
                      "confirm_password": "hunter2222"},
                follow_redirects=False)


def test_cross_origin_post_is_rejected(client):
    _portal_session(client)
    # A different-origin page tries to drive the logged-in browser into a state
    # change with the Lax cookie attached.
    resp = client.post("/logout", follow_redirects=False,
                       headers={"origin": _FOREIGN_ORIGIN})
    assert resp.status_code == 403


def test_cross_origin_referer_is_rejected(client):
    _portal_session(client)
    resp = client.post("/logout", follow_redirects=False,
                       headers={"referer": _FOREIGN_ORIGIN + "/x"})
    assert resp.status_code == 403


def test_same_origin_post_is_allowed(client):
    _portal_session(client)
    resp = client.post("/logout", follow_redirects=False,
                       headers={"origin": _SAME_ORIGIN})
    assert resp.status_code == 303  # the normal logout redirect


def test_post_without_origin_is_allowed(client):
    # Same-origin form posts and non-browser clients send no Origin, and must
    # not be blocked (browsers always set Origin on a cross-origin POST).
    _portal_session(client)
    resp = client.post("/logout", follow_redirects=False)
    assert resp.status_code == 303


def test_admin_cross_origin_post_is_rejected(client, monkeypatch):
    # A lured admin's browser must not be driven into disabling an account from
    # a different-origin page.
    monkeypatch.setattr(settings, "admin_emails", "admin@example.com")
    client.post("/signup",
                data={"email": "admin@example.com", "password": "hunter2222",
                      "confirm_password": "hunter2222"},
                follow_redirects=False)
    from app.database import SessionLocal
    from app.models import Account
    db = SessionLocal()
    try:
        target = Account(email="victim@example.com", password_hash="",
                         created_at="2026-01-01T00:00:00+00:00")
        db.add(target)
        db.commit()
        target_id = target.id
    finally:
        db.close()
    resp = client.post(f"/admin/accounts/{target_id}/disable",
                       follow_redirects=False,
                       headers={"origin": _FOREIGN_ORIGIN})
    assert resp.status_code == 403
    # The target was not disabled.
    db = SessionLocal()
    try:
        assert db.query(Account).filter_by(id=target_id).first().disabled == 0
    finally:
        db.close()
