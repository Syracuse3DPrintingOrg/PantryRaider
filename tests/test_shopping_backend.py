"""The shopping-list backend seam (FoodAssistant-g0fd).

Covers:
  * shopping_source.active_backend derivation: explicit setting wins; auto
    follows Grocy except on a Mealie-recipes install with Mealie configured.
  * The /mealie/shopping* endpoints answering the SAME wire shapes from a
    Grocy list: GET list (lists/list/items with Mealie field names), add,
    toggle via the full-item PUT the pages send, delete, clear-done, the HA
    summary, and the deck count.
  * The foods typeahead suggesting Grocy product names in grocy mode.
  * add-missing sending a native recipe's missing ingredients to Grocy
    (no more Mealie-required 400) and add-items doing the same.
  * The quick-add and autocheck helpers routing through the seam.

GrocyClient methods are monkeypatched (no network); the app's real SQLite is
used for the native recipe half.
"""
import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SERVICE_DIR = Path(__file__).parent.parent / "service"

_TAG = uuid.uuid4().hex[:8]


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    cwd = os.getcwd()
    os.chdir(_SERVICE_DIR)
    try:
        from app.config import settings

        data_dir = tmp_path_factory.mktemp("data")
        settings.data_dir = str(data_dir)

        from app.main import app

        settings.grocy_base_url = "http://grocy.test"
        settings.grocy_api_key = "test-grocy-key"
        # Native install with no Mealie: shopping must run entirely on Grocy.
        settings.mealie_base_url = ""
        settings.mealie_api_key = ""
        settings.recipes_backend = ""
        settings.shopping_backend = ""
        settings.auth_required = False
        settings.auth_password = ""

        with TestClient(app) as c:
            yield c
    finally:
        os.chdir(cwd)


@pytest.fixture(autouse=True)
def reset_backends():
    from app.config import settings
    settings.recipes_backend = ""
    settings.shopping_backend = ""
    settings.mealie_base_url = ""
    settings.mealie_api_key = ""
    yield
    settings.recipes_backend = ""
    settings.shopping_backend = ""
    settings.mealie_base_url = ""
    settings.mealie_api_key = ""


# ── A fake Grocy shopping store ──────────────────────────────────────────────

class FakeGrocyStore:
    """In-memory stand-in for Grocy's shopping tables, patched onto GrocyClient."""

    def __init__(self):
        self.lists = [{"id": 1, "name": "Shopping list"}]
        self.items: dict[int, dict] = {}
        self.products = [{"id": 7, "name": "Milk"}, {"id": 8, "name": "Almond milk"}]
        self.next_id = 100

    def install(self, monkeypatch):
        from app.services.grocy import GrocyClient
        store = self

        async def get_shopping_lists(self):
            return list(store.lists)

        async def ensure_shopping_list(self):
            return int(store.lists[0]["id"])

        async def get_shopping_items(self, list_id):
            products = {str(p["id"]): p["name"] for p in store.products}
            rows = [dict(i) for i in store.items.values()
                    if int(i["shopping_list_id"]) == int(list_id)]
            for r in rows:
                pid = r.get("product_id")
                r["product_name"] = products.get(str(pid), "") if pid else ""
            return sorted(rows, key=lambda x: int(x["id"]))

        async def add_shopping_item(self, list_id, note, amount=1.0, product_id=None):
            store.next_id += 1
            store.items[store.next_id] = {
                "id": store.next_id, "shopping_list_id": int(list_id),
                "note": note, "amount": amount, "done": 0,
                "product_id": product_id,
            }
            return {"created_object_id": store.next_id}

        async def toggle_shopping_item(self, item_id, done):
            store.items[int(item_id)]["done"] = int(done)

        async def delete_shopping_item(self, item_id):
            store.items.pop(int(item_id), None)

        async def clear_done_shopping_items(self, list_id):
            done = [i for i, row in store.items.items() if row.get("done")]
            for i in done:
                store.items.pop(i)
            return len(done)

        async def get_products(self):
            return list(store.products)

        async def product_id_by_name(self, name):
            for p in store.products:
                if p["name"].lower() == (name or "").lower():
                    return int(p["id"])
            return None

        monkeypatch.setattr(GrocyClient, "get_shopping_lists", get_shopping_lists)
        monkeypatch.setattr(GrocyClient, "ensure_shopping_list", ensure_shopping_list)
        monkeypatch.setattr(GrocyClient, "get_shopping_items", get_shopping_items)
        monkeypatch.setattr(GrocyClient, "add_shopping_item", add_shopping_item)
        monkeypatch.setattr(GrocyClient, "toggle_shopping_item", toggle_shopping_item)
        monkeypatch.setattr(GrocyClient, "delete_shopping_item", delete_shopping_item)
        monkeypatch.setattr(GrocyClient, "clear_done_shopping_items", clear_done_shopping_items)
        monkeypatch.setattr(GrocyClient, "get_products", get_products)
        monkeypatch.setattr(GrocyClient, "product_id_by_name", product_id_by_name)
        return store


