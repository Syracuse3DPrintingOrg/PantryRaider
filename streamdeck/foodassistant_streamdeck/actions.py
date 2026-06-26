"""Action registry for the Stream Deck controller.

Each key on the deck is bound to an action. An action carries enough metadata
to render its key (label, colour, whether it shows a live count) and a kind
that tells the controller what to do when the key is pressed. The functions
here are pure: they describe actions and run the HTTP side effects, but they
never touch the deck hardware directly. The controller passes in a small
context object for the few effects that reach back to the device (brightness,
paging, kiosk navigation).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Optional

def _clock_label(now: Any, show_date: bool = True) -> str:
    """Render a clock key face from a ``datetime``-like ``now``.

    Top line is the 24-hour ``HH:MM`` time; when ``show_date`` is set a second
    line carries an abbreviated weekday and day-of-month (e.g. "Thu 26"). Pure:
    it formats whatever ``now`` it is handed, so the controller can pass the
    current local time each fast-loop tick and tests can pass a fixed datetime.
    """
    time_str = now.strftime("%H:%M")
    if not show_date:
        return time_str
    return f"{time_str}\n{now.strftime('%a %-d')}"


# Longest meal-name a meal_today info key shows before truncation, so the name
# stays glanceable on a single key face. Matches the recipe-timer label cap.
MEAL_TODAY_LABEL_MAX: int = 14


def meal_today_label(mealplan: dict, fallback: str = "No meal") -> str:
    """Extract today's planned meal name from a /mealie/mealplan response.

    The response shape is ``{"start": iso, "days": {iso: [entry, ...]}}`` where
    each entry carries a ``title``. Picks the first entry on the start date,
    cleans and truncates its title to a deck-safe length, and falls back to
    ``fallback`` when there is no plan for today. Pure, so it is unit-testable
    without any network.
    """
    if not isinstance(mealplan, dict):
        return fallback
    start = mealplan.get("start", "")
    days = mealplan.get("days") or {}
    entries = days.get(start) or []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        title = clean_timer_label(entry.get("title", ""), MEAL_TODAY_LABEL_MAX)
        if title:
            return title
    return fallback


class TimerState:
    """Mutable per-key countdown timer.

    Short press: add 1 minute (starts from idle; rapid presses accumulate).
    Long press: reset to idle immediately.
    When the countdown expires, ``alerting`` flips to True; the next short
    press dismisses it.
    """

    def __init__(self) -> None:
        self._minutes: int = 0       # 0 = idle; positive = minutes set
        self._deadline: float = 0.0  # monotonic clock target
        self.alerting: bool = False

    def is_running(self) -> bool:
        return self._minutes > 0 and not self.alerting

    def remaining_seconds(self) -> int:
        if not self.is_running():
            return 0
        return max(0, int(self._deadline - time.monotonic()))

    def label(self, base_label: str) -> str:
        if self.alerting:
            return "Done!"
        if self._minutes == 0:
            return base_label
        secs = self.remaining_seconds()
        if secs <= 0:
            return "Done!"
        return f"{secs // 60}:{secs % 60:02d}"

    # The expired-alert colours the key blinks between: a bright red on the
    # "on" phase and a dim red on the "off" phase, so the key flashes until the
    # alert is dismissed.
    _ALERT_BRIGHT = "#ef4444"
    _ALERT_DIM = "#450a0a"

    def color(self, base_color: str, blink_phase: int = 0) -> str:
        if self.alerting:
            return self.alert_color(blink_phase)
        if self._minutes == 0:
            return base_color
        secs = self.remaining_seconds()
        return "#f59e0b" if secs < 60 else "#0d9488"

    def alert_color(self, blink_phase: int) -> str:
        """Colour for an expired alert at the given blink phase.

        Even phases are bright, odd phases are dim, so successive poll ticks
        flash the key. Only meaningful while ``alerting`` is True.
        """
        return self._ALERT_BRIGHT if blink_phase % 2 == 0 else self._ALERT_DIM

    def alert_active(self) -> bool:
        return self.alerting

    def short_press(self) -> None:
        """Add one minute. Dismisses the alert if one is active."""
        if self.alerting:
            self.alerting = False
            self._minutes = 0
            self._deadline = 0.0
            return
        self._minutes += 1
        self._deadline = time.monotonic() + self._minutes * 60

    def set_minutes(self, minutes: int) -> None:
        """Start (or restart) the countdown at a fixed number of minutes.

        Used by timer-override keys that carry a preset duration: one short
        press loads the whole preset rather than adding a single minute.
        """
        minutes = max(0, int(minutes))
        self.alerting = False
        self._minutes = minutes
        self._deadline = time.monotonic() + minutes * 60 if minutes else 0.0

    def long_press(self) -> None:
        """Reset the timer to idle immediately."""
        self.alerting = False
        self._minutes = 0
        self._deadline = 0.0

    def press(self) -> None:
        """Backward-compatible alias for short_press."""
        self.short_press()

    def tick(self) -> bool:
        """Return True (and set alerting) if the timer just expired."""
        if self.is_running() and self.remaining_seconds() <= 0:
            self.alerting = True
            self._minutes = 0
            return True
        return False


# How many characters of a recipe-derived label fit comfortably on a timer key
# face before the render layer would have to shrink or wrap it past readability.
# A short cap keeps the labels glanceable ("Pasta", "Sauce") and matches the
# stock timer labels in length.
RECIPE_TIMER_LABEL_MAX: int = 12


def clean_timer_label(label: str, max_len: int = RECIPE_TIMER_LABEL_MAX) -> str:
    """Reduce a recipe step label to a deck-safe short string.

    Collapses internal whitespace (including newlines) to single spaces, trims
    the ends, and truncates overly long labels with a trailing ellipsis so they
    still fit a key face. An empty or whitespace-only label returns "" so the
    caller can fall back to the stock default.
    """
    cleaned = " ".join(str(label or "").split())
    if not cleaned:
        return ""
    max_len = max(1, int(max_len))
    if len(cleaned) <= max_len:
        return cleaned
    if max_len == 1:
        return cleaned[:1]
    return cleaned[: max_len - 1].rstrip() + "…"


def recipe_timer_key_specs(
    suggestions: list[dict],
    slots: int,
    default_labels: Optional[list[str]] = None,
) -> list[dict]:
    """Map recipe timer suggestions onto up to ``slots`` timer-key descriptors.

    Returns a list of exactly ``slots`` dicts, one per timer key, each shaped
    ``{"label": str, "seconds": Optional[float], "step_index": Optional[int]}``.
    The first N slots (N == len(suggestions), truncated to ``slots``) carry a
    cleaned, face-safe label and the suggestion's duration; any remaining slots
    fall back to their stock label with ``seconds`` None so they behave exactly
    like a manual timer key.

    ``default_labels`` supplies the stock per-slot label (e.g. "Timer 1"); a
    missing or short list falls back to a generic "Timer". The function is pure:
    no clock, no I/O, so it is fully unit-testable. An empty suggestion list (no
    active recipe) yields the unchanged defaults for every slot.
    """
    slots = max(0, int(slots))
    default_labels = list(default_labels or [])
    specs: list[dict] = []
    for i in range(slots):
        fallback = default_labels[i] if i < len(default_labels) else "Timer"
        if i < len(suggestions):
            suggestion = suggestions[i] or {}
            label = clean_timer_label(suggestion.get("label", "")) or fallback
            specs.append({
                "label": label,
                "seconds": suggestion.get("seconds"),
                "step_index": suggestion.get("step_index"),
            })
        else:
            specs.append({"label": fallback, "seconds": None, "step_index": None})
    return specs


# Largest PIN the buffer will hold. Generous enough for any reasonable unlock
# code; extra presses past this are ignored rather than silently truncating a
# longer code into a different one.
PIN_MAX_LEN: int = 12


class PinBuffer:
    """Accumulates a numeric PIN entered on the deck keypad.

    The buffer never exposes the entered digits for rendering; callers ask for
    ``masked()`` (a row of dots) or ``length()`` so the actual code is never
    drawn on a key face. ``digit`` appends, ``backspace`` removes the last
    digit, and ``clear`` empties the whole buffer. ``value`` is only read when
    the controller submits the code over HTTP.
    """

    def __init__(self, max_len: int = PIN_MAX_LEN) -> None:
        self._digits: list[str] = []
        self._max_len = max(1, int(max_len))

    def digit(self, ch: str) -> None:
        """Append a single digit. Non-digits and overflow are ignored."""
        if len(ch) == 1 and ch.isdigit() and len(self._digits) < self._max_len:
            self._digits.append(ch)

    def backspace(self) -> None:
        if self._digits:
            self._digits.pop()

    def clear(self) -> None:
        self._digits.clear()

    def length(self) -> int:
        return len(self._digits)

    def is_empty(self) -> bool:
        return not self._digits

    @property
    def value(self) -> str:
        """The raw entered PIN. Only the submit path should read this."""
        return "".join(self._digits)

    def masked(self) -> str:
        """A face-safe representation: one dot per entered digit."""
        return "•" * len(self._digits)


# Logical keys on the on-deck keypad. Digit keys carry the digit itself; the two
# editing keys use these sentinel names.
KEYPAD_CLEAR = "clear"
KEYPAD_ENTER = "enter"
KEYPAD_CANCEL = "cancel"


def keypad_specs() -> dict[str, ActionSpec]:
    """Build the ActionSpecs used on the keypad page.

    Digits 0-9 plus a clear/backspace, an enter/submit, and a cancel that drops
    back to the normal layout. These are generated rather than stored in the
    static ACTIONS registry so the keypad never appears as a bindable key in a
    user's config.
    """
    specs: dict[str, ActionSpec] = {}
    for d in "0123456789":
        specs[f"keypad_{d}"] = ActionSpec(
            name=f"keypad_{d}", label=d, color="#1e293b",
            kind="keypad", keypad_key=d,
        )
    specs[f"keypad_{KEYPAD_CLEAR}"] = ActionSpec(
        name=f"keypad_{KEYPAD_CLEAR}", label="Clear", color="#7f1d1d",
        kind="keypad", keypad_key=KEYPAD_CLEAR,
    )
    specs[f"keypad_{KEYPAD_ENTER}"] = ActionSpec(
        name=f"keypad_{KEYPAD_ENTER}", label="Enter", color="#166534",
        kind="keypad", keypad_key=KEYPAD_ENTER,
    )
    specs[f"keypad_{KEYPAD_CANCEL}"] = ActionSpec(
        name=f"keypad_{KEYPAD_CANCEL}", label="Cancel", color="#334155",
        kind="keypad", keypad_key=KEYPAD_CANCEL,
    )
    return specs


async def submit_pin(client: Any, base_url: str, pin: str) -> bool:
    """Submit a PIN to the app's login endpoint. Returns True on success.

    The app authenticates with a password (which may be a numeric PIN) posted
    to ``/ui/login`` as a form field. A successful login answers with a redirect
    to the dashboard (status < 400 without following it); a wrong code answers
    401. Network or service errors return False so the deck shows an error state
    rather than crashing.
    """
    base = base_url.rstrip("/")
    try:
        r = await client.post(
            f"{base}/ui/login",
            data={"password": pin},
            follow_redirects=False,
        )
        return r.status_code < 400
    except Exception:  # noqa: BLE001 - surface as failure, never crash
        return False


_WEATHER_CONDITION_CODES: dict[int, str] = {
    113: "Sunny", 116: "Partly\nCloudy", 119: "Cloudy", 122: "Overcast",
    143: "Mist", 176: "Patchy\nRain", 179: "Patchy\nSnow",
    182: "Sleet", 185: "Drizzle", 200: "Thunder", 227: "Blowing\nSnow",
    230: "Blizzard", 248: "Fog", 260: "Ice Fog", 263: "Drizzle",
    266: "Drizzle", 281: "Drizzle", 284: "Ice Drizzle",
    293: "Light\nRain", 296: "Light\nRain", 299: "Rain", 302: "Rain",
    305: "Heavy\nRain", 308: "Heavy\nRain", 311: "Sleet", 314: "Sleet",
    317: "Light\nSleet", 320: "Mod.\nSleet", 323: "Light\nSnow",
    326: "Light\nSnow", 329: "Snow", 332: "Snow", 335: "Heavy\nSnow",
    338: "Heavy\nSnow", 350: "Ice", 353: "Showers", 356: "Showers",
    359: "Heavy\nRain", 362: "Sleet", 365: "Sleet", 368: "Snow\nShowers",
    371: "Snow\nShowers", 374: "Ice", 377: "Ice", 386: "Thunder",
    389: "Thunder", 392: "T-Storm", 395: "Blizzard",
}


# How long the weather and forecast keys stay on a non-default stat or day
# after the last press before the idle loop returns them to index 0. Short
# enough that a glance-and-leave deck looks stock again within a few breaths,
# long enough to read a couple of stats in one sitting.
WEATHER_AUTO_RESET_SECS: float = 30.0


def should_auto_reset(now: float, last_interaction: float,
                      window_secs: float = WEATHER_AUTO_RESET_SECS) -> bool:
    """True when ``window_secs`` have elapsed since the last cycle press.

    Pure boundary helper so the idle reset is unit-testable without sleeping.
    ``last_interaction`` of 0 (never cycled away from the default) never
    triggers a reset, and the comparison is inclusive at the window edge so a
    press exactly ``window_secs`` old is treated as just expired.
    """
    if last_interaction <= 0:
        return False
    return (now - last_interaction) >= window_secs


class WeatherState:
    """Fetches and caches current weather from wttr.in (no API key required).

    ``location`` is any city name, zip code, or lat,lon string. When empty,
    wttr.in auto-detects the location from the requester's IP address.
    ``units`` is 'f' (Fahrenheit) or 'c' (Celsius).

    The weather key cycles through a list of stat renderers (current temp plus
    condition at index 0, then feels-like, humidity, and wind) and the forecast
    key cycles through the cached forecast days (today at index 0). Both indices
    sit at 0 by default so an un-pressed deck renders exactly as before; a press
    advances the matching index and stamps ``last_interaction`` so the idle loop
    can return it to the default after ``WEATHER_AUTO_RESET_SECS``.
    """

    def __init__(self, location: str = "", units: str = "f") -> None:
        self.location = location
        self.units = units.lower()
        self._label: str = "Weather"
        self._color: str = "#1e40af"
        self._fetched_at: float = 0.0
        self._error: bool = False
        self._fc_label: str = "Forecast"
        self._fc_color: str = "#0e7490"
        # Parsed current-condition fields kept for the per-stat renderers. They
        # stay empty until the first successful fetch, at which point label()
        # and the stat renderers begin using them.
        self._cond: dict[str, str] = {}
        # Per-day forecast rows, each a small dict the forecast renderers read.
        self._forecast_days: list[dict[str, str]] = []
        # Cycle indices. 0 is the default in both cases, so an un-pressed deck
        # looks identical to the pre-cycle behaviour.
        self._stat_idx: int = 0
        self._day_idx: int = 0
        # Monotonic timestamp of the last cycle press, or 0 while at the default.
        self.last_interaction: float = 0.0

    def age_seconds(self) -> float:
        return time.monotonic() - self._fetched_at

    # -- weather stat cycle (pure) ----------------------------------------

    def _stat_temp(self) -> tuple[str, str]:
        """Default stat: current temp and condition, matching the old label."""
        return self._label, self._color

    def _stat_feels_like(self) -> tuple[str, str]:
        key = "FeelsLikeF" if self.units == "f" else "FeelsLikeC"
        unit_sym = "F" if self.units == "f" else "C"
        val = self._cond.get(key, "?")
        return f"Feels\n{val}°{unit_sym}", "#3730a3"

    def _stat_humidity(self) -> tuple[str, str]:
        val = self._cond.get("humidity", "?")
        return f"Humid\n{val}%", "#155e75"

    def _stat_wind(self) -> tuple[str, str]:
        if self.units == "f":
            val = self._cond.get("windspeedMiles", "?")
            unit = "mph"
        else:
            val = self._cond.get("windspeedKmph", "?")
            unit = "kph"
        return f"Wind\n{val}\n{unit}", "#1e40af"

    # Ordered stat renderers. Index 0 is the stock temp+condition view.
    @property
    def _stat_renderers(self):
        return (
            self._stat_temp,
            self._stat_feels_like,
            self._stat_humidity,
            self._stat_wind,
        )

    @property
    def stat_count(self) -> int:
        return len(self._stat_renderers)

    def cycle_stat(self) -> int:
        """Advance to the next weather stat, wrapping around. Returns the index."""
        self._stat_idx = (self._stat_idx + 1) % self.stat_count
        self.last_interaction = time.monotonic()
        return self._stat_idx

    def current_stat_label(self, base_label: str) -> str:
        if not self._fetched_at:
            return base_label
        return self._stat_renderers[self._stat_idx % self.stat_count]()[0]

    def current_stat_color(self, base_color: str) -> str:
        if not self._fetched_at:
            return self._color
        return self._stat_renderers[self._stat_idx % self.stat_count]()[1]

    # -- forecast day cycle (pure) ----------------------------------------

    @property
    def forecast_day_count(self) -> int:
        """Number of cached forecast days (at least 1 once fetched)."""
        return max(1, len(self._forecast_days))

    def cycle_forecast_day(self) -> int:
        """Advance to the next forecast day, wrapping around. Returns the index."""
        self._day_idx = (self._day_idx + 1) % self.forecast_day_count
        self.last_interaction = time.monotonic()
        return self._day_idx

    def forecast_label_for(self, index: int) -> str:
        """High/low label for the day at ``index`` (0 == today), with a tag."""
        if not self._forecast_days:
            return self._fc_label
        day = self._forecast_days[index % len(self._forecast_days)]
        hi = day.get("hi", "?")
        lo = day.get("lo", "?")
        tag = day.get("tag", "")
        body = f"H{hi} L{lo}"
        return f"{tag}\n{body}" if tag else body

    def current_forecast_label(self, base_label: str) -> str:
        if not self._fetched_at:
            return base_label
        return self.forecast_label_for(self._day_idx)

    # -- legacy single-day accessors (index 0, unchanged behaviour) -------

    def label(self, base_label: str) -> str:
        return self.current_stat_label(base_label)

    def color(self, base_color: str) -> str:
        return self.current_stat_color(base_color)

    def forecast_label(self, base_label: str) -> str:
        return self.current_forecast_label(base_label)

    def forecast_color(self, base_color: str) -> str:
        return self._fc_color

    # -- idle reset (pure) -------------------------------------------------

    def reset_to_default(self) -> None:
        """Return both cycles to their default index and clear the timer."""
        self._stat_idx = 0
        self._day_idx = 0
        self.last_interaction = 0.0

    async def refresh(self) -> None:
        try:
            import httpx
            loc = self.location.strip().replace(" ", "+") if self.location.strip() else ""
            url = f"https://wttr.in/{loc}?format=j1"
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url, headers={"User-Agent": "foodassistant-streamdeck/1.0"})
            if r.status_code != 200:
                self._label = "No signal"
                self._color = "#6b7280"
                self._error = True
                self._fc_label = "No signal"
                return
            data = r.json()
            cond = data["current_condition"][0]
            self._cond = cond
            temp_key = "temp_F" if self.units == "f" else "temp_C"
            temp = cond.get(temp_key, "?")
            unit_sym = "F" if self.units == "f" else "C"
            code = int(cond.get("weatherCode", 113))
            desc = _WEATHER_CONDITION_CODES.get(code, cond.get("weatherDesc", [{}])[0].get("value", ""))
            self._label = f"{temp}°{unit_sym} {desc}"
            self._color = "#1e40af"
            self._error = False
            try:
                hi_key = "maxtempF" if self.units == "f" else "maxtempC"
                lo_key = "mintempF" if self.units == "f" else "mintempC"
                # Cache every returned day so the forecast key can cycle through
                # them; tag the first three with friendly names (the rest, if
                # any, fall back to the bare date).
                tags = ("Today", "Tmrw", "Day 3")
                days: list[dict[str, str]] = []
                for i, day in enumerate(data.get("weather", [])):
                    days.append({
                        "hi": str(day.get(hi_key, "?")),
                        "lo": str(day.get(lo_key, "?")),
                        "tag": tags[i] if i < len(tags) else str(day.get("date", "")),
                    })
                self._forecast_days = days
                if days:
                    self._fc_label = f"H{days[0]['hi']} L{days[0]['lo']}"
                    self._fc_color = "#0e7490"
            except Exception:
                self._fc_label = "Forecast"
        except Exception:
            self._label = "No signal"
            self._color = "#6b7280"
            self._error = True
            self._fc_label = "No signal"
        finally:
            self._fetched_at = time.monotonic()


_HA_STATE_COLOR_ON = "#15803d"
_HA_STATE_COLOR_OFF = "#475569"
_HA_STATE_COLOR_ERROR = "#6b7280"

_HA_ON_STATES = frozenset({"on", "home", "open", "playing", "active", "locked"})


class HaEntityState:
    """Caches Home Assistant entity state for a single key.

    Refreshed from the HA REST API. The key shows a green background when
    the entity is in an "on-like" state and gray otherwise. Unavailable or
    error states fall back to a neutral gray so the key is never misleading.
    """

    def __init__(self, entity_id: str, color_on: str = _HA_STATE_COLOR_ON,
                 color_off: str = _HA_STATE_COLOR_OFF) -> None:
        self.entity_id = entity_id
        self.color_on = color_on
        self.color_off = color_off
        self._state: str = ""   # raw HA state string
        self._fetched_at: float = 0.0

    def age_seconds(self) -> float:
        return time.monotonic() - self._fetched_at

    def is_on(self) -> bool:
        return self._state.lower() in _HA_ON_STATES

    def label(self, base_label: str) -> str:
        if not self._fetched_at:
            return base_label
        suffix = "On" if self.is_on() else "Off"
        return f"{base_label}\n{suffix}"

    def color(self, base_color: str) -> str:
        if not self._fetched_at:
            return base_color
        if self._state in ("unavailable", "unknown", ""):
            return _HA_STATE_COLOR_ERROR
        return self.color_on if self.is_on() else self.color_off

    async def refresh(self, ha_base_url: str, ha_token: str) -> None:
        try:
            import httpx
            url = f"{ha_base_url.rstrip('/')}/api/states/{self.entity_id}"
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {ha_token}",
                             "Content-Type": "application/json"},
                )
            if r.status_code == 200:
                self._state = r.json().get("state", "unknown")
            else:
                self._state = "unavailable"
        except Exception:
            self._state = "unavailable"
        finally:
            self._fetched_at = time.monotonic()


@dataclass(frozen=True)
class ActionSpec:
    """Static description of one bindable action."""

    name: str
    label: str
    color: str            # key background, "#rrggbb"
    kind: str             # "status" | "trigger" | "nav" | "system"
    status_field: str = ""   # for kind=="status": which polled count to show
    target_path: str = ""    # for kind=="nav": app path to open in the kiosk
    ha_entity_id: str = ""   # for kind=="ha_entity": HA entity to show/toggle
    ha_service: str = ""     # for kind=="ha_entity": HA service to call on press
    keypad_key: str = ""     # for kind=="keypad": digit or clear/enter/cancel
    timer_minutes: int = 0   # for kind=="timer" overrides: preset minutes (0=cycle)
    weather_location: str = ""  # for kind=="weather" overrides: per-key location
    description: str = ""
    icon: str = ""           # Bootstrap Icons glyph name (without the "bi-"
                             # prefix) drawn above the label; see ACTION_ICONS.
    color_on: str = ""       # for kind=="ha_entity" overrides: optional on-state
                             # background; empty falls back to the HA default.
    color_off: str = ""      # for kind=="ha_entity" overrides: optional off-state
                             # background; empty falls back to the HA default.
    item: str = ""           # for kind=="shopping_add" overrides: the product
                             # name to quick-add to the Mealie shopping list.
    macro_actions: tuple = ()  # for kind=="macro" overrides: ordered action names
                             # to run in sequence. A tuple keeps the frozen
                             # dataclass hashable.


# Single source of truth for key iconography. Each action maps to the same
# Bootstrap Icons glyph the web UI uses for that feature, so the deck face and
# the browser stay visually in sync. Values are glyph names without the "bi-"
# prefix (the render layer rasterises them from the vendored bootstrap-icons
# font). Keep this in step with service/app/navigation.py (nav tab icons) and
# the action buttons in the page templates:
#   nav tabs (navigation.py): inventory=grid, expiring=clock-history,
#     add=plus-circle, pending=hourglass-split, recipes=journal-richtext,
#     cook=fire, mealplan=calendar-week, shopping=cart, defaults=table.
#   commit button (pending.html): cloud-upload.
# The remaining keys are deck-only widgets with no web equivalent; they use the
# closest standard Bootstrap glyph (timers=stopwatch, weather=cloud-sun,
# forecast=thermometer-half, brightness=brightness-high, paging=chevrons,
# Home Assistant=house).
ACTION_ICONS: dict[str, str] = {
    "expiring": "clock-history",
    "pending": "hourglass-split",
    "commit": "cloud-upload",
    "add": "plus-circle",
    "inventory": "grid",
    "cook": "fire",
    "recipes": "journal-richtext",
    "mealplan": "calendar-week",
    "shopping": "cart",
    "defaults": "table",
    "brightness": "brightness-high",
    "page_next": "chevron-right",
    "page_prev": "chevron-left",
    "timer_1": "stopwatch",
    "timer_2": "stopwatch",
    "timer_3": "stopwatch",
    "weather": "cloud-sun",
    "forecast": "thermometer-half",
    "ha_1": "house",
    "ha_2": "house",
    "ha_3": "house",
    "ha_4": "house",
    "ha_5": "house",
    "pin": "shield-lock",
    "clock": "clock",
    "shopping_count": "cart",
    "ready": "check2-circle",
    "meal_today": "calendar-event",
    "cooked": "fire",
    "timer_eggs": "egg-fried",
    "timer_pasta": "stopwatch",
    "timer_rice": "stopwatch",
}


# The actions a key can be bound to. status_field names must match the keys
# produced by poll_status() below.
ACTIONS: dict[str, ActionSpec] = {
    "expiring": ActionSpec(
        name="expiring",
        label="Expiring",
        color="#b54708",
        kind="status",
        status_field="expiring",
        target_path="ui/expiring",
        description="Count of items expired or expiring within the soon window. "
        "Press to open the expiring list and refresh.",
    ),
    "pending": ActionSpec(
        name="pending",
        label="Pending",
        color="#1d4ed8",
        kind="status",
        status_field="pending",
        target_path="ui/pending",
        description="Count of scanned items waiting to be committed. "
        "Press to open the pending list and refresh.",
    ),
    "commit": ActionSpec(
        name="commit",
        label="Commit",
        color="#15803d",
        kind="trigger",
        description="Commit every pending scan into the inventory.",
    ),
    "add": ActionSpec(
        name="add",
        label="Add",
        color="#b45309",
        kind="nav",
        target_path="ui/add",
        description="Open the add-item page on the attached display.",
    ),
    "inventory": ActionSpec(
        name="inventory",
        label="Stock",
        color="#0f766e",
        kind="nav",
        target_path="ui/",
        description="Open the inventory dashboard on the attached display.",
    ),
    "cook": ActionSpec(
        name="cook",
        label="Cook",
        color="#7e22ce",
        kind="nav",
        target_path="ui/cook",
        description="Open the recipe suggestions page on the attached display.",
    ),
    "recipes": ActionSpec(
        name="recipes",
        label="Recipes",
        color="#7e22ce",
        kind="nav",
        target_path="ui/recipes",
        description="Open the Recipes page.",
    ),
    "mealplan": ActionSpec(
        name="mealplan",
        label="Plan",
        color="#7e22ce",
        kind="nav",
        target_path="ui/mealplan",
        description="Open the Meal Plan page.",
    ),
    "shopping": ActionSpec(
        name="shopping",
        label="Shop",
        color="#7e22ce",
        kind="nav",
        target_path="ui/shopping",
        description="Open the Shopping list page.",
    ),
    "defaults": ActionSpec(
        name="defaults",
        label="Defaults",
        color="#7e22ce",
        kind="nav",
        target_path="ui/defaults",
        description="Open the storage Defaults page.",
    ),
    "brightness": ActionSpec(
        name="brightness",
        label="Bright",
        color="#475569",
        kind="system",
        description="Cycle the deck brightness.",
    ),
    "page_next": ActionSpec(
        name="page_next",
        label="More",
        color="#334155",
        kind="system",
        description="Show the next page of keys.",
    ),
    "page_prev": ActionSpec(
        name="page_prev",
        label="Back",
        color="#334155",
        kind="system",
        description="Show the previous page of keys.",
    ),
    "pin": ActionSpec(
        name="pin",
        label="Unlock",
        color="#1d4ed8",
        kind="pin",
        description="Switch the deck into a numeric keypad to unlock the "
        "PIN-locked app, then return to the normal layout.",
    ),
    "timer_1": ActionSpec(
        name="timer_1",
        label="Timer 1",
        color="#0d9488",
        kind="timer",
        description="Countdown timer (press to cycle: 5/10/15/30/60 min or stop).",
    ),
    "timer_2": ActionSpec(
        name="timer_2",
        label="Timer 2",
        color="#0d9488",
        kind="timer",
        description="Second independent countdown timer.",
    ),
    "timer_3": ActionSpec(
        name="timer_3",
        label="Timer 3",
        color="#0d9488",
        kind="timer",
        description="Third independent countdown timer.",
    ),
    "weather": ActionSpec(
        name="weather",
        label="Weather",
        color="#1e40af",
        kind="weather",
        description="Current weather from wttr.in. Configure location and units in config.toml. "
        "Press to refresh. No API key required.",
    ),
    "forecast": ActionSpec(
        name="forecast",
        label="Forecast",
        color="#0e7490",
        kind="forecast",
        description="Today's high/low from wttr.in. Shares the weather fetch. "
        "Press to refresh. No API key required.",
    ),
    "ha_1": ActionSpec(name="ha_1", label="HA 1", color=_HA_STATE_COLOR_OFF, kind="ha_entity",
                       description="Home Assistant entity slot 1. Configure in config.toml."),
    "ha_2": ActionSpec(name="ha_2", label="HA 2", color=_HA_STATE_COLOR_OFF, kind="ha_entity",
                       description="Home Assistant entity slot 2. Configure in config.toml."),
    "ha_3": ActionSpec(name="ha_3", label="HA 3", color=_HA_STATE_COLOR_OFF, kind="ha_entity",
                       description="Home Assistant entity slot 3. Configure in config.toml."),
    "ha_4": ActionSpec(name="ha_4", label="HA 4", color=_HA_STATE_COLOR_OFF, kind="ha_entity",
                       description="Home Assistant entity slot 4. Configure in config.toml."),
    "ha_5": ActionSpec(name="ha_5", label="HA 5", color=_HA_STATE_COLOR_OFF, kind="ha_entity",
                       description="Home Assistant entity slot 5. Configure in config.toml."),
    "clock": ActionSpec(
        name="clock",
        label="Clock",
        color="#1f2937",
        kind="clock",
        description="Current time (HH:MM) and date, updated every second. "
        "Pure local clock, no network.",
    ),
    "shopping_count": ActionSpec(
        name="shopping_count",
        label="Shop",
        color="#0f766e",
        kind="status",
        status_field="shopping",
        target_path="ui/shopping",
        description="Count of items on the Mealie shopping list. "
        "Press to open the shopping list and refresh.",
    ),
    "ready": ActionSpec(
        name="ready",
        label="Ready",
        color="#15803d",
        kind="status",
        status_field="ready",
        target_path="ui/cook",
        description="How many recipes are cookable from current stock alone. "
        "Press to open the Cook page and refresh.",
    ),
    "meal_today": ActionSpec(
        name="meal_today",
        label="Tonight",
        color="#7e22ce",
        kind="info",
        status_field="meal_today",
        target_path="ui/mealplan",
        description="Today's planned meal from the meal plan. "
        "Press to open the Meal Plan page.",
    ),
    "cooked": ActionSpec(
        name="cooked",
        label="Cooked",
        color="#b45309",
        kind="trigger",
        description="Mark the active Current Recipe as cooked, consuming its "
        "matched inventory items. No-op when no recipe is active.",
    ),
    "timer_eggs": ActionSpec(
        name="timer_eggs",
        label="Eggs",
        color="#0d9488",
        kind="timer",
        timer_minutes=6,
        description="Preset 6-minute timer for soft-boiled eggs.",
    ),
    "timer_pasta": ActionSpec(
        name="timer_pasta",
        label="Pasta",
        color="#0d9488",
        kind="timer",
        timer_minutes=10,
        description="Preset 10-minute pasta timer.",
    ),
    "timer_rice": ActionSpec(
        name="timer_rice",
        label="Rice",
        color="#0d9488",
        kind="timer",
        timer_minutes=18,
        description="Preset 18-minute rice timer.",
    ),
}

# Stamp each spec with its glyph from the single-source-of-truth map above, so
# the icon travels with the ActionSpec (and the web catalog) without repeating
# the name in every literal.
for _name, _glyph in ACTION_ICONS.items():
    _spec = ACTIONS.get(_name)
    if _spec is not None:
        ACTIONS[_name] = replace(_spec, icon=_glyph)
del _name, _glyph, _spec


def icon_for(name: str) -> str:
    """Return the Bootstrap Icons glyph name for an action, or "" if none."""
    return ACTION_ICONS.get(name, "")


# Order used when no explicit key list is configured. The controller trims or
# paginates this to fit the connected deck, so a longer list simply fills a 15
# or 32 key deck with real actions and still paginates a 6 key Mini.
#
# Ordering is by usefulness, most-glanced first: the two live status counts,
# then the common inventory actions, then the cook/recipe navigation, the meal
# planning pages, the kitchen timers, the weather widgets, and finally the
# brightness control (paging is appended automatically by the layout when the
# list overflows the deck, so it is not listed here). Every name below must
# resolve in ACTIONS.
DEFAULT_ORDER: list[str] = [
    "expiring",
    "pending",
    "ready",
    "shopping_count",
    "commit",
    "add",
    "inventory",
    "cook",
    "recipes",
    "mealplan",
    "shopping",
    "meal_today",
    "cooked",
    "timer_1",
    "timer_2",
    "timer_3",
    "timer_eggs",
    "timer_pasta",
    "timer_rice",
    "clock",
    "weather",
    "forecast",
    "brightness",
]


_GROUP_BY_KIND = {
    "status": "Status", "trigger": "Actions", "nav": "Navigation",
    "system": "System", "timer": "Timers", "weather": "Weather",
    "forecast": "Weather", "ha_entity": "Home Assistant",
    "clock": "Info", "info": "Info",
}


def catalog() -> list[dict]:
    """Describe every assignable action for the web grid editor."""
    items = [{
        "name": spec.name,
        "label": spec.label,
        "kind": spec.kind,
        "group": _GROUP_BY_KIND.get(spec.kind, "Other"),
        "color": spec.color,
        "icon": spec.icon,
        "description": getattr(spec, "description", ""),
    } for spec in ACTIONS.values()]
    items.append({"name": "blank", "label": "Empty", "kind": "blank",
                  "group": "System", "color": "#1f2937",
                  "description": "Leave this key blank."})
    return items


def resolve(name: str) -> Optional[ActionSpec]:
    """Look up an action by name, or None if it is not known."""
    return ACTIONS.get(name)


# Override key types exposed in the setup UI, mapped to the ActionSpec kind the
# controller already knows how to render and dispatch. "default" is a sentinel
# that leaves the slot's stock action in place (used to clear an override).
OVERRIDE_TYPES: tuple[str, ...] = (
    "ha_action", "timer", "weather", "shopping_add", "macro", "default"
)

_OVERRIDE_DEFAULT_COLORS = {
    "ha_action": _HA_STATE_COLOR_OFF,
    "timer": "#0d9488",
    "weather": "#1e40af",
    "shopping_add": "#0f766e",
    "macro": "#6d28d9",
}

_OVERRIDE_DEFAULT_ICONS = {
    "ha_action": "house",
    "timer": "stopwatch",
    "weather": "cloud-sun",
    "shopping_add": "cart-plus",
    "macro": "collection-play",
}

# Longest item name a shopping_add key shows on its face before truncation, so a
# long product name stays glanceable on a single key.
SHOPPING_ADD_LABEL_MAX: int = 12


def override_to_spec(slot: int, override: dict) -> Optional[ActionSpec]:
    """Build an ActionSpec from a single key-override entry, or None.

    ``override`` is one user-configured slot from ``streamdeck_key_overrides``:
    a dict with ``type`` (one of OVERRIDE_TYPES) and type-specific fields. The
    returned spec carries a stable, slot-unique ``name`` so per-key timer and
    HA state can be keyed off it without colliding with the static ACTIONS.
    A ``default`` type, an unknown type, or a missing required field returns
    None so the caller keeps the slot's stock action.
    """
    if not isinstance(override, dict):
        return None
    otype = override.get("type", "")
    if otype not in OVERRIDE_TYPES or otype == "default":
        return None

    name = f"override_{int(slot)}"
    label = str(override.get("label", "")).strip()
    icon = str(override.get("icon", "")).strip() or _OVERRIDE_DEFAULT_ICONS.get(otype, "")
    color = _OVERRIDE_DEFAULT_COLORS.get(otype, "#374151")

    if otype == "ha_action":
        # Either a bare entity_id (toggled via homeassistant.toggle) or an
        # explicit service such as "script.goodnight". A service without a
        # target entity is still valid (scripts and scenes take no entity_id).
        entity_id = str(override.get("entity_id", "")).strip()
        service = str(override.get("service", "")).strip()
        if not entity_id and not service:
            return None
        if not service:
            service = "homeassistant.toggle"
        if not entity_id and "." in service:
            # A bare service like "script.goodnight" implies its own entity.
            entity_id = service
        if not label:
            label = (entity_id or service).split(".", 1)[-1].replace("_", " ").title()
        # Optional per-override on/off background colours. Empty strings leave
        # the controller free to fall back to the stock HA on/off palette.
        color_on = str(override.get("color_on", "")).strip()
        color_off = str(override.get("color_off", "")).strip()
        if color_off:
            color = color_off
        return ActionSpec(
            name=name, label=label, color=color, kind="ha_entity",
            ha_entity_id=entity_id, ha_service=service, icon=icon,
            color_on=color_on, color_off=color_off,
        )

    if otype == "timer":
        try:
            minutes = max(0, int(override.get("minutes", 0)))
        except (TypeError, ValueError):
            minutes = 0
        return ActionSpec(
            name=name, label=label or "Timer", color=color, kind="timer",
            timer_minutes=minutes, icon=icon,
        )

    if otype == "weather":
        location = str(override.get("location", override.get("source", ""))).strip()
        # A weather override can render either the current-conditions tile
        # (default) or a paired high/low forecast tile for the same location.
        # The forecast variant mirrors the global "forecast" key but draws from
        # this override's own per-location WeatherState. A full paired-slot
        # layout (one current key plus an auto-placed forecast key) is deferred;
        # for now each override picks one face via the "forecast" flag, which is
        # the smallest coherent version of the feature.
        if _truthy(override.get("forecast")):
            fc_icon = str(override.get("icon", "")).strip() or "thermometer-half"
            fc_color = override.get("color") or "#0e7490"
            return ActionSpec(
                name=name, label=label or "Forecast", color=fc_color,
                kind="forecast", weather_location=location, icon=fc_icon,
            )
        return ActionSpec(
            name=name, label=label or "Weather", color=color, kind="weather",
            weather_location=location, icon=icon,
        )

    if otype == "shopping_add":
        # A quick-add key carries the product name to push onto the Mealie
        # shopping list. Without an item there is nothing to add, so the slot
        # keeps its stock action.
        item = str(override.get("item", "")).strip()
        if not item:
            return None
        if not label:
            label = clean_timer_label(item, SHOPPING_ADD_LABEL_MAX) or "Add"
        return ActionSpec(
            name=name, label=label, color=color, kind="shopping_add",
            item=item, icon=icon,
        )

    if otype == "macro":
        # A macro key runs several existing actions in order. The names are
        # validated lazily at press time (resolve() lookup), so an unknown name
        # is simply skipped rather than rejecting the whole override here. A
        # macro with no usable names still maps so the key reads as configured.
        raw = override.get("actions", [])
        names: list[str] = []
        if isinstance(raw, (list, tuple)):
            for entry in raw:
                entry = str(entry).strip()
                if entry:
                    names.append(entry)
        elif isinstance(raw, str):
            for entry in raw.split(","):
                entry = entry.strip()
                if entry:
                    names.append(entry)
        if not names:
            return None
        return ActionSpec(
            name=name, label=label or "Macro", color=color, kind="macro",
            macro_actions=tuple(names), icon=icon,
        )

    return None


def _truthy(value: Any) -> bool:
    """Loosely interpret a JSON/TOML flag as a boolean.

    Accepts real booleans plus the common string spellings ("true", "1",
    "yes", "on") so a forecast flag survives a round-trip through settings.json
    or config.toml regardless of how it was serialised.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on"}
    return False


