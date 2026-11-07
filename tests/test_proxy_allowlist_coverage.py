"""Every Grocy and Mealie request a satellite makes is one the proxy allows.

The main server's proxy refuses paths off its list (proxy_path_policy
'enforce', the default from 0.21), so a client method that starts calling a
new path must reach services/proxy_policy.py's ALLOW table in the same change.
Three checks hold that together:

- every public GrocyClient and MealieClient coroutine, run in satellite mode
  against a recording transport, sends only requests classify() allows;
- an AST scan of service/app finds the client call sites with literal or
  f-string paths (_request, _get, _post, _scoped, _cached_list,
  _ensure_object, _list_all), and every one is allowed, apart from those in
  a GrocyClient method marked @_server_only, which a satellite refuses
  before it sends anything (checked here too);
- every path fielded satellites called, v0.6.0 onwards
  (tests/data/r1_fleet_proxy_client_paths.json), is still allowed.

No network: the clients' shared httpx client is swapped for a MockTransport,
and any other outgoing connection fails at once.
"""
from __future__ import annotations

import ast
import inspect
import json
import sys
from datetime import date
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.models.food import FoodItem  # noqa: E402
from app.services import grocy as grocy_svc  # noqa: E402
from app.services import mealie as mealie_svc  # noqa: E402
from app.services import proxy_policy  # noqa: E402
from app.services.grocy import GrocyClient  # noqa: E402
from app.services.mealie import MealieClient  # noqa: E402

SERVER = "http://server.test:9284"
_HISTORY = ROOT / "tests" / "data" / "r1_fleet_proxy_client_paths.json"

# --- canned answers ---------------------------------------------------------

_PRODUCT = {"id": 1, "name": "Milk", "location_id": 1, "product_group_id": 1,
            "qu_id_purchase": 1, "qu_id_stock": 1, "default_best_before_days": 7}
_ENTRY = {"id": 1, "product_id": 1, "amount": 2, "best_before_date": "2026-10-10",
          "location_id": 1, "stock_id": "abc", "open": 0, "price": None,
          "purchased_date": "2026-09-30"}
_GROCY_OBJECTS = {
    "products": [_PRODUCT],
    "locations": [{"id": 1, "name": "Fridge", "is_freezer": 0}],
    "product_groups": [{"id": 1, "name": "Dairy"}],
    "quantity_units": [{"id": 1, "name": "Piece", "name_plural": "Pieces"}],
    "quantity_unit_conversions": [],
    "shopping_lists": [{"id": 1, "name": "Shopping list"}],
    "shopping_list": [{"id": 1, "shopping_list_id": 1, "note": "eggs", "done": 1,
                       "product_id": 1, "amount": 1}],
    "product_barcodes": [],
    "stock": [_ENTRY],
    "stock_log": [{"id": 1, "product_id": 1, "amount": -1, "transaction_type": "consume",
                   "spoiled": 0, "undone": 0, "row_created_timestamp": "2026-09-20 10:00:00"},
                  {"id": 2, "product_id": 1, "amount": -1, "transaction_type": "consume",
                   "spoiled": 0, "undone": 0, "row_created_timestamp": "2026-09-25 10:00:00"}],
}


def _grocy_answer(method: str, path: str):
    segs = path.split("/")
    if method == "GET":
        if path == "api/stock":
            return [{"product_id": 1, "amount": 2, "best_before_date": "2026-10-10",
                     "amount_opened": 0, "product": dict(_PRODUCT)}]
        if path == "api/system/info":
            return {"grocy_version": {"Version": "4.5.0"}}
        if len(segs) == 5 and segs[1:3] == ["stock", "products"] and segs[4] == "entries":
            return [dict(_ENTRY)]
        if len(segs) == 3 and segs[1] == "objects":
            return [dict(row) for row in _GROCY_OBJECTS.get(segs[2], [])]
        if len(segs) == 4 and segs[1] == "objects":
            rows = _GROCY_OBJECTS.get(segs[2], [])
            return dict(rows[0]) if rows else {}
        return []
    if method == "POST" and len(segs) == 3 and segs[1] == "objects":
        return {"created_object_id": 5}
    if method == "POST":
        return [dict(_ENTRY)]
    return None


