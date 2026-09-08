import html as _html
import re

import httpx
from datetime import date, timedelta
from ..config import settings
from ..models.food import FoodItem
from ..storage_categories import classify_location, location_for


def sniff_test_new_date(old_iso: str | None, delta_days: int,
                        today: date | None = None) -> str | None:
    """The new best-by after a passed sniff test. Pure.

    The sniff happens today and the item is good NOW, so an already-expired
    date extends from today, not from the stale date (a +3 on something four
    days past would otherwise still land in the past). A future date simply
    gains the extra days. An undated entry stays undated (None).
    """
    if not old_iso:
        return None
    if today is None:
        today = date.today()
    old = date.fromisoformat(old_iso)
    return (max(old, today) + timedelta(days=delta_days)).isoformat()


def stock_entry_edit_body(entry: dict, new_date: str) -> dict:
    """The Grocy body that moves one stock entry's best-by and nothing else. Pure.

    Grocy does not expose ``stock`` through its generic /objects API (a PUT
    there answers "Entity does not exist or is not exposed"), so entries are
    edited through the dedicated /stock/entry endpoint. That endpoint restates
    the whole row, so the entry's own amount, location, and open flag are
    passed straight back through.

    Price is the trap: the endpoint writes whatever it is given, so sending a
    default of 0 for an entry that never had a price stamps a real 0 onto it
    and quietly corrupts Grocy's inventory value and price history. An unpriced
    entry therefore omits the field entirely, which leaves it null.
    """
    body = {
        "amount": entry.get("amount"),
        "best_before_date": new_date,
        "location_id": entry.get("location_id"),
        "open": entry.get("open") or 0,
    }
    price = entry.get("price")
    if price is not None:
        body["price"] = price
    return body


class GrocyError(Exception):
    """Raised with Grocy's actual error message instead of a bare HTTP status.

    Also raised, with an honest user-forward message, when Grocy (or the main
    server, on a satellite) cannot be reached at all, so every route that
    handles GrocyError degrades the same way during an outage instead of
    letting a raw httpx connection error bubble up as a 500.

    ``kind`` says which failure this is, so a banner can pick honest copy:
    "unreachable" (connection failed, comes back on its own), "http" (Grocy
    answered an error status), "config" (Grocy is up but its own setup is
    broken, which never comes back on its own), or "bad_response" (something
    other than Grocy's API answered). ``hint`` is an optional user-forward
    fix for a recognised problem (see known_grocy_fix_hint).
    """

    def __init__(self, message: str = "", hint: str | None = None,
                 kind: str = "error"):
        super().__init__(message)
        self.hint = hint
        self.kind = kind


def unreachable_message() -> str:
    """The user-forward message for a dead upstream connection.

    A satellite talks to Grocy through the main server's proxy, so a connect
    failure there means the main server is gone, not Grocy itself.
    """
    if settings.is_satellite():
        return "The main server is not reachable. Inventory will return when it is."
    return "Grocy is not reachable. Inventory will return when it is."


# --- Non-JSON answers from Grocy ------------------------------------------
# Grocy answers a broken config.php with HTTP 200 and an HTML page, not an API
# error. The 2026-09-06 incident: a Grocy image update to 4.7.0 moved its auth
# middleware into a Grocy\Middleware\Auth sub-namespace, the persisted
# config.php still named the old class, and every API call came back as a
# text/html "Invalid setting in config.php" page. Parsing that as JSON raised a
# JSONDecodeError, which the UI dressed up as a temporary outage. It is not
# temporary: it needs a one-line edit, so the helpers below turn the page into
# Grocy's own words plus a hint naming the fix.

_TAG_BREAKS = re.compile(r"<\s*(?:br|/p|/div|/li|/h[1-6]|/tr|/td|/pre)\s*/?>", re.I)
_TAGS = re.compile(r"<[^>]+>")
_SCRIPT_OR_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1\s*>", re.I | re.S)
_SEPARATOR_LINE = re.compile(r"^[-=_*~]{3,}$")
# A page carrying a password field is somebody's login screen (a reverse
# proxy, an SSO portal, or Grocy's own UI when the address lacks /api), never
# a Grocy error report.
_PASSWORD_FIELD = re.compile(r"<input[^>]*type\s*=\s*['\"]?password", re.I)
# Words that only a page written by Grocy or PHP itself carries.
_GROCY_MARKERS = ("config.php", "invalid setting", "grocy", "fatal error",
                  "uncaught", "exception", "stack trace", "parse error")