def overrides_to_specs(overrides: list, key_count: int) -> dict:
    """Parse a list of slot overrides into a ``{slot_index: ActionSpec}`` map.

    Each override is a dict with a ``slot`` index and type-specific fields (see
    ``override_to_spec``). Entries whose slot is outside ``[0, key_count)`` or
    whose type cannot be built are skipped, so a malformed entry never displaces
    a valid one. When two overrides target the same slot the last one wins.
    """
    out: dict[int, ActionSpec] = {}
    if not isinstance(overrides, list) or key_count < 1:
        return out
    for entry in overrides:
        if not isinstance(entry, dict):
            continue
        try:
            slot = int(entry.get("slot"))
        except (TypeError, ValueError):
            continue
        if not (0 <= slot < key_count):
            continue
        spec = override_to_spec(slot, entry)
        if spec is not None:
            out[slot] = spec
    return out


async def poll_status(client: Any, base_url: str, soon_days: int = 7) -> dict[str, int]:
    """Fetch the live counts shown on status keys.

    Returns a flat mapping of status_field -> integer. Network or service
    errors collapse to zeros so a key never shows a stale or crashing value.
    """
    out = {"expiring": 0, "pending": 0, "shopping": 0, "ready": 0}
    base = base_url.rstrip("/")
    try:
        r = await client.get(f"{base}/expiring/summary")
        if r.status_code == 200:
            s = r.json()
            out["expiring"] = (
                int(s.get("expired", 0))
                + int(s.get("today", 0))
                + int(s.get("within_3_days", 0))
                + (int(s.get("within_7_days", 0)) if soon_days >= 7 else 0)
            )
    except Exception:
        pass
    try:
        r = await client.get(f"{base}/pending/count")
        if r.status_code == 200:
            out["pending"] = int(r.json().get("count", 0))
    except Exception:
        pass
    # Shopping-list size and ready-to-cook count come from tiny dedicated count
    # endpoints so the poll stays cheap. Each degrades to 0 on any failure so a
    # status key never shows a stale or crashing value.
    try:
        r = await client.get(f"{base}/mealie/shopping/count")
        if r.status_code == 200:
            out["shopping"] = int(r.json().get("count", 0))
    except Exception:
        pass
    try:
        r = await client.get(f"{base}/mealie/suggest/ready-count")
        if r.status_code == 200:
            out["ready"] = int(r.json().get("count", 0))
    except Exception:
        pass
    return out


