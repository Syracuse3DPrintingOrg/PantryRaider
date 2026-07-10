import httpx
from datetime import date
from ..config import settings
from ..models.food import FoodItem
from ..storage_categories import classify_location, location_for


class GrocyError(Exception):
    """Raised with Grocy's actual error message instead of a bare HTTP status.

    Also raised, with an honest user-forward message, when Grocy (or the main
    server, on a satellite) cannot be reached at all, so every route that
    handles GrocyError degrades the same way during an outage instead of
    letting a raw httpx connection error bubble up as a 500.
    """


def unreachable_message() -> str:
    """The user-forward message for a dead upstream connection.

    A satellite talks to Grocy through the main server's proxy, so a connect
    failure there means the main server is gone, not Grocy itself.
    """
    if settings.is_satellite():
        return "The main server is not reachable. Inventory will return when it is."
    return "Grocy is not reachable. Inventory will return when it is."


def stock_has_product(name: str, stock: list[dict]) -> bool:
    """True when ``name`` already has a stock entry (case-insensitive match).

    Pure and testable: it only compares the given name against the names already
    present in a Grocy stock list. Used to flag a scanned item as a duplicate
    (already in inventory) without blocking the add. An empty name never matches.
    Note this is informational only: adding the same product with a different
    best-before date still creates a separate Grocy stock entry, so each scan
    keeps its own expiration (Grocy keys stock entries by best-before date).
    """
    wanted = (name or "").strip().lower()
    if not wanted:
        return False
    for entry in stock or []:
        if float(entry.get("amount") or 0) <= 0:
            continue
        product = entry.get("product") or {}
        entry_name = (product.get("name") or entry.get("name") or "").strip().lower()
        if entry_name and entry_name == wanted:
            return True
    return False


# Shared connection pool for the life of the app
_client = httpx.AsyncClient(timeout=15.0)


