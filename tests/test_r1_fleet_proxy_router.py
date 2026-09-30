"""The satellite proxy hop (routers/proxy.py) with its path policy.

Drives /api/proxy through a TestClient with the upstream httpx call stubbed,
covering: allowed calls still forward (rebuilt from validated segments, a
barcode with a space intact, repeated query keys kept), an older satellite
that sends no X-PR-Client header still works, account and admin paths are
refused in every mode, off-list paths forward in report mode and are refused
under enforce, only refusals reach the action inbox (once per day), and the
owner's GET /admin/proxy-paths review.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.routers import proxy  # noqa: E402
from app.services import proxy_policy  # noqa: E402

KEY = "primary-secret"
EXTRA = "kitchen-secret"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "api_key", KEY, raising=False)
    monkeypatch.setattr(settings, "extra_api_keys", [EXTRA], raising=False)
    monkeypatch.setattr(settings, "extra_api_key_names", ["Kitchen Pi"], raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "grocy-admin-key", raising=False)
    monkeypatch.setattr(settings, "mealie_base_url", "http://mealie.test", raising=False)
    monkeypatch.setattr(settings, "mealie_api_key", "mealie-admin-key", raising=False)
    monkeypatch.setattr(settings, "proxy_path_policy", "report", raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    proxy_policy.reset()
    proxy._refusals_noted.clear()
    proxy._warned.clear()
    proxy._refusal_counts.clear()
    upstream = AsyncMock(return_value=httpx.Response(
        200, json={"ok": True}, headers={"content-type": "application/json"}))
    monkeypatch.setattr(proxy._client, "request", upstream)
    _clear_items()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(proxy.router)
    app.include_router(proxy.admin_router)
    yield TestClient(app), upstream
    proxy_policy.reset()
    _clear_items()


def _clear_items():
    from app.database import SessionLocal
    from app.models.db_models import ActionItem
    db = SessionLocal()
    try:
        db.query(ActionItem).filter(ActionItem.kind == "proxy_refused").delete()
        db.commit()
    finally:
        db.close()


def _refusal_items():
    from app.database import SessionLocal
    from app.models.db_models import ActionItem
    db = SessionLocal()
    try:
        return [(r.title, r.body, r.dedupe_key)
                for r in db.query(ActionItem).filter(ActionItem.kind == "proxy_refused")]
    finally:
        db.close()


def _hdr(key=KEY, client="satellite/0.20.3"):
    h = {"X-API-Key": key}
    if client:
        h["X-PR-Client"] = client
    return h


# --- unchanged gate ----------------------------------------------------------

def test_key_is_still_required(env):
    client, upstream = env
    assert client.get("/api/proxy/grocy/api/stock").status_code == 401
    assert client.get("/api/proxy/grocy/api/stock",
                      headers={"X-API-Key": "nope"}).status_code == 401
    upstream.assert_not_called()


# --- allowed traffic ---------------------------------------------------------

def test_allowed_call_forwards_with_server_credentials(env):
    client, upstream = env
    r = client.get("/api/proxy/grocy/api/stock", headers=_hdr())
    assert r.status_code == 200 and r.json() == {"ok": True}
    method, url = upstream.call_args.args
    assert (method, url) == ("GET", "http://grocy.test/api/stock")
    assert upstream.call_args.kwargs["headers"]["GROCY-API-KEY"] == "grocy-admin-key"


def test_barcode_with_space_is_rebuilt_as_one_segment(env):
    client, upstream = env
    r = client.post("/api/proxy/grocy/api/stock/products/by-barcode/4006381%20333931/consume",
                    headers=_hdr(), json={"amount": 1})
    assert r.status_code == 200
    assert upstream.call_args.args[1] == \
        "http://grocy.test/api/stock/products/by-barcode/4006381%20333931/consume"


def test_repeated_query_keys_are_kept(env):
    client, upstream = env
    r = client.get("/api/proxy/grocy/api/objects/shopping_list"
                   "?query%5B%5D=shopping_list_id%3D1&query%5B%5D=done%3D0",
                   headers=_hdr())
    assert r.status_code == 200
    params = upstream.call_args.kwargs["params"]
    assert params == [("query[]", "shopping_list_id=1"), ("query[]", "done=0")]


def test_older_satellite_without_client_header_still_works(env):
    # A v0.20.0 satellite sends only X-API-Key: every path it uses forwards,
    # including the legacy stock PUT from satellites up to v0.18.98.
    client, upstream = env
    for method, path in (("GET", "api/stock"),
                         ("PUT", "api/objects/stock/4"),
                         ("GET", "api/objects/shopping_list/3")):
        r = client.request(method, f"/api/proxy/grocy/{path}",
                           headers=_hdr(client=None), json={} if method == "PUT" else None)
        assert r.status_code == 200, path
    rows = {row["template"]: row for row in proxy_policy.snapshot()}
    assert rows["api/stock"]["clients"] == {"unlabelled": 1}


def test_mealie_scoped_and_image_paths_forward(env):
    client, upstream = env
    for method, path in (("GET", "api/households/shopping/lists/abc"),
                         ("GET", "api/groups/mealplans"),
                         ("GET", "api/media/recipes/abc/images/original.webp"),
                         ("PATCH", "api/recipes/chili")):
        r = client.request(method, f"/api/proxy/mealie/{path}", headers=_hdr())
        assert r.status_code == 200, path
    assert upstream.call_args.kwargs["headers"]["Authorization"] == "Bearer mealie-admin-key"


# --- refusals ------------------------------------------------------------------

def test_hard_deny_refused_in_report_mode(env):
    client, upstream = env
    r = client.get("/api/proxy/grocy/api/objects/api_keys", headers=_hdr(EXTRA))
    assert r.status_code == 403
    assert r.json()["code"] == "proxy_path_denied"
    upstream.assert_not_called()
    items = _refusal_items()
    assert len(items) == 1
    title, body, dedupe = items[0]
    assert '"Kitchen Pi"' in body and "satellite/0.20.3" in body
    # A denied path is never fixed by updating, so the item does not say so.
    assert "same version" not in body and "No Pantry Raider device" in body
    assert EXTRA not in body and EXTRA not in dedupe  # never the key itself


def test_hard_deny_mealie_admin(env):
    client, upstream = env
    r = client.post("/api/proxy/mealie/api/admin/users", headers=_hdr(), json={})
    assert r.status_code == 403 and r.json()["code"] == "proxy_path_denied"
    upstream.assert_not_called()


def test_invalid_path_is_400(env):
    client, upstream = env
    # A '%' still in the path after decoding means a second layer of encoding.
    r = client.get("/api/proxy/grocy/api/stock%25zz", headers=_hdr())
    assert r.status_code == 400 and r.json()["code"] == "proxy_path_invalid"
    r = client.get("/api/proxy/grocy/api/objects/products%5C1", headers=_hdr())
    assert r.status_code == 400
    r = client.get("/api/proxy/grocy/stock", headers=_hdr())
    assert r.status_code == 400
    upstream.assert_not_called()


def test_offlist_forwards_in_report_mode_without_an_action_item(env, caplog):
    client, upstream = env
    with caplog.at_level("WARNING", logger="foodassistant.proxy"):
        r = client.post("/api/proxy/grocy/api/chores/3/execute", headers=_hdr(), json={})
        client.post("/api/proxy/grocy/api/chores/3/execute", headers=_hdr(), json={})
    assert r.status_code == 200
    assert upstream.call_count == 2
    assert _refusal_items() == []
    warnings = [rec for rec in caplog.records if "not on the list" in rec.getMessage()]
    assert len(warnings) == 1  # throttled
    assert KEY not in warnings[0].getMessage()
    row = [x for x in proxy_policy.snapshot() if x["verdict"] == "offlist"][0]
    assert row["template"] == "api/chores/ID/execute" and row["count"] == 2


def test_offlist_refused_under_enforce(env, monkeypatch):
    client, upstream = env
    monkeypatch.setattr(settings, "proxy_path_policy", "enforce", raising=False)
    r = client.post("/api/proxy/grocy/api/chores/3/execute", headers=_hdr(), json={})
    assert r.status_code == 403
    assert r.json() == {
        "detail": ("The main server does not pass this Grocy request along "
                   "(POST /api/chores/3/execute). Update this device and the "
                   "main server to the same version."),
        "code": "proxy_path_not_allowed"}
    upstream.assert_not_called()
    # A retry the same day does not add a second item.
    client.post("/api/proxy/grocy/api/chores/4/execute", headers=_hdr(), json={})
    items = _refusal_items()
    assert len(items) == 1
    assert "same version" in items[0][1]


def test_case_and_punctuation_tricks_are_refused_in_report_mode(env):
    client, upstream = env
    for backend, path in (("grocy", "api/Users"), ("grocy", "api/objects/API_KEYS"),
                          ("grocy", "api/users;x"), ("mealie", "api/Admin/users"),
                          ("mealie", "api/users/SELF")):
        r = client.get(f"/api/proxy/{backend}/{path}", headers=_hdr())
        assert r.status_code == 403, path
        assert r.json()["code"] == "proxy_path_denied"
    upstream.assert_not_called()


def test_inbox_items_per_key_are_capped_per_day(env):
    # A key probing many denied paths raises a handful of items, not one per path.
    client, upstream = env
    for i in range(proxy._REFUSAL_ITEMS_PER_DAY + 15):
        r = client.get(f"/api/proxy/grocy/api/users/probe{i}", headers=_hdr())
        assert r.status_code == 403
    assert len(_refusal_items()) == proxy._REFUSAL_ITEMS_PER_DAY
    # Another key still gets its own item.
    client.get("/api/proxy/grocy/api/users", headers=_hdr(EXTRA))
    assert len(_refusal_items()) == proxy._REFUSAL_ITEMS_PER_DAY + 1
    upstream.assert_not_called()


def test_offlist_warnings_are_capped(env, caplog, monkeypatch):
    client, upstream = env
    monkeypatch.setattr(proxy, "_WARN_MAX", 3)
    with caplog.at_level("WARNING", logger="foodassistant.proxy"):
        for i in range(10):
            client.get(f"/api/proxy/grocy/api/made-up-{i}", headers=_hdr())
    warnings = [rec for rec in caplog.records if "not on the list" in rec.getMessage()]
    assert len(warnings) == 3
    assert upstream.call_count == 10  # report mode still passes every one along


def test_allowed_paths_still_forward_under_enforce(env, monkeypatch):
    client, upstream = env
    monkeypatch.setattr(settings, "proxy_path_policy", "enforce", raising=False)
    r = client.get("/api/proxy/grocy/api/stock/products/5/entries", headers=_hdr())
    assert r.status_code == 200


def test_unknown_backend_still_404(env):
    client, _ = env
    r = client.get("/api/proxy/ollama/api/tags", headers=_hdr())
    assert r.status_code == 404


# --- review endpoint -----------------------------------------------------------

def test_admin_proxy_paths_lists_the_record(env):
    client, _ = env
    client.get("/api/proxy/grocy/api/stock", headers=_hdr(EXTRA))
    client.get("/api/proxy/grocy/api/users", headers=_hdr())
    r = client.get("/admin/proxy-paths")
    assert r.status_code == 200
    body = r.json()
    assert body["policy"] == "report"
    by_tpl = {row["template"]: row for row in body["paths"]}
    assert by_tpl["api/stock"]["credentials"] == {"Kitchen Pi": 1}
    assert by_tpl["api/users"]["verdict"] == "deny"
    assert body["paths"][0]["verdict"] == "deny"
    assert os.path.exists(os.path.join(settings.data_dir, "proxy_paths.json"))


def test_credential_names(env):
    assert proxy._credential_name("") is None
    assert proxy._credential_name("wrong") is None
    assert proxy._credential_name(KEY) == "primary key"
    assert proxy._credential_name(EXTRA) == "Kitchen Pi"
    settings.extra_api_key_names = []
    assert proxy._credential_name(EXTRA) == "extra key 1"