@pytest.fixture()
def grocy_store(monkeypatch):
    return FakeGrocyStore().install(monkeypatch)


# ── Backend rule ─────────────────────────────────────────────────────────────

def test_shopping_backend_rule(client):
    from app.config import settings
    from app.services import shopping_source

    # Native recipes, no Mealie: shopping follows Grocy.
    assert shopping_source.active_backend() == "grocy"

    # Mealie recipes still in use: the Mealie list is kept.
    settings.mealie_base_url = "http://mealie.test"
    settings.mealie_api_key = "key"
    settings.recipes_backend = "mealie"
    assert shopping_source.active_backend() == "mealie"

    # After the recipe migration the list follows Grocy, even with Mealie
    # still connected as an import source.
    settings.recipes_backend = "native"
    assert shopping_source.active_backend() == "grocy"

    # An explicit choice always wins.
    settings.shopping_backend = "mealie"
    assert shopping_source.active_backend() == "mealie"
    settings.shopping_backend = "grocy"
    settings.recipes_backend = "mealie"
    assert shopping_source.active_backend() == "grocy"


# ── Wire shapes over Grocy ───────────────────────────────────────────────────

def test_get_shopping_wire_shape_from_grocy(client, grocy_store):
    r = client.post("/mealie/shopping/items",
                    json={"list_id": "", "note": "Milk", "quantity": 2})
    assert r.status_code == 200
    item_id = r.json()["id"]
    assert item_id

    data = client.get("/mealie/shopping").json()
    # The exact fields the Shopping page, deck, and HA sensor read.
    assert data["list"]["name"] == "Shopping list"
    assert [l["name"] for l in data["lists"]] == ["Shopping list"]
    (item,) = data["items"]
    assert item["id"] == item_id
    assert item["note"] == "Milk"
    assert item["checked"] is False
    assert item["quantity"] == 2
    assert item["display"] == "2 x Milk"
    # "Milk" matched a Grocy product, so it rides product-linked.
    assert item["food"] == {"name": "Milk"}


def test_toggle_delete_and_clear_done_over_grocy(client, grocy_store):
    a = client.post("/mealie/shopping/items", json={"list_id": "1", "note": "Apples"}).json()["id"]
    b = client.post("/mealie/shopping/items", json={"list_id": "1", "note": "Bread"}).json()["id"]

    # The pages PUT the whole item back with checked flipped; the seam only
    # needs the flag.
    item = next(i for i in client.get("/mealie/shopping").json()["items"] if i["id"] == a)
    r = client.put(f"/mealie/shopping/items/{a}", json={**item, "checked": True})
    assert r.status_code == 200
    items = client.get("/mealie/shopping").json()["items"]
    assert next(i for i in items if i["id"] == a)["checked"] is True
    # Checked items sort last.
    assert [i["id"] for i in items] == [b, a]

    assert client.delete(f"/mealie/shopping/items/{b}").status_code == 200
    r = client.post("/mealie/shopping/clear-done", json={"list_id": "1"})
    assert r.status_code == 200 and r.json()["removed"] == 1
    assert client.get("/mealie/shopping").json()["items"] == []


