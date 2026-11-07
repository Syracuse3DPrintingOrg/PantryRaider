import asyncio
import functools
import html as _html
import json
import re

import httpx
from urllib.parse import quote
from datetime import date, timedelta
from ..config import settings
from ..models.food import FoodItem
from ..storage_categories import classify_location, custom_categories, location_for
from . import proxy_policy, upstream
from ..plain_digits import plain_int


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

    A per-date Inventory row's actions add two, both raised before anything
    is written: "group" (Grocy would take other stock than the row's own) and
    "gone" (the row's stock is no longer there). An add raises "unit", also
    before anything is written, when the unit typed measures something other
    than the product's own unit and Grocy has no conversion between them.
    """

    def __init__(self, message: str = "", hint: str | None = None,
                 kind: str = "error"):
        super().__init__(message)
        self.hint = hint
        self.kind = kind


_STATUS_PREFIX = re.compile(r"^Grocy \d{3} on \S+: ")


def grocy_said(e: Exception) -> str:
    """Grocy's own words from a GrocyError, as one sentence. Pure.

    Drops the status and path in front ("Grocy 400 on /objects/...: ") and
    takes error_message out of a JSON answer, for copy shown in the inbox.
    """
    text = _STATUS_PREFIX.sub("", str(e).strip())
    if text.startswith("{"):
        try:
            text = str(json.loads(text).get("error_message") or text)
        except (ValueError, AttributeError):
            pass
    text = text.strip() or "Grocy did not answer."
    return text if text[-1] in ".!?" else text + "."


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
#
# These are exactly the names the repair helpers map (NAMES in
# docker/grocy-init/10-repair-auth-class.sh and
# scripts/image-build/foodassistant-grocy-repair), so the app offers the fix,
# and the Repair button, for the same classes the repair will actually change.
AUTH_CLASS_NAMES = ("Default", "ReverseProxy", "Ldap")
AUTH_CLASS_OLD_VALUES = tuple(
    "Grocy\\Middleware\\" + name + "AuthMiddleware" for name in AUTH_CLASS_NAMES)
AUTH_CLASS_NEW_VALUES = tuple(
    "Grocy\\Middleware\\Auth\\" + name + "AuthMiddleware" for name in AUTH_CLASS_NAMES)
AUTH_CLASS_OLD_VALUE = AUTH_CLASS_OLD_VALUES[0]
AUTH_CLASS_NEW_VALUE = AUTH_CLASS_NEW_VALUES[0]
_AUTH_CLASS_BUILTIN = "(?:" + "|".join(AUTH_CLASS_NAMES) + ")AuthMiddleware"
# Backslashes may arrive doubled (JSON-escaped log lines), so match one or more.
# The match has to be the whole class name, optionally with PHP's leading
# backslash: a class of someone's own that only starts like a built-in one
# (Grocy\Middleware\DefaultAuthMiddlewareCustom) or lives in another namespace
# is left alone by the repair helpers, so it gets no hint here either.
_AUTH_CLASS_OLD = re.compile(
    r"(?<![\w\\])\\*Grocy\\+Middleware\\+(" + _AUTH_CLASS_BUILTIN + r")(?!\w|\\+\w)")
_AUTH_CLASS_NEW = re.compile(r"Middleware\\+Auth\\+" + _AUTH_CLASS_BUILTIN)


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

# The same fix when it is not known which login handler config.php names (Grocy
# did not answer with its error page just then). It names no class, so an LDAP
# or reverse-proxy install is never steered to the default login.
AUTH_CLASS_ANY_HINT = (
    "Grocy 4.7 moved its login handlers, and the config.php in Grocy's data "
    "folder may still name an old one. To fix it: back up config.php (it is in "
    "Grocy's data folder, /config/data inside the container), add Auth\\ "
    "after Grocy\\Middleware\\ in the AUTH_CLASS line and keep the rest of the "
    "class name as it is, then restart Grocy."
)

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
    default login. Only those three whole class names count (AUTH_CLASS_NAMES,
    the set the repair helpers change), so a custom class gets no hint. A
    message that already names a new class is some other problem, so it gets
    no hint rather than a wrong one.
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

    Fed the Expiring page's rows (expiring_by_date), the hook is the soonest
    of those rows and also carries its ``location_id``, ``location_name``,
    ``amount``, ``unit_name`` and ``unit_name_plural``, so the Review card
    names exactly that stock and its chips and toss act on that row alone.
    A product weighed with its container carries ``tare_weight``.
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
            if entry.get("tare_weight") or is_tare_weighed(product):
                # Weighed with its container: the card leaves the toss to
                # Grocy (is_tare_weighed).
                out[name]["tare_weight"] = True
            if entry.get("by_date"):
                out[name].update({
                    "location_id": entry.get("location_id"),
                    "location_name": entry.get("location_name") or "",
                    "amount": float(entry.get("amount") or 0),
                    "unit_name": entry.get("unit_name") or "",
                    "unit_name_plural": (entry.get("unit_name_plural")
                                         or entry.get("unit_name") or ""),
                })
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


def _urgency(best_before_date: str | None, today: date) -> tuple[int | None, str]:
    """(days_remaining, urgency bucket) for a best-by date. Pure."""
    if not best_before_date:
        return None, "unknown"
    days_remaining = (date.fromisoformat(best_before_date) - today).days
    if days_remaining < 0:
        return days_remaining, "expired"
    if days_remaining == 0:
        return days_remaining, "today"
    if days_remaining <= 3:
        return days_remaining, "3d"
    if days_remaining <= 7:
        return days_remaining, "7d"
    return days_remaining, "ok"


def stock_by_location(stock_rows: list[dict]) -> dict[int, dict[str, dict]]:
    """Product id -> location id -> {amount, amount_opened, best_before_date},
    from the raw /objects/stock rows. Pure.

    Each location's best-by is its earliest entry's, matching how /stock
    reports a product's next due date. Rows without a product or a positive
    amount are skipped.
    """
    out: dict[int, dict[str, dict]] = {}
    for row in stock_rows or []:
        try:
            pid = int(row.get("product_id") or 0)
            amount = float(row.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        if not pid or amount <= 0:
            continue
        loc = str(row.get("location_id") or "")
        part = out.setdefault(pid, {}).setdefault(
            loc, {"amount": 0.0, "amount_opened": 0.0, "best_before_date": None})
        part["amount"] += amount
        if str(row.get("open") or "0") not in ("0", "", "False", "false"):
            part["amount_opened"] += amount
        bbd = row.get("best_before_date")
        if bbd and (part["best_before_date"] is None or bbd < part["best_before_date"]):
            part["best_before_date"] = bbd
    return out


# Amounts at or below this are float dust Grocy leaves behind (a consume that
# spans a 0.1 and a 0.2 row can leave 2.8e-17 of one); they are not stock.
_DUST = 1e-9


def stock_by_location_and_date(stock_rows: list[dict]) -> dict[int, dict[str, dict]]:
    """Product id -> location id -> best-by date -> {amount, amount_opened,
    added}, from the raw /objects/stock rows. Pure.

    stock_by_location one level down: stock bought on different days keeps
    each purchase's best-by date, so one place can hold several. A row with
    no location is filed under "", one with no date under None. ``added`` is
    the newest row's creation time (its purchase date when it has none). Rows
    without a product, and dust, are skipped.
    """
    out: dict[int, dict[str, dict]] = {}
    for row in stock_rows or []:
        try:
            pid = int(row.get("product_id") or 0)
            amount = float(row.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        if not pid or amount <= _DUST:
            continue
        loc = str(row.get("location_id") or "")
        part = out.setdefault(pid, {}).setdefault(loc, {}).setdefault(
            row.get("best_before_date") or None,
            {"amount": 0.0, "amount_opened": 0.0, "added": None})
        part["amount"] += amount
        if str(row.get("open") or "0") not in ("0", "", "False", "false"):
            part["amount_opened"] += amount
        added = row.get("row_created_timestamp") or row.get("purchased_date")
        if added and (part["added"] is None or added > part["added"]):
            part["added"] = added
    return out


def best_by_breakdown(parts: dict) -> list[dict]:
    """One place's stock_by_location_and_date entry as a list of
    ``{"best_before_date", "amount"}``, earliest date first and undated stock
    last. Pure."""
    return [{"best_before_date": bbd, "amount": parts[bbd]["amount"]}
            for bbd in sorted(parts, key=lambda d: (d is None, d or ""))]


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


# --- One row per best-by date ---------------------------------------------------
# A per-date Inventory row is the stock with one best-by date in one place (a
# "group"), and its buttons must take only that stock. Grocy takes stock by its
# stock id, the hash consume, open and transfer accept as stock_entry_id, and
# walks the rows carrying it in the order /stock/products/{id}/entries lists
# them: the product's default consume location first, then opened, then
# earliest due (undated stock first). One stock id can sit on rows of other
# dates and places (a partial transfer copies it, a merge renames rows to it),
# so a group row is only safe to name when Grocy would take it first for its
# stock id. The actions below take one such row at a time by its exact amount
# (a summed amount can leave a float crumb behind or spill onto the next row),
# and refuse before writing anything when the stock they need sits behind
# other stock. Their date token is an ISO date, or "none" for stock with no
# best-by date.

# What a per-date row answers when its stock is gone (GrocyError kind "gone"),
# and when it is weighed with its container: Grocy reads a consume amount for
# those as the gross weight and opens or moves none of them (kind "group").
BEST_BY_ROW_GONE = "This row is no longer in stock."
BEST_BY_ROW_TARE = ("Pantry Raider can't split a product weighed with its container "
                    "by date, so nothing was changed.")
# A raced scoped sniff test: the row's stock was there at the read but merged
# or moved before any of it could be written.
BEST_BY_ROW_MOVED = "This row changed since the page loaded, so its date was not moved."


def is_tare_weighed(product: dict | None) -> bool:
    """True for a product Grocy weighs with its container (tare weight). Pure.

    Grocy reads a consume amount for such a product as what the scale shows,
    container included, so an amount from a page row means something else
    there."""
    return str((product or {}).get("enable_tare_weight_handling") or "0") not in ("0", "")


# How far a row's current total may drift from the amount the page showed
# before it counts as changed: float sums, never a real difference.
_EXPECTED_TOLERANCE = 1e-6


def best_by_group_total(entries: list[dict], location_id, best_before_date: str) -> float:
    """How much stock a per-date row holds right now. Pure."""
    return sum(_amount(e) for e in entries or []
               if in_best_by_group(e, location_id, best_before_date))


def check_expected_amount(entries: list[dict], location_id, best_before_date: str,
                          expected: float | None) -> None:
    """Refuse a per-date action when the row no longer holds what the page
    showed. Pure.

    ``expected`` is the amount the page showed for the row (None skips the
    check). Raises GrocyError "gone" when the row is empty and "changed" when
    its total differs by more than float dust, so nothing is written on stock
    the person never saw.
    """
    if expected is None:
        return
    total = best_by_group_total(entries, location_id, best_before_date)
    if total <= _DUST:
        raise GrocyError(BEST_BY_ROW_GONE, kind="gone")
    if abs(total - float(expected)) > max(_EXPECTED_TOLERANCE,
                                          _EXPECTED_TOLERANCE * abs(float(expected))):
        raise GrocyError(
            f"This row changed since the page loaded: it now holds {total:g}, "
            f"not {float(expected):g}. Nothing was changed.", kind="changed")


def _amount(entry: dict) -> float:
    try:
        return float(entry.get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_open(entry: dict) -> bool:
    return str(entry.get("open") or "0") not in ("0", "", "False", "false")


def _token_date(best_before_date: str) -> str | None:
    """The best-by date a per-date row's token stands for. Pure."""
    return None if best_before_date == "none" else best_before_date


def in_best_by_group(entry: dict, location_id, best_before_date: str) -> bool:
    """True when a stock entry belongs to the per-date row of ``location_id``
    and ``best_before_date`` ("none" for stock with no date). Pure.

    Dust is not stock (see _DUST), so it belongs to no row.
    """
    return (_amount(entry) > _DUST
            and _location_id(entry) == int(location_id or 0)
            and (entry.get("best_before_date") or None) == _token_date(best_before_date))


def _group_rows_and_firsts(entries: list[dict], location_id, best_before_date: str,
                           unopened: bool) -> list[tuple[dict, dict]]:
    """Each group row with the row Grocy takes first for its stock id, in
    Grocy's order. Pure.

    Consume and transfer walk the rows at ``location_id``; open walks the
    unopened rows of the whole product. Rows without a stock id cannot be
    named, and dust is not stock.
    """
    firsts: dict[str, dict] = {}
    out = []
    for e in entries or []:
        stock_id = e.get("stock_id")
        if not stock_id or _amount(e) <= _DUST:
            continue
        if _is_open(e) if unopened else _location_id(e) != int(location_id or 0):
            continue
        first = firsts.setdefault(stock_id, e)
        if in_best_by_group(e, location_id, best_before_date):
            out.append((e, first))
    return out


def best_by_group_head(entries: list[dict], location_id, best_before_date: str, *,
                       unopened: bool = False) -> dict | None:
    """The first group row Grocy would take first for its own stock id, or
    None. Pure.

    ``entries`` is /stock/products/{id}/entries exactly as Grocy returned it:
    that list is the order Grocy walks, so it is never re-sorted. Naming that
    row's stock id (with the location, for consume and transfer) makes Grocy
    start on that very row. With ``unopened`` the pool is what open walks: the
    unopened rows carrying the stock id anywhere in the product.
    """
    return next((row for row, first in _group_rows_and_firsts(
        entries, location_id, best_before_date, unopened) if first is row), None)


def best_by_group_blocker(entries: list[dict], location_id, best_before_date: str, *,
                          unopened: bool = False) -> dict | None:
    """The first row outside the group that Grocy would take before a group
    row carrying the same stock id, or None. Pure.

    It is what a refusal names: an opened unit, an earlier date, another place.
    """
    return next((first for _row, first in _group_rows_and_firsts(
        entries, location_id, best_before_date, unopened)
        if not in_best_by_group(first, location_id, best_before_date)), None)


def best_by_group_reach(entries: list[dict], location_id, best_before_date: str,
                        amount: float | None = None) -> float:
    """How much of a group the head-by-head walk takes: all of it, or up to
    ``amount``. Pure.

    It takes best_by_group_head's row whole (or the rest of ``amount``) and
    walks on without it, as the consume and move loops do. Taking a row can
    leave a row of another date first for the same stock id, and then the rest
    of the group is out of reach.
    """
    rows = list(entries or [])
    reached = 0.0
    while amount is None or amount - reached > _DUST:
        head = best_by_group_head(rows, location_id, best_before_date)
        if head is None:
            break
        take = _amount(head)
        if amount is not None and take - (amount - reached) > _DUST:
            take = amount - reached
        reached += take
        rows = [e for e in rows if e is not head]
    return reached


def _use_refusal(blocker: dict | None, best_before_date: str) -> str:
    """Why a per-date row's consume would take other stock first. Pure."""
    if blocker is not None and _is_open(blocker):
        return "Grocy would use an opened one first, so nothing was used."
    blocker_date = (blocker or {}).get("best_before_date")
    row_date = _token_date(best_before_date)
    if blocker_date and row_date and blocker_date < row_date:
        return "Grocy would use one with an earlier best-by date first, so nothing was used."
    return "Grocy would use other stock first, so nothing was used."


def best_by_take(entries: list[dict], location_id, best_before_date: str,
                 amount: float) -> float:
    """How much a per-date row's consume takes, worked out before any write.
    Pure.

    Asking for at least what the row holds takes all of it as Grocy holds it
    now, so Use one on half a unit uses the half. Raises GrocyError "gone"
    when the row holds nothing, and "group" when the head-by-head walk cannot
    reach the amount.
    """
    total = sum(_amount(e) for e in entries or []
                if in_best_by_group(e, location_id, best_before_date))
    if total <= _DUST:
        raise GrocyError(BEST_BY_ROW_GONE, kind="gone")
    want = total if amount >= total - _DUST else float(amount)
    if best_by_group_reach(entries, location_id, best_before_date, want) < want - _DUST:
        blocker = best_by_group_blocker(entries, location_id, best_before_date)
        err = GrocyError(_use_refusal(blocker, best_before_date), kind="group")
        # An opened unit in the same place is what Grocy takes first there;
        # the Expiring page offers a toss from the whole place for that case.
        err.opened_first = blocker is not None and _is_open(blocker)
        raise err
    return want


def best_by_label_stock_id(rows: list[dict], location_id,
                           best_before_date: str) -> str | None:
    """The stock id a label printed from a per-date row codes, or None. Pure.

    ``rows`` are one product's raw stock rows. A scanned label uses up stock
    by its stock id alone, wherever in the product it is, so the newest group
    row whose stock id no other row carries wins; failing that, the newest
    group row. "Newest" is the row created last, as for any label.
    """
    group = [r for r in rows or []
             if r.get("stock_id") and in_best_by_group(r, location_id, best_before_date)]
    if not group:
        return None
    elsewhere = {r.get("stock_id") for r in rows or []
                 if _amount(r) > _DUST and not in_best_by_group(r, location_id, best_before_date)}
    own = [r for r in group if r["stock_id"] not in elsewhere]
    newest = max(own or group, key=lambda r: (str(r.get("row_created_timestamp") or ""),
                                              _int_or_none(r.get("id")) or 0))
    return newest["stock_id"]


# What /stock reports for the whole product: on a row of one best-by date
# those would contradict the row's own amount, so they are left off it.
_PRODUCT_TOTALS = ("amount_aggregated", "amount_opened_aggregated",
                   "is_aggregated_amount", "value")


def expiring_by_date(stock: list[dict], stock_rows: list[dict], days: int,
                     today: date | None = None) -> list[dict]:
    """The Expiring page's rows: one per product, place and best-by date
    within ``days`` (expired included), soonest first. Pure.

    ``stock`` is /stock (one row per product, for the product itself) and
    ``stock_rows`` the raw /objects/stock rows. A product's /stock row sums
    every entry and carries only the earliest date, so three expired yogurts
    and two good ones read as five expired, and acting on that row took all
    five. Each row here is the stock of one per-date row instead
    (stock_by_location_and_date): its own ``amount`` and
    ``amount_opened``, its place as ``location_id``, its newest ``added_date``,
    ``by_date`` set, ``stock_entry_id`` for its label
    (best_by_label_stock_id), and ``tare_weight`` for a product weighed with
    its container (is_tare_weighed). Stock outside the window and undated stock get
    no row, whatever else the product holds.
    """
    if today is None:
        today = date.today()
    products = {_int_or_none(e.get("product_id")): e for e in stock or []}
    product_rows: dict[int, list[dict]] = {}
    for row in stock_rows or []:
        product_rows.setdefault(_int_or_none(row.get("product_id")) or 0, []).append(row)
    out = []
    for pid, places in stock_by_location_and_date(stock_rows).items():
        entry = products.get(pid)
        if entry is None:
            continue  # /stock names the product; without it there is no row to show
        for loc_id, dated in places.items():
            for day, part in dated.items():
                if not day:
                    continue
                remaining = (date.fromisoformat(day) - today).days
                if remaining > days:
                    continue
                row = {k: v for k, v in entry.items() if k not in _PRODUCT_TOTALS}
                row.update({
                    "amount": part["amount"],
                    "amount_opened": part["amount_opened"],
                    "best_before_date": day,
                    "days_remaining": remaining,
                    "location_id": int(loc_id) if loc_id.isdigit() else None,
                    "added_date": part["added"],
                    "by_date": True,
                    "stock_entry_id": best_by_label_stock_id(
                        product_rows.get(pid), loc_id, day),
                    # Weighed with its container: an amount from this row
                    # is not what Grocy's consume means for it.
                    "tare_weight": is_tare_weighed(entry.get("product")),
                })
                out.append(row)
    out.sort(key=lambda r: (r["days_remaining"],
                            ((r.get("product") or {}).get("name") or "").lower(),
                            r["location_id"] or 0))
    return out


def _log_rows(booked, kind: str) -> list[dict]:
    """The stock_log rows of one type in a consume, open or transfer answer,
    float dust left out. Pure."""
    return [r for r in (booked if isinstance(booked, list) else [])
            if isinstance(r, dict) and r.get("transaction_type") == kind
            and abs(_amount(r)) > _DUST]


def _booked_in_group(row: dict, location_id, best_before_date: str) -> bool:
    """True when a stock_log row booked a per-date row's own stock. Pure.

    A log row carries the place and best-by date the stock had before the
    booking, so it compares with the row's own.
    """
    return (_location_id(row) == int(location_id or 0)
            and (row.get("best_before_date") or None) == _token_date(best_before_date))


def _best_by_arrivals(booked, location_id,
                      best_before_date: str) -> tuple[list[dict], list[dict]]:
    """The transfer_to rows of a transfer answer that carried a per-date row's
    own stock, and those that carried other stock. Pure.

    Grocy books each moved row as a pair sharing one correlation id: a
    transfer_from row with the place and date the stock left with, and a
    transfer_to row with the place and date it arrived with.
    """
    sent = {r.get("correlation_id"): r for r in _log_rows(booked, "transfer_from")}
    own, other = [], []
    for row in _log_rows(booked, "transfer_to"):
        source = sent.get(row.get("correlation_id"))
        if source is not None and _booked_in_group(source, location_id, best_before_date):
            own.append(row)
        else:
            other.append(row)
    return own, other


def _transfer_redates(booked) -> list[tuple[str | None, str | None]]:
    """(date the stock left with, date it arrived with) for each row of a
    transfer answer that Grocy re-dated on the way (its own freezer rule).
    Pure."""
    sent = {r.get("correlation_id"): r for r in _log_rows(booked, "transfer_from")}
    out = []
    for row in _log_rows(booked, "transfer_to"):
        source = sent.get(row.get("correlation_id"))
        if source is None:
            continue
        left_with = source.get("best_before_date") or None
        arrived_with = row.get("best_before_date") or None
        if left_with != arrived_with:
            out.append((left_with, arrived_with))
    return out


def _nothing_booked(e: Exception) -> bool:
    """True when Grocy answered that no row carried the stock id it was sent.

    A merge since the read can rename a stock id while the stock is still
    there, so this means "read again", not "gone". Grocy booked nothing.
    """
    return "No transaction was found" in str(e)


def _int_or_none(value) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def qu_conversion_factor(conversions: list[dict], product_id: int,
                         from_qu: int, to_qu: int) -> float | None:
    """How many ``to_qu`` make one ``from_qu`` for ``product_id``. Pure.

    ``conversions`` are Grocy quantity_unit_conversions rows (from_qu_id,
    to_qu_id, factor, product_id). A product's own conversion beats a global
    one (no product_id), as in Grocy; a conversion written the other way round
    is used inverted. None when the two units are not linked at all. The
    resolved view Grocy 4 also offers carries the same columns, already
    chained, so it can be passed in unchanged.
    """
    if from_qu == to_qu:
        return 1.0
    for scope in (product_id, None):
        for c in conversions or []:
            if _int_or_none(c.get("product_id")) != scope:
                continue
            src = _int_or_none(c.get("from_qu_id"))
            dst = _int_or_none(c.get("to_qu_id"))
            try:
                factor = float(c.get("factor") or 0)
            except (TypeError, ValueError):
                continue
            if factor <= 0:
                continue
            if (src, dst) == (from_qu, to_qu):
                return factor
            if (src, dst) == (to_qu, from_qu):
                return 1.0 / factor
    return None


def unit_matches(unit: dict, name: str) -> bool:
    """True when ``name`` is this Grocy quantity unit's singular or plural. Pure.

    Read the way a typed unit is (_unit_key): case and dots do not matter,
    except that "T" (a tablespoon) is never "t" (a teaspoon).
    """
    key = _unit_key(name)
    return bool(key) and key in (
        _unit_key(unit.get("name") or ""),
        _unit_key(unit.get("name_plural") or ""),
    )


# Units people type, as the Grocy quantity unit each one means: (name, plural,
# other spellings). "item" is the queue's default when no unit was typed, so it
# and its kin mean Grocy's own Piece, which is what every add used before.
# Spellings are lowercase, except "T", the one place case matters.
_KNOWN_UNITS = (
    ("Piece", "Pieces", ("item", "items", "pc", "pcs", "ea", "each", "unit",
                         "units", "count", "ct")),
    ("Milligram", "Milligrams", ("mg", "mgs", "milligramme", "milligrammes")),
    ("Gram", "Grams", ("g", "gr", "grs", "gm", "gms")),
    ("Kilogram", "Kilograms", ("kg", "kgs", "kilo", "kilos")),
    ("Ounce", "Ounces", ("oz",)),
    ("Pound", "Pounds", ("lb", "lbs")),
    ("Milliliter", "Milliliters", ("ml", "millilitre", "millilitres")),
    ("Liter", "Liters", ("l", "ltr", "litre", "litres")),
    ("Teaspoon", "Teaspoons", ("tsp", "tsps", "t")),
    ("Tablespoon", "Tablespoons", ("tbsp", "tbsps", "tbs", "tbl", "T")),
    ("Fluid ounce", "Fluid ounces", ("fl oz", "floz")),
    ("Cup", "Cups", ("c",)),
    ("Pint", "Pints", ("pt",)),
    ("Quart", "Quarts", ("qt",)),
    ("Gallon", "Gallons", ("gal",)),
    ("Pack", "Packs", ("pk", "package", "packages", "pkg", "packet", "packets")),
    ("Box", "Boxes", ()),
    ("Bunch", "Bunches", ()),
    ("Loaf", "Loaves", ()),
)

# How big a unit with a fixed size is: (what it measures, how many grams or
# milliliters one holds, in US kitchen measures). Used to convert between two
# of them only when Grocy has no conversion of its own, so "500 g" into a
# product kept in Kilograms is 0.5. A Piece is a count, which no weight or
# volume turns into.
_UNIT_SIZES = {
    "Milligram": ("weight", 0.001),
    "Gram": ("weight", 1.0),
    "Kilogram": ("weight", 1000.0),
    "Ounce": ("weight", 28.349523125),
    "Pound": ("weight", 453.59237),
    "Milliliter": ("volume", 1.0),
    "Liter": ("volume", 1000.0),
    "Teaspoon": ("volume", 4.92892159375),
    "Tablespoon": ("volume", 14.78676478125),
    "Fluid ounce": ("volume", 29.5735295625),
    "Cup": ("volume", 236.5882365),
    "Pint": ("volume", 473.176473),
    "Quart": ("volume", 946.352946),
    "Gallon": ("volume", 3785.411784),
    "Piece": ("count", 1.0),
}

# Counts with a fixed size are Pieces, never a unit of their own: a dozen
# eggs is 12 Pieces, so using one leaves 11.
_FIXED_COUNTS = {
    "dozen": 12, "dozens": 12, "doz": 12,
    "half dozen": 6, "half a dozen": 6, "half-dozen": 6,
    "pair": 2, "pairs": 2,
}

# Plurals the s-and-es rule gets wrong, by their singular.
_IRREGULAR_PLURALS = {
    "leaf": "leaves", "half": "halves", "loaf": "loaves", "knife": "knives",
    "potato": "potatoes", "tomato": "tomatoes",
}
_IRREGULAR_SINGULARS = {p: s for s, p in _IRREGULAR_PLURALS.items()}


# What the queue sends when no unit was typed. An existing product takes the
# amount in its own stock unit, as every add did before units were read.
_NO_UNIT = frozenset({"", "item", "items"})


def _unit_key(typed: str) -> str:
    """``typed`` as one spelling: no dots, single spaces, lowercase. Pure.

    "Fl. Oz.", "fl  oz" and "fl oz" are all "fl oz"; "doz." is "doz". A lone
    capital "T" stays as it is: in a recipe it is a tablespoon, and "t" a
    teaspoon.
    """
    key = " ".join((typed or "").replace(".", " ").split())
    return key if key == "T" else key.lower()


def counted_out(quantity: float, typed: str) -> tuple[float, str] | None:
    """``quantity`` of a fixed count ("dozen") as (Pieces, "Piece"). Pure.

    None when ``typed`` is not a fixed count, so the unit stays as typed.
    """
    size = _FIXED_COUNTS.get(_unit_key(typed))
    return (quantity * size, "Piece") if size else None


def unit_names(typed: str) -> tuple[str, str, set[str]]:
    """The Grocy unit a typed unit means: (name, plural, every spelling). Pure.

    Known units ("kg", "Kilograms", "pcs") come back as the one name Grocy
    should hold them under. Anything else keeps the word typed, made singular
    and capitalized like Grocy's own units ("bags" is Bag, Bags). Nothing
    typed is Piece, and so is anything that cannot be a unit's name (digits,
    brackets or other symbols, more than two words, one letter that is no
    known unit). The spellings are as _unit_key reads them, for finding a
    unit Grocy already has under any of them.
    """
    low = _unit_key(typed)
    if (len(low) > 30 or len(low.split()) > 2
            or not re.fullmatch(r"[^\W\d_]+(?:[ '-][^\W\d_]+)*", low)):
        low = ""
    for name, plural, aliases in _KNOWN_UNITS:
        spellings = {name.lower(), plural.lower(), *aliases}
        if not low or low in spellings:
            return name, plural, spellings
    if len(low) == 1:
        # "x" or "q" is no unit anyone keeps food in, so it is a count.
        return unit_names("")
    if low in _IRREGULAR_SINGULARS:
        singular = _IRREGULAR_SINGULARS[low]
    elif re.search(r"(ss|x|ch|sh)es$", low):
        singular = low[:-2]
    elif low.endswith("s") and not re.search(r"(ss|us|is)$", low):
        # "bus", "citrus" and "glass" are already singular.
        singular = low[:-1]
    else:
        singular = low
    plural = _IRREGULAR_PLURALS.get(singular) or (
        singular + ("es" if re.search(r"(s|x|ch|sh)$", singular) else "s"))
    return (singular[:1].upper() + singular[1:], plural[:1].upper() + plural[1:],
            {low, singular, plural})


def unit_size(name: str) -> tuple[str, float] | None:
    """What the unit ``name`` measures and its size (_UNIT_SIZES), or None. Pure.

    None for a unit with no fixed size, such as a Pack or a Bag, and for
    nothing typed at all.
    """
    return _UNIT_SIZES.get(unit_names(name)[0]) if _unit_key(name) else None


def builtin_factor(from_unit: str, to_unit: str) -> float | None:
    """How many ``to_unit`` make one ``from_unit``, from _UNIT_SIZES. Pure.

    None when either unit has no fixed size or the two measure different
    things (a weight and a volume).
    """
    src, dst = unit_size(from_unit), unit_size(to_unit)
    if not src or not dst or src[0] != dst[0]:
        return None
    return src[1] / dst[1]


def unit_label(unit: dict, amount: float) -> str:
    """``unit``'s name for ``amount`` of it: plural unless exactly one. Pure."""
    name = unit.get("name") or ""
    try:
        one = float(amount) == 1
    except (TypeError, ValueError):
        one = False
    return name if one else (unit.get("name_plural") or name)


def find_unit(units: list[dict], typed: str) -> dict | None:
    """The Grocy quantity unit ``typed`` names, by any spelling, or None. Pure.

    A unit named exactly as typed wins over one that only shares a spelling,
    so a kitchen holding both "kg" and "Kilogram" gets the one it typed.
    """
    exact = next((u for u in units or [] if unit_matches(u, typed)), None)
    if exact is not None:
        return exact
    spellings = unit_names(typed)[2]
    return next((u for u in units or [] if any(
        unit_matches(u, s) for s in spellings)), None)


# Shared connection pool for the life of the app
_client = httpx.AsyncClient(timeout=15.0)
_JSON = {"Content-Type": "application/json"}

SERVER_ONLY_MESSAGE = "Grocy is managed on the main server."


def _server_only(fn):
    """For a method that changes how Grocy itself is set up. A Bandit's calls
    go through its main server's proxy, which carries none of these, so on a
    Bandit it refuses before anything is sent."""
    @functools.wraps(fn)
    async def wrapper(self, *args, **kwargs):
        if self._upstream or settings.is_satellite():
            raise GrocyError(SERVER_ONLY_MESSAGE, kind="config")
        return await fn(self, *args, **kwargs)
    return wrapper


def _stock_unit_name(entry: dict, product: dict, units: dict[str, str]) -> str | None:
    """The stock unit's name for one /stock row, or None when it is unknown.

    Older Grocy nested a qu_unit_stock object on the row; 4.7 carries only the
    id (qu_id_stock) on the product, so look that up in the units table.
    """
    nested = entry.get("quantity_unit_stock") or product.get("qu_unit_stock")
    if isinstance(nested, dict) and nested.get("name"):
        return nested["name"]
    qu_id = product.get("qu_id_stock") or entry.get("qu_id_stock")
    if qu_id is None:
        return None
    return units.get(str(qu_id)) or None


class GrocyClient:
    """One instance per request. Lookup tables (locations, groups, units,
    products) are cached on the instance so multi-item imports don't re-fetch
    them for every item."""

    def __init__(self):
        self._upstream = bool(settings.is_satellite() and settings.upstream_linked())
        if self._upstream:
            # A satellite has no Docker network, so it cannot reach the server's
            # internal Grocy (http://grocy:80). Route calls through the main
            # server's authenticated proxy, which forwards them to its Grocy.
            # The headers name this device and its release (X-PR-Client) and
            # carry its key; services/upstream.py builds them for every call.
            self.base = settings.remote_server_url.rstrip("/") + "/api/proxy/grocy/api"
            self.headers = upstream.request_kwargs(extra=_JSON)["headers"]
        else:
            self.base = settings.grocy_base_url.rstrip("/") + "/api"
            self.headers = {
                "GROCY-API-KEY": settings.grocy_api_key,
                "Content-Type": "application/json",
            }
        self._cache: dict[str, list[dict]] = {}

    def _send_kwargs(self) -> dict:
        if self._upstream:
            return upstream.request_kwargs(extra=_JSON)
        return {"headers": self.headers}

    async def _request(self, method: str, path: str, body: dict | None = None) -> list | dict:
        try:
            r = await _client.request(
                method, f"{self.base}{path}", json=body, **self._send_kwargs()
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
            refused = proxy_policy.refusal_detail(r.status_code, body)
            if refused:
                # The main server's proxy would not carry this call (a device
                # and server on different releases). Its words, not "Grocy 403".
                raise GrocyError(refused, kind="config")
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
        """Find the Grocy location ``name`` or create it. Updates cache.

        A place Grocy already has under its own name (see _LOCATION_ALIASES)
        is used when ``name`` itself is missing, so a fresh Grocy keeps one
        fridge. That place is skipped when it is hidden in Grocy, marked as a
        freezer, or filed under a custom storage category of its own. A
        Freezer created here is marked as one in Grocy. Locations that already
        exist are never changed.
        """
        rows = await self._cached_list("/objects/locations")
        if not any(row["name"].lower() == name.lower() for row in rows):
            owned = {c["location"].strip().lower() for c in custom_categories()}
            for alias in _LOCATION_ALIASES.get(name.lower(), ()):
                if alias.lower() in owned:
                    continue
                for row in rows:
                    if (row["name"].lower() == alias.lower()
                            and str(row.get("active", 1)) != "0"
                            and str(row.get("is_freezer") or 0) in ("0", "")):
                        return int(row["id"])
        extra = None
        if name.lower() == (location_for("frozen") or "").lower():
            # Grocy re-dates a transfer into or out of a freezer only for a
            # product with its own freezing/thawing days, which the products
            # created here leave at 0, so the Move dates stay Pantry Raider's.
            extra = {"is_freezer": 1}
        return await self._ensure_object("/objects/locations", name, extra)

    async def ensure_product_group(self, name: str) -> int:
        return await self._ensure_object("/objects/product_groups", name)

    async def ensure_quantity_unit(self, name: str = "Piece") -> int:
        """The id of the Grocy unit ``name`` means (unit_names), made if missing."""
        rows = await self._cached_list("/objects/quantity_units")
        row = find_unit(rows, name)
        if row is not None:
            return int(row["id"])
        singular, plural, _ = unit_names(name)
        try:
            result = await self._post(
                "/objects/quantity_units", {"name": singular, "name_plural": plural}
            )
        except GrocyError:
            # Grocy refuses a second unit of the same name, so when two adds
            # make the same new unit at once, the one that loses finds the
            # other's unit here instead of failing.
            rows[:] = await self._get("/objects/quantity_units")
            row = find_unit(rows, name)
            if row is None:
                raise
            return int(row["id"])
        new_id = int(result["created_object_id"])
        rows.append({"id": new_id, "name": singular, "name_plural": plural})
        return new_id

    async def ensure_product(self, item: FoodItem, location_id: int, group_id: int) -> int:
        products = await self.get_products()
        name_lower = item.name.lower()
        for p in products:
            if p["name"].lower() == name_lower:
                return int(p["id"])
        # A new product is stocked, bought, used and priced in the unit typed
        # for it, so "2 kg" rice is 2 Kilograms. Grocy fills the consume and
        # price units from the stock unit itself.
        qu_id = await self.ensure_quantity_unit(item.unit)
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
        products.append({"id": new_id, "name": item.name, "qu_id_stock": qu_id})
        return new_id

    async def add_stock(self, product_id: int, item: FoodItem,
                        location_id: int | None = None) -> dict:
        best_before = (
            item.best_by_date.isoformat() if item.best_by_date else date.today().isoformat()
        )
        # When the item came off a back-dated receipt, land it on the receipt's
        # purchase date; otherwise Grocy defaults the entry to today.
        purchased = (
            item.purchased_on.isoformat() if item.purchased_on else date.today().isoformat()
        )
        body = {
            "amount": item.quantity,
            "best_before_date": best_before,
            "purchased_date": purchased,
            "price": None,
            "note": item.brand or "",
        }
        # Without a location Grocy files the entry under the product's default
        # one, so milk added as frozen would sit in the fridge with a freezer
        # best-by date.
        if location_id is not None:
            body["location_id"] = location_id
        return await self._post(f"/stock/products/{product_id}/add", body)

    async def consume_stock(self, product_id: int, amount: float = 1.0,
                            spoiled: bool = False,
                            location_id: int | None = None) -> dict:
        # ``spoiled`` is the difference between "we ate it" and "we threw it
        # out"; Grocy keeps it in the stock log, which is what the waste
        # insights read. Defaults to eaten so no existing caller changes.
        # ``location_id`` limits the consume to one location's stock (#73).
        body = {"amount": amount, "spoiled": bool(spoiled)}
        if location_id:
            body["location_id"] = int(location_id)
        return await self._post(f"/stock/products/{product_id}/consume", body)

    async def open_stock(self, product_id: int, amount: float = 1.0) -> dict:
        """Mark one unit opened. Grocy then applies the product's
        best_before_after_open rule, so an opened jar's expiry turns honest."""
        return await self._post(f"/stock/products/{product_id}/open", {
            "amount": amount,
        })

    async def _refuse_tare_weight(self, product_id: int) -> None:
        """Refuse a per-date action on a product weighed with its container.

        Grocy reads a consume amount for such a product as the gross weight
        on the scale, and opens or moves none of it, so a per-date row cannot
        act on its own stock alone (GrocyError kind "group", before any write).
        """
        for product in await self.get_products():
            if _int_or_none(product.get("id")) == int(product_id):
                if is_tare_weighed(product):
                    raise GrocyError(BEST_BY_ROW_TARE, kind="group")
                return

    async def consume_stock_group(self, product_id: int, amount: float,
                                  location_id: int, best_before_date: str,
                                  spoiled: bool = False,
                                  expected_amount: float | None = None) -> dict:
        """Consume ``amount`` from one per-date row: only the stock with
        ``best_before_date`` ("none" for stock with no date) at ``location_id``.

        A consume with just the location takes what Grocy would use next
        there, an opened unit or an earlier date first. This one names a stock
        id on every call and takes one Grocy row at a time, always the row
        Grocy takes first for its stock id (best_by_group_head), by its exact
        amount. When the amount needs rows that sit behind other stock,
        nothing is written (best_by_take).

        Every booking Grocy reports is checked against the row's place and
        date, and a stock id renamed by a merge since the read (Grocy then
        books nothing) is read again once. Another writer can still get in the
        way part-way, so the answer says how much was ``consumed`` of how much
        was ``requested``, and ``complete`` is False when that fell short.

        ``expected_amount`` is what the page showed for the row; when the row
        holds something else now, nothing is written (check_expected_amount).
        """
        await self._refuse_tare_weight(product_id)
        entries_path = f"/stock/products/{product_id}/entries"
        entries = await self._get(entries_path) or []
        check_expected_amount(entries, location_id, best_before_date, expected_amount)
        want = best_by_take(entries, location_id, best_before_date, amount)
        consumed, stopped, reread = 0.0, False, False
        # Each call takes one row, so this only caps a Grocy that keeps
        # answering without booking.
        for _ in range(len(entries) + 10):
            left = want - consumed
            if left <= _DUST:
                break
            head = best_by_group_head(entries, location_id, best_before_date)
            if head is None:
                stopped = True
                break
            row_amount = _amount(head)
            try:
                booked = await self._post(f"/stock/products/{product_id}/consume", {
                    # The row's own amount exactly as Grocy listed it, unless
                    # less than the whole row is wanted.
                    "amount": row_amount if row_amount - left <= _DUST else left,
                    "spoiled": bool(spoiled),
                    "location_id": int(location_id),
                    "stock_entry_id": head["stock_id"],
                })
            except GrocyError as e:
                if reread or not _nothing_booked(e):
                    # Once some of the row is used, a failure is a short use:
                    # an error would read as if nothing had changed.
                    if consumed > _DUST:
                        stopped = True
                        break
                    raise
                reread = True
                entries = await self._get(entries_path) or []
                if consumed <= _DUST:
                    # Nothing is used yet, so the refusals still come first.
                    check_expected_amount(entries, location_id, best_before_date,
                                          expected_amount)
                    want = best_by_take(entries, location_id, best_before_date, amount)
                continue
            rows = _log_rows(booked, "consume")
            own = [r for r in rows if _booked_in_group(r, location_id, best_before_date)]
            consumed -= sum(_amount(r) for r in own)
            if not own or len(own) < len(rows):
                # Grocy took other stock too, or nothing at all: stop here.
                stopped = True
                break
            entries = await self._get(entries_path) or []
        return {
            "product_id": product_id,
            "consumed": consumed,
            "requested": want,
            "complete": not stopped and want - consumed <= _DUST,
        }

    async def consume_stock_at_place(self, product_id: int, amount: float,
                                     location_id: int, best_before_date: str,
                                     spoiled: bool = False,
                                     expected_amount: float | None = None) -> dict:
        """Consume ``amount`` from one place in Grocy's own order, for a
        per-date row Grocy cannot take alone (an opened unit there goes first).

        This is the explicit second step after consume_stock_group refused:
        the amount is the row's, but Grocy picks which stock in the place it
        takes, opened units and earlier dates first, so it can take stock
        with another best-by date. The row is still checked against what the
        page showed (``expected_amount``), and the place must hold the amount.
        """
        await self._refuse_tare_weight(product_id)
        entries = await self._get(f"/stock/products/{product_id}/entries") or []
        check_expected_amount(entries, location_id, best_before_date, expected_amount)
        if best_by_group_total(entries, location_id, best_before_date) <= _DUST:
            raise GrocyError(BEST_BY_ROW_GONE, kind="gone")
        at_place = sum(_amount(e) for e in entries
                       if _location_id(e) == int(location_id) and _amount(e) > _DUST)
        if float(amount) - at_place > _DUST:
            raise GrocyError("There is not that much in this place any more, "
                             "so nothing was changed.", kind="changed")
        await self.consume_stock(product_id, float(amount), spoiled=spoiled,
                                 location_id=location_id)
        return {
            "product_id": product_id,
            "consumed": float(amount),
            "requested": float(amount),
            "complete": True,
            "place_wide": True,
        }

    async def open_stock_group(self, product_id: int, location_id: int,
                               best_before_date: str) -> dict:
        """Mark one unit of one per-date row opened: only stock with
        ``best_before_date`` ("none" for stock with no date) at ``location_id``.

        Grocy's open has no location: it opens the next unopened unit
        anywhere in the product, or, given a stock id, the first unopened row
        carrying it, wherever that is (best_by_group_head with ``unopened``).
        The amount stops at that row's own: asked for 1 on a row holding half
        a unit, Grocy would open the other half from the next row with the
        stock id, in another place or with another date.

        Returns how much Grocy ``opened``, ``new_best_by``, the date the
        opened unit carries now (Grocy shortens it to the product's days after
        opening, never past the old date), and ``complete``, False when Grocy
        opened anything outside the row.
        """
        await self._refuse_tare_weight(product_id)
        entries_path = f"/stock/products/{product_id}/entries"
        for reread in (False, True):
            entries = await self._get(entries_path) or []
            head = await self._best_by_open_head(entries, location_id, best_before_date)
            amount = min(1.0, _amount(head))
            try:
                booked = await self._post(f"/stock/products/{product_id}/open", {
                    "amount": amount,
                    "stock_entry_id": head["stock_id"],
                })
                break
            except GrocyError as e:
                # A stock id renamed by a merge since the read: read again once.
                if reread or not _nothing_booked(e):
                    raise
        rows = _log_rows(booked, "product-opened")
        own = [r for r in rows if _booked_in_group(r, location_id, best_before_date)]
        opened = sum(_amount(r) for r in own)
        # The opened part keeps the row's id; the unopened rest gets a new one.
        fresh = await self._fresh_entry(product_id, head["id"])
        return {
            "product_id": product_id,
            "opened": opened,
            "new_best_by": (fresh or {}).get("best_before_date"),
            "complete": len(own) == len(rows) and amount - opened <= _DUST,
        }

    async def _best_by_open_head(self, entries: list[dict], location_id,
                                 best_before_date: str) -> dict:
        """The row a per-date row's open names, or the GrocyError saying why
        there is none (nothing is written either way)."""
        group = [e for e in entries if in_best_by_group(e, location_id, best_before_date)]
        if not group:
            raise GrocyError(BEST_BY_ROW_GONE, kind="gone")
        if all(_is_open(e) for e in group):
            raise GrocyError("Everything on this row is already open.", kind="group")
        head = best_by_group_head(entries, location_id, best_before_date, unopened=True)
        if head is not None:
            return head
        blocker = best_by_group_blocker(entries, location_id, best_before_date, unopened=True)
        if blocker is None:
            message = "Grocy would open other stock first, so nothing was opened."
        elif _location_id(blocker) != int(location_id):
            place = ""
            try:
                for loc in await self._cached_list("/objects/locations"):
                    if _int_or_none(loc.get("id")) == _location_id(blocker):
                        place = loc.get("name") or ""
            except GrocyError:
                pass  # the name only words the refusal
            message = (f"Grocy would open one in {place or 'another place'} first, "
                       "so nothing was opened.")
        else:
            message = ("Grocy would open one with another best-by date first, "
                       "so nothing was opened.")
        raise GrocyError(message, kind="group")

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
        if expiring:
            await self._name_locations_and_units(expiring)
        return expiring

    async def get_expiring_by_date(self, days: int = 7, *,
                                   stock: list[dict] | None = None) -> list[dict]:
        """The Expiring page's rows (expiring_by_date): one per product, place
        and best-by date within ``days``, so its buttons act on that stock
        alone. get_expiring keeps one row per product for the counts and
        sensors that only ask how many products are expiring. A caller that
        already holds /stock passes it as ``stock``, saving that read."""
        if stock is None:
            stock, stock_rows = await asyncio.gather(
                self._get("/stock"), self._get("/objects/stock"))
        else:
            stock_rows = await self._get("/objects/stock")
        expiring = expiring_by_date(stock, stock_rows, days)
        if expiring:
            await self._name_locations_and_units(expiring)
        return expiring

    async def _name_locations_and_units(self, entries: list[dict]) -> None:
        """Add location_name and unit_name to /stock rows, in place.

        /stock carries only ids (the entry's or product's location_id, the
        product's qu_id_stock). A failed lookup leaves the names blank: they
        label the row, so they never fail the read that wanted them.
        """
        async def _rows(path):
            try:
                return await self._cached_list(path)
            except Exception:  # noqa: BLE001
                return []
        location_rows, unit_rows = await asyncio.gather(
            _rows("/objects/locations"), _rows("/objects/quantity_units"))
        locations = {str(r.get("id")): r.get("name") or "" for r in location_rows}
        units = {str(u.get("id")): u.get("name") for u in unit_rows if u.get("id") is not None}
        plurals = {str(u.get("id")): u.get("name_plural") or u.get("name")
                   for u in unit_rows if u.get("id") is not None}
        for entry in entries:
            product = entry.get("product") or {}
            loc_id = str(entry.get("location_id") or product.get("location_id") or "")
            entry["location_name"] = locations.get(loc_id, "")
            entry["unit_name"] = _stock_unit_name(entry, product, units) or ""
            entry["unit_name_plural"] = (_stock_unit_name(entry, product, plurals)
                                         or entry["unit_name"])

    def dashboard_product_groups(self) -> list[dict]:
        """Every Grocy product group as ``{"id", "name"}``, sorted by name.

        Read from the rows the last get_full_stock call on this client already
        fetched, so the Inventory page's category filter costs no request of
        its own. Before that call it is an empty list."""
        out = []
        for g in getattr(self, "_dashboard_groups", None) or []:
            try:
                gid = int(g.get("id"))
            except (TypeError, ValueError, AttributeError):
                continue
            name = str(g.get("name") or "").strip()
            if name:
                out.append({"id": gid, "name": name})
        return sorted(out, key=lambda g: (g["name"].lower(), g["id"]))

    async def get_full_stock(self, *, dates: str = "") -> list[dict]:
        """Return all stock entries enriched with name, location, days_remaining, urgency, and storage bucket.

        ``dates="breakdown"`` is the Inventory page's reading of the same rows.
        Each place's amount, opened amount and earliest best-by come from that
        place's own stock entries, a place holding more than one best-by date
        also lists them as ``best_by_dates`` (best_by_breakdown), and a product
        with no stock of its own is left out. Without it the rows are the ones
        every other caller, the Home Assistant sensor among them, has always
        had.

        ``dates="split"`` reads the same way, but a place holding more than one
        best-by date gets a row per date instead of the list: each carries
        ``by_date``, that date's own amount, opened amount and newest added
        date, and ``stock_entry_id`` for its label (best_by_label_stock_id).
        A place with a single date keeps its one row, and so does stock with
        no recorded place, which no action could take by date; neither lists
        its dates. A product's rows come earliest date first, so a stable
        sort keeps them that way among ties.
        """
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
            self._cached_list("/objects/quantity_units"),
            return_exceptions=True,
        )
        for r in results[:4]:
            if isinstance(r, BaseException):
                raise r
        raw, location_rows, group_rows, stock_rows, unit_rows = results
        # Grocy 4.7's /stock rows name the stock unit only by qu_id_stock, so
        # the name comes from the units table. Units are a nicety: a failed
        # read leaves the unit blank rather than failing the whole dashboard.
        if isinstance(unit_rows, BaseException):
            unit_rows = []
        units = {str(u.get("id")): u.get("name") for u in unit_rows if u.get("id") is not None}
        plurals = {str(u.get("id")): u.get("name_plural") or u.get("name")
                   for u in unit_rows if u.get("id") is not None}
        locations = {str(loc["id"]): loc["name"] for loc in location_rows}
        groups = {str(g["id"]): g["name"] for g in group_rows}
        self._dashboard_groups = group_rows   # for dashboard_product_groups()
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
        # /stock also reports one row per product under the product's default
        # location, so stock kept in two places showed its whole total in one
        # (#73). The raw rows carry the real location_id of every entry.
        by_location = stock_by_location(stock_rows)
        # The breakdown reads every place from the raw rows alone, date by
        # date. /stock's best-by for a product kept in one place also counts
        # its sub-products' stock, so that row could show a date missing from
        # its own list of dates. The split reads them the same way.
        by_date = (stock_by_location_and_date(stock_rows)
                   if dates in ("breakdown", "split") else {})
        # A per-date row's label codes one of the row's own stock ids.
        product_rows: dict[int, list[dict]] = {}
        if dates == "split":
            for stock_row in stock_rows:
                product_rows.setdefault(
                    _int_or_none(stock_row.get("product_id")) or 0, []).append(stock_row)
        today = date.today()
        result = []
        for entry in raw:
            product = entry.get("product") or {}
            name = product.get("name") or f"Product {entry.get('product_id', '?')}"
            pid = int(entry.get("product_id", 0))
            group_id = str(product.get("product_group_id") or "")
            total = float(entry.get("amount") or 0)
            # Float dust, or a parent product whose stock all sits in its
            # sub-products (they have rows of their own): nothing to list.
            if dates in ("breakdown", "split") and total <= _DUST:
                continue
            row = {
                "product_id": pid,
                "name": name,
                "unit": _stock_unit_name(entry, product, units),
                # For an amount other than one: "12 Pieces", not "12 Piece".
                "unit_plural": _stock_unit_name(entry, product, plurals),
                "category": groups.get(group_id, ""),
                # The same group by id and name, for the Inventory page's
                # category filter. No group, or a group since deleted, is
                # None and "" (the page's "No category").
                "product_group_id": (int(group_id)
                                     if group_id in groups and group_id.isdigit()
                                     else None),
                "product_group_name": groups.get(group_id, ""),
                "added_date": added.get(pid),
                # The product's total across every location, for callers
                # (the recipe matcher) that ask "is there enough of it".
                "product_amount": total,
            }
            splits = by_location.get(pid)
            places = by_date.get(pid)
            # Only a place the rows themselves name is split by date: a
            # per-date row's buttons name its place, and stock filed under the
            # default location for want of one (below) cannot be taken there.
            named = {loc_id for loc_id in places or {} if loc_id.isdigit()}
            if places:
                # Every place from its own rows. Stock kept in one place is
                # filed where the plain reading files it: under the product's
                # default location when its rows record none.
                if len(places) == 1:
                    only = next(iter(places))
                    places = {only or str(entry.get("location_id")
                                          or product.get("location_id") or ""): places[only]}
                splits = {loc_id: {
                    "amount": sum(p["amount"] for p in parts.values()),
                    "amount_opened": sum(p["amount_opened"] for p in parts.values()),
                    "best_before_date": min((d for d in parts if d), default=None),
                } for loc_id, parts in places.items()}
            elif not splits or len(splits) < 2:
                # Prefer the per-entry location_id; fall back to the product's
                # default, and keep /stock's own amounts and best-by.
                only = next(iter(splits)) if splits else None
                splits = {only or str(entry.get("location_id")
                                      or product.get("location_id") or ""): {
                    "amount": total,
                    "amount_opened": opened.get(pid, 0.0),
                    "best_before_date": entry.get("best_before_date"),
                }}
            first = len(result)
            for loc_id, part in splits.items():
                loc_name = locations.get(loc_id, "")
                if dates == "split" and loc_id in named and len(places[loc_id]) > 1:
                    # One row per date. A place holding a single date keeps
                    # its one row: all of its stock has that date, so the
                    # row's buttons that name the place (Use one, Mark all
                    # consumed, Move) can only take that.
                    for day, dated in places[loc_id].items():
                        days_remaining, urgency = _urgency(day, today)
                        result.append({
                            **row,
                            "added_date": dated["added"],
                            "amount": dated["amount"],
                            "best_before_date": day,
                            "days_remaining": days_remaining,
                            "urgency": urgency,
                            "location_id": int(loc_id),
                            "location_name": loc_name,
                            "storage_bucket": classify_location(loc_name),
                            "amount_opened": dated["amount_opened"],
                            "by_date": True,
                            "stock_entry_id": best_by_label_stock_id(
                                product_rows.get(pid), loc_id, day or "none"),
                        })
                    continue
                bbd = part["best_before_date"]
                days_remaining, urgency = _urgency(bbd, today)
                result.append({
                    **row,
                    "amount": part["amount"],
                    "best_before_date": bbd,
                    "days_remaining": days_remaining,
                    "urgency": urgency,
                    "location_id": int(loc_id) if loc_id.isdigit() else None,
                    "location_name": loc_name,
                    "storage_bucket": classify_location(loc_name),
                    "amount_opened": part["amount_opened"],
                })
                # A row with one best-by date gets no list; its date says it all.
                if dates == "breakdown" and places and len(places[loc_id]) > 1:
                    result[-1]["best_by_dates"] = best_by_breakdown(places[loc_id])
            if dates == "split":
                # Earliest date first (undated last), then by place.
                result[first:] = sorted(result[first:], key=lambda r: (
                    r["best_before_date"] is None, r["best_before_date"] or "",
                    r["location_id"] or 0))
        return result

    async def edit_product(self, product_id: int,
                           category: str | None = None,
                           best_before_date: str | None = None, *,
                           from_location_id: int | None = None,
                           from_best_before_date: str | None = None) -> dict:
        """Update category (product group) and/or best-by date for every open stock entry.

        ``from_location_id`` with ``from_best_before_date`` ("none" for stock
        with no date) moves the date of one row's entries only, a per-date
        row's or a plain row's whose place holds that single date, and says
        how many were ``updated``; the category is the whole product's either
        way. The row's entries are picked before anything is written,
        so a row whose stock has gone answers kind "gone" and changes nothing.
        """
        scoped = from_location_id is not None or from_best_before_date is not None
        if scoped and (not from_location_id or from_best_before_date is None):
            # Half a scope must never fall back to dating every entry.
            raise GrocyError("A per-date edit needs both the place and the best-by date.")
        group_ids = None
        if scoped and best_before_date is not None:
            entries = await self._get(f"/stock/products/{product_id}/entries") or []
            group_ids = [e.get("id") for e in entries if e.get("id") and in_best_by_group(
                e, from_location_id, from_best_before_date)]
            if not group_ids:
                raise GrocyError(BEST_BY_ROW_GONE, kind="gone")

        if category is not None:
            group_id = await self.ensure_product_group(category)
            await self._request("PUT", f"/objects/products/{product_id}",
                                {"product_group_id": group_id})

        if group_ids is not None:
            # Each entry is read again just before its write (see
            # _fresh_entry). One a merge took away, or one that has left the
            # place or the date since, is no longer the row's to change.
            updated = 0
            for entry_id in group_ids:
                entry = await self._fresh_entry(product_id, entry_id)
                if (entry is not None
                        and in_best_by_group(entry, from_location_id, from_best_before_date)
                        and await self._write_best_by(entry, best_before_date)):
                    updated += 1
            return {"product_id": product_id, "updated": updated}

        if best_before_date is not None:
            # One target date for every entry, so a write can only merge a row
            # into one that already carries it, and the fresh read skips that
            # row: no stale amount is ever written back.
            entries = await self._get(f"/stock/products/{product_id}/entries") or []
            for entry_id in [e.get("id") for e in entries if e.get("id")]:
                await self._set_entry_best_by(product_id, entry_id, best_before_date)

        if scoped:
            return {"product_id": product_id, "updated": 0}
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

    async def extend_best_by(self, product_id: int, delta_days: int, *,
                             location_id: int | None = None,
                             best_before_date: str | None = None,
                             within_days: int = 7) -> dict:
        """Sniff test passed: push the expiring stock's best-by out by
        ``delta_days`` (from today when the entry is already past its date).

        Only stock that is actually expiring moves. ``best_before_date`` (an
        ISO date) takes the entries with that date, in ``location_id`` when it
        is given: an Expiring row's own stock. Without it, the dated entries
        due within ``within_days`` of today, the Expiring page's default
        window. A chicken kept good in the freezer until next year never
        moves because the one in the fridge passed a sniff test. A row whose
        stock has gone answers kind "gone" before anything is written.

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
        if best_before_date is not None:
            def expiring(e: dict) -> bool:
                return (_amount(e) > _DUST
                        and e.get("best_before_date") == best_before_date
                        and (location_id is None or _location_id(e) == int(location_id)))
        else:
            last_day = (date.today() + timedelta(days=within_days)).isoformat()

            def expiring(e: dict) -> bool:
                return bool(e.get("best_before_date")) and e["best_before_date"] <= last_day

        entries = await self._get(f"/stock/products/{product_id}/entries") or []
        candidates = sorted(
            (e for e in entries if e.get("id") and e.get("best_before_date") and expiring(e)),
            key=lambda e: e["best_before_date"], reverse=True)
        if best_before_date is not None and not candidates:
            raise GrocyError(BEST_BY_ROW_GONE, kind="gone")
        updated = 0
        new_dates: list[str] = []
        for candidate in candidates:
            row = await self._fresh_entry(product_id, candidate["id"])
            # A row that has moved since the read (another writer, or a merge
            # into a row already extended) is no longer this one's to extend.
            if row is None or not expiring(row):
                continue
            new_date = sniff_test_new_date(row.get("best_before_date"), delta_days)
            if not new_date:
                continue
            if not await self._write_best_by(row, new_date):
                continue
            updated += 1
            new_dates.append(new_date)
        if best_before_date is not None and not updated:
            # The row's stock was there at the read but merged or moved before
            # any of it was written: say so rather than "no date to extend".
            raise GrocyError(BEST_BY_ROW_MOVED, kind="gone")
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
                           propose_best_by=None,
                           from_location_id: int | None = None,
                           from_best_before_date: str | None = None,
                           amount: float | None = None) -> dict:
        """Transfer all stock of a product to the location for `bucket` and
        make that the product's default location.

        ``from_location_id`` moves only the stock held at that one location
        (#73: a product kept in two places is listed once per place). The
        default location then changes only when no stock is left elsewhere.
        With ``from_best_before_date`` too, only the stock with that best-by
        date there moves: one per-date row (see _move_best_by_group).
        ``amount`` (which needs ``from_location_id``) moves only that much of
        it, and asking for at least what is there moves all of it (see
        _move_amount_from_location).

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
        if from_best_before_date is not None:
            return await self._move_best_by_group(
                product_id, to_name, propose_best_by, from_location_id,
                from_best_before_date, amount)
        if amount is not None:
            if not from_location_id:
                raise GrocyError("A move of part of the stock needs the place it is in.")
            moving = await self._move_amount_from_location(
                product_id, to_name, propose_best_by, int(from_location_id), amount)
            if moving is not None:
                return moving
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
        grocy_dated: set[int] = set()
        settled: set[str] = set()
        # Each pass moves one whole row, so this only caps a Grocy that never
        # lets go of one.
        for _ in range(len(entries) + 10):
            row = next((e for e in entries if _movable(e, to_id)
                        and (not from_location_id
                             or _location_id(e) == int(from_location_id))), None)
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
            booked = await self._post(f"/stock/products/{product_id}/transfer", transfer)
            moved += amount
            # Into or out of a Grocy freezer, Grocy dates the arrival itself
            # for a product with its own freezing or thawing days, over the
            # date written above. Report the date Grocy holds.
            for left_with, arrived_with in _transfer_redates(booked):
                i = next((i for i, u in enumerate(best_by_updates)
                          if i not in grocy_dated and u["new"] == left_with), None)
                if i is None:
                    best_by_updates.append({"old": left_with, "new": left_with})
                    i = len(best_by_updates) - 1
                best_by_updates[i]["new"] = arrived_with
                grocy_dated.add(i)
            entries = await self._get(entries_path) or []

        # Entries without a location can't be transferred, but changing the
        # product's default location still re-buckets them on the dashboard.
        # A one-location move leaves stock elsewhere on the old default.
        if not from_location_id or not any(
                _movable(e, to_id) for e in entries):
            await self._request("PUT", f"/objects/products/{product_id}",
                                {"location_id": to_id})

        # One transfer moves in one temperature direction, so every update
        # agrees; the earliest resulting date is the one the user will see
        # drive the expiring list. Grocy can date undated stock it freezes.
        dated = [u for u in best_by_updates if u["old"] and u["new"]]
        change = None
        if dated:
            change = "extended" if dated[0]["new"] > dated[0]["old"] else "shortened"
        new_dates = [u["new"] for u in best_by_updates if u["new"]]
        return {
            "product_id": product_id,
            "moved_amount": moved,
            "location_id": to_id,
            "best_by_updates": best_by_updates,
            "new_best_by": min(new_dates) if new_dates else None,
            "best_by_change": change,
        }

    async def _move_amount_from_location(self, product_id: int, to_name: str,
                                         propose_best_by, from_id: int,
                                         amount: float) -> dict | None:
        """move_product for ``amount`` of the stock at ``from_id``, or None
        when that is all of it (the whole-place move then runs as before).

        The stock moves one best-by date at a time, through
        _move_best_by_group, in the order Grocy itself would use it (undated
        first, then earliest due), so only what arrives is re-dated. A date
        Grocy cannot move on its own stops the move: before anything moved it
        is refused as usual, after that the answer says what was
        ``left_behind``.
        """
        if amount <= _DUST:
            raise GrocyError("Nothing to move.")
        entries = await self._get(f"/stock/products/{product_id}/entries") or []
        at_source = [e for e in entries if _amount(e) > _DUST and _location_id(e) == from_id]
        total = sum(_amount(e) for e in at_source)
        if total <= _DUST:
            raise GrocyError(BEST_BY_ROW_GONE, kind="gone")
        if amount >= total - _DUST:
            return None
        tokens = list(dict.fromkeys(e.get("best_before_date") or "none" for e in at_source))

        moved = 0.0
        best_by_updates: list[dict] = []
        to_id = None
        for token in tokens:
            if amount - moved <= _DUST:
                break
            try:
                part = await self._move_best_by_group(
                    product_id, to_name, propose_best_by, from_id, token, amount - moved)
            except GrocyError as e:
                # Gone since the read: the next date can still serve.
                if e.kind == "gone":
                    continue
                if moved > _DUST:
                    break
                raise
            to_id = part["location_id"]
            moved += part["moved_amount"]
            best_by_updates += part["best_by_updates"]
            if to_id == from_id or part["left_behind"] > _DUST:
                break
        if to_id is None:
            raise GrocyError(BEST_BY_ROW_GONE, kind="gone")

        dated = [u for u in best_by_updates if u["old"] and u["new"]]
        change = None
        if dated:
            change = "extended" if dated[0]["new"] > dated[0]["old"] else "shortened"
        new_dates = [u["new"] for u in best_by_updates if u["new"]]
        return {
            "product_id": product_id,
            "moved_amount": moved,
            "location_id": to_id,
            "best_by_updates": best_by_updates,
            "new_best_by": min(new_dates) if new_dates else None,
            "best_by_change": change,
            "left_behind": (0.0 if to_id == from_id or amount - moved <= _DUST
                            else amount - moved),
        }

    async def _move_best_by_group(self, product_id: int, to_name: str, propose_best_by,
                                  from_location_id: int | None,
                                  best_before_date: str,
                                  amount: float | None = None) -> dict:
        """move_product for one per-date row: only the stock with
        ``best_before_date`` ("none" for stock with no date) at
        ``from_location_id``, or only ``amount`` of it.

        A partial amount takes the head's row in part, and Grocy splits it:
        the part that moves arrives as a new row with the same stock id, and
        only that arrival is re-dated, never the rest left at the source.

        Nothing is written, not even a new location, until a fresh read shows
        that Grocy hands the whole row over one head at a time: each transfer
        names the stock id of a row Grocy takes first for it
        (best_by_group_head) and moves that row whole. A transfer never merges
        rows, but a date write does, and it renames stock ids across the
        product, so the plan is made again before every transfer, and a move
        that stops part-way says how much was ``left_behind``.

        The new best-by comes once from the row's date and the place it
        leaves, and each arrival is re-dated before the next transfer. Left
        to the end, the first write could merge a moved row with stock already
        there that still carries the old date, and the next write would re-date
        that stock too. When Grocy dated the arrival itself (its own freezer
        rule), its date stands, as on any move, and ``best_by_updates`` lists
        the dates Grocy holds.
        """
        if not from_location_id:
            raise GrocyError("A per-date move needs the place the stock is in.")
        from_id = int(from_location_id)
        await self._refuse_tare_weight(product_id)
        entries_path = f"/stock/products/{product_id}/entries"
        entries = await self._get(entries_path) or []

        def left_at_source(rows: list[dict]) -> float:
            return sum(_amount(e) for e in rows
                       if in_best_by_group(e, from_id, best_before_date))

        total = left_at_source(entries)
        if total <= _DUST:
            raise GrocyError(BEST_BY_ROW_GONE, kind="gone")
        want = total if amount is None or amount >= total - _DUST else float(amount)
        if best_by_group_reach(entries, from_id, best_before_date, want) < want - _DUST:
            raise GrocyError("Grocy would move other stock with it, so nothing was moved.",
                             kind="group")
        to_id = await self.ensure_location(to_name)

        old_iso = _token_date(best_before_date)
        new_iso = None
        if propose_best_by is not None and old_iso and to_id != from_id:
            names = {str(loc["id"]): loc["name"]
                     for loc in await self._cached_list("/objects/locations")}
            proposed = propose_best_by(date.fromisoformat(old_iso),
                                       classify_location(names.get(str(from_id), "")))
            new_iso = proposed.isoformat() if proposed is not None else None

        moved = 0.0
        best_by_updates: list[dict] = []
        reread = False
        # Each pass moves one whole row, so this only caps a Grocy that never
        # lets go of one.
        for _ in range(len(entries) + 10):
            left = min(left_at_source(entries), want - moved)
            if to_id == from_id or left <= _DUST:
                break
            head = best_by_group_head(entries, from_id, best_before_date)
            if head is None or best_by_group_reach(
                    entries, from_id, best_before_date, left) < left - _DUST:
                break   # a merge since the last pass put other stock in the way
            # The head whole, or the part of it still to move: a whole row is
            # sent by its exact amount, so no float crumb stays behind.
            take = _amount(head) if _amount(head) - left <= _DUST else left
            there = {str(e.get("id")) for e in entries if _location_id(e) == to_id}
            try:
                booked = await self._post(f"/stock/products/{product_id}/transfer", {
                    "amount": take,
                    "location_id_from": from_id,
                    "location_id_to": to_id,
                    "stock_entry_id": head["stock_id"],
                })
            except GrocyError as e:
                if reread or not _nothing_booked(e):
                    if moved > _DUST:
                        break
                    raise
                reread = True
                entries = await self._get(entries_path) or []
                continue
            arrived, strays = _best_by_arrivals(booked, from_id, best_before_date)
            moved += sum(_amount(r) for r in arrived)
            entries = await self._get(entries_path) or []
            grocy_dated = [r for r in arrived if (r.get("best_before_date") or None) != old_iso]
            if grocy_dated:
                best_by_updates += [{"old": old_iso, "new": r.get("best_before_date")}
                                    for r in grocy_dated]
            elif new_iso and arrived:
                # The rows that just arrived: the head itself, or the part of
                # it a partial transfer left as a new row there.
                stock_ids = {r.get("stock_id") for r in arrived}
                landed = [e for e in entries if _location_id(e) == to_id
                          and str(e.get("id")) not in there
                          and e.get("stock_id") in stock_ids
                          and (e.get("best_before_date") or None) == old_iso]
                for e in landed:
                    if await self._set_entry_best_by(product_id, e.get("id"), new_iso):
                        best_by_updates.append({"old": old_iso, "new": new_iso})
                if landed:
                    entries = await self._get(entries_path) or []
            if strays or not arrived:
                break   # Grocy moved other stock as well, or nothing
        # Only what was asked for and did not move is left behind; the rest of
        # a partial move stays at the source on purpose. What the subtraction
        # leaves of a finished move is float dust, not stock.
        left_behind = 0.0
        if to_id != from_id:
            left_behind = left_at_source(entries) - (total - want)
            if left_behind <= _DUST:
                left_behind = 0.0

        # As for one place's whole stock, the default location follows once
        # none of the product is left anywhere else.
        if moved > _DUST and not any(_movable(e, to_id) for e in entries):
            await self._request("PUT", f"/objects/products/{product_id}",
                                {"location_id": to_id})

        dated = [u for u in best_by_updates if u["old"] and u["new"]]
        change = None
        if dated:
            change = "extended" if dated[0]["new"] > dated[0]["old"] else "shortened"
        new_dates = [u["new"] for u in best_by_updates if u["new"]]
        return {
            "product_id": product_id,
            "moved_amount": moved,
            "location_id": to_id,
            "best_by_updates": best_by_updates,
            "new_best_by": min(new_dates) if new_dates else None,
            "best_by_change": change,
            "left_behind": left_behind,
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

    async def _product_conversions(self, product_id: int) -> list[dict]:
        """Unit conversions that apply to ``product_id``.

        Grocy 4 resolves chained and default conversions in a view; older
        releases only have the plain table, which qu_conversion_factor reads
        directly (product-specific, then global)."""
        try:
            return await self._get(
                "/objects/quantity_unit_conversions_resolved"
                f"?query%5B%5D=product_id%3D{product_id}")
        except GrocyError:
            return await self.get_unit_conversions()

    async def find_barcode(self, barcode: str) -> dict | None:
        """The Grocy product registered under ``barcode``, or None.

        Reads Grocy's own barcode table, so a product that was already set up
        in Grocy (for example before Pantry Raider was installed) is recognized
        with its name and units instead of being looked up again elsewhere
        (issue #65). The result carries the barcode's own unit and amount (a
        case barcode can mean "1 Pack") and ``factor``: how many stock units
        one of that unit holds, or None when Grocy has no conversion for it.
        """
        barcode = (barcode or "").strip()
        if not barcode:
            return None
        rows = await self._get(
            "/objects/product_barcodes?query%5B%5D=barcode%3D"
            + quote(barcode, safe=""))
        row = next((r for r in rows or [] if r.get("barcode") == barcode), None)
        product_id = _int_or_none(row and row.get("product_id"))
        if not product_id:
            return None
        product = await self._get(f"/objects/products/{product_id}")
        if not product or not product.get("name"):
            return None
        units = {int(u["id"]): u for u in
                 await self._cached_list("/objects/quantity_units")}
        stock_qu = _int_or_none(product.get("qu_id_stock"))
        barcode_qu = _int_or_none(row.get("qu_id")) or stock_qu
        try:
            amount = float(row.get("amount") or 0) or 1.0
        except (TypeError, ValueError):
            amount = 1.0
        factor = None
        if barcode_qu and stock_qu:
            factor = qu_conversion_factor(
                await self._product_conversions(product_id),
                product_id, barcode_qu, stock_qu)
        if factor is None:
            # No way to turn the barcode's unit into stock units: fall back
            # to the stock unit so the amount is never silently scaled wrong.
            barcode_qu, factor = stock_qu, 1.0
        location = group = ""
        loc_id = _int_or_none(product.get("location_id"))
        if loc_id:
            location = next((loc["name"] for loc in
                             await self._cached_list("/objects/locations")
                             if _int_or_none(loc.get("id")) == loc_id), "")
        group_id = _int_or_none(product.get("product_group_id"))
        if group_id:
            group = next((g["name"] for g in
                          await self._cached_list("/objects/product_groups")
                          if _int_or_none(g.get("id")) == group_id), "")
        return {
            "product_id": product_id,
            "name": product["name"],
            "unit": (units.get(barcode_qu) or {}).get("name") or "item",
            "amount": amount,
            "factor": factor,
            "location": location,
            "location_id": loc_id,
            "product_group": group,
            "default_best_before_days": _int_or_none(
                product.get("default_best_before_days")) or 0,
        }

    async def stock_amount(self, product_id: int, quantity: float,
                           unit: str) -> float:
        """``quantity`` of ``unit`` expressed in the product's stock unit.

        ``unit`` is a Grocy quantity unit name (singular or plural). A unit
        Grocy does not know, or one with no conversion for this product, is
        taken as the stock unit itself, which is what the add used before,
        unless it plainly measures something else (_in_stock_unit)."""
        product = await self._get(f"/objects/products/{product_id}")
        return (await self._in_stock_unit(product, quantity, unit))[0]

    async def _in_stock_unit(self, product: dict, quantity: float, unit: str,
                             typed: str | None = None) -> tuple[float, str | None]:
        """``quantity`` of ``unit`` in ``product``'s stock unit, and a note.

        Grocy's own conversion wins; with none, two units of a fixed size that
        measure the same thing convert by _UNIT_SIZES ("500 g" into Kilograms
        is 0.5). The note is None when the amount is right as it stands: the
        units match or convert, the product's stock unit is unknown, or no
        unit was typed (the queue's "item", which is never converted).

        A weight, a volume or a fixed count ("1 dozen") into a product kept in
        a unit that measures something else raises GrocyError, so the item
        stays in the queue to be fixed rather than "2 kg" going in as 2
        Pieces. Anything else with no conversion (a Bag, or "pcs" into a
        product kept in Pounds) is kept as it was, in the stock unit, and the
        note says so. ``typed`` is the amount as typed when a fixed count was
        turned into Pieces."""
        if _unit_key(unit) in _NO_UNIT and not typed:
            # Nothing typed: the amount is in the product's own unit, as it
            # always was. Read as Piece it could be scaled by a Piece
            # conversion the kitchen set up for something else.
            return quantity, None
        product_id = _int_or_none(product.get("id"))
        stock_qu = _int_or_none(product.get("qu_id_stock"))
        if not stock_qu:
            return quantity, None
        units = await self._cached_list("/objects/quantity_units")
        unit_qu = _int_or_none((find_unit(units, unit) or {}).get("id"))
        stock = next((u for u in units if _int_or_none(u.get("id")) == stock_qu), {})
        factor = None
        if unit_qu:
            factor = 1.0 if unit_qu == stock_qu else qu_conversion_factor(
                await self._product_conversions(product_id),
                product_id, unit_qu, stock_qu)
        if not factor and stock.get("name"):
            factor = builtin_factor(unit, stock["name"])
        if factor:
            return quantity * factor, None
        name = product.get("name") or "This product"
        stock_names = stock.get("name_plural") or stock.get("name") or "another unit"
        source = f"Pieces ({typed})" if typed else unit.strip()
        size, stock_size = unit_size(unit), unit_size(stock.get("name") or "")
        loose_count = unit_names(unit)[0] == "Piece" and not typed
        if size and stock_size and size[0] != stock_size[0] and not loose_count:
            raise GrocyError(
                f"{name} is kept in {stock_names} in Grocy, and "
                f"{typed or unit.strip()} cannot "
                f"be turned into {stock_names} without a unit conversion. Change "
                f"the unit to {stock_names}, or add a conversion for it in "
                f"Grocy, then add it again.", kind="unit")
        label = unit_label(stock, quantity) or "stock units"
        return quantity, (
            f"{name} is kept in {stock_names} in Grocy, which has no conversion "
            f"from {source}, so it was added as {quantity:g} {label}.")

    async def _count_out(self, product: dict | None,
                         item: FoodItem) -> tuple[FoodItem, str | None]:
        """``item`` with a fixed count ("1 dozen") as Pieces, and what was typed.

        Left as it is, with None, when the unit is no fixed count, or when
        ``product`` already uses a Grocy unit of that name under any spelling
        ("doz." finds a Dozen unit): a kitchen that keeps eggs in its own
        Dozen unit adds 1 Dozen, never 12 of them."""
        counted = counted_out(item.quantity, item.unit)
        if not counted:
            return item, None
        stock_qu = _int_or_none((product or {}).get("qu_id_stock"))
        if stock_qu:
            units = await self._cached_list("/objects/quantity_units")
            # Every spelling of this count, read the way a typed unit is
            # (unit_matches), so "doz." finds a unit named Dozen. Not
            # find_unit: it reads "half a dozen" as no unit, which is Piece.
            size = _FIXED_COUNTS[_unit_key(item.unit)]
            same_size = [k for k, v in _FIXED_COUNTS.items() if v == size]
            row = next((u for u in units if any(
                unit_matches(u, s) for s in same_size)), None)
            unit_qu = _int_or_none((row or {}).get("id"))
            if unit_qu and (unit_qu == stock_qu or qu_conversion_factor(
                    await self._product_conversions(int(product["id"])),
                    int(product["id"]), unit_qu, stock_qu)):
                return item.model_copy(update={"unit": row["name"]}), None
        typed = f"{item.quantity:g} {item.unit.strip()}"
        return item.model_copy(update={"quantity": counted[0], "unit": counted[1]}), typed

    async def add_to_product(self, product_id: int, item: FoodItem,
                             location_id: int | None = None) -> dict:
        """Stock ``item`` onto an existing product, converting its unit.

        Used for a scan Grocy already recognized (issue #65): the stock lands
        on that exact product, never on a new one created by name.
        ``location_id`` is the product's own Grocy location, kept from the scan
        while the review screen left it chosen. Without one the stock goes to
        the storage area the user picked on review. Carries the same
        ``unit_note`` as import_item when the amount went in unconverted."""
        product = await self._get(f"/objects/products/{product_id}")
        item, typed = await self._count_out(product, item)
        amount, note = await self._in_stock_unit(product, item.quantity,
                                                 item.unit, typed)
        if location_id is None:
            location_id = await self.ensure_location(
                _STORAGE_LABEL[item.storage_type.value])
        await self.add_stock(product_id, item.model_copy(update={"quantity": amount}),
                             location_id)
        result = {"product_id": product_id, "name": item.name, "amount": amount}
        if note:
            result["unit_note"] = note
        return result

    async def import_item(self, item: FoodItem) -> dict:
        existing = next((p for p in await self.get_products()
                         if p["name"].lower() == item.name.lower()), None)
        # "1 dozen" is 12 Pieces before anything else sees it, so a new
        # product is made in Pieces and never in a Dozen unit.
        item, typed = await self._count_out(existing, item)
        note = None
        if existing is not None:
            # A product Grocy already has keeps its own unit: "500 g" of
            # flour kept in Kilograms goes in as 0.5. Worked out before
            # anything is made in Grocy, since a unit that cannot go in
            # refuses the whole add.
            amount, note = await self._in_stock_unit(existing, item.quantity,
                                                     item.unit, typed)
            item = item.model_copy(update={"quantity": amount})
        storage_name = _STORAGE_LABEL[item.storage_type.value]
        location_id = await self.ensure_location(storage_name)
        group_id = await self.ensure_product_group(item.category.value)
        product_id = await self.ensure_product(item, location_id, group_id)
        await self.add_stock(product_id, item, location_id)
        # Link the scanned barcode to the product so a later consume-mode scan
        # can resolve it. Best effort: a failure here must not lose the stock
        # add that already happened.
        if getattr(item, "barcode", None):
            try:
                await self.ensure_product_barcode(product_id, item.barcode)
            except Exception:  # noqa: BLE001
                pass
        result = {"product_id": product_id, "name": item.name}
        if note:
            result["unit_note"] = note
        return result

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

    # --- Tidying up older installs' locations (services/grocy_locations.py) ---

    async def get_locations(self) -> list[dict]:
        """Grocy's locations as they are now, never the per-instance cache."""
        return await self._get("/objects/locations") or []

    @_server_only
    async def mark_location_freezer(self, location_id: int) -> None:
        """Mark one location as a freezer in Grocy. Nothing else changes."""
        await self._request("PUT", f"/objects/locations/{int(location_id)}",
                            {"is_freezer": 1})

    @_server_only
    async def _held_at(self, location_id: int) -> list[dict]:
        """The stock rows in one location, for merge_location only (the main
        server's proxy does not carry this read for a Bandit)."""
        rows = await self._get(f"/stock/locations/{int(location_id)}/entries") or []
        return [e for e in rows if _amount(e) > _DUST]

    async def _transfer_all_rows(self, product_id: int, from_id: int, to_id: int) -> int:
        """Move every stock row of one product from ``from_id`` to ``to_id``,
        one whole row per transfer. Returns how many rows moved.

        As in move_product, each transfer names the stock id and exact amount
        of the row Grocy takes first for that id, and the entries are read
        again after each one. Dust is not stock (see _DUST), the same rule
        _held_at applies, so a crumb is neither sent nor counted as left.
        """
        entries_path = f"/stock/products/{product_id}/entries"
        entries = await self._get(entries_path) or []
        moved = 0
        for _ in range(len(entries) + 10):
            row = next((e for e in entries
                        if _amount(e) > _DUST and _location_id(e) == from_id), None)
            if row is None:
                break
            first = (_transfer_pool(entries, row, from_id) or [row])[0]
            body = {"amount": _amount(first), "location_id_from": from_id,
                    "location_id_to": to_id}
            if first.get("stock_id"):
                body["stock_entry_id"] = first["stock_id"]
            await self._post(f"/stock/products/{product_id}/transfer", body)
            moved += 1
            entries = await self._get(entries_path) or []
        return moved

    @_server_only
    async def merge_location(self, from_id: int, to_id: int) -> dict:
        """Move everything in location ``from_id`` to ``to_id``, then hide
        ``from_id`` in Grocy (active=0). Nothing is deleted.

        Stock moves row by row (_transfer_all_rows), so each row keeps its
        amount, best-by date, opened state, price and purchase date. Grocy
        re-dates a transfer only between a freezer and a non-freezer, so the
        caller checks that neither place is a freezer. Then every product whose
        default location or default consume location is ``from_id`` points at
        ``to_id``, and so does Grocy's preset location for new products. The
        stock journal keeps its history as it was.

        Every step reads Grocy again before it writes, so a merge cut short (a
        power cut on a Pi) carries on from where it stopped when run again.
        The place is hidden only once no stock and no product is left in it;
        until then ``left`` names the products still there, ``weighed`` those
        weighed with their container (Grocy transfers none of them) and
        ``errors`` what Grocy answered. Returns {"moved_rows", "products",
        "left", "weighed", "errors", "hidden"}.
        """
        from_id, to_id = int(from_id), int(to_id)
        products = await self._get("/objects/products") or []
        names = {str(p.get("id")): str(p.get("name") or "") for p in products}
        tare = {str(p.get("id")) for p in products
                if str(p.get("enable_tare_weight_handling") or "0") not in ("0", "")}
        moved_rows = 0
        errors: list[str] = []
        weighed: list[str] = []
        held = await self._held_at(from_id)
        stuck: set[str] = set()
        for pid in dict.fromkeys(str(e.get("product_id")) for e in held):
            if pid in tare:
                # Grocy 4.7.1 refuses to transfer these at all.
                weighed.append(names.get(pid) or "A product")
                stuck.add(pid)
                continue
            try:
                moved_rows += await self._transfer_all_rows(int(pid), from_id, to_id)
            except GrocyError as e:
                if e.kind == "unreachable":
                    raise
                errors.append(f"{names.get(pid) or 'A product'}: {grocy_said(e)}")
                stuck.add(pid)

        # A product whose stock could not move keeps pointing where that stock
        # is; it moves over on the next press, once its stock has.
        repointed = 0
        for p in await self._get("/objects/products") or []:
            if str(p.get("id")) in stuck:
                continue
            change = {}
            if _location_id(p) == from_id:
                change["location_id"] = to_id
            if _int_or_none(p.get("default_consume_location_id")) == from_id:
                change["default_consume_location_id"] = to_id
            if change:
                await self._request("PUT", f"/objects/products/{int(p['id'])}", change)
                repointed += 1
        preset = "/user/settings/product_presets_location_id"
        if _int_or_none((await self._get(preset) or {}).get("value")) == from_id:
            await self._request("PUT", preset, {"value": to_id})

        left = list(dict.fromkeys(names.get(str(e.get("product_id"))) or "A product"
                                  for e in await self._held_at(from_id)))
        pointing = [p for p in await self._get("/objects/products") or []
                    if from_id in (_location_id(p),
                                   _int_or_none(p.get("default_consume_location_id")))]
        hidden = not left and not pointing
        if hidden:
            await self._request("PUT", f"/objects/locations/{from_id}", {"active": 0})
        return {"moved_rows": moved_rows, "products": repointed, "left": left,
                "weighed": weighed, "errors": errors, "hidden": hidden}

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

# Pantry Raider's location name (lower case) → Grocy's own name for the same
# place, used by ensure_location only when the first is missing. A fresh Grocy
# ships with a single "Fridge"; filing fridge stock there, not in a new
# "Refrigerator" beside it, leaves one fridge. An install that already has a
# "Refrigerator" keeps using it.
_LOCATION_ALIASES = {
    "refrigerator": ("Fridge",),
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
    pid = plain_int(pid_raw)
    if pid is None or pid <= 0:
        return None
    sid = parts[3] if len(parts) == 4 else ""
    if len(parts) == 4 and not (sid.isascii() and sid.isalnum()):
        return None
    return pid, sid
