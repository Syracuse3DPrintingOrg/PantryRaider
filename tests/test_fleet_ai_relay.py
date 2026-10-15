"""AI, recipe and camera-detection relays a 0.21 satellite runs on its server.

The server side is /api/fleet/* (routers/fleet.py); the satellite side is
providers/fleet.py (AI), the relay in services/recipes_external.py and the
choice of provider in dependencies.py. A request is checked against a fixed
table of operations and argument shapes, so a caller can never pick a
function by name or send a picture larger than the limit.
"""
from __future__ import annotations

import asyncio
import base64
import sys
from datetime import date
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.models.food import AnalysisResult, FoodItem  # noqa: E402
from app.providers.base import VisionProvider  # noqa: E402
from app.routers import fleet as fleet_router  # noqa: E402

KEY = "fleet-test-key-fake"
PEER = ("192.168.1.20", 50000)
JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class StubProvider(VisionProvider):
    """Records every call and answers something recognisable per op."""

    def __init__(self):
        self.calls = []

    async def analyze_food(self, image_data, mime_type):
        self.calls.append(("analyze_food", image_data, mime_type))
        return AnalysisResult(items=[FoodItem(name="Milk", quantity=2,
                                              best_by_date=date(2026, 10, 9))],
                              image_type="food")

    async def analyze_receipt(self, image_data, mime_type):
        self.calls.append(("analyze_receipt", image_data, mime_type))
        return AnalysisResult(items=[FoodItem(name="Eggs")], image_type="receipt",
                              store="Corner Shop", purchased_on=date(2026, 9, 30))

    async def extract_receipt_prices(self, image_data, mime_type):
        self.calls.append(("extract_receipt_prices", image_data, mime_type))
        return "Eggs 3.99"

    async def health_check(self):
        self.calls.append(("health_check",))
        return True

    async def enrich_product(self, info):
        self.calls.append(("enrich_product", info))
        return {"name": info.get("product_name", "")}

    async def estimate_shelf_lives(self, items):
        self.calls.append(("estimate_shelf_lives", items))
        return [{"storage_type": "refrigerated", "shelf_life_days": 7} for _ in items]

    async def generate_recipe(self, name, extra_instructions=""):
        self.calls.append(("generate_recipe", name, extra_instructions))
        return {"name": name}

    async def suggest_from_inventory(self, items, limit=8, preferences=""):
        self.calls.append(("suggest_from_inventory", items, limit, preferences))
        return [{"name": "Omelette"}]

    async def estimate_nutrition(self, name, servings=1.0):
        self.calls.append(("estimate_nutrition", name, servings))
        return {"calories": 100}

    async def extract_recipe(self, image_data=None, mime_type=None, page_text=None):
        self.calls.append(("extract_recipe", image_data, mime_type, page_text))
        return {"name": "From page" if page_text else "From photo"}

    async def optimize_recipe(self, recipe):
        self.calls.append(("optimize_recipe", recipe))
        return dict(recipe, optimized=True)

    async def parse_ingredients(self, lines):
        self.calls.append(("parse_ingredients", lines))
        return [{"food": line} for line in lines]

    async def identify_barcode(self, barcode):
        self.calls.append(("identify_barcode", barcode))
        return {"name": "Thing", "barcode": barcode}


@pytest.fixture
def server(monkeypatch, tmp_path):
    """This test process plays the main server; its key is the satellite's."""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "deployment_mode", "server")
    monkeypatch.setattr(settings, "api_key", KEY)
    monkeypatch.setattr(settings, "auth_password", "")
    monkeypatch.setattr(settings, "ai_token_budget", 0)
    fleet_router.reset_caches()
    stub = StubProvider()
    monkeypatch.setattr(fleet_router, "_provider", lambda role: stub)
    yield stub
    fleet_router.reset_caches()


@pytest.fixture
def client(server):
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app, client=PEER)


def ai(client, op, args=None, key=KEY, **extra):
    body = {"role": extra.pop("role", "vision"), "op": op, "args": args or {}}
    body.update(extra)
    headers = {"X-API-Key": key} if key else {}
    return client.post("/api/fleet/ai", json=body, headers=headers)


# --- the round trip, satellite provider to server route ---------------------------