async def fetch_timer_suggestions(client: Any, base_url: str) -> list[dict]:
    """Fetch the active recipe's timer suggestions, or [] on any failure.

    Each entry is ``{label, seconds, step_index}``. An absent recipe answers an
    empty list; a network or service error collapses to [] so the timer keys
    quietly fall back to their manual behaviour rather than crashing the loop.
    """
    base = base_url.rstrip("/")
    try:
        r = await client.get(f"{base}/current-recipe/timer-suggestions")
        if r.status_code == 200:
            data = r.json()
            out = data.get("suggestions", [])
            return out if isinstance(out, list) else []
    except Exception:  # noqa: BLE001 - surface as no suggestions, never crash
        pass
    return []


async def fetch_meal_today(client: Any, base_url: str, fallback: str = "No meal") -> str:
    """Fetch today's planned meal name for the meal_today info key, or fallback.

    Calls /mealie/mealplan and extracts the first entry on the start (today)
    date via ``meal_today_label``. Any network or service failure degrades to
    ``fallback`` so the info key shows a neutral face rather than crashing.
    """
    base = base_url.rstrip("/")
    try:
        r = await client.get(f"{base}/mealie/mealplan")
        if r.status_code == 200:
            return meal_today_label(r.json(), fallback)
    except Exception:  # noqa: BLE001 - surface as the fallback, never crash
        pass
    return fallback


