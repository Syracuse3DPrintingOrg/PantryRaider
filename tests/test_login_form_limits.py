"""The login form refuses a body stuffed with thousands of junk fields.

/ui/login is public by design, so anyone who can reach the port can post to
it. FastAPI walks every key of a form body for each declared Form parameter,
and Starlette releases before 1.3.1 set no cap on how many urlencoded fields
it would parse. Twenty thousand junk fields (about 170 KB) held the single
uvicorn worker for several seconds of CPU, which froze the kiosk, the timers
and the API for everyone in the house. Starlette 1.3.1 and later stop at 1000
fields and answer 400 before the route runs; these checks fail if the pinned
Starlette ever walks that back.
"""
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.passwords import hash_secret  # noqa: E402
from app.services import rate_limit  # noqa: E402

_PW = "kitchen-secret"


@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
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
    monkeypatch.setattr(settings, "cloud_instance_token", "", raising=False)
    # A lockout left behind by an earlier login test would turn the ordinary
    # login below into a 429, so start and finish with a clean limiter.
    rate_limit.login_guard.reset("testclient")
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        rate_limit.login_guard.reset("testclient")
        os.chdir(cwd)


def test_login_rejects_more_than_1000_form_fields(client):
    body = "&".join(f"junk{i}=x" for i in range(1001))
    r = client.post(
        "/ui/login",
        content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "Too many fields" in r.text


def test_ordinary_login_post_is_not_refused(client):
    r = client.post("/ui/login", data={"mode": "local", "password": _PW},
                    follow_redirects=False)
    assert r.status_code != 400
    assert r.status_code == 303