class GrocyClient:
    """One instance per request. Lookup tables (locations, groups, units,
    products) are cached on the instance so multi-item imports don't re-fetch
    them for every item."""

    def __init__(self):
        if settings.is_satellite() and settings.remote_server_url and settings.upstream_api_key:
            # A satellite has no Docker network, so it cannot reach the server's
            # internal Grocy (http://grocy:80). Route calls through the main
            # server's authenticated proxy, which forwards them to its Grocy.
            self.base = settings.remote_server_url.rstrip("/") + "/api/proxy/grocy/api"
            self.headers = {
                "X-API-Key": settings.upstream_api_key,
                "Content-Type": "application/json",
            }
        else:
            self.base = settings.grocy_base_url.rstrip("/") + "/api"
            self.headers = {
                "GROCY-API-KEY": settings.grocy_api_key,
                "Content-Type": "application/json",
            }
        self._cache: dict[str, list[dict]] = {}

    async def _request(self, method: str, path: str, body: dict | None = None) -> list | dict:
        try:
            r = await _client.request(
                method, f"{self.base}{path}", headers=self.headers, json=body
            )
        except httpx.HTTPError as e:
            # Connection refused, DNS failure, timeout: the service is down or
            # unreachable. Surface it as a GrocyError with honest copy so every
            # caller degrades consistently (FoodAssistant-2cmm).
            raise GrocyError(unreachable_message()) from e
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

    async def has_in_stock(self, name: str) -> bool:
        """True when a product named ``name`` currently has stock in Grocy.

        Used to flag a scanned item as already in inventory (a duplicate). It is
        informational only and never blocks adding: a later add with a different
        best-before date still lands as its own stock entry.
        """
        if not (name or "").strip():
            return False
        return stock_has_product(name, await self.get_stock())

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
        # When the item came off a back-dated receipt, land it on the receipt's
        # purchase date; otherwise Grocy defaults the entry to today.
        purchased = (
            item.purchased_on.isoformat() if item.purchased_on else date.today().isoformat()
        )
        return await self._post(f"/stock/products/{product_id}/add", {
            "amount": item.quantity,
            "best_before_date": best_before,
            "purchased_date": purchased,
            "price": None,
            "note": item.brand or "",
        })

    async def consume_stock(self, product_id: int, amount: float = 1.0) -> dict:
        return await self._post(f"/stock/products/{product_id}/consume", {
            "amount": amount,
            "spoiled": False,
        })

    async def consume_by_barcode(self, barcode: str, amount: float = 1.0) -> dict:
        """Consume stock for the product carrying ``barcode`` (Grocy native).

        Grocy resolves the barcode to its product, so the scanner can use up an
        item without a separate lookup. Raises GrocyError (via _post) when the
        barcode is unknown or there is no stock to consume.
        """
        return await self._post(
            f"/stock/products/by-barcode/{barcode}/consume",
            {"amount": amount, "spoiled": False},
        )

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

    async def get_full_stock(self) -> list[dict]:
        """Return all stock entries enriched with name, location, days_remaining, urgency, and storage bucket."""
        raw = await self._get("/stock")
        locations = {
            str(loc["id"]): loc["name"]
            for loc in await self._cached_list("/objects/locations")
        }
        groups = {
            str(g["id"]): g["name"]
            for g in await self._cached_list("/objects/product_groups")
        }

        # /stock aggregates per product and drops timestamps; the raw stock
        # table has row_created_timestamp per entry: take the newest per
        # product as "date added".
        added: dict[int, str] = {}
        for row in await self._get("/objects/stock"):
            pid = int(row.get("product_id") or 0)
            ts = row.get("row_created_timestamp") or row.get("purchased_date") or ""
            if pid and ts and ts > added.get(pid, ""):
                added[pid] = ts
        today = date.today()
        result = []
        for entry in raw:
            product = entry.get("product") or {}
            name = product.get("name") or f"Product {entry.get('product_id', '?')}"
            # Prefer the per-entry location_id; fall back to the product's default
            loc_id = str(entry.get("location_id") or product.get("location_id") or "")
            loc_name = locations.get(loc_id, "")
            bucket = classify_location(loc_name)

            bbd = entry.get("best_before_date")
            if bbd:
                d = date.fromisoformat(bbd)
                days_remaining = (d - today).days
                if days_remaining < 0:
                    urgency = "expired"
                elif days_remaining == 0:
                    urgency = "today"
                elif days_remaining <= 3:
                    urgency = "3d"
                elif days_remaining <= 7:
                    urgency = "7d"
                else:
                    urgency = "ok"
            else:
                days_remaining = None
                urgency = "unknown"

            pid = int(entry.get("product_id", 0))
            group_id = str(product.get("product_group_id") or "")
            result.append({
                "product_id": pid,
                "name": name,
                "amount": float(entry.get("amount") or 0),
                "unit": product.get("qu_unit_stock", {}).get("name") if product.get("qu_unit_stock") else None,
                "best_before_date": bbd,
                "days_remaining": days_remaining,
                "urgency": urgency,
                "location_name": loc_name,
                "storage_bucket": bucket,
                "category": groups.get(group_id, ""),
                "added_date": added.get(pid),
            })
        return result

    async def edit_product(self, product_id: int,
                           category: str | None = None,
                           best_before_date: str | None = None) -> dict:
        """Update category (product group) and/or best-by date for every open stock entry."""
        if category is not None:
            group_id = await self.ensure_product_group(category)
            await self._request("PUT", f"/objects/products/{product_id}",
                                {"product_group_id": group_id})

        if best_before_date is not None:
            entries = await self._get(f"/stock/products/{product_id}/entries")
            for entry in entries:
                entry_id = entry.get("id") or entry.get("stock_id")
                if not entry_id:
                    continue
                await self._request("PUT", f"/objects/stock/{entry_id}",
                                    {**entry, "best_before_date": best_before_date})

        return {"product_id": product_id}

    async def move_product(self, product_id: int, bucket: str) -> dict:
        """Transfer all stock of a product to the location for `bucket` and
        make that the product's default location."""
        to_name = location_for(bucket)
        if not to_name:
            raise GrocyError(f"Unknown storage bucket: {bucket}")
        to_id = await self.ensure_location(to_name)

        entries = await self._get(f"/stock/products/{product_id}/entries")
        moved = 0.0
        for entry in entries:
            amount = float(entry.get("amount") or 0)
            from_id = int(entry.get("location_id") or 0)
            if amount <= 0 or from_id == to_id:
                continue
            if from_id:
                await self._post(f"/stock/products/{product_id}/transfer", {
                    "amount": amount,
                    "location_id_from": from_id,
                    "location_id_to": to_id,
                })
                moved += amount

        # Entries without a location can't be transferred, but changing the
        # product's default location still re-buckets them on the dashboard.
        await self._request("PUT", f"/objects/products/{product_id}",
                            {"location_id": to_id})
        return {"product_id": product_id, "moved_amount": moved, "location_id": to_id}

    async def product_id_by_name(self, name: str) -> int | None:
        """The id of the product named ``name`` (case-insensitive), or None."""
        for p in await self.get_products():
            if p["name"].lower() == name.lower():
                return int(p["id"])
        return None

    async def ensure_product_barcode(self, product_id: int, barcode: str) -> bool:
        """Register ``barcode`` on ``product_id`` unless it is already known.

        Grocy's by-barcode endpoints only resolve barcodes recorded in its
        product_barcodes table; without this, an item added through the app
        could never be consumed by scanning it. Returns True when a new
        barcode row was created.
        """
        barcode = (barcode or "").strip()
        if not barcode:
            return False
        existing = await self._get(
            f"/objects/product_barcodes?query%5B%5D=barcode%3D{barcode}"
        )
        if existing:
            return False
        await self._post("/objects/product_barcodes",
                         {"product_id": product_id, "barcode": barcode})
        return True

    async def import_item(self, item: FoodItem) -> dict:
        storage_name = _STORAGE_LABEL[item.storage_type.value]
        location_id = await self.ensure_location(storage_name)
        group_id = await self.ensure_product_group(item.category.value)
        product_id = await self.ensure_product(item, location_id, group_id)
        await self.add_stock(product_id, item)
        # Link the scanned barcode to the product so a later consume-mode scan
        # can resolve it. Best effort: a failure here must not lose the stock
        # add that already happened.
        if getattr(item, "barcode", None):
            try:
                await self.ensure_product_barcode(product_id, item.barcode)
            except Exception:  # noqa: BLE001
                pass
        return {"product_id": product_id, "name": item.name}

    async def get_shopping_lists(self) -> list[dict]:
        return await self._cached_list("/objects/shopping_lists")

    async def ensure_shopping_list(self) -> int:
        """Return the first shopping list id, creating one if none exist."""
        lists = await self.get_shopping_lists()
        if lists:
            return int(lists[0]["id"])
        result = await self._post("/objects/shopping_lists", {"name": "Shopping list"})
        new_id = int(result["created_object_id"])
        lists.append({"id": new_id, "name": "Shopping list"})
        return new_id

    async def get_shopping_items(self, list_id: int) -> list[dict]:
        items = await self._get(
            f"/objects/shopping_list_items?query%5B%5D=shopping_list_id%3D{list_id}"
        )
        products = {str(p["id"]): p["name"] for p in await self.get_products()}
        for item in items:
            pid = item.get("product_id")
            item["product_name"] = products.get(str(pid), "") if pid else ""
        return sorted(items, key=lambda x: int(x.get("id") or 0))

    async def add_shopping_item(self, list_id: int, note: str, amount: float = 1.0,
                                product_id: int | None = None) -> dict:
        body = {
            "shopping_list_id": list_id,
            "note": note,
            "amount": amount,
            "done": 0,
        }
        # Optional product link: when the caller resolved the text to a known
        # Grocy product, carry its id so Grocy shows the item as that product.
        if product_id is not None:
            body["product_id"] = product_id
        return await self._post("/objects/shopping_list_items", body)

    async def toggle_shopping_item(self, item_id: int, done: bool) -> None:
        row = await self._get(f"/objects/shopping_list_items/{item_id}")
        await self._request("PUT", f"/objects/shopping_list_items/{item_id}",
                            {**row, "done": int(done)})

    async def delete_shopping_item(self, item_id: int) -> None:
        await self._request("DELETE", f"/objects/shopping_list_items/{item_id}")

    async def clear_done_shopping_items(self, list_id: int) -> int:
        items = await self.get_shopping_items(list_id)
        done_ids = [int(i["id"]) for i in items if i.get("done")]
        for iid in done_ids:
            await self.delete_shopping_item(iid)
        return len(done_ids)

    async def get_restock_suggestions(self, days: int = 30, min_consumes: int = 2) -> list[dict]:
        """Return products that were consumed recently but are now out of stock.

        Looks at 'consume' and 'product-opened' transactions in the last ``days``
        days, counts occurrences per product, and cross-checks against current
        stock. Returns products with at least ``min_consumes`` consume events and
        zero stock remaining, sorted by consume frequency (most frequent first).
        """
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

        log_rows = await self._get(
            "/objects/stock_log?order=row_created_timestamp%3Adesc&limit=500"
        )
        products = {str(p["id"]): p["name"] for p in await self.get_products()}
        stock = {str(e.get("product_id")): float(e.get("amount") or 0)
                 for e in await self._get("/stock")}

        consume_counts: dict[str, int] = {}
        for row in log_rows:
            ts = (row.get("row_created_timestamp") or "")[:10]
            if ts < cutoff:
                continue
            if row.get("transaction_type") not in ("consume", "product-opened"):
                continue
            pid = str(row.get("product_id") or "")
            if pid:
                consume_counts[pid] = consume_counts.get(pid, 0) + 1

        suggestions = []
        for pid, count in consume_counts.items():
            if count < min_consumes:
                continue
            if float(stock.get(pid, 0)) > 0:
                continue
            suggestions.append({
                "product_id": int(pid),
                "product_name": products.get(pid, f"Product {pid}"),
                "consume_count": count,
                "days": days,
            })
        suggestions.sort(key=lambda x: -x["consume_count"])
        return suggestions

    async def get_stock_log(self, limit: int = 50) -> list[dict]:
        """Return recent stock log entries, newest first, enriched with product names."""
        rows = await self._get(f"/objects/stock_log?limit={limit}&order=row_created_timestamp%3Adesc")
        products = {str(p["id"]): p["name"] for p in await self.get_products()}
        result = []
        for row in rows:
            pid = str(row.get("product_id") or "")
            result.append({
                "id": row.get("id"),
                "timestamp": (row.get("row_created_timestamp") or "")[:19],
                "product_name": products.get(pid, f"Product {pid}"),
                "transaction_type": row.get("transaction_type") or "",
                "amount": float(row.get("amount") or 0),
                "note": row.get("note") or "",
                "location_id": row.get("location_id"),
            })
        return result

    async def health_check(self) -> bool:
        try:
            await self._get("/system/info")
            return True
        except Exception:
            return False


# StorageType enum value → Grocy location name, used by import_item. The enum
# has "dry" where the dashboard uses the "pantry" bucket; both name the same
# Grocy location. Custom categories are move-only and not reachable here.
_STORAGE_LABEL = {
    "refrigerated": "Refrigerator",
    "frozen": "Freezer",
    "room_temp": "Counter / Room Temp",
    "dry": "Pantry / Dry Storage",
}