async def mark_current_recipe_cooked(client: Any, base_url: str) -> str:
    """Mark the active Current Recipe as cooked. Returns a short status face.

    Reads the active recipe's slug from /current-recipe, then posts it to
    /mealie/cooked to consume the matched inventory items. Returns a brief
    confirmation ("Cooked" with the consumed count) on success, "No recipe" when
    nothing is active, and "Failed" on any error, so a press always degrades to a
    readable face rather than crashing the controller.
    """
    base = base_url.rstrip("/")
    try:
        r = await client.get(f"{base}/current-recipe")
        if r.status_code != 200:
            return "Failed"
        recipe = (r.json() or {}).get("recipe") or {}
    except Exception:  # noqa: BLE001 - never crash a press
        return "Failed"
    # The active recipe carries the Mealie slug in its ``id`` field (set by
    # from_mealie_detail); accept a few aliases defensively.
    slug = (recipe.get("id") or recipe.get("slug") or recipe.get("source_slug") or "")
    slug = str(slug).strip()
    if not slug:
        return "No recipe"
    try:
        r = await client.post(f"{base}/mealie/cooked", json={"slug": slug})
        if r.status_code == 200:
            consumed = len((r.json() or {}).get("consumed") or [])
            return f"Cooked {consumed}" if consumed else "Cooked"
        return "Failed"
    except Exception:  # noqa: BLE001
        return "Failed"