@pytest.fixture
def fleet_provider(server, monkeypatch):
    """A FleetProvider whose HTTP client reaches this app in-process."""
    from app.main import app
    from app.providers import fleet as fleet_mod
    monkeypatch.setattr(settings, "remote_server_url", "http://testserver")
    monkeypatch.setattr(settings, "upstream_api_key", KEY)

    def _client():
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=PEER),
                                 base_url="http://testserver")
    monkeypatch.setattr(fleet_mod, "_http", _client)
    return fleet_mod.FleetProvider


def _run(coro):
    return asyncio.run(coro)


def test_every_op_round_trips(server, fleet_provider):
    p = fleet_provider("vision")
    food = _run(p.analyze_food(JPEG, "image/jpeg"))
    expected = _run(StubProvider().analyze_food(JPEG, "image/jpeg"))
    assert isinstance(food, AnalysisResult) and food == expected
    receipt = _run(p.analyze_receipt(JPEG, "image/png"))
    assert receipt == _run(StubProvider().analyze_receipt(JPEG, "image/png"))
    assert _run(p.extract_receipt_prices(JPEG, "image/webp")) == "Eggs 3.99"
    assert _run(p.health_check()) is True
    assert _run(p.enrich_product({"product_name": "Oats"})) == {"name": "Oats"}
    assert _run(p.estimate_shelf_lives([{"product_name": "Milk"}])) == [
        {"storage_type": "refrigerated", "shelf_life_days": 7}]
    assert _run(p.generate_recipe("Soup", "no salt")) == {"name": "Soup"}
    assert _run(p.suggest_from_inventory(["eggs", "milk"], 3, "quick")) == [{"name": "Omelette"}]
    assert _run(p.estimate_nutrition("Soup", 2)) == {"calories": 100}
    assert _run(p.extract_recipe(page_text="Mix and bake.")) == {"name": "From page"}
    assert _run(p.extract_recipe(JPEG, "image/jpeg")) == {"name": "From photo"}
    assert _run(p.optimize_recipe({"name": "Soup"})) == {"name": "Soup", "optimized": True}
    assert _run(p.parse_ingredients(["1 egg"])) == [{"food": "1 egg"}]
    assert _run(p.identify_barcode("0123456789012")) == {"name": "Thing",
                                                         "barcode": "0123456789012"}
    ops = [c[0] for c in server.calls]
    assert set(ops) == set(fleet_router.AI_OPS)
    # The picture arrives byte for byte with its type.
    assert ("analyze_food", JPEG, "image/jpeg") in server.calls
    assert ("suggest_from_inventory", ["eggs", "milk"], 3, "quick") in server.calls


def test_fleet_provider_records_no_local_usage(server, fleet_provider, monkeypatch, tmp_path):
    from app.services import usage
    monkeypatch.setattr(usage, "over_budget", lambda now=None: True)
    p = fleet_provider("vision")
    with pytest.raises(HTTPException) as exc:
        _run(p.generate_recipe("Soup"))
    from app.routers.analyze import _BUDGET_MSG
    assert exc.value.status_code == 429
    assert exc.value.detail == _BUDGET_MSG
    assert not (tmp_path / "ai_usage.json").exists()


def test_fleet_provider_unreachable_server(monkeypatch):
    from app.providers import fleet as fleet_mod
    monkeypatch.setattr(settings, "remote_server_url", "http://192.0.2.1:9284")

    def _down(request):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(fleet_mod, "_http",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_down)))
    p = fleet_mod.FleetProvider("enrich")
    with pytest.raises(HTTPException) as exc:
        _run(p.enrich_product({"a": 1}))
    assert exc.value.status_code == 502
    assert exc.value.detail == fleet_mod.UNREACHABLE_MSG
    assert _run(p.health_check()) is False


def test_fleet_provider_passes_the_servers_refusal(monkeypatch):
    from app.providers import fleet as fleet_mod
    monkeypatch.setattr(settings, "remote_server_url", "http://192.0.2.1:9284")
    seen = {}

    def _answer(request):
        seen["role"] = request.content
        return httpx.Response(403, json={"detail": "This key is not allowed to do that."})
    monkeypatch.setattr(fleet_mod, "_http",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_answer)))
    with pytest.raises(HTTPException) as exc:
        _run(fleet_mod.FleetProvider("enrich").identify_barcode("12345678"))
    assert exc.value.status_code == 403
    assert exc.value.detail == "This key is not allowed to do that."
    assert b'"role": "enrich"' in seen["role"] or b'"role":"enrich"' in seen["role"]


