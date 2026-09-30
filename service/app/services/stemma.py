"""Plug-in STEMMA QT / Qwiic accessories (FoodAssistant-etsc, -kh1m).

Adafruit STEMMA QT and SparkFun Qwiic are the same thing electrically: 3.3V
I2C on a 4-pin JST-SH connector, which a Pi speaks natively on /dev/i2c-1.
The host gadgets agent owns the bus (gadgets/foodassistant_gadgets/i2c/) and
this module is the app side of it: the registry the agent pulls, the pure
normalization of each kind's options, and the output state the agent's LEDs
follow.

The registry mirrors the hygrometer pattern (settings.stemma_devices, one
entry per device, added from the Gadgets pane), with two deliberate
differences:

* An id is the bus-plus-address form ``i2c:1:0x30`` rather than a MAC. QT
  addresses are strap-pinned, so the same board in the same jumper
  configuration keeps the same id across replugs and reboots.
* The list is device-local and never relayed upstream. A BLE sensor can be
  heard by whichever radio is nearest and so is managed on the server; a QT
  board is physically plugged into ONE device, so its registry belongs to
  that device.

The NeoKey 1x4 (FoodAssistant-kh1m, closing FoodAssistant-65gh) is the first
inhabitant: four backlit keys map to the four scanner modes, a press POSTs
the existing scanner-mode API, and the lit key follows the active mode no
matter which surface changed it.
"""
from __future__ import annotations

import re
import time

from .scanner_mode import MODE_LABELS, SCANNER_MODES
from .ttl_cache import TTLCache

# Device families this app side knows. Phase 1 drives the NeoKey; the agent
# reports anything else it finds as discovered-but-unsupported, so the list
# grows here as drivers land (presence, temperature, encoder, NeoPixel).
STEMMA_KINDS: tuple[str, ...] = ("neokey",)

# Friendly names for the cards and the discovered list.
KIND_LABELS = {
    "neokey": "NeoKey 1x4",
}

# How many keys a NeoKey 1x4 has, which is also the length of its keymap.
NEOKEY_KEYS = 4

# A keymap slot that does nothing. The dropdowns offer it so a key can be
# left dark rather than forced onto a mode.
KEYMAP_NONE = ""

# A keymap slot holds one ACTION STRING (FoodAssistant-bo9v). Three forms:
#
#   "<mode>"        a scanner mode, exactly as before ("inventory", "consume",
#                   "shopping", "audit"): the press selects that mode.
#   "nav:<page>"    the press sends the kiosk to a page (NAV_PAGES below).
#   "timer:<op>"    the press runs one timer operation (TIMER_KEY_ACTIONS).
#
# Bare mode strings stay valid unprefixed so every keymap saved before actions
# existed keeps meaning exactly what it meant. The agent mirrors the two new
# tables (gadgets/foodassistant_gadgets/i2c/drivers/neokey.py) so both sides
# normalize a pulled keymap identically; tests/test_stemma.py pins them.
NAV_ACTION_PREFIX = "nav:"
TIMER_ACTION_PREFIX = "timer:"

# The pages a key can jump the kiosk to: page key -> (label, kiosk path).
# Drawn from the navigation registry (app/navigation.py NAV_TABS) and pinned
# to it by tests, so a moved page cannot leave a key pointing at a dead path.
# Kept as its own table (not computed from NAV_TABS) because the agent mirrors
# the KEY SET and a mirror needs a stable, hand-readable list.
NAV_PAGES: dict[str, dict] = {
    "start":     {"label": "Glance",        "path": "ui/start"},
    "inventory": {"label": "Inventory",     "path": "ui/inventory"},
    "expiring":  {"label": "Expiring",      "path": "ui/expiring"},
    "add":       {"label": "Manage",        "path": "ui/add"},
    "pending":   {"label": "Review",        "path": "ui/pending"},
    "cook":      {"label": "Cook",          "path": "ui/cook"},
    "recipes":   {"label": "Recipes",       "path": "ui/recipes"},
    "mealplan":  {"label": "Meal Plan",     "path": "ui/mealplan"},
    "shopping":  {"label": "Shopping",      "path": "ui/shopping"},
    "guide":     {"label": "Kitchen Guide", "path": "ui/kitchen-guide"},
    "timers":    {"label": "Timers",        "path": "ui/timers"},
    "weather":   {"label": "Weather",       "path": "ui/weather"},
    "camera":    {"label": "Cameras",       "path": "ui/camera"},
}

