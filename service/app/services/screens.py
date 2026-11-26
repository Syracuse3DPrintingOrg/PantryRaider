"""Screens: every display that can show Pantry Raider, and events for one.

A screen is the main install's own kitchen display (id "server"), a Bandit's
display (id is the Bandit's device id), or a foreign screen that signed up
with a screen key: a tablet running a kiosk app, a wall panel, a plain
browser. Home Assistant reads the list to offer "which screen shows this",
and anything that sends an on-screen event can name one screen so only that
one moves.

Every screen pulls its own events (GET /api/screens/poll), a Bandit included,
so the sender never needs a screen's address and nothing has to reach into a
device. The exception is the main install's own display, whose events go into
the local ring marked for the display (services/ha_events.add_for_display).

What is kept where:

- Foreign screens: data_dir/screens.json, the usual small state file. Each
  belongs to the key that registered it, and that key can touch no other.
- Bandits: the device registry (services/devices.py). A Bandit can be
  targeted once it reports the "screen1" capability, which says it pulls.
- Pending events and sign-in tickets: memory only. An event lives two minutes
  and a ticket one, so neither is worth a disk write, and after a restart a
  screen sees its cursor ahead of the server's and starts over.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .state_lock import state_write_lock
from .state_writer import StateWriter

SERVER_ID = "server"
SCREEN_CAP = "screen1"
ID_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
# Pages a caller can name without knowing a screen's own addresses.
# "app" is the whole interface, starting at the start page: what a kitchen
# display always shows, and what a tablet switches into on request.
# "cook" is cook mode: the recipe one step at a time, large.
PAGES = ("home", "recipe", "timers", "app", "cook")
APP_PATH = "ui/start?kiosk=1"
_KIOSK_PATHS = {"home": "ui/", "recipe": "ui/current-recipe", "timers": "ui/timers",
                "app": "ui/start", "cook": "ui/current-recipe?cook=1"}
_FOREIGN_PATHS = {"home": "screen/", "recipe": "screen/recipe", "timers": "screen/timers",
                  "app": APP_PATH, "cook": "screen/cook"}

MAX_SCREENS = 64
ONLINE_SECONDS = 90.0
_EVENT_TTL = 120.0
_MAX_EVENTS = 50
TICKET_TTL = 60.0
_SEEN_FLUSH_SECONDS = 300.0

_lock = threading.Lock()
_screens: dict[str, dict] = {}
_mtime: int | None = None
_writer = StateWriter("screens")
_last_flush = 0.0

# Pending events per screen id, one id counter across all of them.
_queues: dict[str, list[dict]] = {}
_next_event = 1
# Monotonic time of each screen's last poll (the heartbeat).
_seen: dict[str, float] = {}
# The page each screen said it was on with its last poll: (monotonic time,
# page). Memory only; it is about what the tablet shows this minute.
_pages: dict[str, tuple[float, str]] = {}
PAGE_SECONDS = 90.0
# sha256(ticket) -> (screen id, monotonic expiry, opens the whole interface)
_tickets: dict[str, tuple[str, float, bool]] = {}


def _state_file() -> Path:
    from ..config import settings
    return Path(settings.data_dir) / "screens.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_locked() -> None:
    global _screens, _mtime
    if _writer.busy():
        return
    try:
        sf = _state_file()
        mtime = sf.stat().st_mtime_ns
    except OSError:
        return
    if mtime == _mtime:
        return
    try:
        data = json.loads(sf.read_text())
        rows = {}
        for raw in data.get("screens", []):
            sid = str(raw.get("id", ""))
            if ID_RE.match(sid) and sid != SERVER_ID:
                rows[sid] = {
                    "id": sid,
                    "label": str(raw.get("label", ""))[:60],
                    "kind": str(raw.get("kind", "browser"))[:32] or "browser",
                    "owner": str(raw.get("owner", "")),
                    "width": int(raw.get("width", 0) or 0),
                    "height": int(raw.get("height", 0) or 0),
                    "created": str(raw.get("created", "")),
                    "last_seen": str(raw.get("last_seen", "")) or None,
                }
                if isinstance(raw.get("wall"), bool):
                    rows[sid]["wall"] = raw["wall"]
    except (OSError, ValueError, TypeError, AttributeError):
        return
    _mtime = mtime
    _screens = rows


def _save_locked() -> None:
    global _last_flush
    try:
        blob = {"screens": [_screens[k] for k in sorted(_screens)]}
        _writer.write(_state_file(), json.dumps(blob).encode("utf-8"), _written)
        _last_flush = time.monotonic()
    except OSError:
        pass


def _written(mtime: int | None) -> None:
    global _mtime
    if mtime is not None:
        _mtime = mtime


# --- The list ----------------------------------------------------------------


def _clean_label(value, fallback: str) -> str:
    text = " ".join(str(value or "").split())
    text = "".join(ch for ch in text if ch.isprintable())[:60]
    return text or fallback


def _clean_kind(value) -> str:
    text = re.sub(r"[^a-z0-9_]", "", str(value or "").strip().lower())[:32]
    if text in ("", SERVER_ID, "bandit"):
        return "browser"
    return text


def _is_online(sid: str) -> bool:
    seen = _seen.get(sid)
    return seen is not None and time.monotonic() - seen <= ONLINE_SECONDS


def _server_screen() -> dict:
    from . import ha_events
    from ..config import settings
    label = _clean_label(getattr(settings, "kitchen_name", ""), "") or "Main screen"
    return {"id": SERVER_ID, "label": label, "kind": "server",
            "online": ha_events.display_online(), "last_seen": None,
            "targetable": True}


def _bandit_screens() -> list[dict]:
    try:
        from . import devices
        rows = devices.list_devices()
    except Exception:
        return []
    out = []
    for d in rows:
        sid = str(d.get("device_id") or "")
        if not sid or sid == SERVER_ID:
            continue
        pulls = SCREEN_CAP in (d.get("caps") or [])
        out.append({
            "id": sid,
            "label": _clean_label(d.get("label") or d.get("hostname"), "Bandit"),
            "kind": "bandit",
            # A Bandit that pulls its events is here when its pull is; one on
            # an older build only has its config pull to go by.
            "online": _is_online(sid) if pulls else bool(d.get("online")),
            "last_seen": d.get("last_seen"),
            "targetable": pulls,
        })
    return out


def _foreign(row: dict) -> dict:
    return {"id": row["id"], "label": row["label"], "kind": row["kind"],
            "online": _is_online(row["id"]), "last_seen": row.get("last_seen"),
            "targetable": True, "width": row.get("width", 0),
            "height": row.get("height", 0)}


def list_screens(owner: str | None = None) -> list[dict]:
    """Every screen, or only the foreign screens one key registered."""
    with _lock:
        _load_locked()
        rows = [dict(r) for r in _screens.values()]
    foreign = sorted((_foreign(r) for r in rows
                      if owner is None or r.get("owner") == owner),
                     key=lambda s: s["label"].lower())
    if owner is not None:
        return foreign
    from ..config import settings
    if settings.is_satellite():
        return foreign
    return [_server_screen()] + sorted(_bandit_screens(),
                                       key=lambda s: s["label"].lower()) + foreign


def get(screen_id: str) -> dict | None:
    """One screen by id, or None."""
    sid = str(screen_id or "")
    for s in list_screens():
        if s["id"] == sid:
            return s
    return None


def owner_of(screen_id: str) -> str | None:
    """The key that registered a foreign screen, or None for any other id."""
    with _lock:
        _load_locked()
        row = _screens.get(str(screen_id or ""))
        return row.get("owner") if row else None


class ScreenError(Exception):
    """A register call that cannot be carried out; ``str()`` is for the caller."""


def register(owner: str, screen_id: str, label: str = "", kind: str = "",
             width: int = 0, height: int = 0) -> dict:
    """Add or update a foreign screen for the key ``owner``."""
    sid = str(screen_id or "").strip().lower()
    if not ID_RE.match(sid) or sid == SERVER_ID:
        raise ScreenError("screen_id must be 1 to 40 of a-z, 0-9, - and _.")
    try:
        w, h = max(0, min(int(width or 0), 20000)), max(0, min(int(height or 0), 20000))
    except (TypeError, ValueError):
        w = h = 0
    for b in _bandit_screens():
        if b["id"] == sid:
            raise ScreenError("That id belongs to another screen. Choose a different one.")
    with _lock, state_write_lock(_state_file()):
        _load_locked()
        row = _screens.get(sid)
        if row is not None and row.get("owner") != owner:
            raise ScreenError("That id belongs to another screen. Choose a different one.")
        if row is None:
            if len(_screens) >= MAX_SCREENS:
                raise ScreenError("There are too many screens. Remove one in Settings first.")
            row = {"id": sid, "owner": owner, "created": _now_iso(), "last_seen": None,
                   "label": "", "kind": "browser", "width": 0, "height": 0}
            _screens[sid] = row
        row["label"] = _clean_label(label, row.get("label") or sid)
        if kind or not row.get("kind"):
            row["kind"] = _clean_kind(kind)
        if w and h:
            row["width"], row["height"] = w, h
        row["last_seen"] = _now_iso()
        _seen[sid] = time.monotonic()
        _save_locked()
        return _foreign(dict(row))


def set_label(screen_id: str, label: str) -> bool:
    with _lock, state_write_lock(_state_file()):
        _load_locked()
        row = _screens.get(str(screen_id or ""))
        if row is None:
            return False
        row["label"] = _clean_label(label, row["id"])
        _save_locked()
        return True


def forget(screen_id: str) -> bool:
    sid = str(screen_id or "")
    with _lock, state_write_lock(_state_file()):
        _load_locked()
        if _screens.pop(sid, None) is None:
            return False
        _queues.pop(sid, None)
        _seen.pop(sid, None)
        _pages.pop(sid, None)
        _save_locked()
        return True


def forget_owned_by(owner: str) -> int:
    """Drop the screens a key registered (the key was turned off)."""
    with _lock, state_write_lock(_state_file()):
        _load_locked()
        gone = [sid for sid, r in _screens.items() if r.get("owner") == owner]
        for sid in gone:
            del _screens[sid]
            _queues.pop(sid, None)
            _seen.pop(sid, None)
        if gone:
            _save_locked()
        return len(gone)


# --- The wall tablet profile -------------------------------------------------
# A tablet on a wall shows the whole interface with bigger type, fewer menu
# entries and fewer buttons (static/css/wall.css). It is kept per screen, as
# "wall" on the screen's row once someone chooses in Settings; until then a
# kiosk app's screen has it on and every other kind has it off.

WALL_DEFAULT_KINDS = frozenset({"kiosk_satellite"})


def wall_default(kind) -> bool:
    """Whether a screen of this kind gets the profile before anyone chooses."""
    return str(kind or "") in WALL_DEFAULT_KINDS


def wall_profile(screen_id) -> bool:
    """Whether this foreign screen shows the wall tablet profile."""
    with _lock:
        _load_locked()
        row = _screens.get(str(screen_id or ""))
        if row is None:
            return False
        chosen = row.get("wall")
        return chosen if isinstance(chosen, bool) else wall_default(row.get("kind"))


def set_wall_profile(screen_id, on) -> bool:
    """Turn the profile on or off for one foreign screen; None goes back to
    the default for its kind. False when there is no such screen."""
    with _lock, state_write_lock(_state_file()):
        _load_locked()
        row = _screens.get(str(screen_id or ""))
        if row is None:
            return False
        if on is None:
            row.pop("wall", None)
        else:
            row["wall"] = bool(on)
        _save_locked()
        return True


# --- Events for one screen ---------------------------------------------------


def page_path(kind: str, page: str) -> str:
    """The address of a named page on a screen of this kind, or ""."""
    table = _KIOSK_PATHS if kind in ("server", "bandit") else _FOREIGN_PATHS
    return table.get(str(page or "").strip().lower(), "")


def _summary(screen: dict) -> dict:
    return {"id": screen["id"], "label": screen["label"], "online": screen["online"]}


def send(screen_id: str, event: dict) -> dict | None:
    """Queue ``event`` for one screen. Returns {"id", "label", "online"} for
    the screen, or None when there is no such screen or it cannot be sent to
    (a Bandit on a build that does not pull)."""
    global _next_event
    screen = get(screen_id)
    if screen is None or not screen.get("targetable"):
        return None
    clean = {k: v for k, v in dict(event).items() if k not in ("id", "ts", "display")}
    if screen["id"] == SERVER_ID:
        from . import ha_events
        ha_events.add_for_display(clean)
        return _summary(screen)
    now = time.time()
    with _lock:
        clean["id"] = _next_event
        clean["ts"] = now
        _next_event += 1
        queue = _queues.setdefault(screen["id"], [])
        queue.append(clean)
        cutoff = now - _EVENT_TTL
        queue[:] = [e for e in queue if e["ts"] >= cutoff][-_MAX_EVENTS:]
    return _summary(screen)


def send_navigate(screen_id: str, *, page: str = "", path: str = "") -> dict | None:
    """Move one screen to a named page, or to an app path it is given as is."""
    screen = get(screen_id)
    if screen is None:
        return None
    target = page_path(screen["kind"], page) if page else str(path or "")
    if not target:
        return None
    event = {"type": "navigate", "path": target, "always": True}
    if page:
        event["page"] = page
    return send(screen_id, event)


def send_close(screen_id: str) -> dict | None:
    """Take one screen back to where it rests. A foreign screen is told to
    close (its app drops the page; a plain browser goes to its home page); a
    kitchen display, which has nothing underneath, goes to its home page."""
    screen = get(screen_id)
    if screen is None:
        return None
    if screen["kind"] in ("server", "bandit"):
        return send_navigate(screen_id, page="home")
    return send(screen_id, {"type": "close", "path": page_path(screen["kind"], "home")})


def nudge_timers(_event=None) -> None:
    """Tell every foreign screen the timer list changed, so one that draws
    its own timer bar reads the list again without a poll loop of its own."""
    with _lock:
        _load_locked()
        ids = list(_screens)
    for sid in ids:
        with _lock:
            queue = _queues.get(sid) or []
            if queue and queue[-1].get("type") == "timers" and time.time() - queue[-1]["ts"] < 0.2:
                continue
        send(sid, {"type": "timers"})


def _touch(screen_id: str) -> None:
    """A poll is the heartbeat. last_seen reaches the disk every few minutes
    at most: it is only there so the list still has it after a restart."""
    _seen[screen_id] = time.monotonic()
    with _lock:
        row = _screens.get(screen_id)
        if row is None:
            return
        row["last_seen"] = _now_iso()
        due = time.monotonic() - _last_flush >= _SEEN_FLUSH_SECONDS
    if due:
        with _lock, state_write_lock(_state_file()):
            _save_locked()


def poll(screen_id: str, after_id: int = 0) -> dict:
    """Events for one screen newer than ``after_id``, plus the last id."""
    sid = str(screen_id or "")
    _touch(sid)
    try:
        after = int(after_id)
    except (TypeError, ValueError):
        after = 0
    cutoff = time.time() - _EVENT_TTL
    with _lock:
        last = _next_event - 1
        queue = _queues.get(sid) or []
        fresh = [dict(e) for e in queue if e["id"] > after and e["ts"] >= cutoff]
    return {"events": fresh, "last_id": last}


async def wait_for_events(screen_id: str, after_id: int = 0, timeout: float = 25.0,
                          is_gone=None) -> dict:
    """Long poll: answers the moment this screen has an event, when the
    caller's cursor is ahead of the server's (a restart), or at the timeout."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    next_gone = 0.0
    try:
        after = int(after_id)
    except (TypeError, ValueError):
        after = 0
    while True:
        result = poll(screen_id, after)
        now = time.monotonic()
        behind = after < 999999999 and result["last_id"] < after
        if result["events"] or behind or now >= deadline:
            return result
        if is_gone is not None and now >= next_gone:
            next_gone = now + 0.5
            try:
                if await is_gone():
                    return result
            except Exception:
                is_gone = None
        await asyncio.sleep(0.05)


