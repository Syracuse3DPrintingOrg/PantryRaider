import asyncio
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

    The rest of the row's provenance goes the same way. A field left out comes
    back null, so leaving these out would wipe the purchase date a back-dated
    receipt set, the note carrying the brand, and the store the item came from,
    every time anyone passed a sniff test or moved an item to the freezer. Each
    rides along only when the entry has one: an empty value is omitted rather
    than sent, because Grocy rejects a blank date outright.
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
    for field in ("purchased_date", "note", "shopping_location_id"):
        value = entry.get(field)
        if value is not None and value != "":
            body[field] = value
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
    return "Your inventory is not reachable right now. It will return when it is."


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

# The AUTH_CLASS namespace move (Grocy 4.7.0). It moved every built-in login
# handler (Default, ReverseProxy and Ldap) from Grocy\Middleware into
# Grocy\Middleware\Auth. The old values still appear in older config.php
# files; the new Default one is Grocy's own config-dist.php default.
AUTH_CLASS_OLD_VALUE = "Grocy\\Middleware\\DefaultAuthMiddleware"
AUTH_CLASS_NEW_VALUE = "Grocy\\Middleware\\Auth\\DefaultAuthMiddleware"
# Backslashes may arrive doubled (JSON-escaped log lines), so match one or more.
_AUTH_CLASS_OLD = re.compile(
    r"Grocy\\+Middleware\\+((?:Default|ReverseProxy|Ldap)AuthMiddleware)")
_AUTH_CLASS_NEW = re.compile(
    r"Middleware\\+Auth\\+(?:Default|ReverseProxy|Ldap)AuthMiddleware")


def auth_class_hint(class_name: str = "DefaultAuthMiddleware") -> str:
    """The config.php fix for a pre-4.7 AUTH_CLASS, naming the class the
    install actually uses (DefaultAuthMiddleware, ReverseProxyAuthMiddleware
    or LdapAuthMiddleware). Pure."""
    return (
        "Grocy 4.7 moved its login handler and the config.php in Grocy's data "
        "folder still names the old one. To fix it: back up config.php (it is "
        "in Grocy's data folder, /config/data inside the container), change "
        "the AUTH_CLASS line to Grocy\\Middleware\\Auth\\" + class_name
        + ", and restart Grocy."
    )


AUTH_CLASS_HINT = auth_class_hint()

NOT_DATA_MESSAGE = (
    "Grocy answered with a web page instead of inventory data. The Grocy "
    "address in Settings may point at a login page or another site rather "
    "than Grocy itself."
)

NO_VERSION_MESSAGE = (
    "Grocy's status check came back without a Grocy version. The Grocy "
    "address in Settings may point at another site rather than Grocy itself."
)