# The one-shot timer operations a key can fire: op -> label. Chosen for a
# key with no screen: each one is safe to hit blind and shows its effect on
# the pad itself (the bar grows, the strobe stops, a bar appears).
TIMER_KEY_ACTIONS: dict[str, str] = {
    "start5":  "Start a 5 minute timer",
    "add5":    "Add 5 minutes to the next timer",
    "dismiss": "Dismiss a finished timer",
}

# How many seconds the start5/add5 operations move (5 minutes).
TIMER_ACTION_SECONDS = 300

# Default brightness for a NeoKey's LEDs, as a percentage. Bright enough to
# read across a kitchen, dim enough not to glare at 6am.
NEOKEY_DEFAULT_BRIGHTNESS = 40

# One fixed color per scanner mode, as (r, g, b). Fixed by the app rather
# than user-picked so every surface that wants a mode color uses the same
# one; the agent has a matching fallback table for when the app is briefly
# unreachable (tests/test_stemma.py asserts the two agree).
MODE_COLORS: dict[str, tuple[int, int, int]] = {
    "inventory": (0, 200, 83),    # green: stocking things up
    "consume": (255, 145, 0),     # amber: using things up
    "shopping": (0, 145, 255),    # blue: building the list
    "audit": (170, 0, 255),       # purple: counting, changing nothing
}

# What an unlit-but-mapped key shows: its mode color at this fraction of the
# brightness, so all four keys read as labelled while only the active one is
# obviously on.
IDLE_LED_SCALE = 0.12

# A device with no heartbeat for this long reads as unplugged in the UI. The
# agent pushes every few seconds, so this is generous.
STEMMA_STALE_SECONDS = 90

# The id shape: bus number plus 7-bit address, both as the agent writes them.
_ID_RE = re.compile(r"^i2c:(\d+):0x([0-9a-f]{2})$")


def device_id(bus: int, address: int) -> str:
    """The stable id for a device at one address on one bus."""
    return f"i2c:{int(bus)}:0x{int(address):02x}"


def parse_device_id(value) -> tuple[int, int] | None:
    """(bus, address) from a device id, or None when it is not one of ours."""
    m = _ID_RE.match(str(value or "").strip().lower())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2), 16)


def norm_id(value) -> str:
    """Normalize an id for comparison. Lowercase, unlike the BLE classes: an
    I2C id is not a MAC, and 0x30 reads better than 0X30."""
    return str(value or "").strip().lower()


def kind_label(kind: str) -> str:
    return KIND_LABELS.get(str(kind or ""), str(kind or "").title() or "Accessory")


# --------------------------------------------------------------------------
# Options normalization (pure)
# --------------------------------------------------------------------------

def default_keymap() -> list[str]:
    """The out-of-the-box key order: the scanner modes as SCANNER_MODES lists
    them, left to right, so key 1 is Stock and key 4 is Audit."""
    return list(SCANNER_MODES[:NEOKEY_KEYS])


def normalize_key_action(value) -> str:
    """One keymap slot cleaned to a valid action string, or "" for junk.

    A bare scanner mode passes unchanged (the original vocabulary, so every
    saved keymap keeps working). "nav:<page>" passes when the page is in
    NAV_PAGES and "timer:<op>" when the op is in TIMER_KEY_ACTIONS; anything
    else becomes "" (the key does nothing) rather than silently landing on a
    real action: a typo must never make a key consume stock or jump a screen.
    The agent applies the same rule (neokey.normalize_key_action) so a pulled
    keymap means the same thing on both sides."""
    action = str(value or "").strip().lower()
    if action in SCANNER_MODES:
        return action
    if (action.startswith(NAV_ACTION_PREFIX)
            and action[len(NAV_ACTION_PREFIX):] in NAV_PAGES):
        return action
    if (action.startswith(TIMER_ACTION_PREFIX)
            and action[len(TIMER_ACTION_PREFIX):] in TIMER_KEY_ACTIONS):
        return action
    return KEYMAP_NONE


