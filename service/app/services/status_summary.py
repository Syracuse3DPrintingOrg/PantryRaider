"""Pure state-mapping helpers for the Settings Status dashboard.

The Status pane (FoodAssistant-w00b) is a health dashboard: it aggregates the
checks that already back the individual settings panes (network, Forager, the
update check, Grocy/Mealie/Home Assistant connections) and shows each as a
coloured pill with a short, user-forward line and a "Fix" link into the pane
that owns it.

All of the network and subprocess work happens in the router, which gathers
raw check results with short timeouts and degrades every failure to a plain
dict (never an exception). This module is the pure half: it maps those raw
results to a pill state, a one-line detail, and the settings pane a Fix link
should open. Keeping the mapping here (rather than inline in the route) means
the states are exercised directly by unit tests without any network or Pi.

State vocabulary (matches the .set-pill CSS classes in setup.html):

  good    green   connected / healthy / up to date
  warn    amber   degraded / update available / not configured / off
  bad     red     unreachable / error / sign-in expired
  unknown grey    could not determine (the check itself did not answer)
"""
from __future__ import annotations

# Which settings pane each row's Fix / Manage link opens. Kept beside the
# mappers so a route or a test can look up the destination for any row.
FIX_PANES = {
    "pi_health": "pane-network",
    "connection": "pane-network",
    "update": "pane-backups",
    "forager": "pane-forager",
    "remote_access": "pane-forager",
    "main_server": "pane-devices",
    "grocy": "pane-inventory",
    "mealie": "pane-personalization-recipes",
    "home_assistant": "pane-home-assistant",
}


def _item(key: str, label: str, state: str, detail: str) -> dict:
    return {
        "key": key,
        "label": label,
        "state": state,
        "detail": detail,
        "fix_pane": FIX_PANES.get(key, ""),
    }


def map_pi_health(raw: dict | None) -> dict:
    """Raspberry Pi power / thermal / storage health from the host bridge feed.

    ``raw`` is the ``/setup/system/health`` shape: ``{ok, warnings}``. Any
    active warning (undervoltage, throttling, heat, low storage) drops the row
    to amber with the first warning's text; a clean feed reads healthy.
    """
    if not raw or not raw.get("ok"):
        return _item("pi_health", "Device health", "unknown",
                     "Health could not be read from this device.")
    warnings = raw.get("warnings") or []
    if warnings:
        first = warnings[0]
        text = first.get("message") if isinstance(first, dict) else str(first)
        detail = text or "This device reported a warning."
        extra = len(warnings) - 1
        if extra > 0:
            detail += f" (+{extra} more)"
        return _item("pi_health", "Device health", "warn", detail)
    return _item("pi_health", "Device health", "good",
                 "Power, temperature, and storage are healthy.")


def map_connection(raw: dict | None) -> dict:
    """Which link carries the network, from ``/setup/network/status``.

    ``raw`` is ``{ok, active_connection, ssid, ethernet, ...}``. Ethernet or
    Wi-Fi carrying the default route reads good; no active link reads bad.
    """
    if not raw or not raw.get("ok"):
        return _item("connection", "Connection", "unknown",
                     "Connection status is unavailable.")
    active = raw.get("active_connection", "")
    if active == "wired":
        return _item("connection", "Connection", "good", "Connected over Ethernet.")
    if active == "wifi":
        ssid = raw.get("ssid", "")
        detail = f"Connected to Wi-Fi ({ssid})." if ssid else "Connected over Wi-Fi."
        return _item("connection", "Connection", "good", detail)
    return _item("connection", "Connection", "bad", "This device is offline.")


def map_update(checked_at: float, available: bool, latest: str) -> dict:
    """Software update status from the cached update-check bookkeeping.

    Uses the values the Check for updates action records, so the dashboard is
    instant and never blocks on GitHub. Never checked reads unknown; a pending
    version reads amber; otherwise up to date.
    """
    if not checked_at:
        return _item("update", "Software", "unknown",
                     "Updates have not been checked yet.")
    if available:
        ver = f" ({latest})" if latest else ""
        return _item("update", "Software", "warn", f"An update is available{ver}.")
    return _item("update", "Software", "good", "You are on the latest version.")


