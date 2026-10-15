"""Paired keys nobody used: the inbox offers to remove them (FoodAssistant-x60df).

A key from an approved pairing that no request has used a day later gets one
inbox item with a Remove key button. Nothing is ever removed on its own (a
device slow to connect for the first time must keep its key), a key whose
device connects later keeps it and its item leaves the inbox, and the button
is an admin action that keeps a key used since the item appeared.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from app.config import settings
from app.database import SessionLocal
from app.models.db_models import ActionItem
from app.passwords import hash_secret
from app.services import action_items as ai
from app.services import credentials

SERVICE = Path(__file__).resolve().parents[1] / "service"
DAY = 86400.0


@pytest.fixture(autouse=True)
def _clean_table():
    db = SessionLocal()
    db.query(ActionItem).delete()
    db.commit()
    db.close()
    yield
    db = SessionLocal()
    db.query(ActionItem).delete()
    db.commit()
    db.close()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "api_key", "", raising=False)
    monkeypatch.setattr(settings, "extra_api_keys", [], raising=False)
    monkeypatch.setattr(settings, "extra_api_key_names", [], raising=False)
    monkeypatch.setattr(settings, "legacy_mirror", True, raising=False)
    credentials.reset(None, persist=True)
    yield tmp_path
    credentials.reset(persist=False)


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def client(store, monkeypatch):
    monkeypatch.chdir(SERVICE)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True, raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _paired(name="kitchen-pi", scope="satellite", age=2 * DAY):
    return credentials.mint(scope, name, "paired", created_by="pairing", now=time.time() - age)


def _offers(db):
    return [i for i in ai.list_active(db) if i["kind"] == ai.KIND_PAIRED_KEY_UNUSED]


def _row(db, cid):
    db.expire_all()
    return (db.query(ActionItem).filter(ActionItem.kind == ai.KIND_PAIRED_KEY_UNUSED)
            .filter(ActionItem.payload.contains(cid)).one())


def _password(monkeypatch, viewer=""):
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret("hunter2"), raising=False)
    monkeypatch.setattr(settings, "totp_secret", "", raising=False)
    monkeypatch.setattr(settings, "viewer_password", hash_secret(viewer) if viewer else "",
                        raising=False)


# --- Which keys are offered -----------------------------------------------------

def test_only_paired_keys_unused_a_day_after_approval_are_offered(store):
    old, _ = _paired("old-pi")
    _paired("young-pi", age=3600)
    used, _ = _paired("used-pi")
    credentials.record_use(used["id"], {"ip": "192.168.1.30"})
    refused, _ = _paired("refused-cub", scope="display")
    credentials.record_refusal(refused["id"], "/timers")
    gone, _ = _paired("gone-pi")
    credentials.revoke(gone["id"])
    credentials.mint("satellite", "made-by-hand", "manual", now=time.time() - 9 * DAY)
    assert [k["id"] for k in credentials.unused_paired_keys()] == [old["id"]]
    # The day counts from approval, when the key was minted.
    young = next(r for r in credentials.all_records() if r["name"] == "young-pi")
    later = credentials.unused_paired_keys(now=time.time() + DAY)
    assert {k["id"] for k in later} == {old["id"], young["id"]}


@pytest.fixture
def pairing_state():
    from app.services import ha_events, pairing
    pairing.reset()
    yield
    pairing.reset()
    ha_events.reset()       # the request's on-screen toast


def test_an_approved_pairing_that_never_connects_is_offered_a_day_later(
        client, monkeypatch, db, pairing_state):
    monkeypatch.setattr(settings, "local_device_pairing_enabled", True, raising=False)
    monkeypatch.setattr("app.routers.pairing.pairing.is_local_network_request",
                        lambda request: True)
    asked = client.post("/api/pairing/request", json={"hostname": "pantry-bandit"}).json()
    approved = client.post("/api/pairing/approve", json={"request_id": asked["request_id"]})
    assert approved.status_code == 200 and approved.json()["ok"]
    assert credentials.unused_paired_keys() == []
    assert ai.refresh_unused_paired_keys(db) == 0
    assert ai.refresh_unused_paired_keys(db, now=time.time() + DAY + 60) == 1
    [offer] = _offers(db)
    assert offer["title"] == "pantry-bandit has not connected yet"


# --- The inbox item --------------------------------------------------------------

def test_one_offer_per_key_that_names_the_device_and_the_key(store, db):
    rec, _ = _paired()
    assert ai.refresh_unused_paired_keys(db) == 1
    assert ai.refresh_unused_paired_keys(db) == 1
    [offer] = _offers(db)
    assert offer["title"] == "kitchen-pi has not connected yet"
    assert offer["dedupe_key"] == ai.paired_key_dedupe_key(rec["id"])
    assert offer["payload"] == {"credential_id": rec["id"], "name": "kitchen-pi"}
    assert offer["level"] == "info"
    body = offer["body"]
    assert body.startswith("You approved kitchen-pi on ")
    assert "remove the key" in body and "goes away once the device connects" in body
    assert "—" not in body and "–" not in body


def test_the_approval_date_reads_like_a_date():
    this_year = time.localtime().tm_year
    assert ai._approved_on(f"{this_year}-03-04T12:00:00Z").startswith("March ")
    assert ai._approved_on("2001-03-04T12:00:00Z").endswith(", 2001")
    assert ai._approved_on("not a date") == ""
    assert ai._approved_on(None) == ""


def test_nothing_is_removed_on_its_own(store, db):
    rec, secret = _paired()
    for days in (1, 30, 400):
        ai.refresh_unused_paired_keys(db, now=time.time() + days * DAY)
        credentials.retirement_pass(now=time.time() + days * DAY)
    assert credentials.lookup(secret)["id"] == rec["id"]
    assert credentials.get(rec["id"])["status"] == "active"
    assert len(_offers(db)) == 1


def test_a_key_used_later_drops_its_offer(store, db):
    rec, _ = _paired()
    ai.refresh_unused_paired_keys(db)
    credentials.record_use(rec["id"], {"ip": "192.168.1.30"})
    assert ai.refresh_unused_paired_keys(db) == 0
    assert _offers(db) == []
    row = _row(db, rec["id"])
    assert row.status == "archived" and row.dedupe_key is None
    ai.refresh_unused_paired_keys(db)
    assert _offers(db) == []


def test_a_revoked_key_drops_its_offer(store, db):
    rec, _ = _paired()
    ai.refresh_unused_paired_keys(db)
    credentials.revoke(rec["id"])
    ai.refresh_unused_paired_keys(db)
    assert _offers(db) == []


def test_an_archived_offer_stays_archived_while_the_key_is_unused(store, db):
    _paired()
    ai.refresh_unused_paired_keys(db)
    [offer] = _offers(db)
    ai.archive(db, offer["id"])
    ai.refresh_unused_paired_keys(db)
    assert _offers(db) == []


def test_the_inbox_drops_the_offer_once_the_device_connects(client, store, db, monkeypatch):
    import app.routers.action_items as router

    async def no_grocy(_db):
        return 0

    monkeypatch.setattr(ai, "refresh_food_expired", no_grocy)
    monkeypatch.setattr(router, "_last_refresh", -1e18, raising=False)
    rec, _ = _paired()
    listed = client.get("/action-items").json()
    assert [i["kind"] for i in listed["items"]] == [ai.KIND_PAIRED_KEY_UNUSED]
    credentials.record_use(rec["id"], {"ip": "192.168.1.30"})
    monkeypatch.setattr(router, "_last_refresh", -1e18, raising=False)
    assert client.get("/action-items").json()["count"] == 0


def test_the_hourly_pass_raises_the_offer(store, db, monkeypatch):
    from app import main
    from app.services import local_agents
    monkeypatch.setattr(local_agents, "bridge_present", lambda: False)
    _paired()
    asyncio.run(main._access_housekeeping_pass())
    assert [o["title"] for o in _offers(db)] == ["kitchen-pi has not connected yet"]


# --- The Remove key button ---------------------------------------------------------

def test_remove_takes_an_unused_key_away_and_closes_the_offer(client, db):
    rec, secret = _paired()
    ai.refresh_unused_paired_keys(db)
    r = client.post(f"/api/credentials/{rec['id']}/remove-unused")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert credentials.lookup(secret) is None
    assert settings.extra_api_keys == []          # its copy for older versions went too
    assert _offers(db) == []
    assert _row(db, rec["id"]).status == "done"
    listed = {c["id"] for c in client.get("/api/credentials").json()["credentials"]}
    assert rec["id"] not in listed


def test_remove_keeps_a_key_whose_device_connected_since(client, db):
    rec, secret = _paired()
    ai.refresh_unused_paired_keys(db)
    credentials.record_use(rec["id"], {"ip": "192.168.1.30"})
    r = client.post(f"/api/credentials/{rec['id']}/remove-unused")
    assert r.status_code == 409
    assert r.json() == {"ok": False, "used": True,
                        "error": "kitchen-pi has connected since, so its key was kept."}
    assert credentials.lookup(secret)["id"] == rec["id"]
    assert _offers(db) == []
    assert _row(db, rec["id"]).status == "done"


def test_remove_touches_only_paired_keys(client):
    manual, secret = credentials.mint("satellite", "made-by-hand", "manual",
                                      now=time.time() - 9 * DAY)
    r = client.post(f"/api/credentials/{manual['id']}/remove-unused")
    assert r.status_code == 400 and r.json()["ok"] is False
    assert credentials.lookup(secret) is not None
    assert client.post("/api/credentials/c_ffffffffffffffff/remove-unused").status_code == 404


def test_remove_is_for_the_admin_only(client, monkeypatch):
    rec, secret = _paired()
    _password(monkeypatch, viewer="kitchen")
    assert client.post(f"/api/credentials/{rec['id']}/remove-unused").status_code == 401
    _sat, sat_key = credentials.mint("satellite", "another-bandit", "paired")
    r = client.post(f"/api/credentials/{rec['id']}/remove-unused", headers={"X-API-Key": sat_key})
    assert r.status_code == 403
    client.post("/ui/login", data={"password": "kitchen"}, follow_redirects=False)
    assert client.post(f"/api/credentials/{rec['id']}/remove-unused").status_code == 403
    assert credentials.lookup(secret) is not None
    client.cookies.clear()
    client.post("/ui/login", data={"password": "hunter2"}, follow_redirects=False)
    r = client.post(f"/api/credentials/{rec['id']}/remove-unused",
                    headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert credentials.lookup(secret) is not None
    r = client.post(f"/api/credentials/{rec['id']}/remove-unused",
                    headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200
    assert credentials.lookup(secret) is None


def test_remove_refuses_another_site_on_a_kitchen_with_no_password(client):
    rec, secret = _paired()
    r = client.post(f"/api/credentials/{rec['id']}/remove-unused",
                    headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert credentials.lookup(secret) is not None


def test_the_pending_page_offers_the_button_only_to_who_can_use_it(client, monkeypatch):
    page = client.get("/ui/pending")
    assert page.status_code == 200 and 'data-manages-keys="1"' in page.text
    _password(monkeypatch, viewer="kitchen")
    client.post("/ui/login", data={"password": "kitchen"}, follow_redirects=False)
    page = client.get("/ui/pending")
    assert page.status_code == 200 and "data-manages-keys" not in page.text
    assert 'id="action-items-section"' in page.text
    client.cookies.clear()
    client.post("/ui/login", data={"password": "hunter2"}, follow_redirects=False)
    assert 'data-manages-keys="1"' in client.get("/ui/pending").text
    # A Bandit's inbox is its main server's, and the keys live there.
    from app.routers import ui
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    assert ui._may_manage_keys(None) is False


def test_the_inbox_script_wires_the_button_to_the_admin_route():
    js = (SERVICE / "app" / "static" / "js" / "pages" / "pending.js").read_text()
    assert "it.kind === 'paired_key_unused'" in js
    assert "section.dataset.managesKeys === '1'" in js
    assert "'api/credentials/' + encodeURIComponent(p.credential_id) + '/remove-unused'" in js
    assert "fetch('/" not in js