def test_fleet_provider_fits_requests_to_what_the_server_takes(server, fleet_provider):
    """A big pantry, a large per-tier setting or a phone's picture type still
    gets an answer: the satellite trims the request instead of the server
    refusing it."""
    p = fleet_provider("enrich")
    items = [f"item {i}" for i in range(600)]
    assert _run(p.suggest_from_inventory(items, 80, "x" * 3000)) == [{"name": "Omelette"}]
    op, sent, limit, prefs = server.calls[-1]
    assert op == "suggest_from_inventory"
    assert sent == items[:500] and limit == 50 and len(prefs) == 2000
    assert _run(p.parse_ingredients(["y" * 600] * 250)) == [{"food": "y" * 500}] * 200
    assert _run(p.generate_recipe("n" * 300, "e" * 3000)) == {"name": "n" * 200}
    assert _run(p.extract_recipe(page_text="t" * 250000)) == {"name": "From page"}
    assert len(server.calls[-1][3]) == 200000
    assert _run(fleet_provider("vision").analyze_food(JPEG, "image/jpg")).items
    assert ("analyze_food", JPEG, "image/jpeg") in server.calls


def test_shelf_life_batch_is_none_when_the_server_has_no_batch_path(
        server, fleet_provider, monkeypatch):
    # A server on a direct key (or on a Forager without the batch kind)
    # answers None, and the satellite then asks line by line as before.
    async def no_batch(items):
        server.calls.append(("estimate_shelf_lives", items))
        return None
    monkeypatch.setattr(server, "estimate_shelf_lives", no_batch)
    p = fleet_provider("enrich")
    assert _run(p.estimate_shelf_lives([{"product_name": "Milk"}])) is None
    assert server.calls == [("estimate_shelf_lives", [{"product_name": "Milk"}])]


def _answering(monkeypatch, status, body):
    from app.providers import fleet as fleet_mod
    monkeypatch.setattr(settings, "remote_server_url", "http://192.0.2.1:9284")
    monkeypatch.setattr(fleet_mod, "_http", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body))))
    return fleet_mod.FleetProvider("enrich")


def test_shelf_life_batch_from_a_server_before_the_op(monkeypatch):
    # A server from before the op refuses it like any request it cannot read.
    p = _answering(monkeypatch, 400, {"detail": fleet_router.BAD_REQUEST_MSG})
    assert _run(p.estimate_shelf_lives([{"product_name": "Milk"}])) is None


def test_shelf_life_batch_still_passes_other_failures(monkeypatch):
    # Anything else is a real failure, not a missing op: line by line would
    # fail the same way, so the caller keeps the defaults instead.
    p = _answering(monkeypatch, 429, {"detail": "AI token budget reached for this month."})
    with pytest.raises(HTTPException) as exc:
        _run(p.estimate_shelf_lives([{"product_name": "Milk"}]))
    assert exc.value.status_code == 429


@pytest.mark.parametrize("result", [
    [{"shelf_life_days": 5}],                       # one answer for two lines
    {"shelf_life_days": 5},
    "five days",
])
def test_shelf_life_batch_answer_of_the_wrong_shape_is_none(monkeypatch, result):
    p = _answering(monkeypatch, 200, {"ok": True, "result": result})
    assert _run(p.estimate_shelf_lives([{"product_name": "Milk"},
                                        {"product_name": "Eggs"}])) is None


def test_shelf_life_batch_keeps_only_object_answers(monkeypatch):
    p = _answering(monkeypatch, 200, {"ok": True, "result": [
        {"shelf_life_days": 5}, None, "soon"]})
    assert _run(p.estimate_shelf_lives([{"product_name": n} for n in "abc"])) == [
        {"shelf_life_days": 5}, None, None]