def normalize_keymap(raw) -> list[str]:
    """A stored keymap cleaned into exactly NEOKEY_KEYS entries.

    Each slot is an action string (see normalize_key_action): a scanner mode,
    a "nav:<page>" jump, or a "timer:<op>" operation. Unknown values and junk
    become "" (the key does nothing). A missing or unusable list falls back to
    the default order, so a NeoKey added with no options still works."""
    if not isinstance(raw, (list, tuple)):
        return default_keymap()
    out: list[str] = []
    for i in range(NEOKEY_KEYS):
        value = raw[i] if i < len(raw) else None
        out.append(normalize_key_action(value))
    return out


def is_mode_action(action) -> bool:
    """Whether an action string is a plain scanner mode (the legacy form)."""
    return str(action or "").strip().lower() in SCANNER_MODES


def nav_action_path(action) -> str:
    """The kiosk path a "nav:<page>" action opens, or "" for anything else."""
    action = normalize_key_action(action)
    if action.startswith(NAV_ACTION_PREFIX):
        return NAV_PAGES[action[len(NAV_ACTION_PREFIX):]]["path"]
    return ""


def key_action_label(action) -> str:
    """The human label for one keymap slot, for the settings card.

    "Nothing" for an empty slot, the short mode label for a mode, the page
    name for a nav action, and the operation label for a timer action."""
    action = normalize_key_action(action)
    if not action:
        return "Nothing"
    if action in SCANNER_MODES:
        return MODE_LABELS.get(action, action.title())
    if action.startswith(NAV_ACTION_PREFIX):
        return NAV_PAGES[action[len(NAV_ACTION_PREFIX):]]["label"]
    return TIMER_KEY_ACTIONS[action[len(TIMER_ACTION_PREFIX):]]


def normalize_brightness(raw, default: int = NEOKEY_DEFAULT_BRIGHTNESS) -> int:
    """LED brightness as a 0-100 percent. 0 is a legitimate choice (a NeoKey
    used as a dark keypad), so it is kept rather than treated as unset."""
    try:
        value = int(round(float(raw)))
    except (TypeError, ValueError):
        return default
    return max(0, min(100, value))


def normalize_options(kind: str, raw) -> dict:
    """The per-kind options block, cleaned. Unknown kinds keep nothing: an
    option only means something to the driver that reads it."""
    opts = raw if isinstance(raw, dict) else {}
    if kind == "neokey":
        return {
            "keymap": normalize_keymap(opts.get("keymap")),
            "brightness": normalize_brightness(opts.get("brightness")),
        }
    return {}


def normalize_device(raw) -> dict | None:
    """One registry entry, cleaned. None when it has no usable id."""
    if not isinstance(raw, dict):
        return None
    dev_id = norm_id(raw.get("id"))
    if not parse_device_id(dev_id):
        return None
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in STEMMA_KINDS:
        return None
    return {
        "id": dev_id,
        "kind": kind,
        "name": str(raw.get("name") or "").strip()[:60],
        "options": normalize_options(kind, raw.get("options")),
    }


def configured_devices() -> list[dict]:
    """The sanitized stemma_devices list from settings."""
    from ..config import settings
    out = []
    for dev in settings.stemma_devices or []:
        entry = normalize_device(dev)
        if entry:
            out.append(entry)
    return out


def find_device(dev_id: str, devices: list[dict] | None = None) -> dict | None:
    key = norm_id(dev_id)
    for dev in (configured_devices() if devices is None else devices):
        if norm_id(dev.get("id")) == key:
            return dev
    return None


# --------------------------------------------------------------------------
# Key to action (pure)
# --------------------------------------------------------------------------

def mode_for_key(keymap, index: int) -> str:
    """The action string key ``index`` (0-based) fires, or "" for none.
    Historically named for the mode-only days; the name stays because the
    common case is still a scanner mode."""
    keys = normalize_keymap(keymap)
    if 0 <= index < len(keys):
        return keys[index]
    return KEYMAP_NONE