async def add_shopping_item(client: Any, base_url: str, item: str) -> str:
    """Quick-add a favourite item to the Mealie shopping list. Returns a face.

    The app's POST /mealie/shopping/items needs the target list id, so this
    first reads /mealie/shopping to discover the default list (the same list the
    web UI shows), then posts the item note onto it. Returns "Added" on success,
    "No list" when Mealie has no shopping list, and "Failed" on any error, so a
    press always degrades to a readable face rather than crashing the controller.
    """
    item = str(item or "").strip()
    if not item:
        return "Empty"
    base = base_url.rstrip("/")
    try:
        r = await client.get(f"{base}/mealie/shopping")
        if r.status_code != 200:
            return "Failed"
        list_id = ((r.json() or {}).get("list") or {}).get("id") or ""
    except Exception:  # noqa: BLE001 - never crash a press
        return "Failed"
    if not list_id:
        return "No list"
    try:
        r = await client.post(
            f"{base}/mealie/shopping/items",
            json={"list_id": list_id, "note": item, "quantity": 1.0},
        )
        return "Added" if r.status_code == 200 else "Failed"
    except Exception:  # noqa: BLE001
        return "Failed"


async def start_recipe_timer(
    client: Any, base_url: str, step_index: Any = None,
    label: str = "", seconds: Any = None,
) -> bool:
    """Start a shared server timer from a recipe suggestion. Returns True on 200.

    Posts to /current-recipe/timers/start so every surface (web UI, satellites)
    sees the same countdown. Identify the suggestion by step_index or label;
    seconds, when given, is passed through. Best-effort: any failure returns
    False so a press still drives the local TimerState for the deck's own face.
    """
    base = base_url.rstrip("/")
    payload: dict[str, Any] = {}
    if step_index is not None:
        payload["step_index"] = step_index
    if label:
        payload["label"] = label
    if seconds is not None:
        payload["seconds"] = seconds
    try:
        r = await client.post(f"{base}/current-recipe/timers/start", json=payload)
        return r.status_code == 200
    except Exception:  # noqa: BLE001 - shared timer is best-effort
        return False