_DESCRIBE_MAX = 300

# The AUTH_CLASS namespace move (Grocy 4.7.0). The old value still appears in
# older config.php files; the new one is Grocy's own config-dist.php default.
AUTH_CLASS_OLD_VALUE = "Grocy\\Middleware\\DefaultAuthMiddleware"
AUTH_CLASS_NEW_VALUE = "Grocy\\Middleware\\Auth\\DefaultAuthMiddleware"
# Backslashes may arrive doubled (JSON-escaped log lines), so match one or more.
_AUTH_CLASS_OLD = re.compile(r"Grocy\\+Middleware\\+DefaultAuthMiddleware")
_AUTH_CLASS_NEW = re.compile(r"Middleware\\+Auth\\+DefaultAuthMiddleware")

AUTH_CLASS_HINT = (
    "Grocy 4.7 moved its login handler and the config.php in Grocy's data "
    "folder still names the old one. To fix it: back up config.php (it is in "
    "Grocy's data folder, /config/data inside the container), change the "
    "AUTH_CLASS line to " + AUTH_CLASS_NEW_VALUE + ", and restart Grocy."
)

NOT_DATA_MESSAGE = (
    "Grocy answered with a web page instead of inventory data. The Grocy "
    "address in Settings may point at a login page or another site rather "
    "than Grocy itself."
)


def html_to_text(text: str) -> str:
    """Plain sentences from an HTML (or plain) body. Pure.

    Tags go, <br> and block closers become line breaks, entities are decoded,
    whitespace collapses, and decorative separator lines ("----------") are
    dropped. The result never contains markup, so it is safe to show.
    """
    raw = text or ""
    raw = _SCRIPT_OR_STYLE.sub(" ", raw)
    raw = _TAG_BREAKS.sub("\n", raw)
    raw = _TAGS.sub(" ", raw)
    raw = _html.unescape(raw)
    lines = []
    for line in raw.splitlines():
        line = " ".join(line.split())
        if not line or _SEPARATOR_LINE.match(line):
            continue
        lines.append(line)
    return " ".join(lines)


def describe_grocy_html_error(text: str, content_type: str = "") -> str | None:
    """Grocy's own error message, as plain text, from a non-JSON body. Pure.

    Returns the trimmed message (about 300 characters) when the body reads
    like a page Grocy or PHP wrote about a problem, and None when it does not:
    a JSON body, an empty body, a reverse-proxy or SSO login page, or a plain
    web-server error page. None means "not Grocy's words", so the caller
    falls back to generic copy instead of quoting a stranger's page.
    """
    if "json" in (content_type or "").lower():
        return None
    raw = (text or "").strip()
    if not raw:
        return None
    if _PASSWORD_FIELD.search(raw):
        return None
    message = html_to_text(raw)
    if not message:
        return None
    lowered = message.lower()
    if not any(marker in lowered for marker in _GROCY_MARKERS):
        return None
    if len(message) > _DESCRIBE_MAX:
        cut = message[:_DESCRIBE_MAX].rsplit(" ", 1)[0].rstrip(" ,;:")
        message = cut + "..."
    return message


def known_grocy_fix_hint(message: str) -> str | None:
    """A user-forward fix for a recognised Grocy error message, or None. Pure.

    Today this knows one problem: config.php naming the pre-4.7 AUTH_CLASS
    (Grocy\\Middleware\\DefaultAuthMiddleware, without the Auth namespace).
    A message that already names the new class is some other problem, so it
    gets no hint rather than a wrong one.
    """
    text = message or ""
    if "AUTH_CLASS" not in text:
        return None
    if _AUTH_CLASS_OLD.search(text) and not _AUTH_CLASS_NEW.search(text):
        return AUTH_CLASS_HINT
    return None


def config_error_message(message: str) -> str:
    """The banner copy for a Grocy-reported problem: Grocy is up, its own
    setup is what is broken, and a refresh will not change that."""
    return "Grocy is running but reported a problem with its own setup: " + message