def test_fleet_provider_health_is_cached_on_the_satellite(monkeypatch):
    from app.providers import fleet as fleet_mod
    monkeypatch.setattr(settings, "remote_server_url", "http://192.0.2.1:9284")
    hits = []

    def _answer(request):
        hits.append(request.url.path)
        return httpx.Response(200, json={"ok": True, "result": True})
    monkeypatch.setattr(fleet_mod, "_http",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_answer)))
    p = fleet_mod.FleetProvider("vision")
    assert all(_run(p.health_check()) for _ in range(3))
    assert hits == ["/api/fleet/ai"]
    fleet_mod._health_vision.invalidate()
    assert _run(p.health_check()) is True
    assert len(hits) == 2


# --- the server route --------------------------------------------------------------


def test_unknown_op_and_role(client):
    assert ai(client, "delete_everything").status_code == 400
    assert ai(client, "__init__").status_code == 400
    r = ai(client, "health_check", role="admin")
    assert r.status_code == 400
    assert r.json()["detail"] == fleet_router.BAD_REQUEST_MSG


@pytest.mark.parametrize("op, args, extra", [
    ("analyze_food", {}, {}),                                           # no picture
    ("analyze_food", {}, {"image_b64": "not base64!!", "mime_type": "image/jpeg"}),
    ("analyze_food", {}, {"image_b64": _b64(JPEG), "mime_type": "text/html"}),
    ("analyze_receipt", {"extra": 1}, {"image_b64": _b64(JPEG), "mime_type": "image/jpeg"}),
    ("enrich_product", {"info": "text"}, {}),
    ("enrich_product", {"info": {"x": "y" * 70000}}, {}),
    ("estimate_shelf_lives", {}, {}),
    ("estimate_shelf_lives", {"items": "text"}, {}),
    ("estimate_shelf_lives", {"items": [{"product_name": "a"}] * 101}, {}),
    ("estimate_shelf_lives", {"items": ["not an object"]}, {}),
    ("estimate_shelf_lives", {"items": [{"product_name": "y" * 5000}]}, {}),
    ("estimate_shelf_lives", {"items": []},
     {"image_b64": _b64(JPEG), "mime_type": "image/jpeg"}),
    ("generate_recipe", {"name": ""}, {}),
    ("generate_recipe", {"name": "x" * 201}, {}),
    ("generate_recipe", {"name": "Soup", "extra_instructions": "x" * 2001}, {}),
    ("suggest_from_inventory", {"items": ["a"] * 501}, {}),
    ("suggest_from_inventory", {"items": ["a"], "limit": 0}, {}),
    ("suggest_from_inventory", {"items": ["a"], "limit": True}, {}),
    ("suggest_from_inventory", {"items": [1]}, {}),
    ("estimate_nutrition", {"name": "Soup", "servings": 0}, {}),
    ("estimate_nutrition", {"name": "Soup", "servings": 101}, {}),
    ("extract_recipe", {}, {}),
    ("extract_recipe", {"page_text": "x" * 200001}, {}),
    ("optimize_recipe", {"recipe": ["not", "a", "dict"]}, {}),
    ("optimize_recipe", {"recipe": {"x": "y" * 270000}}, {}),
    ("parse_ingredients", {"lines": ["a"] * 201}, {}),
    ("parse_ingredients", {"lines": ["x" * 501]}, {}),
    ("identify_barcode", {"barcode": "12ab5678"}, {}),
    ("identify_barcode", {"barcode": "12345"}, {}),
    ("identify_barcode", {"barcode": "1" * 33}, {}),
    ("health_check", {"anything": 1}, {}),
    ("generate_recipe", {"name": "Soup"}, {"image_b64": _b64(JPEG), "mime_type": "image/jpeg"}),
])
def test_bad_arguments(client, server, op, args, extra):
    r = ai(client, op, args, **extra)
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == fleet_router.BAD_REQUEST_MSG
    assert server.calls == []


def test_body_that_is_not_json(client):
    r = client.post("/api/fleet/ai", content=b"{not json", headers={
        "X-API-Key": KEY, "Content-Type": "application/json"})
    assert r.status_code == 400


def test_oversize_by_content_length(client, server, monkeypatch):
    monkeypatch.setattr(fleet_router, "MAX_BODY_BYTES", 1000)
    r = ai(client, "analyze_food", image_b64=_b64(b"x" * 2000), mime_type="image/jpeg")
    assert r.status_code == 413
    assert r.json()["detail"] == fleet_router.TOO_LARGE_MSG
    assert server.calls == []