def keymap_choices() -> list[dict]:
    """The dropdown options for the mapping editor, grouped.

    Each entry is {value, label, group}: the do-nothing choice first (no
    group), then the scanner modes, the pages a key can open, and the timer
    operations. The Accessories pane renders one optgroup per group name, so
    a new action added here reaches the editor without a JS change."""
    out = [{"value": KEYMAP_NONE, "label": "Nothing", "group": ""}]
    out += [{"value": m, "label": MODE_LABELS.get(m, m.title()),
             "group": "Scanner modes"} for m in SCANNER_MODES]
    out += [{"value": NAV_ACTION_PREFIX + key, "label": page["label"],
             "group": "Open a page"} for key, page in NAV_PAGES.items()]
    out += [{"value": TIMER_ACTION_PREFIX + op, "label": label,
             "group": "Timers"} for op, label in TIMER_KEY_ACTIONS.items()]
    return out


def timer_key_plan(action, timers_list) -> dict:
    """What a "timer:<op>" press should do, given the timers as they stand.

    Pure planning so the truth table tests without the registry: the router
    executes the returned plan against services/timers.py. Shapes:

      {"op": "create", "label": ..., "seconds": ..., "message": ...}
      {"op": "extend", "id": ..., "seconds": ..., "message": ...}
      {"op": "cancel", "ids": [...], "message": ...}
      {"op": "none", "message": ...}

    A press with nothing to act on plans "none" with a plain-words message
    rather than an error: a key hit blind must never scold the person for the
    timers not being in the state the key wanted."""
    op = normalize_key_action(action)
    op = op[len(TIMER_ACTION_PREFIX):] if op.startswith(TIMER_ACTION_PREFIX) else ""
    rows = [t for t in (timers_list or []) if isinstance(t, dict)]
    if op == "start5":
        return {"op": "create", "label": "Quick timer",
                "seconds": TIMER_ACTION_SECONDS,
                "message": "Started a 5 minute timer."}
    if op == "add5":
        running = [t for t in rows
                   if not t.get("expired") and t.get("remaining_seconds") is not None]
        if not running:
            return {"op": "none", "message": "No timer is running."}
        soonest = min(running, key=lambda t: float(t["remaining_seconds"]))
        label = str(soonest.get("label") or "the timer")
        return {"op": "extend", "id": soonest.get("id"),
                "seconds": TIMER_ACTION_SECONDS,
                "message": f"Added 5 minutes to {label}."}
    if op == "dismiss":
        finished = [t.get("id") for t in rows if t.get("expired")]
        if not finished:
            return {"op": "none", "message": "No timer is finished."}
        if len(finished) == 1:
            message = "Timer dismissed."
        else:
            message = f"Dismissed {len(finished)} finished timers."
        return {"op": "cancel", "ids": finished, "message": message}
    return {"op": "none", "message": "That key is not set to a timer action."}


# --------------------------------------------------------------------------
# LED derivation (pure)
# --------------------------------------------------------------------------

# What a key mapped to a page or timer action wears: a white glow, dimmed by
# IDLE_LED_SCALE like an inactive mode key, so the key is visibly mapped
# without joining the mode-color conversation (an action is momentary; it is
# never "active" the way a mode is). The agent keeps a matching fallback
# (drivers/neokey.py ACTION_GLOW); tests assert the two agree.
ACTION_GLOW: tuple[int, int, int] = (255, 255, 255)


def mode_color(mode: str) -> tuple[int, int, int]:
    """The fixed color for a scanner mode, the glow for a page or timer
    action, black for anything unmapped."""
    action = normalize_key_action(mode)
    if action and action not in SCANNER_MODES:
        return ACTION_GLOW
    return MODE_COLORS.get(action, (0, 0, 0))