_RECIPE = {"id": "r1", "slug": "soup", "name": "Soup",
           "recipeIngredient": [{"note": "1 egg", "display": "1 egg"}],
           "recipeInstructions": [{"text": "Cook."}]}


def _mealie_answer(method: str, path: str):
    segs = path.split("/")
    if method == "GET":
        if path == "api/users/self":
            return {"id": "u1", "username": "kitchen", "group": "home"}
        if path == "api/recipes":
            return {"items": [dict(_RECIPE)], "total_pages": 1}
        if len(segs) == 3 and segs[1] == "recipes":
            return dict(_RECIPE)
        if path == "api/foods":
            return {"items": [{"id": "f1", "name": "egg"}], "total_pages": 1}
        if path == "api/units":
            return {"items": [{"id": "u1", "name": "piece"}], "total_pages": 1}
        if segs[-1] == "mealplans":
            return {"items": [{"id": 3, "date": "2026-10-01", "entryType": "dinner",
                               "recipe": dict(_RECIPE)}]}
        if segs[-1] == "lists":
            return {"items": [{"id": "abc", "name": "Groceries"}]}
        if len(segs) >= 5 and segs[-2] == "lists":
            return {"id": "abc", "name": "Groceries", "listItems": []}
        return {}
    if method == "POST" and path in ("api/recipes", "api/recipes/create/url",
                                     "api/recipes/create-url"):
        return "soup"
    if method == "POST" and path in ("api/foods", "api/units"):
        return {"id": "n1", "name": "new"}
    if method == "POST":
        return {"id": "it1"}
    return {}


