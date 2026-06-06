import httpx
from datetime import date
from ..config import settings
from ..models.food import FoodItem


class GrocyClient:
    def __init__(self):
        self.base = settings.grocy_base_url.rstrip("/") + "/api"
        self.headers = {
            "GROCY-API-KEY": settings.grocy_api_key,
            "Content-Type": "application/json",
        }

    async def _get(self, path: str) -> list | dict:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{self.base}{path}", headers=self.headers)
            r.raise_for_status()
            return r.json()

    async def _post(self, path: str, body: dict) -> dict:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(f"{self.base}{path}", headers=self.headers, json=body)
            r.raise_for_status()
            return r.json() if r.content else {}

    async def get_products(self) -> list[dict]:
        return await self._get("/objects/products")

    async def get_product_groups(self) -> list[dict]:
        return await self._get("/objects/product_groups")

    async def get_locations(self) -> list[dict]:
        return await self._get("/objects/locations")

    async def get_stock(self) -> list[dict]:
        return await self._get("/stock")

    async def ensure_location(self, name: str) -> int:
        locations = await self.get_locations()
        for loc in locations:
            if loc["name"].lower() == name.lower():
                return loc["id"]
        result = await self._post("/objects/locations", {"name": name})
        return result["created_object_id"]

    async def ensure_product_group(self, name: str) -> int:
        groups = await self.get_product_groups()
        for g in groups:
            if g["name"].lower() == name.lower():
                return g["id"]
        result = await self._post("/objects/product_groups", {"name": name})
        return result["created_object_id"]

    async def ensure_product(self, item: FoodItem, location_id: int, group_id: int) -> int:
        products = await self.get_products()
        name_lower = item.name.lower()
        for p in products:
            if p["name"].lower() == name_lower:
                return p["id"]
        result = await self._post("/objects/products", {
            "name": item.name,
            "location_id": location_id,
            "product_group_id": group_id,
            "qu_id_purchase": 1,
            "qu_id_stock": 1,
            "qu_factor_purchase_to_stock": 1,
            "default_best_before_days": -1,
            "description": item.notes or "",
        })
        return result["created_object_id"]

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
