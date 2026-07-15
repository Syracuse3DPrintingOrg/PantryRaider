"""Consume-by-barcode: barcode registration at import and legacy self-heal."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))

from app.models.food import FoodItem  # noqa: E402
from app.services.grocy import GrocyClient  # noqa: E402


def _client(monkeypatch, *, products=None, barcodes=None, posts=None):
    posts = posts if posts is not None else []
    products = products or []
    barcodes = barcodes if barcodes is not None else []

    async def fake_get(self, path):
        if path.startswith("/objects/product_barcodes"):
            return barcodes
        if path == "/objects/products":
            return products
        return []

    async def fake_post(self, path, payload):
        posts.append((path, payload))
        return {"created_object_id": 99}

    monkeypatch.setattr(GrocyClient, "_get", fake_get)
    monkeypatch.setattr(GrocyClient, "_post", fake_post)
    monkeypatch.setattr(GrocyClient, "get_products",
                        lambda self: fake_get(self, "/objects/products"))
    return GrocyClient(), posts


def test_ensure_product_barcode_registers_new(monkeypatch):
    c, posts = _client(monkeypatch)
    created = asyncio.run(c.ensure_product_barcode(7, "078000035483"))
    assert created is True
    assert posts == [("/objects/product_barcodes",
                      {"product_id": 7, "barcode": "078000035483"})]


def test_ensure_product_barcode_skips_known(monkeypatch):
    c, posts = _client(monkeypatch, barcodes=[{"id": 1, "barcode": "x"}])
    assert asyncio.run(c.ensure_product_barcode(7, "x")) is False
    assert posts == []


def test_ensure_product_barcode_ignores_blank(monkeypatch):
    c, posts = _client(monkeypatch)
    assert asyncio.run(c.ensure_product_barcode(7, "  ")) is False
    assert posts == []


def test_import_item_links_the_scanned_barcode(monkeypatch):
    c, posts = _client(monkeypatch)

    async def fake_ensure_location(self, name):
        return 1

    async def fake_ensure_group(self, name):
        return 2

    async def fake_ensure_product(self, item, lid, gid):
        return 42

    async def fake_add_stock(self, pid, item):
        return {}

    monkeypatch.setattr(GrocyClient, "ensure_location", fake_ensure_location)
    monkeypatch.setattr(GrocyClient, "ensure_product_group", fake_ensure_group)
    monkeypatch.setattr(GrocyClient, "ensure_product", fake_ensure_product)
    monkeypatch.setattr(GrocyClient, "add_stock", fake_add_stock)

    item = FoodItem(name="Jiffy Corn Muffin Mix", barcode="078000035483")
    result = asyncio.run(c.import_item(item))
    assert result["product_id"] == 42
    assert ("/objects/product_barcodes",
            {"product_id": 42, "barcode": "078000035483"}) in posts


def test_import_item_without_barcode_registers_nothing(monkeypatch):
    c, posts = _client(monkeypatch)
    for name in ("ensure_location", "ensure_product_group"):
        async def fake(self, *a, _n=name):
            return 1
        monkeypatch.setattr(GrocyClient, name, fake)

    async def fake_ensure_product(self, item, lid, gid):
        return 42

    async def fake_add_stock(self, pid, item):
        return {}

    monkeypatch.setattr(GrocyClient, "ensure_product", fake_ensure_product)
    monkeypatch.setattr(GrocyClient, "add_stock", fake_add_stock)
    asyncio.run(c.import_item(FoodItem(name="Loose Apples")))
    assert not any(p[0] == "/objects/product_barcodes" for p in posts)


def test_product_id_by_name_case_insensitive(monkeypatch):
    c, _ = _client(monkeypatch,
                   products=[{"id": 5, "name": "Jiffy Corn Muffin Mix"}])
    assert asyncio.run(c.product_id_by_name("jiffy corn muffin mix")) == 5
    assert asyncio.run(c.product_id_by_name("Nope")) is None