def repair_available() -> bool:
    """Whether this device can fix Grocy's config.php itself.

    Only a Pi Hosted appliance runs both the host bridge (which can reach into
    the container) and the Grocy container it would repair. A server has no
    bridge, and a satellite's Grocy lives on its main server.
    """
    return settings.deployment_mode == "pi_hosted"


def error_payload(e: Exception) -> dict:
    """The JSON body a route answers a Grocy failure with (a 502).

    Always carries ``detail`` (the honest message every outage banner shows).
    A Grocy-reported setup problem adds ``config_error`` so a banner drops
    the "comes back on its own" copy, plus the fix ``hint`` and whether this
    device offers the one-click ``repairable`` path (Pi Hosted only).
    """
    payload: dict = {"detail": str(e) or unreachable_message()}
    if isinstance(e, GrocyError) and e.kind == "config":
        payload["config_error"] = True
        if e.hint:
            payload["hint"] = e.hint
            payload["repairable"] = repair_available()
    return payload


def _non_json_error(text: str, content_type: str, path: str, status: int) -> GrocyError:
    """The GrocyError for a 2xx answer that is not JSON."""
    message = describe_grocy_html_error(text, content_type)
    if message:
        return GrocyError(config_error_message(message),
                          hint=known_grocy_fix_hint(message), kind="config")
    return GrocyError(NOT_DATA_MESSAGE, kind="bad_response")


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


def is_hard_expiry(entry: dict) -> bool:
    """True when a stock row's product carries a hard expiration date. Pure.

    Grocy has two due types: 1 is a best-before (a quality guess, safe to
    extend after a sniff test) and 2 is a hard expiration (a safety date).
    Offering to keep a hard-expiration item a few more days would invite
    eating something genuinely unsafe, so every sniff surface checks this
    first. The flag lives on the product inside a /stock row, but some
    shapes carry it on the row itself, so both spots are checked; anything
    missing or unreadable counts as a best-before.
    """
    raw = (entry or {}).get("due_type")
    if raw is None:
        raw = ((entry or {}).get("product") or {}).get("due_type")
    try:
        return int(raw) == 2
    except (TypeError, ValueError):
        return False


def stock_sniff_candidates(stock: list[dict], days: int = 7,
                           today: date | None = None) -> dict[str, dict]:
    """Lower-cased product name -> sniff-test hook for stock expiring soon. Pure.

    The Review screen offers the Expiring page's sniff test on a pending row
    whose product already sits in stock with a best-by date inside the window
    the Expiring page shows by default (``days``, expired included, matching
    get_expiring's delta <= days cut). Only dated, in-stock entries with a
    numeric product id qualify: /expiring/extend needs the id, and an undated
    entry has nothing to extend. Entries whose product carries a hard
    expiration date (due_type 2, see is_hard_expiry) never qualify: that is
    a safety date, not a quality guess, so no sniff test is offered on it
    anywhere. When a name appears more than once the earliest date wins,
    since that is the entry driving the expiring row.
    """
    if today is None:
        today = date.today()
    out: dict[str, dict] = {}
    for entry in stock or []:
        if float(entry.get("amount") or 0) <= 0:
            continue
        if is_hard_expiry(entry):
            continue
        product = entry.get("product") or {}
        name = (product.get("name") or entry.get("name") or "").strip().lower()
        bbd = entry.get("best_before_date")
        if not name or not bbd:
            continue
        try:
            pid = int(entry.get("product_id") or product.get("id") or 0)
            remaining = (date.fromisoformat(bbd) - today).days
        except (TypeError, ValueError):
            continue
        if not pid or remaining > days:
            continue
        prev = out.get(name)
        if prev is None or remaining < prev["days_remaining"]:
            out[name] = {"product_id": pid,
                         "best_before_date": bbd,
                         "days_remaining": remaining}
    return out