def map_forager(raw: dict | None) -> dict:
    """Forager account link from ``/setup/cloud/status``.

    ``raw`` is ``{linked, reachable, valid, ...}``. Not linked reads amber (an
    optional account); linked but rejected or unreachable reads bad; a valid
    link reads good.
    """
    if raw is None:
        return _item("forager", "Forager account", "unknown",
                     "Account status is unavailable.")
    if not raw.get("linked"):
        return _item("forager", "Forager account", "warn", "No account connected.")
    if not raw.get("reachable"):
        return _item("forager", "Forager account", "bad", "Forager could not be reached.")
    if not raw.get("valid"):
        return _item("forager", "Forager account", "bad",
                     "This device is no longer signed in. Connect again.")
    email = raw.get("account_email", "")
    detail = f"Signed in as {email}." if email else "Account connected."
    return _item("forager", "Forager account", "good", detail)


def map_remote_access(raw: dict | None) -> dict:
    """Remote access (tunnel) from ``/setup/tunnel/status``.

    ``raw`` is ``{enabled, up, reachable, public_url}``. Off reads amber (an
    opt-in feature); on and connected reads good; on but not connected reads
    bad.
    """
    if raw is None:
        return _item("remote_access", "Remote access", "unknown",
                     "Remote access status is unavailable.")
    if not raw.get("enabled"):
        return _item("remote_access", "Remote access", "warn", "Turned off.")
    if raw.get("up"):
        url = raw.get("public_url", "")
        detail = f"On and reachable at {url}." if url else "On and reachable."
        return _item("remote_access", "Remote access", "good", detail)
    return _item("remote_access", "Remote access", "bad",
                 "Turned on, but not connected right now.")


def map_main_server(last_sync: dict | None) -> dict:
    """A satellite's link to its main server, from the last sync record.

    ``last_sync`` is ``settings.satellite_last_sync`` (``{at, ok, error}``).
    No sync yet reads amber; a failed sync reads bad; a good sync reads good.
    """
    if not last_sync or not last_sync.get("at"):
        return _item("main_server", "Main server", "warn",
                     "This display has not synced with the main server yet.")
    if last_sync.get("ok"):
        return _item("main_server", "Main server", "good",
                     "Connected to the main server.")
    return _item("main_server", "Main server", "bad",
                 last_sync.get("error") or "The last sync with the main server failed.")


def map_service(key: str, label: str, configured: bool, ok: bool) -> dict:
    """A connected service (Grocy, Mealie, Home Assistant).

    ``configured`` is whether the service has an address and key saved at all;
    ``ok`` is whether the live connection test just succeeded. Unconfigured
    reads amber, a good test reads good, a failed test reads bad.
    """
    if not configured:
        return _item(key, label, "warn", "Not set up.")
    if ok:
        return _item(key, label, "good", "Connected.")
    return _item(key, label, "bad", "Could not connect.")


def build_summary(raw: dict) -> dict:
    """Assemble the dashboard from gathered raw check results (pure).

    ``raw`` maps a row key to that row's raw check input; only the keys present
    are mapped, so the route probes just the rows the current deployment mode
    shows. Returns ``{ok, items}`` where ``items`` maps each key to
    ``{key, label, state, detail, fix_pane}`` for the pane's JS to render.
    """
    items: dict[str, dict] = {}

    if "pi_health" in raw:
        items["pi_health"] = map_pi_health(raw["pi_health"])
    if "connection" in raw:
        items["connection"] = map_connection(raw["connection"])
    if "update" in raw:
        u = raw["update"] or {}
        items["update"] = map_update(u.get("checked_at", 0.0) or 0.0,
                                     bool(u.get("available")), u.get("latest", ""))
    if "forager" in raw:
        items["forager"] = map_forager(raw["forager"])
    if "remote_access" in raw:
        items["remote_access"] = map_remote_access(raw["remote_access"])
    if "main_server" in raw:
        items["main_server"] = map_main_server(raw["main_server"])
    for key, label in (("grocy", "Grocy"), ("mealie", "Mealie"),
                       ("home_assistant", "Home Assistant")):
        if key in raw:
            svc = raw[key] or {}
            items[key] = map_service(key, label, bool(svc.get("configured")),
                                     bool(svc.get("ok")))

    return {"ok": True, "items": items}