@pytest.fixture
def satellite(monkeypatch):
    """A linked satellite whose two clients talk to a recording transport."""
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    monkeypatch.setattr(settings, "remote_server_url", SERVER, raising=False)
    monkeypatch.setattr(settings, "upstream_api_key", "shared-key", raising=False)
    monkeypatch.setattr(settings, "mealie_base_url", "http://mealie:9000", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "", raising=False)
    monkeypatch.setattr(settings, "mealie_api_key", "", raising=False)

    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for backend in ("grocy", "mealie"):
            prefix = f"/api/proxy/{backend}/"
            if path.startswith(prefix):
                rel = path[len(prefix):]
                seen.append((backend, request.method, rel))
                answer = (_grocy_answer if backend == "grocy" else _mealie_answer)(
                    request.method, rel)
                if answer is None:
                    return httpx.Response(204)
                return httpx.Response(200, json=answer)
        seen.append(("other", request.method, path))
        return httpx.Response(404, json={"detail": "not here"})

    def _no_network(*_a, **_k):
        raise httpx.ConnectError("no network in tests")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _no_network)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _no_network)
    import app.dependencies as deps

    def _no_ai():
        raise RuntimeError("no AI provider in this test")
    monkeypatch.setattr(deps, "get_enrich_provider", _no_ai, raising=False)
    monkeypatch.setattr(grocy_svc, "_client",
                        httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(mealie_svc, "_client",
                        httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(mealie_svc, "_scope", None, raising=False)
    return seen


# --- arguments for every public coroutine ------------------------------------

def _food(name="Oat Milk"):
    return FoodItem(name=name, quantity=1, unit="item", best_by_date=date(2026, 10, 12),
                    barcode="4006381333931")


_COMMON = {
    "note": "eggs", "amount": 1.0, "spoiled": False, "done": True, "days": 7,
    "limit": 5, "min_consumes": 1, "delta_days": 3, "price": 2.5,
    "best_before_date": "2026-10-12", "category": "Dairy", "propose_best_by": None,
}
_GROCY_ARGS = dict(_COMMON, **{
    "list_id": 1, "product_id": 1, "item_id": 1, "location_id": 1, "group_id": 1,
    # A space in the barcode: it must arrive as one path segment.
    "barcode": "4006381 333931", "stock_entry_id": "abc", "name": "Pantry Shelf",
    "bucket": "frozen", "entry": dict(_ENTRY), "item": _food(),
    "quantity": 1.0, "unit": "Pack", "from_id": 1, "to_id": 2,
})
_MEALIE_ARGS = dict(_COMMON, **{
    "list_id": "abc", "item_id": "it1", "entry_id": 3, "date": "2026-10-01",
    "entry_type": "dinner", "recipe_id": "r1", "title": "Soup", "text": "",
    "quantity": 1.0, "slug": "soup", "url": "https://recipes.example/soup",
    "start_date": "2026-10-01", "end_date": "2026-10-07", "search": "soup",
    "per_page": 5, "prefix": "eg", "item": {"note": "eggs", "checked": True},
    "data": {"name": "Soup", "recipeIngredient": ["1 egg"],
             "recipeInstructions": ["Cook."]},
    "ingredients": [{"note": "1 egg"}],
    "structured": [{"food": "egg", "unit": "piece", "quantity": 1, "note": "egg"}],
})


def _public_coroutines(cls):
    return [(name, fn) for name, fn in inspect.getmembers(cls)
            if not name.startswith("_") and inspect.iscoroutinefunction(fn)]


async def _run_all(cls, args):
    ran = []
    for name, fn in _public_coroutines(cls):
        kwargs = {}
        for pname, param in list(inspect.signature(fn).parameters.items())[1:]:
            if pname in args:
                kwargs[pname] = args[pname]
            elif param.default is inspect.Parameter.empty:
                pytest.fail(f"{cls.__name__}.{name} takes '{pname}': add a value "
                            "for it to this test so its requests are checked")
        try:
            await getattr(cls(), name)(**kwargs)
        except Exception:  # noqa: BLE001 - only the requests matter here
            pass
        ran.append(name)
    return ran


def _assert_all_allowed(seen):
    bad = []
    for backend, method, rel in seen:
        if backend == "other":
            bad.append((backend, method, rel, "not a proxy path"))
            continue
        try:
            segments = proxy_policy.validate_path(rel)
        except proxy_policy.ProxyPathInvalid as exc:
            bad.append((backend, method, rel, str(exc)))
            continue
        verdict, _tpl = proxy_policy.classify(backend, method, "/".join(segments))
        if verdict != proxy_policy.VERDICT_ALLOW:
            bad.append((backend, method, rel, verdict))
    assert not bad, f"requests the proxy would refuse: {bad}"


@pytest.mark.anyio
async def test_server_only_methods_send_nothing_from_a_satellite(satellite):
    """The Grocy location tidy-ups (services/grocy_locations.py) hide and
    re-flag locations, which the proxy does not carry."""
    for call in (GrocyClient().merge_location(1, 2),
                 GrocyClient().mark_location_freezer(1)):
        with pytest.raises(grocy_svc.GrocyError):
            await call
    assert satellite == []


def test_the_check_catches_a_path_off_the_list():
    with pytest.raises(AssertionError):
        _assert_all_allowed([("grocy", "POST", "api/chores/3/execute")])
    with pytest.raises(AssertionError):
        _assert_all_allowed([("mealie", "GET", "api/admin/users")])


@pytest.mark.anyio
async def test_every_grocy_client_method_is_allowed(satellite):
    ran = await _run_all(GrocyClient, _GROCY_ARGS)
    # Creation paths: names the canned answers do not have yet.
    client = GrocyClient()
    await client.ensure_product(_food("Rye Bread"), 1, 1)
    await client.ensure_quantity_unit("Jar")
    assert len(ran) >= 30
    assert {b for b, _m, _p in satellite} == {"grocy"}
    methods = {m for _b, m, _p in satellite}
    assert {"GET", "POST", "PUT", "DELETE"} <= methods
    assert len(satellite) >= len(ran)
    _assert_all_allowed(satellite)


@pytest.mark.anyio
async def test_every_mealie_client_method_is_allowed(satellite):
    ran = await _run_all(MealieClient, _MEALIE_ARGS)
    assert len(ran) >= 15
    assert {b for b, _m, _p in satellite} == {"mealie"}
    methods = {m for _b, m, _p in satellite}
    assert {"GET", "POST", "PUT", "DELETE", "PATCH"} <= methods
    _assert_all_allowed(satellite)


@pytest.mark.anyio
async def test_mealie_v1_scope_is_allowed_too(satellite, monkeypatch):
    # A Mealie v1 server answers under groups/ instead of households/.
    monkeypatch.setattr(mealie_svc, "_scope", "groups", raising=False)
    client = MealieClient()
    await client.get_shopping_lists()
    await client.get_mealplan("2026-10-01", "2026-10-07")
    await client.delete_shopping_item("it1")
    assert any(p.startswith("api/groups/") for _b, _m, p in satellite)
    _assert_all_allowed(satellite)


@pytest.mark.anyio
async def test_the_satellite_sends_its_identification(satellite, monkeypatch):
    sent = []
    original = grocy_svc._client

    class _Spy:
        async def request(self, method, url, **kwargs):
            sent.append(kwargs.get("headers") or {})
            return await original.request(method, url, **kwargs)

    monkeypatch.setattr(grocy_svc, "_client", _Spy())
    await GrocyClient().get_stock()
    assert sent and sent[0]["X-PR-Caps"] == "relay1"
    assert sent[0]["X-PR-Client"].startswith("satellite/")


# --- static scan ---------------------------------------------------------------

# helper -> how its HTTP method is known
_HELPERS = {"_request": "arg0", "_scoped": "arg0", "_get": "GET", "_post": "POST",
            "_cached_list": "GET", "_ensure_object": "GET+POST", "_list_all": "GET"}


def _render(node) -> str | None:
    """A literal or f-string path with every placeholder as '1', else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                out.append(str(part.value))
            else:
                out.append("1")
        return "".join(out)
    return None


def _scan_sites():
    sites = []
    for path in sorted((SERVICE / "app").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        backend = "mealie" if rel.endswith("services/mealie.py") else "grocy"
        tree = ast.parse(path.read_text(), filename=rel)
        server_only = [range(fn.lineno, fn.end_lineno + 1) for fn in ast.walk(tree)
                       if isinstance(fn, ast.AsyncFunctionDef)
                       and any(isinstance(d, ast.Name) and d.id == "_server_only"
                               for d in fn.decorator_list)]
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _HELPERS):
                continue
            if any(node.lineno in lines for lines in server_only):
                continue
            how = _HELPERS[node.func.attr]
            args = list(node.args)
            if how == "arg0":
                if len(args) < 2 or not (isinstance(args[0], ast.Constant)
                                         and isinstance(args[0].value, str)):
                    continue
                methods, path_node = [args[0].value.upper()], args[1]
            else:
                if not args:
                    continue
                methods, path_node = how.split("+"), args[0]
            text = _render(path_node)
            if text is None or not text.startswith("/"):
                continue
            text = text.split("?", 1)[0]
            if backend == "mealie" and node.func.attr == "_scoped":
                fulls = ["api/households" + text, "api/groups" + text]
            else:
                fulls = ["api" + text]
            for method in methods:
                for full in fulls:
                    sites.append((rel, node.lineno, backend, method, full))
    return sites


def test_static_client_paths_are_allowed():
    sites = _scan_sites()
    assert len(sites) >= 30, sites
    files = {s[0] for s in sites}
    assert "service/app/services/grocy.py" in files
    assert "service/app/services/mealie.py" in files
    bad = []
    for rel, line, backend, method, full in sites:
        try:
            segments = proxy_policy.validate_path(full)
        except proxy_policy.ProxyPathInvalid as exc:
            bad.append((rel, line, method, full, str(exc)))
            continue
        verdict, _tpl = proxy_policy.classify(backend, method, "/".join(segments))
        if verdict != proxy_policy.VERDICT_ALLOW:
            bad.append((rel, line, method, full, verdict))
    assert not bad, f"client call sites the proxy would refuse: {bad}"


def test_every_recorded_satellite_path_is_still_allowed():
    rows = json.loads(_HISTORY.read_text())
    assert len(rows) >= 65
    for row in rows:
        segments = proxy_policy.validate_path(row["path"])
        verdict, _tpl = proxy_policy.classify(row["backend"], row["method"],
                                              "/".join(segments))
        assert verdict == proxy_policy.VERDICT_ALLOW, row


def test_enforce_is_the_default():
    from app.config import Settings
    assert Settings.model_fields["proxy_path_policy"].default == "enforce"