# The markers that point at Grocy's own setup or code. On an error status the
# bare word "grocy" is not enough: a proxy or tunnel error page names the host
# it could not reach (grocy.example.com), and that is an outage, not a
# problem with Grocy's setup.
_SETUP_MARKERS = tuple(m for m in _GROCY_MARKERS if m != "grocy")


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

    Today this knows one problem: config.php naming a pre-4.7 AUTH_CLASS
    (Grocy\\Middleware\\DefaultAuthMiddleware, ReverseProxyAuthMiddleware or
    LdapAuthMiddleware, without the Auth namespace). The hint names the class
    it found, so an LDAP or reverse-proxy install is not told to switch to the
    default login. A message that already names a new class is some other
    problem, so it gets no hint rather than a wrong one.
    """
    text = message or ""
    if "AUTH_CLASS" not in text:
        return None
    found = _AUTH_CLASS_OLD.search(text)
    if found and not _AUTH_CLASS_NEW.search(text):
        return auth_class_hint(found.group(1))
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


def _location_id(entry: dict) -> int:
    try:
        return int(entry.get("location_id") or 0)
    except (TypeError, ValueError):
        return 0


def _in_stock(entry: dict) -> bool:
    try:
        return float(entry.get("amount") or 0) > 0
    except (TypeError, ValueError):
        return False


def _movable(entry: dict, to_id: int) -> bool:
    """True when a stock row still has to move to location ``to_id``. Pure.

    A row with no recorded location cannot be transferred at all.
    """
    return _in_stock(entry) and _location_id(entry) not in (to_id, 0)


def _transfer_pool(entries: list[dict], row: dict, from_id: int) -> list[dict]:
    """The rows a transfer naming ``row``'s stock id may take from ``from_id``,
    in the order Grocy takes them. Pure.

    Grocy lists a product's entries in the order it uses them up (opened
    first, then earliest due first), and a transfer walks the rows at the
    source carrying the stock id it was given in that same order. Without a
    stock id (a very old Grocy) the transfer walks every row at the source.
    """
    stock_id = row.get("stock_id")
    return [e for e in entries or []
            if _in_stock(e) and _location_id(e) == from_id
            and (not stock_id or e.get("stock_id") == stock_id)]


def _row_after_date_write(entries: list[dict], from_id: int,
                          new_date: str | None, row_id) -> dict | None:
    """The row to move once a date write may have merged it away. Pure.

    The row itself while it is still at the source on the new date: another
    row can carry that date with other details (a price, a store) and not
    merge. A merge keeps the row with the highest id, so once the row is
    gone, the row sitting at the source with the new date (highest id first)
    is where the edited amount lives now; failing that, the row under its own
    id; None when neither is left.
    """
    at_source = [e for e in entries or []
                 if _in_stock(e) and _location_id(e) == from_id]
    own = next((e for e in at_source if str(e.get("id")) == str(row_id)), None)
    if own is not None and own.get("best_before_date") == new_date:
        return own
    dated = [e for e in at_source if e.get("best_before_date") == new_date]
    if dated:
        return max(dated, key=lambda e: int(e.get("id") or 0))
    return own


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
        if 300 <= r.status_code < 400:
            # A redirect is never inventory data: an SSO or reverse proxy
            # sending the request to its login page, or Grocy still coming
            # back after a config repair. The shared client does not follow
            # redirects, and the empty answer used to read as an empty pantry.
            raise GrocyError(NOT_DATA_MESSAGE, kind="bad_response")
        if r.status_code >= 400:
            body = r.text or ""
            grocy_words = describe_grocy_html_error(body, ctype)
            if grocy_words and any(m in grocy_words.lower() for m in _SETUP_MARKERS):
                # Grocy 4.7.1 answers a broken config.php with HTTP 500 (4.6.0
                # and 4.7.0 answered 200), so an error status can carry the
                # same report as the 2xx branch below: Grocy's own words, the
                # fix hint, and kind "config" so no banner calls it a passing
                # outage.
                raise GrocyError(config_error_message(grocy_words),
                                 hint=known_grocy_fix_hint(grocy_words),
                                 kind="config")
            detail = None
            if "html" in ctype.lower() or body.lstrip().startswith("<"):
                # An HTML error page: quote Grocy's words, never raw markup.
                detail = grocy_words or html_to_text(body)[:300].strip()
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
        # None of these four reads depends on another, so the dashboard waits
        # once instead of four times over. return_exceptions keeps a Grocy
        # outage (which fails all four at once) from leaving three exceptions
        # unretrieved; the first one is re-raised so the caller still sees the
        # GrocyError it renders its outage banner from.
        results = await asyncio.gather(
            self._get("/stock"),
            self._cached_list("/objects/locations"),
            self._cached_list("/objects/product_groups"),
            self._get("/objects/stock"),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, BaseException):
                raise r
        raw, location_rows, group_rows, stock_rows = results
        locations = {str(loc["id"]): loc["name"] for loc in location_rows}
        groups = {str(g["id"]): g["name"] for g in group_rows}
        # The Opened badge hangs on amount_opened, which /stock already carries,
        # so it is merged here rather than costing the dashboard a second
        # /stock fetch of its own (FoodAssistant-oyef, ydj2a).
        opened = opened_amounts(raw)

        # /stock aggregates per product and drops timestamps; the raw stock
        # table has row_created_timestamp per entry: take the newest per
        # product as "date added".
        added: dict[int, str] = {}
        for row in stock_rows:
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
                "amount_opened": opened.get(pid, 0.0),
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
            # One target date for every entry, so a write can only merge a row
            # into one that already carries it, and the fresh read skips that
            # row: no stale amount is ever written back.
            entries = await self._get(f"/stock/products/{product_id}/entries") or []
            for entry_id in [e.get("id") for e in entries if e.get("id")]:
                await self._set_entry_best_by(product_id, entry_id, best_before_date)

        return {"product_id": product_id}

    async def _fresh_entry(self, product_id: int, entry_id) -> dict | None:
        """One stock entry as Grocy holds it right now, or None when it is gone.

        Grocy compacts a product's stock after every /stock/entry write: rows
        that then agree on everything except the amount (best-by, purchase
        date, price, open state, location, store and note) are merged into the
        row with the highest id, amounts summed, and the others are deleted.
        A list read before an earlier write therefore holds stale amounts and
        rows that no longer exist, and writing one of those back restates a
        stale amount over the merged total. Every write re-reads its row here
        first.
        """
        for row in await self._get(f"/stock/products/{product_id}/entries") or []:
            if str(row.get("id")) == str(entry_id):
                return row
        return None

    async def _write_best_by(self, row: dict | None, new_date: str) -> bool:
        """PUT ``new_date`` onto a row just read from Grocy. True when written.

        A row that is gone or already carries the date is left alone.
        """
        if not row or not row.get("id") or row.get("best_before_date") == new_date:
            return False
        await self._request("PUT", f"/stock/entry/{row['id']}",
                            stock_entry_edit_body(row, new_date))
        return True

    async def _set_entry_best_by(self, product_id: int, entry_id, new_date: str) -> bool:
        """Move one stock entry's best-by date. True when it was written.

        The row is re-read just before the write (see _fresh_entry), and it is
        skipped when it has been merged away or already carries the date.

        The numeric row id is what /stock/entry wants. ``stock_id`` is Grocy's
        hash for the same row, and Grocy answers it there with an HTTP 500 (a
        TypeError), so an entry without the numeric id is skipped rather than
        sent to a URL that cannot work.
        """
        if not entry_id:
            return False
        return await self._write_best_by(
            await self._fresh_entry(product_id, entry_id), new_date)

    async def extend_best_by(self, product_id: int, delta_days: int) -> dict:
        """Sniff test passed: push every dated stock entry's best-by out by
        ``delta_days`` (from today when the entry is already past its date).

        Reuses the same per-entry PUT path as edit_product, but keeps each
        entry's own date instead of flattening them all to one. Returns the
        earliest resulting date (the one that drives the expiring row) and how
        many entries were updated.

        Entries go latest date first, each new date computed from a fresh read
        of its row. Every new date is max(old, today) + delta, so an entry can
        never land on the date of one still waiting its turn and be merged
        into it (see _fresh_entry). Landing on an entry already extended is
        harmless: that merge sums the amounts and nothing writes it again.
        """
        entries = await self._get(f"/stock/products/{product_id}/entries") or []
        candidates = sorted(
            (e for e in entries if e.get("id") and e.get("best_before_date")),
            key=lambda e: e["best_before_date"], reverse=True)
        updated = 0
        new_dates: list[str] = []
        for candidate in candidates:
            row = await self._fresh_entry(product_id, candidate["id"])
            if row is None:
                continue
            new_date = sniff_test_new_date(row.get("best_before_date"), delta_days)
            if not new_date:
                continue
            if not await self._write_best_by(row, new_date):
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

    async def _date_before_transfer(self, product_id: int, entries: list[dict],
                                    row: dict, from_id: int, date_for,
                                    settled: set[str], updates: list[dict],
                                    ) -> tuple[list[dict], dict | None]:
        """Write the new best-by onto ``row``, and onto every row a transfer
        naming its stock id could take in its place, before anything moves.

        ``date_for(entry)`` is an entry's new date, or None to keep it. Each
        row is dated at most once per move: its id goes into ``settled``, and
        so does the id of the row a write merged it into. A write can merge
        rows and rename their stock ids, so the entries are re-read after each
        round until the pool holds no row still to date. Returns the fresh
        entries and the row to move, or None for the row when it is gone.
        """
        entries_path = f"/stock/products/{product_id}/entries"
        for _round in range(len(entries) + 1):
            written: list[tuple[dict, str]] = []
            for e in _transfer_pool(entries, row, from_id):
                key = str(e.get("id"))
                if key in settled:
                    continue
                settled.add(key)
                new_iso = date_for(e)
                if not new_iso or new_iso == e.get("best_before_date"):
                    continue
                # The same write path as the sniff test and the quick edit:
                # Grocy does not expose stock through /objects, and this helper
                # never stamps a default price onto an unpriced entry
                # (FoodAssistant-fo6c).
                if await self._set_entry_best_by(product_id, e.get("id"), new_iso):
                    updates.append({"old": e.get("best_before_date"), "new": new_iso})
                    written.append((e, new_iso))
            if not written:
                return entries, row
            entries = await self._get(entries_path) or []
            for e, new_iso in written:
                holder = _row_after_date_write(entries, from_id, new_iso, e.get("id"))
                if holder is not None:
                    settled.add(str(holder.get("id")))
            row_date = next((iso for e, iso in written if e is row),
                            row.get("best_before_date"))
            row = _row_after_date_write(entries, from_id, row_date, row.get("id"))
            if row is None:
                return entries, None
        return entries, row

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

        Grocy compacts stock after every date write (see _fresh_entry), so no
        step here trusts a list read before a write. Each pass re-reads the
        entries, dates the row it is about to move, and transfers exactly one
        whole row by its stock id. Grocy's transfer takes a stock id, not a
        row id: when several rows at the source share one (splits and merges
        leave them behind), it takes them opened first, then earliest due
        first. So every row sharing the id gets its new date before anything
        moves, and the transfer names the amount of the row Grocy takes first.

        A merged row carries one date, so the proposal has to be idempotent,
        as propose_transfer_best_by is: asked about a date it proposed, it
        proposes nothing new. A proposal that shifts every date by a fixed
        number of days could shift the units of a merged row twice.
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

        def date_for(entry: dict) -> str | None:
            old_iso = entry.get("best_before_date")
            if not old_iso:
                return None
            from_bucket = classify_location(
                locations.get(str(_location_id(entry)), ""))
            new_date = propose_best_by(date.fromisoformat(old_iso), from_bucket)
            return new_date.isoformat() if new_date is not None else None

        entries_path = f"/stock/products/{product_id}/entries"
        entries = await self._get(entries_path) or []
        moved = 0.0
        best_by_updates: list[dict] = []
        settled: set[str] = set()
        # Each pass moves one whole row, so this only caps a Grocy that never
        # lets go of one.
        for _ in range(len(entries) + 10):
            row = next((e for e in entries if _movable(e, to_id)), None)
            if row is None:
                break
            from_id = int(row["location_id"])
            if propose_best_by is not None:
                entries, row = await self._date_before_transfer(
                    product_id, entries, row, from_id, date_for, settled,
                    best_by_updates)
                if row is None:
                    continue
            first = (_transfer_pool(entries, row, from_id) or [row])[0]
            amount = float(first.get("amount") or 0)
            transfer = {
                "amount": amount,
                "location_id_from": from_id,
                "location_id_to": to_id,
            }
            # Only a very old Grocy lists rows without a stock id; the transfer
            # then takes rows at the source in its own order, which the pool
            # above already accounts for.
            if first.get("stock_id"):
                transfer["stock_entry_id"] = first["stock_id"]
            await self._post(f"/stock/products/{product_id}/transfer", transfer)
            moved += amount
            entries = await self._get(entries_path) or []

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
        # A partial update: Grocy's /objects PUT changes only the columns it is
        # sent. Its GET adds a computed "userfields" key, so sending a read
        # row back fails with "no such column: userfields", and would also
        # rewrite row_created_timestamp.
        await self._request("PUT", f"/objects/shopping_list/{item_id}",
                            {"done": int(done)})

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
            info = await self._get("/system/info")
        except GrocyError as e:
            self.last_error = (str(e) or e.__class__.__name__)[:200]
            self.last_hint = e.hint or ""
            return False
        except Exception as e:
            self.last_error = (str(e) or e.__class__.__name__)[:200]
            self.last_hint = ""
            return False
        if not isinstance(info, dict) or not info.get("grocy_version"):
            # Only Grocy's own status answer counts: an empty body or some
            # other service's JSON at the Grocy address is not a healthy Grocy.
            self.last_error = NO_VERSION_MESSAGE
            self.last_hint = ""
            return False
        self.last_error = ""
        self.last_hint = ""
        return True

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