def test_oversize_by_body(client, server, monkeypatch):
    monkeypatch.setattr(fleet_router, "MAX_BODY_BYTES", 1000)
    import json as _json
    payload = _json.dumps({"role": "vision", "op": "analyze_food", "args": {},
                           "image_b64": _b64(b"x" * 2000),
                           "mime_type": "image/jpeg"}).encode()

    def _chunks():
        for i in range(0, len(payload), 256):
            yield payload[i:i + 256]
    r = client.post("/api/fleet/ai", content=_chunks(), headers={
        "X-API-Key": KEY, "Content-Type": "application/json"})
    assert r.status_code == 413
    assert server.calls == []


def test_oversize_picture(client, server, monkeypatch):
    monkeypatch.setattr(fleet_router, "MAX_IMAGE_BYTES", 100)
    r = ai(client, "analyze_food", image_b64=_b64(b"x" * 200), mime_type="image/jpeg")
    assert r.status_code == 413
    assert r.json()["detail"] == fleet_router.TOO_LARGE_MSG


def test_keys(client, server):
    from app.services import access, credentials
    r = ai(client, "health_check", key="")
    assert r.status_code == 401 and r.json()["detail"] == "Unauthorized"
    assert ai(client, "health_check", key="not-a-key").status_code == 401
    _rec, ha_key = credentials.mint("ha", "Home Assistant", "manual")
    r = ai(client, "health_check", key=ha_key)
    assert r.status_code == 403 and r.json()["detail"] == access.SCOPE_MSG
    _rec, sat_key = credentials.mint("satellite", "Bandit", "paired")
    assert ai(client, "health_check", key=sat_key).status_code == 200
    assert server.calls == [("health_check",)]


def test_budget(client, server, monkeypatch):
    from app.routers.analyze import _BUDGET_MSG
    from app.services import usage
    monkeypatch.setattr(usage, "over_budget", lambda now=None: True)
    r = ai(client, "generate_recipe", {"name": "Soup"})
    assert r.status_code == 429
    assert r.json()["detail"] == _BUDGET_MSG
    assert server.calls == []


def test_provider_refusal_passes_through(client, server, monkeypatch):
    async def _refuse(name, extra_instructions=""):
        raise HTTPException(status_code=403, detail="The provider said no.")
    monkeypatch.setattr(server, "generate_recipe", _refuse)
    r = ai(client, "generate_recipe", {"name": "Soup"})
    assert r.status_code == 403
    assert r.json()["detail"] == "The provider said no."


def test_other_provider_failure_is_502(client, server, monkeypatch):
    async def _boom(name, extra_instructions=""):
        raise RuntimeError("sk-secret-should-not-leak")
    monkeypatch.setattr(server, "generate_recipe", _boom)
    r = ai(client, "generate_recipe", {"name": "Soup"})
    assert r.status_code == 502
    assert r.json()["detail"] == fleet_router.AI_FAILED_MSG
    assert "sk-secret" not in r.text


def test_health_check_is_cached_per_role(client, server):
    for _ in range(3):
        assert ai(client, "health_check").json() == {"ok": True, "result": True}
    assert ai(client, "health_check", role="enrich").json()["result"] is True
    assert server.calls == [("health_check",), ("health_check",)]


def test_analysis_result_is_json(client, server):
    r = ai(client, "analyze_food", image_b64=_b64(JPEG), mime_type="image/jpeg")
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["items"][0]["name"] == "Milk"
    assert result["items"][0]["best_by_date"] == "2026-10-09"
    assert AnalysisResult.model_validate(result).items[0].quantity == 2


# --- which provider a satellite uses -------------------------------------------------


def _satellite(monkeypatch, relay):
    from app import dependencies
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    monkeypatch.setattr(settings, "vision_provider", "gemini")
    monkeypatch.setattr(settings, "enrich_provider", "")
    monkeypatch.setattr(settings, "gemini_api_key", "stale-local-key")
    monkeypatch.setattr(settings, "fleet_relay", relay)
    dependencies.reset_providers()
    return dependencies


