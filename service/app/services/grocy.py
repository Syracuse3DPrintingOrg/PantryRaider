import httpx
from datetime import date
from ..config import settings
from ..models.food import FoodItem


class GrocyError(Exception):
    """Raised with Grocy's actual error message instead of a bare HTTP status."""


# Shared connection pool for the life of the app
_client = httpx.AsyncClient(timeout=15.0)


class GrocyClient:
    """One instance per request. Lookup tables (locations, groups, units,
    products) are cached on the instance so multi-item imports don't re-fetch
    them for every item."""

    def __init__(self):
        self.base = settings.grocy_base_url.rstrip("/") + "/api"
        self.headers = {
            "GROCY-API-KEY": settings.grocy_api_key,
            "Content-Type": "application/json",
        }
        self._cache: dict[str, list[dict]] = {}

    async def _request(self, method: str, path: str, body: dict | None = None) -> list | dict:
        r = await _client.request(
            method, f"{self.base}{path}", headers=self.headers, json=body
        )
        if r.status_code >= 400:
            detail = r.text[:300].strip() or r.reason_phrase
            raise GrocyError(f"Grocy {r.status_code} on {path}: {detail}")
        return r.json() if r.content else {}

    async def _get(self, path: str) -> list | dict:
        return await self._request("GET", path)

    async def _post(self, path: str, body: dict) -> dict:
        return await self._request("POST", path, body)

    async def _cached_list(self, path: str) -> list[dict]:
        if path not in self._cache:
            self._cache[path] = await self._get(path)
        return self._cache[path]

    async def get_products(self) -> list[dict]:
        return await self._cached_list("/objects/products")

    async def get_stock(self) -> list[dict]:
        return await self._get("/stock")

    async def _ensure_object(self, path: str, name: str, extra: dict | None = None) -> int:
        """Find an object by name (case-insensitive) or create it. Updates cache."""
        rows = await self._cached_list(path)
        for row in rows:
            if row["name"].lower() == name.lower():
                return int(row["id"])
        result = await self._post(path, {"name": name, **(extra or {})})
        new_id = int(result["created_object_id"])
        rows.append({"id": new_id, "name": name})
        return new_id

    async def ensure_location(self, name: str) -> int:
        return await self._ensure_object("/objects/locations", name)

    async def ensure_product_group(self, name: str) -> int:
        return await self._ensure_object("/objects/product_groups", name)

    async def ensure_quantity_unit(self, name: str = "Piece") -> int:
        rows = await self._cached_list("/objects/quantity_units")
        for row in rows:
            if row["name"].lower() in (name.lower(), name.lower() + "s"):
                return int(row["id"])
        result = await self._post(
            "/objects/quantity_units", {"name": name, "name_plural": name + "s"}
        )
        new_id = int(result["created_object_id"])
        rows.append({"id": new_id, "name": name})
        return new_id

    async def ensure_product(self, item: FoodItem, location_id: int, group_id: int) -> int:
        products = await self.get_products()
        name_lower = item.name.lower()
        for p in products:
            if p["name"].lower() == name_lower:
                return int(p["id"])
        qu_id = await self.ensure_quantity_unit("Piece")
        result = await self._post("/objects/products", {
            "name": item.name,
            "location_id": location_id,
            "product_group_id": group_id,
            "qu_id_purchase": qu_id,
            "qu_id_stock": qu_id,
            "default_best_before_days": -1,
            "description": item.notes or "",
        })
        new_id = int(result["created_object_id"])
        products.append({"id": new_id, "name": item.name})
        return new_id

    async def add_stock(self, product_id: int, item: FoodItem) -> dict:
        best_before = (
            item.best_by_date.isoformat() if item.best_by_date else date.today().isoformat()
        )
        return await self._post(f"/stock/products/{product_id}/add", {
            "amount": item.quantity,
            "best_before_date": best_before,
            "price": None,
            "note": item.brand or "",
        })

    async def consume_stock(self, product_id: int, amount: float = 1.0) -> dict:
        return await self._post(f"/stock/products/{product_id}/consume", {
            "amount": amount,
            "spoiled": False,
        })

    async def get_expiring(self, days: int = 7) -> list[dict]:
        stock = await self.get_stock()
        today = date.today()
        expiring = []
        for entry in stock:
            if not entry.get("best_before_date"):
                continue
            best_before = date.fromisoformat(entry["best_before_date"])
            delta = (best_before - today).days
            if delta <= days:
                expiring.append({**entry, "days_remaining": delta})
        expiring.sort(key=lambda x: x["days_remaining"])
        return expiring

    async def import_item(self, item: FoodItem) -> dict:
        storage_name = _STORAGE_LABEL[item.storage_type.value]
        location_id = await self.ensure_location(storage_name)
        group_id = await self.ensure_product_group(item.category.value)
        product_id = await self.ensure_product(item, location_id, group_id)
        await self.add_stock(product_id, item)
        return {"product_id": product_id, "name": item.name}

    async def health_check(self) -> bool:
        try:
            await self._get("/system/info")
            return True
        except Exception:
            return False


_STORAGE_LABEL = {
    "refrigerated": "Refrigerator",
    "frozen": "Freezer",
    "room_temp": "Counter / Room Temp",
    "dry": "Pantry / Dry Storage",
}
