"""Public-facing polish: no published API map, a real page for a mistyped
address, robots rules, an honest status page, and readable brand colors."""
import re


HTML = {"Accept": "text/html,application/xhtml+xml"}


def test_api_docs_and_schema_are_not_published(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path


def test_a_mistyped_page_gets_the_portal_page(client):
    resp = client.get("/no-such-page", headers=HTML)
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("text/html")
    assert "Nothing here" in resp.text and 'lang="en"' in resp.text


def test_the_api_keeps_json_404s(client):
    resp = client.get("/v1/nope", headers=HTML)
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}
    assert client.get("/no-such-page").json() == {"detail": "Not Found"}


def test_admin_404_matches_any_missing_page(client):
    # The panel must stay unadvertised: a browser sees the same page there
    # as anywhere else that does not exist.
    a = client.get("/admin", headers=HTML)
    b = client.get("/no-such-page", headers=HTML)
    assert a.status_code == b.status_code == 404
    assert a.text == b.text


def test_robots_keeps_private_pages_out_of_search(client):
    body = client.get("/robots.txt").text
    for path in ("/account", "/admin", "/r/", "/reset", "/v1/"):
        assert f"Disallow: {path}" in body


def test_permissions_policy_is_sent(client):
    assert "camera=()" in client.get("/").headers["permissions-policy"]


def test_status_page_reports_a_database_outage(client, monkeypatch):
    from app.routers import portal
    assert "All systems operational" in client.get("/status").text
    monkeypatch.setattr(portal, "_database_reachable", lambda: False)
    page = client.get("/status").text
    assert "All systems operational" not in page
    assert "having trouble" in page


def _luminance(hex_color):
    rgb = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
           for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _ratio(a, b):
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_brand_colors_meet_wcag_aa_contrast(client):
    css = client.get("/").text
    token = {name: value for name, value in
             re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", css)}
    for surface in ("bg", "surface", "surface-2"):
        assert _ratio(token["primary"], token[surface]) >= 4.5, surface
    assert _ratio("#ffffff", token["primary-fill"]) >= 4.5
    assert _ratio("#ffffff", token["primary-hover"]) >= 4.5


def test_two_factor_setup_shows_the_signed_in_header(client):
    client.post("/signup", data={"email": "dan@example.com",
                                 "password": "hunter2222",
                                 "confirm_password": "hunter2222"})
    page = client.get("/account/2fa/setup").text
    assert "Sign out" in page and "Start free trial" not in page