def test_dependencies_pick_the_fleet_provider_only_with_the_relay(monkeypatch):
    from app.providers.fleet import FleetProvider
    from app.providers.noop import NoOpProvider
    deps = _satellite(monkeypatch, {"v": 1, "caps": ["ai"], "ai_available": True,
                                    "enrich_available": True})
    v, e = deps.get_vision_provider(), deps.get_enrich_provider()
    assert isinstance(v, FleetProvider) and v.role == "vision"
    assert isinstance(e, FleetProvider) and e.role == "enrich"
    deps = _satellite(monkeypatch, {"v": 1, "caps": ["ai"], "ai_available": False,
                                    "enrich_available": False})
    # A stale local key is ignored while the server relays AI.
    assert isinstance(deps.get_vision_provider(), NoOpProvider)
    assert isinstance(deps.get_enrich_provider(), NoOpProvider)
    # Without the ai cap the local key is used as before.
    deps = _satellite(monkeypatch, {"v": 1, "caps": ["recipes_external"]})
    local = StubProvider()
    monkeypatch.setattr(deps, "_build_provider", lambda name, model=None: local)
    assert deps.get_vision_provider() is local
    deps.reset_providers()


def test_usage_says_where_tokens_are_counted(monkeypatch, tmp_path):
    from app.services import usage
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    _satellite(monkeypatch, {"v": 1, "caps": ["ai"], "ai_available": True})
    assert usage.get_usage()["counted_on_server"] is True
    monkeypatch.setattr(settings, "fleet_relay", {})
    assert usage.get_usage()["counted_on_server"] is False


# --- recipe lookups -----------------------------------------------------------------


class _FakeClient:
    def __init__(self, status=200, body=None):
        self.posts = []
        self.status = status
        self.body = body if body is not None else {"ok": True, "result": [{"name": "Stew"}]}

    async def post(self, url, json=None, **kw):
        self.posts.append((url, json, kw.get("headers", {})))
        return httpx.Response(self.status, json=self.body)


def test_recipe_lookup_relays_only_with_the_cap(monkeypatch):
    from app.services import recipes_external as rx
    fake = _FakeClient()
    monkeypatch.setattr(rx, "_client", fake)
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    monkeypatch.setattr(settings, "remote_server_url", "http://192.0.2.1:9284")
    monkeypatch.setattr(settings, "upstream_api_key", KEY)
    monkeypatch.setattr(settings, "recipe_source", "off")
    monkeypatch.setattr(settings, "fleet_relay", {})
    assert _run(rx.search_recipes_by_name("stew")) == []
    assert fake.posts == []
    monkeypatch.setattr(settings, "fleet_relay", {"v": 1, "caps": ["recipes_external"]})
    assert _run(rx.search_recipes_by_name("stew", 5)) == [{"name": "Stew"}]
    url, body, headers = fake.posts[0]
    assert url == "http://192.0.2.1:9284/api/fleet/recipes-external"
    assert body == {"op": "search_recipes_by_name", "args": {"query": "stew", "limit": 5}}
    assert headers["X-API-Key"] == KEY and headers["X-PR-Caps"] == "relay1"
    _run(rx.find_recipes_for_ingredients(["egg"], 4, "vegan", 30, "Italian"))
    assert fake.posts[1][1] == {"op": "find_recipes_for_ingredients", "args": {
        "ingredients": ["egg"], "limit": 4, "dietary": "vegan", "max_time": 30,
        "cuisine": "Italian"}}
    fake.body = {"ok": True, "result": {"name": "Stew", "id": "52"}}
    assert _run(rx.get_external_recipe("52")) == {"name": "Stew", "id": "52"}


