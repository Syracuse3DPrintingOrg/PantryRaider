"""The web portal: signup, login, the account page, and kitchen removal."""

SIGNUP = {"email": "dan@example.com", "password": "hunter2222",
          "confirm_password": "hunter2222"}
LOGIN = {"email": "dan@example.com", "password": "hunter2222"}


def portal_signup(client, data=SIGNUP):
    return client.post("/signup", data=data, follow_redirects=False)


def test_landing_page(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Forager" in resp.text
    assert "Sign up" in resp.text and "Log in" in resp.text
    assert "pantryraider.app" in resp.text
    # The free-and-open-source framing and all three pricing tiers show.
    assert "free and open source" in resp.text
    assert "Cloud Basic" in resp.text and "Premium" in resp.text
    assert "$10" in resp.text and "$3" in resp.text and "$30" in resp.text
    assert "30-day" in resp.text


def test_signup_form_logs_in_and_shows_account(client):
    resp = portal_signup(client)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/account"
    assert "forager_session" in resp.cookies

    page = client.get("/account")
    assert page.status_code == 200
    assert "dan@example.com" in page.text
    assert "Free trial" in page.text
    assert "No kitchens linked yet" in page.text


def test_signup_form_errors(client):
    bad = dict(SIGNUP, confirm_password="different99")
    resp = client.post("/signup", data=bad)
    assert resp.status_code == 400
    assert "did not match" in resp.text

    assert portal_signup(client).status_code == 303
    dup = client.post("/signup", data=SIGNUP)
    assert dup.status_code == 409
    assert "already exists" in dup.text

    short = client.post("/signup", data={"email": "b@example.com",
                                         "password": "short",
                                         "confirm_password": "short"})
    assert short.status_code == 400


def test_login_and_logout(client):
    portal_signup(client)
    client.cookies.clear()

    wrong = client.post("/login", data={"email": "dan@example.com",
                                        "password": "wrong-pass"})
    assert wrong.status_code == 401
    assert "did not match" in wrong.text

    ok = client.post("/login", data=LOGIN, follow_redirects=False)
    assert ok.status_code == 303
    assert "forager_session" in ok.cookies
    assert client.get("/account").status_code == 200

    out = client.post("/logout", follow_redirects=False)
    assert out.status_code == 303
    client.cookies.clear()
    denied = client.get("/account", follow_redirects=False)
    assert denied.status_code == 303
    assert denied.headers["location"] == "/login"


def test_logout_revokes_the_session_server_side(client):
    resp = portal_signup(client)
    token = resp.cookies["forager_session"]
    client.post("/logout")
    # Replaying the old cookie after logout must not work.
    client.cookies.set("forager_session", token)
    denied = client.get("/account", follow_redirects=False)
    assert denied.status_code == 303


def test_account_page_requires_login(client):
    resp = client.get("/account", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_account_page_speaks_plain_language(client, instance_token):
    """Dan's rule: a non-technical person must never see the words token,
    instance, or API on the portal."""
    client.post("/login", data=LOGIN)
    page = client.get("/account").text.lower()
    for word in ("token", "instance", "api"):
        assert word not in page
    # The paired install shows up as a kitchen instead.
    assert "Kitchen Pi" in client.get("/account").text


def test_account_page_billing_is_honest_when_unset(client, monkeypatch):
    from app.config import settings
    portal_signup(client)
    monkeypatch.setattr(settings, "stripe_checkout_url", "")
    assert "Billing is not live yet" in client.get("/account").text
    monkeypatch.setattr(settings, "stripe_checkout_url",
                        "https://buy.stripe.com/test_123")
    page = client.get("/account").text
    assert "Billing is not live yet" not in page
    assert "https://buy.stripe.com/test_123" in page


def test_change_password(client):
    portal_signup(client)

    wrong = client.post("/account/password",
                        data={"current_password": "nope-nope-nope",
                              "new_password": "newpass9999",
                              "confirm_password": "newpass9999"},
                        follow_redirects=False)
    assert wrong.headers["location"] == "/account?e=password-wrong"

    ok = client.post("/account/password",
                     data={"current_password": "hunter2222",
                           "new_password": "newpass9999",
                           "confirm_password": "newpass9999"},
                     follow_redirects=False)
    assert ok.headers["location"] == "/account?m=password-changed"
    assert "updated" in client.get("/account?m=password-changed").text

    # Old password is dead, new one works.
    assert client.post("/v1/accounts/login", json=LOGIN).status_code == 401
    assert client.post("/v1/accounts/login",
                       json={"email": "dan@example.com",
                             "password": "newpass9999"}).status_code == 200


def test_remove_kitchen_revokes_its_credential(client, instance_token):
    client.post("/login", data=LOGIN)
    page = client.get("/account")
    assert "Kitchen Pi" in page.text

    from app.database import SessionLocal
    from app.models import Instance
    db = SessionLocal()
    kitchen_id = db.query(Instance).first().id
    db.close()

    resp = client.post(f"/account/kitchens/{kitchen_id}/remove",
                       follow_redirects=False)
    assert resp.headers["location"] == "/account?m=kitchen-removed"
    assert "Kitchen Pi" not in client.get("/account").text
    # The device's credential died with the row.
    denied = client.get("/v1/instance/me",
                        headers={"Authorization": f"Bearer {instance_token}"})
    assert denied.status_code == 401


def test_remove_kitchen_is_scoped_to_the_account(client, instance_token):
    from app.database import SessionLocal
    from app.models import Instance
    db = SessionLocal()
    kitchen_id = db.query(Instance).first().id
    db.close()

    # A second account cannot remove the first account's kitchen.
    client.post("/signup", data={"email": "eve@example.com",
                                 "password": "eviltwin99",
                                 "confirm_password": "eviltwin99"})
    client.post(f"/account/kitchens/{kitchen_id}/remove")
    ok = client.get("/v1/instance/me",
                    headers={"Authorization": f"Bearer {instance_token}"})
    assert ok.status_code == 200


def _set_kitchen_web_address(name, url):
    """Stand in for enabling remote access: put a kitchen's public web address
    on its instance row, the field the tunnel flow sets when remote access is
    turned on."""
    from app.database import SessionLocal
    from app.models import Instance
    db = SessionLocal()
    try:
        inst = db.query(Instance).filter_by(name=name).first()
        inst.public_url = url
        db.commit()
    finally:
        db.close()


def test_account_shows_kitchen_web_address_when_remote_access_is_on(
        client, instance_token):
    client.post("/login", data=LOGIN)
    _set_kitchen_web_address(
        "Kitchen Pi", "https://kitchen-pi.forager.pantryraider.app")
    page = client.get("/account").text
    # The full https address renders as a link, plus a copy control.
    assert "https://kitchen-pi.forager.pantryraider.app" in page
    assert 'href="https://kitchen-pi.forager.pantryraider.app"' in page
    assert 'target="_blank"' in page
    assert "Copy" in page


def test_account_shows_empty_state_when_no_remote_access(client, instance_token):
    client.post("/login", data=LOGIN)
    page = client.get("/account").text
    assert "Kitchen Pi" in page
    assert "Remote access is not set up for this kitchen" in page


def test_account_shows_each_kitchen_web_address(client, session_token):
    """Two linked kitchens each show their own web address."""
    for name in ("Kitchen Pi", "Cabin"):
        code = client.post("/v1/pairing/code",
                           headers={"Authorization": f"Bearer {session_token}"})
        client.post("/v1/pairing/redeem",
                    json={"code": code.json()["code"], "name": name})
    _set_kitchen_web_address(
        "Kitchen Pi", "https://kitchen-pi.forager.pantryraider.app")
    _set_kitchen_web_address(
        "Cabin", "https://cabin.forager.pantryraider.app")
    client.post("/login", data=LOGIN)
    page = client.get("/account").text
    assert "https://kitchen-pi.forager.pantryraider.app" in page
    assert "https://cabin.forager.pantryraider.app" in page


def test_portal_login_rate_limit(client, monkeypatch):
    from app.config import settings
    portal_signup(client)
    client.cookies.clear()
    monkeypatch.setattr(settings, "login_rate_per_minute", 2)
    for _ in range(2):
        assert client.post("/login", data={"email": "dan@example.com",
                                           "password": "wrong"}
                           ).status_code == 401
    limited = client.post("/login", data=LOGIN)
    assert limited.status_code == 429
    assert "Wait a minute" in limited.text


def test_portal_signup_rate_limit(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "signup_rate_per_minute", 1)
    assert portal_signup(client).status_code == 303
    limited = client.post("/signup", data={"email": "b@example.com",
                                           "password": "hunter2222",
                                           "confirm_password": "hunter2222"})
    assert limited.status_code == 429