def scale_color(color: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    """A color dimmed by factor (0..1), rounded to bytes."""
    f = max(0.0, min(1.0, float(factor)))
    return tuple(max(0, min(255, int(round(c * f)))) for c in color)  # type: ignore[return-value]


def led_colors(keymap, active_mode: str,
               brightness: int = NEOKEY_DEFAULT_BRIGHTNESS) -> list[tuple[int, int, int]]:
    """What each of a NeoKey's four LEDs should show.

    The key mapped to the active mode burns at full brightness in that mode's
    color; the other mapped keys glow faintly in their own colors so the
    layout stays readable in a dark kitchen; a key mapped to nothing stays
    off. A key mapped to a page or timer action wears the dim white
    ACTION_GLOW: an action is momentary, so it is never the bright "active"
    key, but a mapped key must still be findable in the dark. Two keys mapped
    to the same mode both light: the user asked for that, and lying about it
    would be worse than a duplicate.
    """
    keys = normalize_keymap(keymap)
    level = normalize_brightness(brightness) / 100.0
    active = str(active_mode or "").strip().lower()
    out: list[tuple[int, int, int]] = []
    for mode in keys:
        if not mode:
            out.append((0, 0, 0))
            continue
        base = mode_color(mode)
        factor = level if mode == active else level * IDLE_LED_SCALE
        out.append(scale_color(base, factor))
    return out


# --------------------------------------------------------------------------
# The outputs snapshot (pure builder + the endpoint's data)
# --------------------------------------------------------------------------

def request_key_test(device_id: str, key: int) -> dict:
    """Queue a Test click for the agent (see gadgets.set_stemma_key_test)."""
    from . import gadgets
    return gadgets.set_stemma_key_test(device_id, key)


BRAND_PINK: tuple[int, int, int] = (242, 0, 110)   # #F2006E, the raccoon pink
ALARM_RED: tuple[int, int, int] = (255, 0, 0)      # what a timer alarm looks like

# Is anyone actually standing at the Manage screen right now? That screen is
# the only surface asking the kiosk poll for scanner_mode, so its own poll
# doubles as the heartbeat and nothing new goes on the wire (the Cub registry
# earns its heartbeat the same way).
#
# Deliberately in-process and unpersisted, unlike the timer and scanner state:
# this describes a person in front of a panel this second, which is not worth
# an SD-card write every few seconds and means nothing after a restart. A
# worker that has not seen the heartbeat simply shows the resting pink, so the
# degraded answer is the calm default rather than a wrong color.
_MANAGE_FRESH_FOR = 12.0        # the page beats every 5s; two misses forgive
_manage_seen: dict = {"at": 0.0}


def manage_is_fresh(seen_at: float, now: float,
                    within: float = _MANAGE_FRESH_FOR) -> bool:
    """Whether a heartbeat at ``seen_at`` still counts as someone being there.

    Pure. A stamp from the future (a clock step) counts as fresh rather than
    locking the keys pink until the clock catches up.
    """
    if not seen_at:
        return False
    return (now - seen_at) < within


def note_manage_open(now: float | None = None) -> None:
    """Record that the Manage screen just polled, so the colors come in."""
    _manage_seen["at"] = time.time() if now is None else now


def manage_open(now: float | None = None) -> bool:
    """Whether the Manage screen is live enough to wear the mode colors."""
    return manage_is_fresh(_manage_seen["at"],
                           time.time() if now is None else now)


def parse_hex_color(value, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    """A "#rrggbb" setting as an RGB triple, or the fallback. Pure.

    Anything unparseable falls back rather than raising: a typo in a color box
    should leave the keys looking ordinary, never stop them lighting at all.
    """
    text = str(value or "").strip().lstrip("#")
    if len(text) == 3:                       # the "#f0a" shorthand
        text = "".join(c * 2 for c in text)
    if len(text) != 6:
        return fallback
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return fallback


def rest_color() -> tuple[int, int, int]:
    """The configured resting color for the keys."""
    from ..config import settings
    return parse_hex_color(getattr(settings, "neokey_rest_color", None),
                           BRAND_PINK)


def timer_color() -> tuple[int, int, int]:
    """The configured color for the timer bar and its finished strobe."""
    from ..config import settings
    return parse_hex_color(getattr(settings, "neokey_timer_color", None),
                           ALARM_RED)


def led_palette(on_manage: bool,
                resting: tuple[int, int, int] | None = None
                ) -> dict[str, tuple[int, int, int]]:
    """The colors the keys wear. Pure.

    The per-mode colors mean something only next to the Manage screen's tiles,
    which are painted to match them. Away from that screen there is nothing to
    match, and four differently colored keys just read as noise on the counter,
    so the pad rests in brand pink and the active key simply sits brighter than
    its neighbours. Walking up to Manage is what brings the colors in.

    The "action" entry is what a key mapped to a page or timer action wears
    (always the same dim-scaled glow, on Manage or off it): those keys are not
    part of the mode conversation, so they keep their own quiet color rather
    than joining the pink. It rides the outputs payload like the mode colors,
    so the agent renders the app's choice instead of its own copy drifting.
    """
    if on_manage:
        palette = dict(MODE_COLORS)
    else:
        palette = {mode: (resting or BRAND_PINK) for mode in SCANNER_MODES}
    palette["action"] = ACTION_GLOW
    return palette


def build_outputs(mode_state: dict, alarms: list, timers: list,
                  now: float | None = None, key_test: dict | None = None,
                  on_manage: bool = False,
                  resting: tuple[int, int, int] | None = None,
                  timer_rgb: tuple[int, int, int] | None = None) -> dict:
    """The GET /gadgets/outputs payload, from state already in hand.

    Everything an output device needs to render itself, in one small object:
    the scanner mode (which key a NeoKey lights), whether an alarm is up (a
    NeoPixel goes red), and the timer picture (ringing, and how long the
    soonest one has left, for a pulse). Pure so the shape is testable without
    a state file, a registry, or a clock.
    """
    now = time.time() if now is None else now
    mode = str((mode_state or {}).get("mode") or "").strip().lower()
    if mode not in SCANNER_MODES:
        mode = SCANNER_MODES[0]
    running: list[tuple[float, float]] = []      # (remaining, whole duration)
    ringing = False
    for t in timers or []:
        if not isinstance(t, dict):
            continue
        if t.get("expired"):
            ringing = True
            continue
        left = t.get("remaining_seconds")
        if left is not None:
            running.append((float(left),
                            float(t.get("total_seconds") or 0.0)))
    soonest = min(running) if running else None
    out = {
        "scanner_mode": mode,
        "scanner_label": MODE_LABELS.get(mode, mode.title()),
        # The palette rides along so the agent renders the app's colors
        # rather than its own copy drifting from them.
        "mode_colors": {m: list(c)
                        for m, c in led_palette(on_manage, resting).items()},
        "alarm_active": bool(alarms),
        "timer": {
            "ringing": ringing,
            "running": len(running),
            "soonest_remaining": int(soonest[0]) if soonest else None,
            # That same timer's whole duration, so a progress display can show
            # how much is left as a proportion instead of a bare count of
            # seconds. None when the timer never recorded one.
            "soonest_total": (int(soonest[1])
                              if soonest and soonest[1] > 0 else None),
            # The bar and the finished-timer strobe wear this, so the agent
            # never has to keep its own copy of the user's choice.
            "color": list(timer_rgb or ALARM_RED),
        },
        "generated": int(now),
    }
    # A pending Test click from the Settings pane, if one is live. The agent
    # flashes each ts once, so it can ride several polls without repeating.
    if isinstance(key_test, dict) and key_test.get("id"):
        out["key_test"] = {"id": norm_id(key_test.get("id")),
                           "key": int(key_test.get("key") or 0),
                           "ts": float(key_test.get("ts") or 0)}
    return out


# A few seconds of cache on the satellite's upstream mode read (see
# current_mode_state). The NeoKey polls outputs about twice a second and the
# kiosk may poll too; this collapses that into roughly one upstream request per
# interval. A mode someone changed on ANOTHER device can lag by that much,
# which is invisible; a mode changed HERE must not, so note_mode_change primes
# this the instant we set one.
_mode_cache = TTLCache(3.0)


def note_mode_change(state: dict) -> None:
    """Prime the satellite's upstream-mode cache with a mode we just set here.

    Without this, picking a mode on the touchscreen left the lit key stale for
    up to the whole TTL: the satellite already knew the new mode (it is the one
    that forwarded it), but the outputs snapshot kept answering from the old
    cache entry, so no amount of LED polling could catch up. Priming closes that
    gap without shortening the TTL and without another upstream request."""
    if isinstance(state, dict) and state.get("mode") in SCANNER_MODES:
        _mode_cache.set(dict(state))


async def current_mode_state() -> dict:
    """The scanner mode the outputs snapshot should show.

    On a main server this is the local state file, which every worker shares
    already. On a satellite the mode lives upstream (POST
    /pending/scanner-mode forwards there and the local file never moves), so
    reading it locally would light the wrong NeoKey key forever. Fetch it
    from the server instead, cached, and fall back to the local file when the
    server is unreachable: a stale mode beats no LEDs at all.
    """
    from ..config import settings
    if not (settings.is_satellite() and settings.remote_server_url
            and settings.upstream_api_key):
        return scanner_mode_state()
    hit = _mode_cache.get()
    if hit is not None:
        return hit
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                f"{settings.remote_server_url.rstrip('/')}/pending/scanner-mode",
                headers={"X-API-Key": settings.upstream_api_key})
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("mode") in SCANNER_MODES:
            _mode_cache.set(data)
            return data
    except Exception:  # noqa: BLE001 - outputs must never fail on the network
        pass
    return scanner_mode_state()


def scanner_mode_state() -> dict:
    """The local scanner mode state (its own function so the outputs builder
    stays testable without importing the state file machinery)."""
    from .scanner_mode import get_state
    return get_state()


# --------------------------------------------------------------------------
# Live state for the Settings cards
# --------------------------------------------------------------------------

def normalize_heartbeat(entry: dict, now: float) -> dict | None:
    """One kind="stemma" push entry, cleaned into the state file's shape.

    A QT board is bus-powered, so there is no battery to report: the whole
    reading is "this device answered just now", which is what makes the card
    say plugged in or unplugged."""
    if not isinstance(entry, dict):
        return None
    dev_id = norm_id(entry.get("id"))
    if not parse_device_id(dev_id):
        return None
    return {
        "id": dev_id,
        "kind": "stemma",
        "model": str(entry.get("model") or "").strip().lower()[:30],
        "name": str(entry.get("name") or "").strip()[:60],
        "ts": now,
    }


def normalize_discovered(entry: dict, now: float) -> dict | None:
    """One discovered-but-unconfigured QT device from the agent's sweep."""
    if not isinstance(entry, dict):
        return None
    dev_id = norm_id(entry.get("id"))
    parsed = parse_device_id(dev_id)
    if not parsed:
        return None
    model = str(entry.get("model") or "").strip().lower()[:30]
    return {
        "id": dev_id,
        "kind": "stemma",
        "model": model,
        "name": (str(entry.get("name") or "").strip()[:60]
                 or kind_label(model)),
        "address": f"0x{parsed[1]:02x}",
        "supported": bool(entry.get("supported", True)) and model in STEMMA_KINDS,
        "ts": now,
    }


def device_cards(readings: dict, now: float | None = None) -> list[dict]:
    """The configured devices with their live state, for the Settings pane.

    ``readings`` is the stemma block of the gadgets state file (id ->
    heartbeat). A device with no heartbeat at all has never been seen since
    the app started; one whose heartbeat has gone quiet is stale, which on a
    bus-powered board means unplugged.
    """
    now = time.time() if now is None else now
    out = []
    for dev in configured_devices():
        seen = (readings or {}).get(norm_id(dev.get("id"))) or {}
        ts = seen.get("ts")
        age = None if ts is None else max(0.0, now - float(ts))
        parsed = parse_device_id(dev.get("id")) or (1, 0)
        card = dict(dev)
        card.update({
            "address": f"0x{parsed[1]:02x}",
            "label": kind_label(dev.get("kind")),
            "age_seconds": age,
            "stale": age is None or age > STEMMA_STALE_SECONDS,
        })
        if dev.get("kind") == "neokey":
            # "mode" is the slot's whole action string (a mode, "nav:...", or
            # "timer:..."); the field name predates actions and the editor
            # keys off it, so it stays.
            card["keys"] = [
                {"index": i, "mode": action,
                 "label": key_action_label(action),
                 "color": list(mode_color(action))}
                for i, action in enumerate(dev["options"]["keymap"])
            ]
        out.append(card)
    return out