def test_summary_and_count_over_grocy(client, grocy_store):
    client.post("/mealie/shopping/items", json={"list_id": "1", "note": "Eggs"})
    client.post("/mealie/shopping/items", json={"list_id": "1", "note": "Beans"})
    ids = [i["id"] for i in client.get("/mealie/shopping").json()["items"]]
    client.put(f"/mealie/shopping/items/{ids[0]}", json={"checked": True})

    s = client.get("/mealie/shopping/summary").json()
    assert s["count"] == 1 and len(s["items"]) == 1
    assert s["list_name"] == "Shopping list"
    assert client.get("/mealie/shopping/count").json() == {"count": 1}


def test_shopping_degrades_with_error_field(client, monkeypatch):
    from app.services.grocy import GrocyClient, GrocyError

    async def boom(self):
        raise GrocyError("Grocy is not reachable. Inventory will return when it is.")

    monkeypatch.setattr(GrocyClient, "ensure_shopping_list", boom)
    data = client.get("/mealie/shopping").json()
    assert data["items"] == [] and data["list"] is None
    assert "not reachable" in data["error"]
    assert client.get("/mealie/shopping/count").json() == {"count": 0}


# ── Grocy entity path (regression) ───────────────────────────────────────────

def test_grocy_shopping_uses_the_shopping_list_entity(monkeypatch):
    """Grocy stores shopping items in the 'shopping_list' object, not
    'shopping_list_items' (which does not exist and 400s with "Entity does not
    exist or is not exposed"). The higher-level tests stub the client methods,
    so this one exercises the real GrocyClient against a recording transport to
    pin the actual REST path (regression for the Korolev empty-list bug)."""
    import asyncio
    from app.config import settings
    from app.services import grocy as grocy_mod
    from app.services.grocy import GrocyClient

    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)

    calls = []

    class _Resp:
        status_code = 200
        content = b"[]"
        reason_phrase = "OK"
        text = "[]"

        def json(self):
            return []

    class _FakeClient:
        async def request(self, method, url, headers=None, json=None):
            calls.append((method, url))
            return _Resp()

    monkeypatch.setattr(grocy_mod, "_client", _FakeClient())

    g = GrocyClient()
    asyncio.run(g.get_shopping_items(1))
    asyncio.run(g.delete_shopping_item(7))

    paths = [url for _, url in calls]
    assert any("/api/objects/shopping_list?" in p for p in paths), paths
    assert any(p.endswith("/api/objects/shopping_list/7") for p in paths), paths
    # The non-existent entity must never be requested.
    assert not any("shopping_list_items" in p for p in paths), paths


# ── Typeahead ────────────────────────────────────────────────────────────────

def test_foods_suggest_from_grocy_products(client, grocy_store):
    d = client.get("/mealie/foods/suggest", params={"q": "milk"}).json()
    # Prefix hits first, then names that merely contain the text.
    assert d["suggestions"] == ["Milk", "Almond milk"]
    assert client.get("/mealie/foods/suggest", params={"q": ""}).json() == {"suggestions": []}


def test_foods_suggest_keeps_mealie_on_mealie_backend(client, monkeypatch):
    from app.config import settings
    from app.services.mealie import MealieClient

    settings.shopping_backend = "mealie"
    settings.mealie_base_url = "http://mealie.test"
    settings.mealie_api_key = "key"

    async def fake_suggest(self, prefix, limit=8):
        return ["Mealie Milk"]

    monkeypatch.setattr(MealieClient, "suggest_foods", fake_suggest)
    d = client.get("/mealie/foods/suggest", params={"q": "mi"}).json()
    assert d["suggestions"] == ["Mealie Milk"]


# ── add-missing and add-items over Grocy ─────────────────────────────────────