@dataclass
class ActionContext:
    """Effects the controller exposes to action handlers."""

    client: Any                                   # httpx.AsyncClient
    base_url: str
    refresh: Callable[[], Awaitable[None]]        # re-poll and redraw
    navigate: Callable[[str], Awaitable[bool]]    # open an app path in the kiosk
    cycle_brightness: Callable[[], int]           # returns the new percent
    page_next: Callable[[], None]
    page_prev: Callable[[], None]
    timer_press: Callable[[str, bool], None] = field(default=lambda _name, _long=False: None)
    weather_refresh: Callable[[], Awaitable[None]] = field(
        default=lambda: __import__("asyncio").sleep(0)
    )
    # Advance the weather stat / forecast day cycle for a pressed widget key.
    # The arg is the pressed spec's name so per-key override widgets cycle their
    # own WeatherState. Default no-ops keep unit contexts that omit them valid.
    weather_cycle: Callable[[str], None] = field(default=lambda _name: None)
    forecast_cycle: Callable[[str], None] = field(default=lambda _name: None)
    ha_base_url: str = ""
    ha_token: str = ""
    ha_entity_refresh: Callable[[], Awaitable[None]] = field(
        default=lambda: __import__("asyncio").sleep(0)
    )
    # Enter the on-deck PIN keypad (kind=="pin").
    keypad_enter: Callable[[], None] = field(default=lambda: None)
    # Handle a keypad key press (kind=="keypad"); arg is the keypad_key value.
    keypad_press: Callable[[str], Awaitable[None]] = field(
        default=lambda _k: __import__("asyncio").sleep(0)
    )


