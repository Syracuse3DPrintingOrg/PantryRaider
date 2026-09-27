"""The kiosk boot flag must survive the home-page redirect (FoodAssistant-r4kt).

The appliance's kiosk service launches http://localhost/ui/?kiosk=1. That query
flag is the only thing that latches kiosk mode in the browser, and the panel's
touch CSS and safe-area insets both hang off that latch. /ui/ redirects to
whichever page leads the nav (the Glance start page by default), so if the
redirect drops the query, the page that runs the script never sees the flag: the
panel renders as a plain desktop browser with content off the edges, and the
install-the-app bar offers to install the app onto the app.

Seen live on the Bandit twice; it only bites a panel whose browser storage has
been cleared, which is why it looks intermittent.
"""
import os
import sys

import pytest

SERVICE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service")
sys.path.insert(0, SERVICE)

from app.config import settings  # noqa: E402


@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    # An unconfigured install answers everything with the setup wizard.
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "test-key", raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_kiosk_flag_survives_the_home_redirect(client):
    r = client.get("/ui/?kiosk=1", follow_redirects=False)
    if r.status_code == 200:
        pytest.skip("this install renders the dashboard in place; no redirect to carry")
    assert r.status_code in (302, 303, 307)
    assert "kiosk=1" in r.headers.get("location", ""), (
        "the kiosk boot flag was dropped by the home-page redirect; the panel "
        "will come up as a desktop browser")


def test_home_redirect_keeps_other_query_params(client):
    """Not kiosk-specific: the redirect must not eat query strings generally."""
    r = client.get("/ui/?msg=hello&msg_type=success", follow_redirects=False)
    if r.status_code == 200:
        pytest.skip("no redirect on this install")
    loc = r.headers.get("location", "")
    assert "msg=hello" in loc and "msg_type=success" in loc


def test_plain_home_still_redirects_cleanly(client):
    """No query, no stray "?" on the end."""
    r = client.get("/ui/", follow_redirects=False)
    if r.status_code == 200:
        pytest.skip("no redirect on this install")
    assert not r.headers.get("location", "").endswith("?")