def opened_amounts(stock: list[dict]) -> dict[int, float]:
    """Product id -> amount currently marked opened, from a raw /stock list. Pure.

    Grocy's /stock rows carry ``amount_opened``, but the enriched dashboard
    rows do not, so the Inventory router merges it back in through this map.
    Only positive opened amounts are reported: a product missing from the map
    simply has nothing open, which keeps the indicator honest.
    """
    out: dict[int, float] = {}
    for entry in stock or []:
        try:
            pid = int(entry.get("product_id") or 0)
            opened = float(entry.get("amount_opened") or 0)
        except (TypeError, ValueError):
            continue
        if pid and opened > 0:
            out[pid] = out.get(pid, 0.0) + opened
    return out


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
            raise GrocyError(unreachable_message(), kind="unreachable") from e
        ctype = (getattr(r, "headers", None) or {}).get("content-type", "")
        if r.status_code >= 400:
            body = r.text or ""
            detail = None
            if "html" in ctype.lower() or body.lstrip().startswith("<"):
                # An HTML error page: quote Grocy's words, never raw markup.
                detail = describe_grocy_html_error(body, ctype) \
                    or html_to_text(body)[:300].strip()
            if not detail:
                detail = body[:300].strip() or r.reason_phrase
            hint = known_grocy_fix_hint(detail)
            raise GrocyError(f"Grocy {r.status_code} on {path}: {detail}",
                             hint=hint, kind="config" if hint else "http")
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            # A 2xx that is not JSON is Grocy reporting a problem with its own
            # setup as a web page (a broken config.php), or something other
            # than Grocy's API answering. Either way: honest words, no
            # JSONDecodeError escaping as a fake "temporary" outage.
            raise _non_json_error(r.text, ctype, path, r.status_code) from None

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

    async def consume_stock(self, product_id: int, amount: float = 1.0,
                            spoiled: bool = False) -> dict:
        # ``spoiled`` is the difference between "we ate it" and "we threw it
        # out"; Grocy keeps it in the stock log, which is what the waste
        # insights read. Defaults to eaten so no existing caller changes.
        return await self._post(f"/stock/products/{product_id}/consume", {
            "amount": amount,
            "spoiled": bool(spoiled),
        })

    async def open_stock(self, product_id: int, amount: float = 1.0) -> dict:
        """Mark one unit opened. Grocy then applies the product's
        best_before_after_open rule, so an opened jar's expiry turns honest."""
        return await self._post(f"/stock/products/{product_id}/open", {
            "amount": amount,
        })

    async def set_stock_amount(self, product_id: int, amount: float,
                               best_before_date: str | None = None) -> dict:
        """Inventory correction: set the product's absolute stock amount.

        Grocy books the difference itself (an add or a consume in the log).
        The date rides along only when Grocy needs one to book an increase.
        """
        body: dict = {"new_amount": amount}
        if best_before_date:
            body["best_before_date"] = best_before_date
        return await self._post(f"/stock/products/{product_id}/inventory", body)

    async def get_unit_conversions(self) -> list[dict]:
        """Grocy's per-product quantity-unit conversions, for the recipe
        matcher. Cached on the instance like the other lookup tables."""
        if "unit_conversions" not in self._cache:
            self._cache["unit_conversions"] = await self._get(
                "/objects/quantity_unit_conversions")
        return self._cache["unit_conversions"]

    async def set_entry_price(self, entry: dict, price: float) -> dict:
        """Write a real purchase price onto one stock entry.

        The only caller is the receipt flow, which has an actual price from a
        receipt in hand. The never-stamp-a-default rule stands: this method
        requires a positive price, so no code path can write the zero that
        corrupts Grocy's inventory value.
        """
        if not entry.get("id") or not price or price <= 0:
            raise GrocyError("A real price and a stock entry id are required.")
        body = stock_entry_edit_body(entry, entry.get("best_before_date") or "")
        if not body.get("best_before_date"):
            body.pop("best_before_date", None)
        body["price"] = round(float(price), 2)
        return await self._request("PUT", f"/stock/entry/{entry['id']}", body)

    async def consume_by_barcode(self, barcode: str, amount: float = 1.0,
                                 spoiled: bool = False) -> dict:
        """Consume stock for the product carrying ``barcode`` (Grocy native).

        Grocy resolves the barcode to its product, so the scanner can use up an
        item without a separate lookup. Raises GrocyError (via _post) when the
        barcode is unknown or there is no stock to consume.
        """
        return await self._post(
            f"/stock/products/by-barcode/{barcode}/consume",
            {"amount": amount, "spoiled": bool(spoiled)},
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
                await self._set_entry_best_by(entry, best_before_date)

        return {"product_id": product_id}

    async def _set_entry_best_by(self, entry: dict, new_date: str) -> bool:
        """Move one stock entry's best-by date. True when it was written.

        The numeric row id is what /stock/entry wants; ``stock_id`` is Grocy's
        hash for the same row and 404s here, so an entry without the numeric id
        is skipped rather than sent to a URL that cannot work.
        """
        entry_id = entry.get("id")
        if not entry_id:
            return False
        await self._request("PUT", f"/stock/entry/{entry_id}",
                            stock_entry_edit_body(entry, new_date))
        return True

    async def extend_best_by(self, product_id: int, delta_days: int) -> dict:
        """Sniff test passed: push every dated stock entry's best-by out by
        ``delta_days`` (from today when the entry is already past its date).

        Reuses the same per-entry PUT path as edit_product, but keeps each
        entry's own date instead of flattening them all to one. Returns the
        earliest resulting date (the one that drives the expiring row) and how
        many entries were updated.
        """
        entries = await self._get(f"/stock/products/{product_id}/entries")
        updated = 0
        new_dates: list[str] = []
        for entry in entries:
            new_date = sniff_test_new_date(entry.get("best_before_date"), delta_days)
            if not new_date:
                continue
            if not await self._set_entry_best_by(entry, new_date):
                continue
            updated += 1
            new_dates.append(new_date)
        return {
            "product_id": product_id,
            "updated": updated,
            "new_best_by": min(new_dates) if new_dates else None,
        }

    async def product_name_and_group(self, product_id: int) -> tuple[str, str]:
        """The product's name and product-group (category) name, best effort.

        Used by the transfer hook to look up the destination shelf-life rule.
        A missing product or group degrades to a placeholder name and an empty
        category, which the rule lookup treats as "match by name only".
        """
        name, group = f"Product {product_id}", ""
        for p in await self.get_products():
            if int(p.get("id") or 0) != product_id:
                continue
            name = p.get("name") or name
            gid = str(p.get("product_group_id") or "")
            for g in await self._cached_list("/objects/product_groups"):
                if str(g.get("id")) == gid:
                    group = g.get("name") or ""
                    break
            break
        return name, group

    async def move_product(self, product_id: int, bucket: str,
                           propose_best_by=None) -> dict:
        """Transfer all stock of a product to the location for `bucket` and
        make that the product's default location.

        ``propose_best_by`` is an optional callable ``(old_best_by: date,
        from_bucket: str) -> date | None`` (see
        defaults.propose_transfer_best_by). When given, each dated entry's
        best-by is rewritten to the proposal before its transfer, so freezing
        extends and thawing shortens. Entries with no recorded location only
        get re-bucketed by the default-location change; their previous storage
        kind is unknowable, so their dates are honestly left alone.
        """
        to_name = location_for(bucket)
        if not to_name:
            raise GrocyError(f"Unknown storage bucket: {bucket}")
        to_id = await self.ensure_location(to_name)

        locations = {}
        if propose_best_by is not None:
            locations = {
                str(loc["id"]): loc["name"]
                for loc in await self._cached_list("/objects/locations")
            }

        entries = await self._get(f"/stock/products/{product_id}/entries")
        moved = 0.0
        best_by_updates: list[dict] = []
        for entry in entries:
            amount = float(entry.get("amount") or 0)
            from_id = int(entry.get("location_id") or 0)
            if amount <= 0 or from_id == to_id:
                continue
            if from_id:
                old_iso = entry.get("best_before_date")
                if propose_best_by is not None and old_iso:
                    from_bucket = classify_location(locations.get(str(from_id), ""))
                    new_date = propose_best_by(date.fromisoformat(old_iso), from_bucket)
                    if new_date is not None:
                        entry_id = entry.get("id") or entry.get("stock_id")
                        if entry_id:
                            await self._request(
                                "PUT", f"/objects/stock/{entry_id}",
                                {**entry, "best_before_date": new_date.isoformat()})
                            best_by_updates.append(
                                {"old": old_iso, "new": new_date.isoformat()})
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

        # One transfer moves in one temperature direction, so every update
        # agrees; the earliest resulting date is the one the user will see
        # drive the expiring list.
        change = None
        if best_by_updates:
            change = ("extended" if best_by_updates[0]["new"] > best_by_updates[0]["old"]
                      else "shortened")
        return {
            "product_id": product_id,
            "moved_amount": moved,
            "location_id": to_id,
            "best_by_updates": best_by_updates,
            "new_best_by": min(u["new"] for u in best_by_updates) if best_by_updates else None,
            "best_by_change": change,
        }

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
            f"/objects/shopping_list?query%5B%5D=shopping_list_id%3D{list_id}"
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
        return await self._post("/objects/shopping_list", body)

    async def toggle_shopping_item(self, item_id: int, done: bool) -> None:
        row = await self._get(f"/objects/shopping_list/{item_id}")
        await self._request("PUT", f"/objects/shopping_list/{item_id}",
                            {**row, "done": int(done)})

    async def delete_shopping_item(self, item_id: int) -> None:
        await self._request("DELETE", f"/objects/shopping_list/{item_id}")

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

    # Why the last health check failed, in a short plain sentence, or "" when
    # it passed (or never ran). /health carries it so support can see the
    # cause (a broken config.php, an unreachable host) without shell access.
    last_error: str = ""
    last_hint: str = ""

    async def health_check(self) -> bool:
        try:
            await self._get("/system/info")
            self.last_error = ""
            self.last_hint = ""
            return True
        except GrocyError as e:
            self.last_error = (str(e) or e.__class__.__name__)[:200]
            self.last_hint = e.hint or ""
            return False
        except Exception as e:
            self.last_error = (str(e) or e.__class__.__name__)[:200]
            self.last_hint = ""
            return False

    async def consume_stock_entry(self, product_id: int, stock_entry_id: str,
                                  amount: float = 1.0) -> dict | list:
        """Consume from one exact stock entry of a product (FoodAssistant-28f3).

        This is what a printed label's grocycode resolves to: Grocy takes the
        entry's own id in the consume body and books the amount off that entry
        specifically, so the leftover container that was scanned is the one
        that comes off stock. Returns Grocy's booking rows (they carry the
        entry's best-before date, which the scan reply reads back). Raises
        GrocyError (via _post) when the entry is gone or already used up.
        """
        return await self._post(f"/stock/products/{product_id}/consume", {
            "amount": amount,
            "stock_entry_id": stock_entry_id,
        })


# StorageType enum value → Grocy location name, used by import_item. The enum
# has "dry" where the dashboard uses the "pantry" bucket; both name the same
# Grocy location. Custom categories are move-only and not reachable here.
_STORAGE_LABEL = {
    "refrigerated": "Refrigerator",
    "frozen": "Freezer",
    "room_temp": "Counter / Room Temp",
    "dry": "Pantry / Dry Storage",
}


def parse_grocycode(payload: str) -> tuple[int, str] | None:
    """Parse a Grocy product grocycode into (product_id, stock_entry_id). Pure.

    Grocy prints "grcy:p:{product_id}" for a product and
    "grcy:p:{product_id}:{stock_id}" for one exact stock entry (the stock id is
    Grocy's short alphanumeric hash for that entry, e.g. "6a28c889c1193");
    Pantry Raider's own food labels carry the entry form. Returns the parsed
    pair with stock_entry_id == "" for the product-only form, or None for
    anything that is not a well-formed product grocycode (other entity kinds
    like chores, junk ids, extra segments), so a caller can fall back to normal
    barcode handling instead of acting on garbage. Never raises.
    """
    parts = str(payload or "").strip().split(":")
    if len(parts) not in (3, 4):
        return None
    prefix, entity, pid_raw = parts[0], parts[1], parts[2]
    if prefix.lower() != "grcy" or entity.lower() != "p":
        return None
    if not pid_raw.isdigit():
        return None
    pid = int(pid_raw)
    if pid <= 0:
        return None
    sid = parts[3] if len(parts) == 4 else ""
    if len(parts) == 4 and not (sid.isascii() and sid.isalnum()):
        return None
    return pid, sid
