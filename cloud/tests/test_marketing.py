"""The marketing and status pages, plus the admin stats panel."""
import re

from fastapi.testclient import TestClient

from app.config import CLOUD_VERSION, settings
from app.main import app

ADMIN = {"email": "dan@example.com", "password": "hunter2222",
         "confirm_password": "hunter2222"}


def test_pricing_page_renders_all_the_plans(client):
    resp = client.get("/pricing")
    assert resp.status_code == 200
    # Every literal pricing string the marketing copy promises.
    assert "Cloud Basic" in resp.text and "Premium" in resp.text
    assert "$10" in resp.text and "$3" in resp.text and "$30" in resp.text
    assert "30-day" in resp.text
    # The how-it-works framing is on the page too.
    assert "How it works" in resp.text


def test_features_page_renders_how_it_works_and_features(client):
    resp = client.get("/features")
    assert resp.status_code == 200
    assert "How it works" in resp.text
    # The privacy line: photos pass through and are never stored.
    assert "never stored" in resp.text
    # Pricing is reachable from here as well.
    assert "Cloud Basic" in resp.text and "Premium" in resp.text


def test_status_page_is_operational_and_shows_version(client):
    resp = client.get("/status")
    assert resp.status_code == 200
    assert "All systems operational" in resp.text
    assert CLOUD_VERSION in resp.text
    # No per-account data leaks onto the public status page (the support
    # mailto in the shared footer is not private data).
    low = resp.text.lower()
    for word in ("dan@example.com", "kitchen pi", "usage", "ledger"):
        assert word not in low


def test_status_page_hides_private_data_even_when_signed_in(client):
    client.post("/signup", data=ADMIN)
    resp = client.get("/status")
    assert resp.status_code == 200
    assert "dan@example.com" not in resp.text


def test_admin_stats_is_gated_for_anonymous_and_non_admin(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_emails", "someone-else@example.com")
    assert client.get("/admin/stats").status_code == 404
    client.post("/signup", data=ADMIN)  # signed in, but not allowlisted
    assert client.get("/admin/stats").status_code == 404


def test_admin_stats_shows_numbers_for_admin(monkeypatch):
    monkeypatch.setattr(settings, "admin_emails", "dan@example.com")
    c = TestClient(app)
    assert c.post("/signup", data=ADMIN,
                  follow_redirects=False).status_code == 303
    # A second account, signed up on its own client so the admin's session
    # cookie is not replaced.
    TestClient(app).post("/signup", data={"email": "eve@example.com",
                                          "password": "eviltwin99",
                                          "confirm_password": "eviltwin99"})
    page = c.get("/admin/stats")
    assert page.status_code == 200
    assert "Total accounts" in page.text
    assert "Verified accounts" in page.text
    assert "Accounts by plan" in page.text
    # Two accounts, both fresh signups, so both on the trial plan.
    m = re.search(r'Total accounts</div>\s*<div class="value">(\d+)</div>',
                  page.text)
    assert m and m.group(1) == "2"
    assert "trial" in page.text