def test_add_missing_lands_on_grocy_list(client, grocy_store, monkeypatch):
    from app.services.grocy import GrocyClient

    async def empty_stock(self):
        return []

    monkeypatch.setattr(GrocyClient, "get_full_stock", empty_stock)

    name = f"Saffron Bake {_TAG}"
    r = client.post("/mealie/recipes/create", json={
        "name": name,
        "ingredients": ["saffron threads", "boiling water"],
        "instructions": ["Combine."],
    })
    assert r.status_code == 200
    slug = r.json()["slug"]
    try:
        r = client.post("/mealie/suggest/add-missing", json={"slug": slug})
        assert r.status_code == 200
        d = r.json()
        # Water is a freebie; only the real ingredient is added.
        assert d["added"] == 1 and d["items"] == ["saffron threads"]
        assert d["list_name"] == "Shopping list"
        notes = [i["note"] for i in client.get("/mealie/shopping").json()["items"]]
        assert "saffron threads" in notes
    finally:
        client.delete(f"/recipes/{slug}")


def test_add_items_lands_on_grocy_list(client, grocy_store):
    r = client.post("/mealie/shopping/add-items",
                    json={"items": ["capers", " ", "harissa"]})
    assert r.status_code == 200
    d = r.json()
    assert d["added"] == 2 and d["items"] == ["capers", "harissa"]
    notes = [i["note"] for i in client.get("/mealie/shopping").json()["items"]]
    assert {"capers", "harissa"} <= set(notes)


# ── Shared quick-add and autocheck helpers ───────────────────────────────────

@pytest.mark.anyio
async def test_quick_add_routes_to_grocy(client, grocy_store):
    from app.services import shopping_source
    name = await shopping_source.quick_add("Oat milk")
    assert name == "Shopping list"
    assert any(i["note"] == "Oat milk" for i in grocy_store.items.values())


@pytest.mark.anyio
async def test_autocheck_ticks_grocy_items(client, grocy_store):
    grocy_store.items[901] = {"id": 901, "shopping_list_id": 1,
                              "note": "whole milk", "amount": 1, "done": 0,
                              "product_id": None}
    grocy_store.items[902] = {"id": 902, "shopping_list_id": 1,
                              "note": "bread", "amount": 1, "done": 0,
                              "product_id": None}
    from app.services import shopping_source
    await shopping_source.autocheck("Milk")
    assert grocy_store.items[901]["done"] == 1
    assert grocy_store.items[902]["done"] == 0


# ── Mealie mode stays byte-compatible ────────────────────────────────────────

def test_mealie_mode_shopping_unchanged(client, monkeypatch):
    from app.config import settings
    from app.services.mealie import MealieClient

    settings.recipes_backend = "mealie"
    settings.mealie_base_url = "http://mealie.test"
    settings.mealie_api_key = "key"

    calls = {}

    async def get_lists(self):
        return [{"id": "L1", "name": "Groceries"}]

    async def get_list(self, list_id):
        calls["listed"] = list_id
        return {"listItems": [
            {"id": "i1", "note": "milk", "checked": False, "display": "milk"},
        ]}

    async def update_item(self, item_id, item):
        calls["updated"] = (item_id, item.get("checked"))
        return {}

    monkeypatch.setattr(MealieClient, "get_shopping_lists", get_lists)
    monkeypatch.setattr(MealieClient, "get_shopping_list", get_list)
    monkeypatch.setattr(MealieClient, "update_shopping_item", update_item)

    data = client.get("/mealie/shopping").json()
    assert data["list"] == {"id": "L1", "name": "Groceries"}
    assert data["items"][0]["note"] == "milk"

    r = client.put("/mealie/shopping/items/i1", json={"id": "i1", "checked": True})
    assert r.status_code == 200
    assert calls["updated"] == ("i1", True)

    # clear-done is a Grocy-list affordance only.
    assert client.post("/mealie/shopping/clear-done", json={}).status_code == 400
