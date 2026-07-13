"""Signup, login, and the portal account view."""


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["app"] == "pantryraider-cloud"


def test_signup_and_me(client):
    resp = client.post("/v1/accounts/signup",
                       json={"email": "Dan@Example.com", "password": "hunter2222"})
    assert resp.status_code == 200
    token = resp.json()["session_token"]
    assert token.startswith("prs_")

    me = client.get("/v1/accounts/me",
                    headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    body = me.json()
    assert body["email"] == "dan@example.com"  # normalised
    assert body["entitlement"]["active"] is False
    assert body["instances"] == []


def test_signup_rejects_duplicates_and_bad_input(client):
    ok = {"email": "dan@example.com", "password": "hunter2222"}
    assert client.post("/v1/accounts/signup", json=ok).status_code == 200
    assert client.post("/v1/accounts/signup", json=ok).status_code == 409
    assert client.post("/v1/accounts/signup",
                       json={"email": "not-an-email", "password": "hunter2222"}
                       ).status_code == 400
    assert client.post("/v1/accounts/signup",
                       json={"email": "a@b.co", "password": "short"}
                       ).status_code == 400


def test_login(client, session_token):
    ok = client.post("/v1/accounts/login",
                     json={"email": "dan@example.com", "password": "hunter2222"})
    assert ok.status_code == 200
    assert ok.json()["session_token"].startswith("prs_")

    bad = client.post("/v1/accounts/login",
                      json={"email": "dan@example.com", "password": "wrong-pass"})
    assert bad.status_code == 401
    unknown = client.post("/v1/accounts/login",
                          json={"email": "who@example.com", "password": "wrong-pass"})
    # Same message for a wrong password and an unknown email.
    assert unknown.status_code == 401
    assert unknown.json()["detail"] == bad.json()["detail"]


def test_me_requires_valid_session(client):
    assert client.get("/v1/accounts/me").status_code == 401
    assert client.get("/v1/accounts/me",
                      headers={"Authorization": "Bearer prs_bogus"}
                      ).status_code == 401


def test_signup_rate_limit(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "signup_rate_per_minute", 2)
    for i in range(2):
        assert client.post(
            "/v1/accounts/signup",
            json={"email": f"u{i}@example.com", "password": "hunter2222"}
        ).status_code == 200
    assert client.post(
        "/v1/accounts/signup",
        json={"email": "u9@example.com", "password": "hunter2222"}
    ).status_code == 429