# --- Signing a screen's web view in -----------------------------------------


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_ticket(screen_id: str, app: bool = False) -> str:
    """A one-use ticket that opens a page session for this screen for the
    next minute. The key itself never goes in an address. ``app`` makes it a
    session for the whole interface, not only the screen pages."""
    token = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _lock:
        for k in [k for k, v in _tickets.items() if v[1] < now]:
            del _tickets[k]
        if len(_tickets) >= 256:
            _tickets.clear()
        _tickets[_hash(token)] = (str(screen_id), now + TICKET_TTL, bool(app))
    return token


def redeem_ticket(token: str) -> tuple[str, bool] | None:
    """(screen id, whole interface) for a ticket, once; None for anything
    else."""
    if not token:
        return None
    with _lock:
        hit = _tickets.pop(_hash(str(token)), None)
    if hit is None or hit[1] < time.monotonic():
        return None
    return hit[0], hit[2]


def note_page(screen_id: str, page: str) -> None:
    """A screen's own page says which page it is (with each poll), so a
    request can tell a screen in cook mode from one on the recipe."""
    p = str(page or "").strip().lower()
    if p in PAGES:
        _pages[str(screen_id or "")] = (time.monotonic(), p)


def page_of(screen_id: str) -> str:
    """The page a screen is showing ("cook", "recipe", ...), or "" when it
    has not said lately."""
    hit = _pages.get(str(screen_id or ""))
    if not hit or time.monotonic() - hit[0] > PAGE_SECONDS:
        return ""
    return hit[1]


def reset() -> None:
    """Forget everything and drop the state file (tests)."""
    global _screens, _mtime, _next_event, _last_flush
    _writer.wait_idle()
    with _lock:
        _screens = {}
        _mtime = None
        _queues.clear()
        _seen.clear()
        _pages.clear()
        _tickets.clear()
        _next_event = 1
        _last_flush = 0.0
        try:
            _state_file().unlink(missing_ok=True)
        except OSError:
            pass
