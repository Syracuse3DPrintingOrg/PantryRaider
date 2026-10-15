"""The access banner (templates/_access_banner.html): the open-install notice,
the grace-period banner for visitors through a proxy or an unknown name, and
the Docker-gateway hint, on base.html pages and on the standalone Start Page.

access.banner_context is replaced per test, so these cover what the pages
show for each answer, not how the access rules reach it."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from markupsafe import escape

_SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(_SERVICE))

from app.config import settings  # noqa: E402
from app.services import access  # noqa: E402
from page_scripts import with_page_scripts

PAGES = ("/ui/timers", "/ui/start")
OPEN_TEXT = "No password: anyone on your home network"


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app
    cwd = os.getcwd()
    os.chdir(_SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "auth_password", "")
    monkeypatch.setattr(settings, "grocy_base_url", "http://g")
    monkeypatch.setattr(settings, "grocy_api_key", "k")
    monkeypatch.setattr(settings, "demo_mode", False)
    monkeypatch.setattr(settings, "start_page_enabled", True)
    monkeypatch.setattr(settings, "timezone", "UTC")
    try:
        yield TestClient(app, client=("192.168.1.20", 50000))
    finally:
        os.chdir(cwd)


def _banner(monkeypatch, **kw):
    ctx = {"open_notice": False, "grace_until": None, "grace_address": "",
           "grace_kind": None, "gateway_peer": False}
    ctx.update(kw)
    monkeypatch.setattr(access, "banner_context", lambda request: dict(ctx))


def _get(client, path):
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code)
    return r.text


def _banner_html(page: str) -> str:
    start = page.index('id="pr-access-banner"')
    end = page.index("</script>", start)
    return page[start:end]


def test_open_notice_on_base_and_start_pages(client, monkeypatch):
    _banner(monkeypatch, open_notice=True)
    for path in PAGES:
        page = with_page_scripts(_get(client, path))
        assert OPEN_TEXT in page, path
        assert 'id="pr-open-notice"' in page
        assert 'href="setup#pane-security"' in page
        assert 'role="status"' in page
        assert 'aria-label="Dismiss"' in page
        # The dismiss script stores a 30-day expiry and survives blocked storage.
        assert "prOpenNoticeUntil" in page
        assert 'data-pr-dismiss-days="30"' in page
        assert "try {\n      return Number(window.localStorage.getItem(key))" in page
        assert "try {\n      window.localStorage.setItem(key" in page
        assert "pr-grace-banner" not in page


def test_base_pages_pin_the_banner_and_start_keeps_it_in_flow(client, monkeypatch):
    _banner(monkeypatch, open_notice=True)
    base_page = _get(client, "/ui/timers")
    assert "pr-access-banner-fixed" in base_page
    assert 'class="pr-access-spacer"' in base_page
    # The banner comes before the navbar it shifts.
    assert base_page.index('id="pr-access-banner"') < base_page.index('<nav class="navbar')
    # What else sits just under the navbar moves down with it.
    assert "body.access-banner-on .float-nav-dock-left" in base_page
    assert "body.access-banner-on .subpill-bar" in base_page
    assert "body.access-banner-on .pr-pin-readonly" in base_page
    start_page = _get(client, "/ui/start")
    assert "pr-access-banner-flow" in start_page
    assert 'class="pr-access-spacer"' not in start_page
    assert start_page.index('id="pr-access-banner"') < start_page.index('class="start-wrap')


def test_nothing_renders_with_a_password(client, monkeypatch):
    _banner(monkeypatch)
    for path in PAGES:
        page = _get(client, path)
        assert "pr-open-notice" not in page
        assert "pr-grace-banner" not in page
        assert 'id="pr-access-banner"' not in page
        assert OPEN_TEXT not in page


def test_grace_banner_through_a_proxy_asks_for_two_factor(client, monkeypatch):
    _banner(monkeypatch, open_notice=True, grace_until="2026-10-14T00:00:00+00:00",
            grace_kind="proxied", grace_address="pantry.example.com")
    for path in PAGES:
        page = _get(client, path)
        assert 'id="pr-grace-banner"' in page
        assert 'role="alert"' in page
        assert "2026-10-14" in page
        assert "pantry.example.com" in page
        assert "two-factor" in page
        assert "Signing in from outside needs both." in page
        assert "AUTH_PASSWORD" not in page
        assert "prGraceBannerUntil" in page
        assert 'data-pr-dismiss-days="1"' in page
        assert "Open Settings, Security" in page
        text = _banner_html(page)
        # The grace text never offers a password alone for access from outside.
        assert "set a password." not in text
        assert "Other names for this kitchen" not in text


def test_grace_banner_with_a_z_suffixed_time(client, monkeypatch):
    _banner(monkeypatch, open_notice=True, grace_until="2026-10-14T12:00:00Z",
            grace_kind="public", grace_address="198.51.100.7")
    page = _get(client, "/ui/timers")
    assert "from 2026-10-14 it will stop answering at" in page
    assert "198.51.100.7" in page
    assert "two-factor" in page


def test_grace_banner_without_an_address_names_the_proxy(client, monkeypatch):
    _banner(monkeypatch, open_notice=True, grace_until="2026-10-14T00:00:00+00:00",
            grace_kind="proxied", grace_address="")
    for path in PAGES:
        page = _get(client, path)
        assert "requests that come through a proxy or tunnel" in page
        assert "stop answering at" not in page
        assert "two-factor" in page


def test_grace_banner_for_an_unknown_name(client, monkeypatch):
    _banner(monkeypatch, open_notice=True, grace_until="2026-10-14T00:00:00+00:00",
            grace_kind="name", grace_address="pantry.fritz.box")
    for path in PAGES:
        page = _get(client, path)
        assert "pantry.fritz.box" in page
        assert "Other names for this kitchen" in page
        assert "To keep using this name" in page
        assert "two-factor" not in _banner_html(page)


def test_grace_banner_for_unknown_names_seen_by_others(client, monkeypatch):
    _banner(monkeypatch, open_notice=True, grace_until="2026-10-14T00:00:00+00:00",
            grace_kind="name", grace_address="")
    page = _get(client, "/ui/timers")
    assert "names that are not on your home network" in page
    assert "Other names for this kitchen" in page


def test_unreadable_grace_date_still_reads_as_a_sentence(client, monkeypatch):
    _banner(monkeypatch, grace_until="not a date", grace_kind="proxied",
            grace_address="")
    page = _get(client, "/ui/timers")
    assert "so soon it will stop answering requests" in page
    assert "from  it" not in page


def test_gateway_hint_on_unraid_skips_the_compose_advice(client, monkeypatch):
    monkeypatch.setenv("HOST_OS", "Unraid")
    _banner(monkeypatch, open_notice=True, gateway_peer=True)
    for path in PAGES:
        page = _get(client, path)
        assert str(escape(access.GATEWAY_HINT_UNRAID)) in page
        assert "0.0.0.0:9284:8000" not in page


def test_gateway_hint_comes_from_access(client, monkeypatch):
    assert "0.0.0.0:9284:8000" in access.GATEWAY_HINT
    _banner(monkeypatch, open_notice=True, gateway_peer=True)
    for path in PAGES:
        page = _get(client, path)
        hint = str(escape(access.GATEWAY_HINT))
        assert f'<div class="pr-gateway-hint mt-1">{hint}</div>' in page
        # It sits inside the open notice, so its Dismiss hides it too.
        notice = page[page.index('id="pr-open-notice"'):]
        notice = notice[:notice.index('aria-label="Dismiss"')]
        assert hint in notice
    _banner(monkeypatch, open_notice=True, gateway_peer=False)
    for path in PAGES:
        page = _get(client, path)
        assert "0.0.0.0:9284:8000" not in page
        assert "pr-gateway-hint mt-1" not in page


def test_banner_failure_never_breaks_a_page(client, monkeypatch):
    def boom(request):
        raise RuntimeError("banner state unreadable")
    monkeypatch.setattr(access, "banner_context", boom)
    for path in PAGES:
        page = _get(client, path)
        assert 'id="pr-access-banner"' not in page
        assert OPEN_TEXT not in page


def test_demo_mode_shows_only_the_demo_banner(client, monkeypatch):
    _banner(monkeypatch, open_notice=True, grace_until="2026-10-14T00:00:00+00:00",
            grace_kind="proxied", grace_address="pantry.example.com")
    monkeypatch.setattr(settings, "demo_mode", True)
    page = _get(client, "/ui/timers")
    assert 'id="pr-access-banner"' not in page
    assert OPEN_TEXT not in page
    assert 'id="demoBanner"' in page


def test_address_is_escaped(client, monkeypatch):
    _banner(monkeypatch, open_notice=True, grace_until="2026-10-14T00:00:00+00:00",
            grace_kind="proxied", grace_address='<script>alert(1)</script>')
    for path in PAGES:
        page = _get(client, path)
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_no_em_dash_in_banner_text(client, monkeypatch):
    for kind, addr in (("proxied", "pantry.example.com"), ("proxied", ""),
                       ("name", "pantry.fritz.box"), ("name", "")):
        _banner(monkeypatch, open_notice=True, gateway_peer=True,
                grace_until="2026-10-14T00:00:00+00:00", grace_kind=kind,
                grace_address=addr)
        for path in PAGES:
            text = _banner_html(_get(client, path))
            assert "\u2014" not in text and "&mdash;" not in text


def test_theme_context_access_banner_without_a_request():
    # A render outside a real request (tests and tools build a bare object)
    # still gets every key, and a failure means no banner.
    from app import templating

    class _Req:
        class state:
            pass
    ctx = templating.theme_context(_Req())
    ab = ctx["access_banner"]
    assert set(ab) == {"open_notice", "grace_until", "grace_address", "grace_kind",
                       "gateway_peer", "gateway_hint", "grace_date_display"}


def test_access_banner_defaults_when_banner_context_fails(monkeypatch):
    from app import templating

    def boom(request):
        raise ValueError("no state")
    monkeypatch.setattr(access, "banner_context", boom)
    assert templating._access_banner(object()) == {
        "open_notice": False, "grace_until": None, "grace_address": None,
        "grace_kind": None, "gateway_peer": False, "gateway_hint": "",
        "grace_date_display": ""}


def test_access_banner_passes_grace_kind_and_formats_the_date(monkeypatch):
    from app import templating
    monkeypatch.setattr(settings, "timezone", "UTC")
    monkeypatch.setattr(access, "banner_context", lambda request: {
        "open_notice": True, "grace_until": "2026-10-14T00:00:00Z",
        "grace_address": "", "grace_kind": "name", "gateway_peer": True})
    ab = templating._access_banner(object())
    assert ab["grace_kind"] == "name"
    assert ab["grace_date_display"] == "2026-10-14"
    assert ab["gateway_hint"] == access.GATEWAY_HINT
    assert ab["grace_address"] is None



def _lan_request(path="/ui/timers"):
    from starlette.requests import Request
    return Request({"type": "http", "method": "GET", "path": path,
                    "root_path": "", "query_string": b"", "scheme": "http",
                    "headers": [(b"host", b"192.168.1.5:9284")],
                    "client": ("192.168.1.20", 50000),
                    "server": ("192.168.1.5", 9284)})


def test_real_banner_context_drives_the_pages(client, monkeypatch):
    # No stand-in here: the access rules themselves decide. An open install
    # seen from the home network gets the notice on every page.
    for path in PAGES:
        assert OPEN_TEXT in _get(client, path), path
    from app import templating
    assert templating._access_banner(_lan_request())["open_notice"] is True
    # Once a password is set, the same request gets no banner at all.
    monkeypatch.setattr(settings, "auth_password", "a-long-kitchen-password")
    ab = templating._access_banner(_lan_request())
    assert ab["open_notice"] is False
    assert ab["grace_until"] is None
    assert ab["gateway_hint"] == ""
