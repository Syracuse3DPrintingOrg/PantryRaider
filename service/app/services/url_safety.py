"""One URL policy for every link the app renders from a saved value.

A custom nav tab's address ends up in an ``href`` on every page, and a kiosk
navigates to whatever the Stream Deck or Home Assistant asks for. Either one
could run code in the page if a ``javascript:`` (or ``data:``, ``vbscript:``)
address got through, so the checks live here once:

- ``safe_href`` for a link the user saved (http, https, or a page in the app);
- ``is_external`` to decide whether that link opens in a new tab;
- ``safe_nav_path`` for a remote "go to this page" request (same-origin only);
- ``clean_custom_tabs`` and ``report_blocked_tabs`` for the custom nav tabs.

The helpers are pure (no settings, no database) apart from the last two, which
import what they need lazily so this module stays cheap to import anywhere.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

log = logging.getLogger("foodassistant.url_safety")

# A scheme prefix the way a browser reads one: a letter, then letters, digits,
# "+", "-" or ".", then a colon.
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
# C0 controls plus space: what a browser strips from both ends of a URL.
_EDGE = "".join(chr(c) for c in range(0x21))
_ALLOWED_SCHEMES = ("http", "https")

BLOCKED_TAB_MESSAGE = (
    "{label} can't be saved: a tab's address has to start with http:// or "
    "https://, or be a page in this app.")


def _strip_url(value) -> str:
    """Tidy a raw value the way a browser's URL parser does before reading
    it: drop every tab, CR and LF, then trim C0 controls and spaces from both
    ends. Without this, "java\\tscript:" would slip past a scheme check that
    the browser would then happily run."""
    s = "" if value is None else str(value)
    s = s.replace("\t", "").replace("\r", "").replace("\n", "")
    return s.strip(_EDGE)


def safe_href(value) -> str | None:
    """The cleaned link when it is safe to put in an ``href``, else None.

    Safe means one of:
    - http or https with a host ("https://ha.local:8123/lovelace");
    - a value with no scheme, which the browser resolves inside the app
      ("ui/x", "/grocy/", "#", "setup#pane", or a scheme-relative "//host/p").

    Anything else with a scheme (javascript:, data:, vbscript:, file:, a bare
    "http:" with no host) is refused, as is a value that still carries a
    control character after the edges are trimmed. An empty value is None.
    """
    s = _strip_url(value)
    if not s:
        return None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in s):
        return None
    if _SCHEME_RE.match(s):
        try:
            parts = urlsplit(s)
        except ValueError:
            return None
        if parts.scheme.lower() not in _ALLOWED_SCHEMES or not parts.netloc:
            return None
    return s


def is_external(href) -> bool:
    """True when a link leaves the app: http(s), or scheme-relative ("//host",
    including the backslash spellings a browser treats the same way). Used to
    open those links in a new tab instead of replacing the kiosk page."""
    s = _strip_url(href)
    if not s:
        return False
    m = _SCHEME_RE.match(s)
    if m:
        return m.group(0)[:-1].lower() in _ALLOWED_SCHEMES
    return s[:2] in ("//", "\\\\", "/\\", "\\/")


def safe_nav_path(path: str) -> str:
    """Reduce a requested navigation target to a safe same-origin relative path.

    The kiosk navigates to whatever HA sends, so an absolute or scheme-bearing
    URL (``http://...``, ``//evil``, ``javascript:``) must never get through.
    Returns a cleaned relative path (leading slashes stripped) or "" when the
    input is empty or not same-origin. Pure, so it is unit-testable.

    The value is read the way the browser will read it: tabs and line breaks
    removed, the ends trimmed, and a backslash counted as a slash. Otherwise
    "/ javascript:..." would lose its slash here and its space in the browser,
    and "/\\/host" would turn into the off-site "//host".
    """
    p = _strip_url(path)
    if not p or "\\" in p or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in p):
        return ""
    low = p.lower()
    if "://" in low or low.startswith(("//", "http:", "https:", "javascript:", "data:")):
        return ""
    p = p.lstrip("/")
    # What is left must start like a page path: no space or control in front
    # (a browser would trim it and then read a scheme), and no scheme, which
    # would show up as a colon in the first path segment.
    if not p or p[0] <= " " or ":" in p.split("/", 1)[0]:
        return ""
    return p


def clean_custom_tabs(rows) -> tuple[list[dict], list[dict]]:
    """Validate posted custom nav tabs into clean stored dicts.

    Returns ``(clean, rejected)``. ``clean`` is the stored shape
    {id, label, icon, url, parent, heading} with stable, de-duplicated ids,
    built by the same normalizer the nav renders with, so an entry with no
    label is dropped just as before. ``rejected`` lists every tab whose address
    is not allowed, as {id, label, message}; its ``id`` is the id the page
    posted, so the editor can mark that exact row. A heading (no address)
    always passes.
    """
    from ..navigation import normalize_custom_tabs

    raw = rows if isinstance(rows, list) else []
    clean: list[dict] = []
    rejected: list[dict] = []
    # normalize_custom_tabs skips non-dicts and label-less rows, so walk the
    # same filter here to pair each normalized tab with the row it came from.
    posted = [r for r in raw if isinstance(r, dict) and str(r.get("label", "")).strip()]
    for row, tab in zip(posted, normalize_custom_tabs(posted)):
        if tab.get("blocked"):
            rid = row.get("id")
            rejected.append({
                "id": str(rid) if isinstance(rid, str) and rid else tab["key"],
                "label": tab["label"],
                "message": BLOCKED_TAB_MESSAGE.format(label=tab["label"]),
            })
            continue
        clean.append({"id": tab["key"], "label": tab["label"], "icon": tab["icon"],
                      "url": tab["href"], "parent": tab.get("parent", ""),
                      "heading": bool(tab.get("heading"))})
    return clean, rejected


def report_blocked_tabs(db) -> int:
    """Tell the owner, once, about each saved custom tab that is now hidden
    because its address could run code.

    Each newly blocked tab gets one action item in the Review inbox; the ids
    already reported are kept in ``settings.nav_blocked_notified`` so a restart
    does not raise the same item again. A tab that is fixed drops off that
    list, so if it is ever broken again it is reported again. Returns how many
    new items were raised. Never raises: it runs at startup, where a missing
    nav config or a read-only data directory must not stop the app.
    """
    try:
        from ..config import settings
        from ..navigation import normalize_custom_tabs
        from . import action_items

        tabs = normalize_custom_tabs(getattr(settings, "custom_nav_tabs", None) or [])
        blocked = [t for t in tabs if t.get("blocked")]
        notified_raw = getattr(settings, "nav_blocked_notified", None) or []
        notified = {str(k) for k in notified_raw} if isinstance(notified_raw, list) else set()
        new = [t for t in blocked if t["key"] not in notified]
        for tab in new:
            action_items.create(
                db, "nav_tab_blocked", "A custom tab was hidden",
                body=(f'The custom tab "{tab["label"]}" is hidden because its '
                      "address could run code instead of opening a page. Open "
                      "Settings, Navigation to give it a web address or a page "
                      "in this app, or remove it."),
                dedupe_key=f"nav_tab_blocked:{tab['key']}",
                level="warning",
                payload={"tab": tab["key"]},
            )
        # Only the tabs still blocked stay on the list, so a fixed tab that is
        # later broken again gets a fresh notice.
        keep = sorted({t["key"] for t in blocked})
        if keep != sorted(notified):
            try:
                settings.save({"nav_blocked_notified": keep})
            except Exception as exc:  # read-only data dir, full disk
                log.warning("could not record reported nav tabs: %s", exc)
                try:
                    settings.nav_blocked_notified = keep
                except Exception:
                    pass
        return len(new)
    except Exception as exc:
        log.warning("could not check custom nav tabs: %s", exc)
        return 0