async def run_action(spec: ActionSpec, ctx: ActionContext, long_press: bool = False) -> str:
    """Perform the side effect for a pressed key. Returns a short status line.

    Handlers are intentionally forgiving: a failed HTTP call returns a readable
    message rather than raising, so one bad press cannot take the daemon down.
    """
    base = ctx.base_url.rstrip("/")

    if spec.kind == "status":
        # A status key with a target view doubles as a deep link: glance at the
        # live count, press, and the kiosk jumps to the matching list. Without a
        # target view it stays a plain refresh, exactly as before. A missing or
        # unreachable display just means no navigation happened, never an error.
        opened = False
        if spec.target_path:
            opened = await ctx.navigate(spec.target_path)
        await ctx.refresh()
        return "opened" if opened else "refreshed"

    if spec.kind == "trigger" and spec.name == "commit":
        try:
            r = await ctx.client.post(f"{base}/pending/commit", json={})
            if r.status_code == 200:
                imported = int(r.json().get("imported", 0))
                await ctx.refresh()
                return f"committed {imported}"
            return f"commit failed ({r.status_code})"
        except Exception as e:  # noqa: BLE001 - surface, never crash
            return f"commit error: {e}"

    if spec.kind == "trigger" and spec.name == "cooked":
        face = await mark_current_recipe_cooked(ctx.client, base)
        await ctx.refresh()
        return face

    if spec.kind == "nav":
        ok = await ctx.navigate(spec.target_path)
        return "opened" if ok else "no display"

    if spec.kind in ("info", "clock"):
        # An info/clock key may carry a target view (e.g. meal_today -> the meal
        # plan). With one it deep-links the kiosk; without one (the clock) the
        # press is a harmless no-op so the key never feels dead.
        if spec.target_path:
            opened = await ctx.navigate(spec.target_path)
            return "opened" if opened else "no display"
        return "tick"

    if spec.kind == "system" and spec.name == "brightness":
        pct = ctx.cycle_brightness()
        return f"brightness {pct}%"

    if spec.kind == "system" and spec.name == "page_next":
        ctx.page_next()
        return "next page"

    if spec.kind == "system" and spec.name == "page_prev":
        ctx.page_prev()
        return "prev page"

    if spec.kind == "timer":
        ctx.timer_press(spec.name, long_press)
        return f"{spec.name} {'reset' if long_press else '+1min'}"

    if spec.kind == "pin":
        ctx.keypad_enter()
        return "keypad"

    if spec.kind == "keypad":
        await ctx.keypad_press(spec.keypad_key)
        return f"keypad {spec.keypad_key}"

    if spec.kind == "weather":
        # A press cycles to the next stat (temp, feels-like, humidity, wind);
        # the data itself is refreshed on its own timer, not on every tap.
        ctx.weather_cycle(spec.name)
        return "weather stat cycled"

    if spec.kind == "forecast":
        # A press advances to the next forecast day, wrapping around.
        ctx.forecast_cycle(spec.name)
        return "forecast day cycled"

    if spec.kind == "ha_entity":
        entity_id = spec.ha_entity_id
        service = spec.ha_service
        if not entity_id or not service or not ctx.ha_base_url or not ctx.ha_token:
            return "ha_entity: not configured"
        domain, svc = (service.split(".", 1) + ["turn_on"])[:2]
        try:
            import httpx
            url = f"{ctx.ha_base_url.rstrip('/')}/api/services/{domain}/{svc}"
            async with httpx.AsyncClient(timeout=5.0) as ha:
                r = await ha.post(
                    url,
                    json={"entity_id": entity_id},
                    headers={"Authorization": f"Bearer {ctx.ha_token}",
                             "Content-Type": "application/json"},
                )
            await ctx.ha_entity_refresh()
            return f"{entity_id} -> {service} ({r.status_code})"
        except Exception as e:  # noqa: BLE001
            return f"ha error: {e}"

    if spec.kind == "shopping_add":
        face = await add_shopping_item(ctx.client, base, spec.item)
        await ctx.refresh()
        return face

    if spec.kind == "macro":
        # Run each named child action in order, reusing the same dispatcher so
        # the macro behaves exactly as pressing those keys one after another.
        # A name that does not resolve is skipped; a nested macro is skipped too
        # so a macro can never trigger another macro (no recursion loops). The
        # first child that raises stops the run, surfaced as a clear face.
        ran = 0
        for child_name in spec.macro_actions:
            child = resolve(child_name)
            if child is None or child.kind == "macro":
                continue
            try:
                await run_action(child, ctx, long_press=long_press)
            except Exception as e:  # noqa: BLE001 - stop on the first hard error
                return f"macro error: {e}"
            ran += 1
        return f"Ran {ran}"

    return ""