def test_recipe_relay_fits_a_big_pantry(monkeypatch):
    """A pantry with more than 100 items still gets external suggestions: the
    satellite sends the first 100 distinct names, soonest to spoil first."""
    from app.services import recipes_external as rx
    fake = _FakeClient()
    monkeypatch.setattr(rx, "_client", fake)
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    monkeypatch.setattr(settings, "remote_server_url", "http://192.0.2.1:9284")
    monkeypatch.setattr(settings, "fleet_relay", {"v": 1, "caps": ["recipes_external"]})
    names = ["Milk", "milk"] + [f"Item {i}" for i in range(200)] + ["z" * 150]
    assert _run(rx.find_recipes_for_ingredients(names, 99, "d" * 300, 5000)) == [
        {"name": "Stew"}]
    args = fake.posts[-1][1]["args"]
    assert args["ingredients"][:2] == ["Milk", "Item 0"]
    assert len(args["ingredients"]) == 100
    assert args["limit"] == 50 and args["max_time"] == 1440 and len(args["dietary"]) == 200
    _run(rx.search_recipes_by_name("q" * 300, 0))
    assert fake.posts[-1][1]["args"] == {"query": "q" * 200, "limit": 1}


def test_recipe_relay_failure_is_an_empty_answer(monkeypatch):
    from app.services import recipes_external as rx
    monkeypatch.setattr(rx, "_client", _FakeClient(status=502, body={"detail": "x"}))
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    monkeypatch.setattr(settings, "remote_server_url", "http://192.0.2.1:9284")
    monkeypatch.setattr(settings, "fleet_relay", {"v": 1, "caps": ["recipes_external"]})
    assert _run(rx.search_recipes_by_name("stew")) == []
    assert _run(rx.find_recipes_for_ingredients(["egg"])) == []
    assert _run(rx.get_external_recipe("52")) is None


def test_recipe_route(client, monkeypatch):
    from app.services import recipes_external as rx
    seen = []

    async def _search(query, limit=12):
        seen.append((query, limit))
        return [{"name": "Stew"}]
    monkeypatch.setattr(rx, "search_recipes_by_name", _search)
    r = client.post("/api/fleet/recipes-external", headers={"X-API-Key": KEY},
                    json={"op": "search_recipes_by_name", "args": {"query": "stew", "limit": 3}})
    assert r.json() == {"ok": True, "result": [{"name": "Stew"}]}
    assert seen == [("stew", 3)]
    for bad in ({"op": "eval", "args": {}},
                {"op": "get_external_recipe", "args": {"external_id": "1", "source": "x"}},
                {"op": "find_recipes_for_ingredients", "args": {"ingredients": ["a"] * 101}},
                {"op": "find_recipes_for_ingredients", "args": {"ingredients": [],
                                                                "max_time": 1441}}):
        r = client.post("/api/fleet/recipes-external", headers={"X-API-Key": KEY}, json=bad)
        assert r.status_code == 400

    async def _boom(query, limit=12):
        raise RuntimeError("down")
    monkeypatch.setattr(rx, "search_recipes_by_name", _boom)
    r = client.post("/api/fleet/recipes-external", headers={"X-API-Key": KEY},
                    json={"op": "search_recipes_by_name", "args": {"query": "stew"}})
    assert r.status_code == 502
    assert r.json()["detail"] == fleet_router.RECIPES_FAILED_MSG
    assert client.post("/api/fleet/recipes-external", json={}).status_code == 401


# --- Reolink detection ----------------------------------------------------------------


def test_camera_ai_by_name(client, monkeypatch):
    from app.services import cameras
    calls = []

    async def _state(entry, timeout=6.0):
        calls.append(entry["name"])
        return {"people": 1}
    monkeypatch.setattr(cameras, "fetch_reolink_ai_state", _state)
    monkeypatch.setattr(settings, "streamdeck_cameras", [
        {"name": "Door", "snapshot_url": "http://192.168.1.60/snap.jpg"},
        {"name": "Yard", "source": "reolink", "host": "192.168.1.61",
         "username": "admin", "password": "pw"},
    ])
    for _ in range(3):
        r = client.post("/api/fleet/camera-ai", headers={"X-API-Key": KEY},
                        json={"name": "yard"})
        assert r.json() == {"ok": True, "state": {"people": 1}}
    assert calls == ["Yard"]  # cached for two seconds
    for name in ("Door", "Nowhere"):
        r = client.post("/api/fleet/camera-ai", headers={"X-API-Key": KEY},
                        json={"name": name})
        assert r.status_code == 404
        assert r.json()["detail"] == "Unknown camera."
    r = client.post("/api/fleet/camera-ai", headers={"X-API-Key": KEY},
                    json={"name": "x" * 121})
    assert r.status_code == 400
