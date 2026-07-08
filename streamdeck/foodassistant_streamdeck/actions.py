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

import inspect
import time
from pathlib import Path
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Optional

def _resolve_clock_format(clock_format: str) -> str:
    """Resolve a clock_format setting to "12" or "24" for the deck face.

    Mirrors the app's rule (config.format_time_of_day): "12" reads a 12-hour
    face, "24" reads 24-hour, and "auto" (the keep-current-behaviour value)
    reads 24-hour like every other surface. An unknown or missing value also
    falls back to 24-hour, so an older config.toml that never carried the field
    keeps rendering exactly as it did before.
    """
    return "12" if clock_format == "12" else "24"


def _clock_time_str(now: Any, clock_format: str = "auto") -> str:
    """The time line of a clock key face, per ``clock_format``. Pure.

    24-hour reads "15:42". 12-hour reads a compact "3:42P" / "3:42A": a single
    letter AM/PM marker (not "PM") so the face stays within the key's narrow
    width budget while still agreeing with the app's 12-hour reading.
    """
    if _resolve_clock_format(clock_format) == "12":
        hour12 = now.hour % 12 or 12
        marker = "A" if now.hour < 12 else "P"
        return f"{hour12}:{now.minute:02d}{marker}"
    return now.strftime("%H:%M")


def _clock_label(now: Any, show_date: bool = True, clock_format: str = "auto") -> str:
    """Render a clock key face from a ``datetime``-like ``now``.

    Top line is the wall-clock time formatted per ``clock_format`` (24-hour
    "15:42" or a compact 12-hour "3:42P"); when ``show_date`` is set a second
    line carries an abbreviated weekday and day-of-month (e.g. "Thu 26"). Pure:
    it formats whatever ``now`` it is handed, so the controller can pass the
    current local time each fast-loop tick and tests can pass a fixed datetime.
    """
    time_str = _clock_time_str(now, clock_format)
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


# Stages a plain timer key cycles through, one short press per stage. A press
# after the last stage stops the timer. Preset keys (timer_minutes > 0) skip
# the cycle and load their whole duration on a single press instead.
TIMER_CYCLE_MINUTES: tuple[int, ...] = (5, 10, 15, 30, 60)


class TimerState:
    """Per-key view of one shared server timer.

    The server registry (POST/GET/DELETE /timers) is the single source of
    truth: a key press creates or cancels a server timer, and this object only
    remembers which timer id the key is bound to plus the timer's shared
    ``deadline_epoch``. Remaining time is always deadline_epoch minus the
    deck's own wall clock, the same satellite-shareable formula every other
    surface uses, so the deck, the web UI, and satellites agree on the
    countdown.

    When the app cannot be reached a press falls back to a deck-local run
    (``timer_id`` None) so the kitchen timer still works offline; the face and
    expiry behave identically, there is just nothing to show elsewhere.

    When the countdown reaches zero ``alerting`` flips True (via ``tick``) and
    the key blinks until the next short press dismisses it.
    """

    def __init__(self) -> None:
        self.timer_id: Optional[int] = None  # server timer id; None = local run
        self.deadline_epoch: float = 0.0     # wall-clock deadline; 0 = idle
        self.alerting: bool = False
        self.cycle_idx: int = -1             # -1 = not in the press cycle

    # -- state ---------------------------------------------------------------

    def is_running(self, now: Optional[float] = None) -> bool:
        if self.alerting or self.deadline_epoch <= 0:
            return False
        now = time.time() if now is None else now
        return self.deadline_epoch > now

    def remaining_seconds(self, now: Optional[float] = None) -> int:
        if self.alerting or self.deadline_epoch <= 0:
            return 0
        now = time.time() if now is None else now
        return max(0, int(self.deadline_epoch - now))

    def alert_active(self) -> bool:
        return self.alerting

    # -- transitions -----------------------------------------------------

    def bind(self, timer: dict) -> None:
        """Adopt a server timer dict (id + deadline_epoch) as this key's
        countdown. The face then renders from the shared deadline alone."""
        try:
            self.timer_id = int(timer.get("id"))
        except (TypeError, ValueError):
            self.timer_id = None
        try:
            self.deadline_epoch = float(timer.get("deadline_epoch") or 0.0)
        except (TypeError, ValueError):
            self.deadline_epoch = 0.0
        self.alerting = False

    def start_local(self, seconds: float, now: Optional[float] = None) -> None:
        """Offline fallback: run the countdown on this deck alone."""
        now = time.time() if now is None else now
        self.timer_id = None
        self.deadline_epoch = now + max(0.0, float(seconds))
        self.alerting = False

    def clear(self) -> None:
        """Back to idle. Cancelling the bound server timer, if any, is the
        caller's job (the registry owns the countdown)."""
        self.timer_id = None
        self.deadline_epoch = 0.0
        self.alerting = False
        self.cycle_idx = -1

    def next_cycle_seconds(self) -> int:
        """Advance the press cycle and return the next stage in seconds.

        Returns 0 after the last stage (the press stops the timer) and resets
        the cycle so the following press starts over at the first stage.
        """
        self.cycle_idx += 1
        if self.cycle_idx >= len(TIMER_CYCLE_MINUTES):
            self.cycle_idx = -1
            return 0
        return TIMER_CYCLE_MINUTES[self.cycle_idx] * 60

    def tick(self, now: Optional[float] = None) -> bool:
        """Return True (and set alerting) if the timer just expired."""
        now = time.time() if now is None else now
        if not self.alerting and 0 < self.deadline_epoch <= now:
            self.alerting = True
            self.deadline_epoch = 0.0
            self.cycle_idx = -1
            return True
        return False

    # -- face --------------------------------------------------------------

    def label(self, base_label: str, now: Optional[float] = None) -> str:
        if self.alerting:
            return "Done!"
        if self.deadline_epoch <= 0:
            return base_label
        secs = self.remaining_seconds(now)
        if secs <= 0:
            return "Done!"
        return f"{secs // 60}:{secs % 60:02d}"

    # The expired-alert colours the key blinks between: a bright red on the
    # "on" phase and a dim red on the "off" phase, so the key flashes until the
    # alert is dismissed.
    _ALERT_BRIGHT = "#ef4444"
    _ALERT_DIM = "#450a0a"

    def color(self, base_color: str, blink_phase: int = 0,
              now: Optional[float] = None) -> str:
        if self.alerting:
            return self.alert_color(blink_phase)
        if self.deadline_epoch <= 0:
            return base_color
        return "#f59e0b" if self.remaining_seconds(now) < 60 else "#0d9488"

    def alert_color(self, blink_phase: int) -> str:
        """Colour for an expired alert at the given blink phase.

        Even phases are bright, odd phases are dim, so successive poll ticks
        flash the key. Only meaningful while ``alerting`` is True.
        """
        return self._ALERT_BRIGHT if blink_phase % 2 == 0 else self._ALERT_DIM


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


# Longest item name a shopping-check key shows on its face before truncation, so
# a long product name stays glanceable on a single key during a quick unpack.
SHOPPING_CHECK_LABEL_MAX: int = 12


def shopping_item_name(item: dict) -> str:
    """Best-effort display name for a Mealie shopping-list item.

    Mealie items carry a free-text ``note`` (the typed line), and structured
    items also carry a ``display`` string and a ``food`` object with a ``name``.
    Prefer the most human form, falling back through the others, so a key face
    reads naturally whether the item was typed or pulled from a recipe. Pure, so
    it is unit-testable without any network.
    """
    if not isinstance(item, dict):
        return ""
    food = item.get("food") or {}
    name = (food.get("name") if isinstance(food, dict) else "") or ""
    display = item.get("display") or ""
    note = item.get("note") or ""
    for candidate in (display, name, note):
        text = str(candidate or "").strip()
        if text:
            return text
    return ""


def shopping_check_key_specs(
    payload: dict,
    slots: int,
    default_labels: Optional[list[str]] = None,
) -> list[dict]:
    """Map a /mealie/shopping payload onto up to ``slots`` check-key descriptors.

    Returns a list of exactly ``slots`` dicts, one per check key, each shaped
    ``{"label": str, "item_id": Optional[str], "item": Optional[dict]}``. Only
    unchecked items are offered (already-checked-off items drop out), in the
    payload's own order, so the keys mirror the top of the list the way the web
    UI shows it. The first N slots carry a cleaned, face-safe item name plus the
    item id and the full item dict (needed to PUT the check-off back); any
    remaining slots are empty placeholders with ``item_id`` None and a stock
    fallback label so they render as a neutral, inert key.

    ``default_labels`` supplies the stock per-slot label for the empty trailing
    slots; a missing or short list falls back to a blank label. The function is
    pure: no clock, no I/O, so it is fully unit-testable. An empty or malformed
    payload yields all-empty placeholders.
    """
    slots = max(0, int(slots))
    default_labels = list(default_labels or [])
    items = []
    if isinstance(payload, dict):
        raw = payload.get("items")
        if isinstance(raw, list):
            items = [i for i in raw if isinstance(i, dict) and not i.get("checked")]
    specs: list[dict] = []
    for i in range(slots):
        fallback = default_labels[i] if i < len(default_labels) else ""
        if i < len(items):
            item = items[i]
            label = clean_timer_label(
                shopping_item_name(item), SHOPPING_CHECK_LABEL_MAX
            ) or "Item"
            specs.append({
                "label": label,
                "item_id": str(item.get("id") or "") or None,
                "item": item,
            })
        else:
            specs.append({"label": fallback, "item_id": None, "item": None})
    return specs


SHOPPING_CHECK_KEY_COLOR: str = "#0f766e"


def shopping_check_action_specs(
    key_specs: list[dict],
) -> tuple[list[ActionSpec], dict[str, dict]]:
    """Turn shopping_check_key_specs output into deck ActionSpecs plus an item map.

    Returns ``(specs, items)`` where ``specs`` is one ActionSpec (kind
    ``shopping_check``) per real item slot, in order, and ``items`` maps each
    spec's name to the full item dict so the controller can check the right item
    off when that key is pressed. Placeholder slots (no bound item) are skipped,
    so the caller pads the page with blanks. Names are stable per slot index
    (``shopping_check_0`` ..) so a redraw keeps the binding. Pure, so it is
    unit-testable without any device.
    """
    specs: list[ActionSpec] = []
    items: dict[str, dict] = {}
    for i, ks in enumerate(key_specs):
        item = ks.get("item") if isinstance(ks, dict) else None
        if not item or not ks.get("item_id"):
            continue
        name = f"shopping_check_{i}"
        items[name] = item
        specs.append(ActionSpec(
            name=name, label=ks.get("label", "Item"),
            color=SHOPPING_CHECK_KEY_COLOR, kind="shopping_check",
            icon="cart-check",
        ))
    return specs, items


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


def weather_error_face(error: str) -> str:
    """Short tile face for a failed weather fetch, from the endpoint's error.

    The old blanket "No signal" hid the actual problem: a per-key location the
    geocoder cannot find looks identical to a dead network (FoodAssistant-17tb).
    Pure keyword mapping so it is unit-testable; unknown errors keep the old
    face so nothing regresses.
    """
    e = (error or "").lower()
    if "find that location" in e:
        return "Bad\nlocation"
    if "could not reach" in e or "lookup failed" in e or "timed out" in e:
        return "No\nnetwork"
    if "http" in e:
        return "Weather\nbusy"
    if "parse" in e or "forecast data" in e:
        return "No\ndata"
    return "No signal"


class WeatherState:
    """Fetches and caches current weather from the app's /ui/weather/data.

    The app prefers Open-Meteo (honouring weather_api_base) and falls back to
    wttr.in; the deck previously called wttr.in directly, which is frequently
    rate-limited and left the tiles on "No signal" while the web UI's weather
    worked fine on the same device (FoodAssistant-34k7).

    ``location`` is any city name, zip code, or lat,lon string. When empty,
    the server picks the saved location (or geolocates by IP on the wttr.in
    fallback). ``units`` is 'f' (Fahrenheit) or 'c' (Celsius). ``base_url`` is
    the app's base URL, the same one the controller uses for every other call.

    The weather key cycles through a list of stat renderers (current temp plus
    condition at index 0, then feels-like, humidity, and wind) and the forecast
    key cycles through the cached forecast days (today at index 0). Both indices
    sit at 0 by default so an un-pressed deck renders exactly as before; a press
    advances the matching index and stamps ``last_interaction`` so the idle loop
    can return it to the default after ``WEATHER_AUTO_RESET_SECS``.
    """

    def __init__(self, location: str = "", units: str = "f",
                 base_url: str = "http://127.0.0.1:9284") -> None:
        self.location = location
        self.units = units.lower()
        self.base_url = base_url
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

    def _set_no_signal(self, error: str = "") -> None:
        """Show why the tile has no forecast: a short reason face derived from
        the endpoint's error string, or the classic "No signal" when there is
        no detail to give."""
        face = weather_error_face(error)
        self._label = face
        self._color = "#6b7280"
        self._error = True
        self._fc_label = face

    def apply_forecast(self, forecast: dict) -> None:
        """Populate the tile fields from the app's normalized forecast dict
        ({current: {temp, feels, humidity, wind, desc, ...}, days: [...]}).
        Pure, so the mapping is unit-testable without HTTP."""
        cur = forecast.get("current") or {}
        unit_sym = "F" if self.units == "f" else "C"
        temp = cur.get("temp", "?")
        desc = str(cur.get("desc", "") or "")
        self._label = f"{temp}°{unit_sym} {desc}".rstrip()
        self._color = "#1e40af"
        self._error = False
        # The stat renderers still read the wttr-style keys; synthesize them
        # from the normalized current block. Wind is already in the requested
        # units' convention (mph for f, km/h for c), so both keys get it.
        feels = str(cur.get("feels", "?"))
        wind = str(cur.get("wind", "?"))
        self._cond = {
            "FeelsLikeF": feels, "FeelsLikeC": feels,
            "humidity": str(cur.get("humidity", "?")),
            "windspeedMiles": wind, "windspeedKmph": wind,
        }
        # Cache every returned day so the forecast key can cycle through them;
        # tag the first three with friendly names (the rest fall back to the
        # bare date).
        tags = ("Today", "Tmrw", "Day 3")
        days: list[dict[str, str]] = []
        for i, day in enumerate(forecast.get("days") or []):
            if not isinstance(day, dict):
                continue
            days.append({
                "hi": str(day.get("hi", "?")),
                "lo": str(day.get("lo", "?")),
                "tag": tags[i] if i < len(tags) else str(day.get("date", "")),
            })
        self._forecast_days = days
        if days:
            self._fc_label = f"H{days[0]['hi']} L{days[0]['lo']}"
            self._fc_color = "#0e7490"

    async def refresh(self, client=None) -> None:
        """Fetch the forecast from the app and update the tile fields.

        ``client`` lets the controller share one AsyncClient across every
        weather tile in a refresh pass instead of building a connection pool
        per tile. Each request carries a tight connect timeout so one stalled
        tile cannot eat the whole poll budget (FoodAssistant-17tb).
        """
        try:
            import httpx
            base = (self.base_url or "http://127.0.0.1:9284").rstrip("/")
            params: dict[str, str] = {"units": self.units}
            if self.location.strip():
                params["location"] = self.location.strip()
            url = f"{base}/ui/weather/data"
            timeout = httpx.Timeout(15.0, connect=5.0)
            if client is not None:
                r = await client.get(url, params=params, timeout=timeout)
            else:
                async with httpx.AsyncClient(timeout=timeout) as own:
                    r = await own.get(url, params=params)
            if r.status_code != 200:
                self._set_no_signal()
                return
            data = r.json()
            forecast = data.get("forecast") if data.get("ok") else None
            if not isinstance(forecast, dict):
                # The endpoint says why ({ok: false, error}); surface it as a
                # short reason face instead of a blanket "No signal".
                error = data.get("error") if isinstance(data, dict) else ""
                self._set_no_signal(str(error or ""))
                return
            self.apply_forecast(forecast)
        except Exception as e:  # noqa: BLE001
            self._set_no_signal(f"could not reach the app ({e.__class__.__name__})")
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
    weather_units: str = ""  # for kind=="weather" overrides: per-key units ("f"/
                             # "c"); empty falls back to the global units.
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
    scale_factor: float = 0.0  # for kind=="recipe_scale": the factor (0.5/1.0/2.0)
                             # POSTed to /current-recipe/scale on press.
    power_on: bool = False   # for kind=="display_power": True wakes the display,
                             # False blanks it (via the host bridge).
    bridge_path: str = ""    # for kind=="bridge_action": host-bridge path to POST
                             # on press (e.g. "/kiosk/restart").
    camera_name: str = ""    # for kind=="camera"/"camera_full" overrides: which
                             # configured camera to show (matched by name). Empty
                             # falls back to the first configured camera.


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
    "scale_half": "arrows-angle-contract",
    "scale_1x": "arrow-repeat",
    "scale_2x": "arrows-angle-expand",
    "screen_off": "lightbulb-off",
    "screen_on": "lightbulb",
    "health": "heart-pulse",
    "convert": "calculator",
    "timers_view": "clock-history",
    "kiosk_restart": "arrow-clockwise",
    "update": "cloud-arrow-down",
    "reboot": "power",
    "camera": "camera-video",
    "camera_full": "camera",
    "scan_mode": "upc-scan",
    "shopping_check": "cart-check",
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
        label="Pantry",
        color="#b45309",
        kind="nav",
        target_path="ui/add",
        description="Open the Manage Pantry page on the attached display.",
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
        description="Shared countdown timer (press to cycle: 5/10/15/30/60 min "
        "or stop; hold to reset). Shows on every screen too.",
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
    "scale_half": ActionSpec(
        name="scale_half",
        label="0.5x",
        color="#9333ea",
        kind="recipe_scale",
        scale_factor=0.5,
        description="Halve the active Current Recipe's serving scale. "
        "No-op when no recipe is active.",
    ),
    "scale_1x": ActionSpec(
        name="scale_1x",
        label="1x",
        color="#9333ea",
        kind="recipe_scale",
        scale_factor=1.0,
        description="Reset the active Current Recipe to its original scale. "
        "No-op when no recipe is active.",
    ),
    "scale_2x": ActionSpec(
        name="scale_2x",
        label="2x",
        color="#9333ea",
        kind="recipe_scale",
        scale_factor=2.0,
        description="Double the active Current Recipe's serving scale. "
        "No-op when no recipe is active.",
    ),
    "screen_off": ActionSpec(
        name="screen_off",
        label="Screen\nOff",
        color="#334155",
        kind="display_power",
        power_on=False,
        description="Blank the kiosk display now (via the host bridge).",
    ),
    "screen_on": ActionSpec(
        name="screen_on",
        label="Screen\nOn",
        color="#475569",
        kind="display_power",
        power_on=True,
        description="Wake the kiosk display now (via the host bridge).",
    ),
    "health": ActionSpec(
        name="health",
        label="Health",
        color="#15803d",
        kind="health",
        target_path="setup",
        description="Pi power/thermal/disk health from the host bridge. Green "
        "when clear, amber when warnings are present. Press to open Setup.",
    ),
    "convert": ActionSpec(
        name="convert",
        label="Convert",
        color="#0f766e",
        kind="nav",
        target_path="ui/convert",
        description="Open the unit-conversion page on the attached display.",
    ),
    "timers_view": ActionSpec(
        name="timers_view",
        label="Timers",
        color="#0d9488",
        kind="nav",
        target_path="ui/timers",
        description="Open the shared timers page on the attached display.",
    ),
    "kiosk_restart": ActionSpec(
        name="kiosk_restart",
        label="Kiosk",
        color="#b45309",
        kind="bridge_action",
        bridge_path="/kiosk/restart",
        description="Restart the kiosk browser (via the host bridge).",
    ),
    "update": ActionSpec(
        name="update",
        label="Update",
        color="#1d4ed8",
        kind="bridge_action",
        bridge_path="/update",
        description="Pull and redeploy the latest app + Stream Deck (host bridge "
        "OTA). Best-effort: the request is fired and the deck does not block.",
    ),
    "reboot": ActionSpec(
        name="reboot",
        label="Reboot",
        color="#b91c1c",
        kind="bridge_action",
        bridge_path="/reboot",
        description="Reboot the host (via the host bridge). Best-effort: the "
        "request is fired and the deck does not block.",
    ),
    "camera": ActionSpec(
        name="camera",
        label="Camera",
        color="#0f172a",
        kind="camera",
        target_path="ui/camera",
        description="Live camera snapshot. The key face shows the latest frame; "
        "press to open the full feed on the attached display.",
    ),
    "camera_full": ActionSpec(
        name="camera_full",
        label="Camera\nFull",
        color="#0f172a",
        kind="camera_full",
        description="Take over the whole deck with a single live camera frame "
        "sliced across every key. Press any key to exit.",
    ),
    "scan_mode": ActionSpec(
        name="scan_mode",
        label="Scan\nMode",
        color="#0369a1",
        kind="scan_mode",
        description="Cycle the barcode scanner mode (Stock, Use, Shop, Audit). "
        "The same physical scanner then adds to inventory, consumes stock, adds "
        "to the shopping list, or counts stock. The key face shows the active mode.",
    ),
    "shopping_check": ActionSpec(
        name="shopping_check",
        label="Check\nOff",
        color="#0f766e",
        kind="shopping_check_page",
        description="Open a quick-check page whose keys are the top items on the "
        "Mealie shopping list. Press an item to check it off (handy while "
        "unpacking groceries). A Back key returns to the normal layout.",
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


# Full-colour icon set: each action maps to a bundled colour emoji PNG (see
# assets/emoji/<slug>.png, rendered from Noto Color Emoji). The render layer
# composites these when the deck icon style is "color", instead of tinting the
# monochrome Bootstrap glyph. Slugs that repeat (cart, house, refresh) reuse the
# same icon. Keep in step with the files under assets/emoji/.
ACTION_EMOJI: dict[str, str] = {
    "expiring": "alarm", "pending": "hourglass", "commit": "outbox",
    "add": "plus", "inventory": "package", "cook": "fire",
    "recipes": "book", "mealplan": "calendar", "shopping": "cart",
    "defaults": "clipboard", "brightness": "bright",
    "page_next": "next", "page_prev": "prev",
    "timer_1": "timer", "timer_2": "timer", "timer_3": "timer",
    "weather": "weather", "forecast": "thermometer",
    "ha_1": "house", "ha_2": "house", "ha_3": "house", "ha_4": "house", "ha_5": "house",
    "pin": "lock", "clock": "clock", "shopping_count": "cart",
    "ready": "check", "meal_today": "plate", "cooked": "cooking",
    "timer_eggs": "egg", "timer_pasta": "pasta", "timer_rice": "rice",
    "scale_half": "down", "scale_1x": "refresh", "scale_2x": "up",
    "screen_off": "sleep", "screen_on": "sun", "health": "heart",
    "convert": "abacus", "timers_view": "stopwatch",
    "kiosk_restart": "refresh", "update": "inbox", "reboot": "plug",
    "shopping_add": "cart", "macro": "bolt", "shopping_check": "cart",
}


def emoji_for(name: str) -> str:
    """Return the colour-emoji icon slug for an action, or "" if none."""
    return ACTION_EMOJI.get(name, "")


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
    "shopping_check",
    "meal_today",
    "cooked",
    "timer_1",
    "timer_2",
    "timer_3",
    "timer_eggs",
    "timer_pasta",
    "timer_rice",
    "timers_view",
    "convert",
    "clock",
    "weather",
    "forecast",
    "health",
    "camera",
    "screen_off",
    "brightness",
]


_GROUP_BY_KIND = {
    "status": "Status", "trigger": "Actions", "nav": "Navigation",
    "system": "System", "timer": "Timers", "weather": "Weather",
    "forecast": "Weather", "ha_entity": "Home Assistant",
    "clock": "Info", "info": "Info",
    "recipe_scale": "Recipe", "display_power": "System",
    "health": "System", "bridge_action": "System",
    "camera": "Camera", "camera_full": "Camera",
    "scan_mode": "Actions", "ha_service": "Home Assistant",
    "shopping_check_page": "Actions", "shopping_check": "Actions",
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
    "ha_action", "timer", "weather", "shopping_add", "macro", "camera", "media", "default"
)

_OVERRIDE_DEFAULT_COLORS = {
    "ha_action": _HA_STATE_COLOR_OFF,
    "timer": "#0d9488",
    "weather": "#1e40af",
    "shopping_add": "#0f766e",
    "macro": "#6d28d9",
    "camera": "#0f172a",
    "media": "#7c3aed",
}

# Media transport actions a "media" override can bind to, mapped to the Home
# Assistant media_player service and the glyph shown on the key. Each service
# takes only the entity_id, so the existing ha_service dispatch handles them.
MEDIA_ACTIONS: dict[str, dict] = {
    "play_pause": {"service": "media_player.media_play_pause", "icon": "play-circle", "label": "Play/Pause"},
    "next":       {"service": "media_player.media_next_track", "icon": "skip-forward", "label": "Next"},
    "previous":   {"service": "media_player.media_previous_track", "icon": "skip-backward", "label": "Previous"},
    "volume_up":  {"service": "media_player.volume_up", "icon": "volume-up", "label": "Volume +"},
    "volume_down": {"service": "media_player.volume_down", "icon": "volume-down", "label": "Volume -"},
    "stop":       {"service": "media_player.media_stop", "icon": "stop-circle", "label": "Stop"},
}

_OVERRIDE_DEFAULT_ICONS = {
    "ha_action": "house",
    "timer": "stopwatch",
    "weather": "cloud-sun",
    "shopping_add": "cart-plus",
    "macro": "collection-play",
    "camera": "camera-video",
    "media": "play-circle",
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
        # Optional per-key units ("f"/"c"); empty leaves it blank so the
        # controller falls back to the global units when building the per-key
        # WeatherState. A non-empty value is normalised to a lowercase letter.
        units = str(override.get("units", "")).strip().lower()
        if units not in ("f", "c"):
            units = ""
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
                kind="forecast", weather_location=location,
                weather_units=units, icon=fc_icon,
            )
        return ActionSpec(
            name=name, label=label or "Weather", color=color, kind="weather",
            weather_location=location, weather_units=units, icon=icon,
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

    if otype == "media":
        # A media transport key calls a Home Assistant media_player service on a
        # chosen entity. It is stateless (kind "ha_service"): no on/off polling,
        # just a fixed face that fires the service on press.
        entity_id = str(override.get("entity_id", "")).strip()
        action = str(override.get("action", "play_pause")).strip()
        meta = MEDIA_ACTIONS.get(action) or MEDIA_ACTIONS["play_pause"]
        if not entity_id:
            return None
        if not icon or icon == _OVERRIDE_DEFAULT_ICONS.get("media"):
            icon = meta["icon"]
        return ActionSpec(
            name=name, label=label or meta["label"], color=color, kind="ha_service",
            ha_entity_id=entity_id, ha_service=meta["service"], icon=icon,
        )

    if otype == "camera":
        # Pick which configured camera a camera key shows (by name; empty = the
        # first camera). The "full" flag takes over the whole deck on press
        # instead of drawing a single-key snapshot face.
        cam_name = str(override.get("camera", override.get("camera_name", ""))).strip()
        full = _truthy(override.get("full"))
        kind = "camera_full" if full else "camera"
        if not label:
            label = cam_name or ("Camera\nFull" if full else "Camera")
        return ActionSpec(
            name=name, label=label, color=color, kind=kind,
            camera_name=cam_name, icon=icon,
            target_path="" if full else "ui/camera",
        )

    return None


def _entity_from_camera_proxy(url: str) -> tuple[str, str]:
    """Recover (entity_id, ha_base) from an HA camera_proxy URL, or ("", "").

    Lets a camera saved before entity-based proxying (its snapshot URL had the
    token baked in) still be fetched with a bearer header instead.
    """
    if not url or "/api/camera_proxy" not in url:
        return "", ""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
    except Exception:
        return "", ""
    parts = parsed.path.split("/")
    entity = ""
    for i, seg in enumerate(parts):
        if seg in ("camera_proxy", "camera_proxy_stream") and i + 1 < len(parts):
            entity = parts[i + 1]
            break
    if not entity:
        return "", ""
    base = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
    return entity, base


def camera_snapshot_target(cam: dict, ha_base: str, ha_token: str) -> tuple[str, Optional[dict]]:
    """Resolve a camera dict to (snapshot_url, headers) the deck can fetch.

    Home Assistant cameras (an ``ha_entity``, or a legacy ``/api/camera_proxy``
    URL) are fetched from HA with an ``Authorization: Bearer`` header, since HA
    rejects the long-lived token in the query string. Everything else uses the
    stored ``snapshot_url`` with no extra headers. Pure, so it is unit-testable.
    """
    if not isinstance(cam, dict):
        return "", None
    base = (ha_base or "").rstrip("/")
    entity = str(cam.get("ha_entity", "")).strip()
    snap = str(cam.get("snapshot_url", "")).strip()
    if not entity and "/api/camera_proxy" in snap:
        entity, parsed_base = _entity_from_camera_proxy(snap)
        if not base:
            base = parsed_base
    if entity and base and ha_token:
        from urllib.parse import quote
        return f"{base}/api/camera_proxy/{quote(entity, safe='')}", {"Authorization": f"Bearer {ha_token}"}
    return snap, None


def camera_target_path(base_path: str, camera_name: str) -> str:
    """Build the kiosk URL a camera key opens, carrying the requested camera.

    A camera key names which configured camera it shows; that name must travel
    to the kiosk so the camera page opens that feed rather than the first one
    (FoodAssistant-f230). When a name is set, append it as a ``?cam=`` query
    param (the page resolves a name or index to a camera). An empty name leaves
    the path untouched, so the page falls back to camera 0. Pure and testable.
    """
    path = (base_path or "ui/camera").strip()
    name = (camera_name or "").strip()
    if not name:
        return path
    from urllib.parse import urlencode
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}{urlencode({'cam': name})}"


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
    ``override_to_spec``). Entries with a negative or unparseable slot, or whose
    type cannot be built, are skipped, so a malformed entry never displaces a
    valid one. When two overrides target the same slot the last one wins.

    A slot at or beyond ``key_count`` is deliberately KEPT: the layout paginates
    when more keys are configured than fit one deck page, and the web editor
    numbers grid slots continuously across pages, so a custom key placed on page
    two carries a slot larger than the deck's key count. Dropping those here is
    what silently lost page-two custom keys (FoodAssistant-n0r1); pagination and
    range checks belong to ``layout.apply_overrides``.
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
        if slot < 0:
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


async def fetch_timers(client: Any, base_url: str) -> list[dict]:
    """Fetch every running shared timer from the server, or [] on any failure.

    Each entry is the server's timer dict (label, remaining_seconds, running,
    expired, deadline_epoch, ...). Used to mirror a recipe timer started on
    another surface onto the matching deck key. A network or service error
    collapses to [] so the timer keys quietly keep their last state.
    """
    base = base_url.rstrip("/")
    try:
        r = await client.get(f"{base}/timers")
        if r.status_code == 200:
            data = r.json()
            out = data.get("timers", [])
            return out if isinstance(out, list) else []
    except Exception:  # noqa: BLE001 - surface as no timers, never crash
        pass
    return []


def _timer_deadline(timer: dict) -> float:
    """The shared wall-clock deadline of a server timer dict, or 0.0."""
    try:
        return float(timer.get("deadline_epoch") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def sync_timer_bindings(
    bindings: dict[str, TimerState],
    key_labels: dict[str, str],
    server_timers: list[dict],
    now_epoch: float,
) -> bool:
    """Reconcile per-key timer bindings against the polled server registry.

    The registry is the single source of truth, so this runs on every status
    poll: a bound timer that has vanished from the server (cancelled from the
    web UI or another surface) clears its key back to idle, a bound timer
    still present refreshes the key's deadline (clock drift, or the timer was
    restarted elsewhere), and an idle key adopts a running, unclaimed server
    timer whose cleaned label matches the key's, so a "Pasta" timer started
    from the web lands on the deck's Pasta key. Local-only runs (timer_id
    None with a live deadline, the offline fallback) and undismissed expiry
    alerts keep their face. Returns True when any face changed so the caller
    can redraw. Pure: the caller fetches the timer list and supplies the
    clock, so it is unit-testable without I/O.
    """
    changed = False
    by_id: dict[int, dict] = {}
    for t in server_timers or []:
        if isinstance(t, dict) and isinstance(t.get("id"), int):
            by_id[t["id"]] = t
    claimed = {b.timer_id for b in bindings.values() if b.timer_id is not None}

    for binding in bindings.values():
        if binding.timer_id is None:
            continue
        t = by_id.get(binding.timer_id)
        if t is None:
            # Cancelled elsewhere: clear the face (and any expired alert).
            if binding.deadline_epoch > 0 or binding.alerting:
                changed = True
            binding.clear()
            continue
        if binding.alerting or t.get("expired"):
            # Expiry is driven locally by tick(); an expired server timer stays
            # listed until dismissed, so leave the blinking alert alone.
            continue
        deadline = _timer_deadline(t)
        if deadline > 0 and abs(deadline - binding.deadline_epoch) > 1.0:
            binding.deadline_epoch = deadline
            changed = True

    for name, want_label in key_labels.items():
        binding = bindings.get(name)
        if binding is None or binding.timer_id is not None:
            continue
        if binding.deadline_epoch > 0 or binding.alerting:
            continue  # a local-only run or an undismissed alert owns this face
        want = clean_timer_label(want_label)
        if not want:
            continue
        for t in server_timers or []:
            if not isinstance(t, dict) or t.get("expired"):
                continue
            tid = t.get("id")
            if not isinstance(tid, int) or tid in claimed:
                continue
            if clean_timer_label(t.get("label", "")) != want:
                continue
            if _timer_deadline(t) <= now_epoch:
                continue
            binding.bind(t)
            claimed.add(tid)
            changed = True
            break
    return changed


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


async def post_deck_confirmation(client: Any, base_url: str, message: str) -> None:
    """Queue a short on-screen confirmation for a deck action (FoodAssistant-rdlo).

    A non-navigating press (a shopping quick-add, a scanner-mode switch) leaves
    the kitchen screen unchanged, so the user cannot tell it worked. The
    controller talks to the app and the app relays a toast to the kiosk that
    shows even when on-screen Home Assistant events are turned off (it is deck
    feedback, not HA traffic). Best-effort by design: no client, an older app
    without the endpoint, or any network error is swallowed so a press never
    fails just because the confirmation could not be shown."""
    msg = str(message or "").strip()
    if client is None or not msg:
        return
    base = base_url.rstrip("/")
    try:
        await client.post(f"{base}/events/confirm", json={"message": msg})
    except Exception:  # noqa: BLE001 - a confirmation is never worth a crash
        pass


async def fetch_shopping_items(client: Any, base_url: str) -> dict:
    """Fetch the current Mealie shopping list payload, or an empty one on error.

    Returns the app's /mealie/shopping shape ``{list, items, ...}`` so the caller
    can map the top items onto check keys via ``shopping_check_key_specs``. Any
    network or service failure degrades to ``{"items": []}`` so the check keys
    quietly empty out rather than crashing the poll loop.
    """
    base = base_url.rstrip("/")
    try:
        r = await client.get(f"{base}/mealie/shopping")
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, dict) else {"items": []}
    except Exception:  # noqa: BLE001 - surface as no items, never crash
        pass
    return {"items": []}


async def check_shopping_item(client: Any, base_url: str, item: dict) -> str:
    """Check a Mealie shopping-list item off the list. Returns a short face.

    PUTs the full item back to /mealie/shopping/items/{id} with ``checked`` set
    True (the same shape the web UI sends to toggle an item), so the app forwards
    the update to Mealie. Returns "Checked" on success, "Empty" when there is no
    item to check, and "Failed" on any error, so a press always degrades to a
    readable face rather than crashing the controller.
    """
    if not isinstance(item, dict):
        return "Empty"
    item_id = str(item.get("id") or "").strip()
    if not item_id:
        return "Empty"
    base = base_url.rstrip("/")
    payload = dict(item)
    payload["checked"] = True
    try:
        r = await client.put(
            f"{base}/mealie/shopping/items/{item_id}", json=payload,
        )
        return "Checked" if r.status_code == 200 else "Failed"
    except Exception:  # noqa: BLE001 - never crash a press
        return "Failed"


def _timer_from_response(resp: Any) -> Optional[dict]:
    """Extract the created timer dict from a 200 timer-create response."""
    if resp.status_code != 200:
        return None
    try:
        timer = (resp.json() or {}).get("timer")
    except Exception:  # noqa: BLE001 - malformed body means no timer
        return None
    return timer if isinstance(timer, dict) else None


async def create_server_timer(
    client: Any, base_url: str, label: str, seconds: float,
) -> Optional[dict]:
    """Create a shared server timer (POST /timers). Returns its dict or None.

    The returned dict carries the id and deadline_epoch the deck key binds to,
    so every surface (web UI, satellites, this deck) renders the same
    countdown. Best-effort: any failure returns None so the press can fall
    back to a deck-local countdown."""
    base = base_url.rstrip("/")
    try:
        r = await client.post(f"{base}/timers", json={"label": label, "seconds": seconds})
        return _timer_from_response(r)
    except Exception:  # noqa: BLE001 - shared timer is best-effort
        return None


async def create_recipe_timer(
    client: Any, base_url: str, step_index: Any = None,
    label: str = "", seconds: Any = None,
) -> Optional[dict]:
    """Create a shared server timer from a recipe suggestion. Returns its dict
    or None.

    Posts to /current-recipe/timers/start so every surface (web UI,
    satellites) sees the same countdown. Identify the suggestion by step_index
    or label; seconds, when given, is passed through. Best-effort: any failure
    returns None so a press can fall back to a deck-local countdown.
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
        return _timer_from_response(r)
    except Exception:  # noqa: BLE001 - shared timer is best-effort
        return None


async def cancel_server_timer(client: Any, base_url: str, timer_id: Any) -> bool:
    """Cancel a shared server timer (DELETE /timers/{id}). Returns True when
    the timer is gone (a 404 means someone else already removed it, which is
    the same outcome). Best-effort: any network failure returns False."""
    try:
        tid = int(timer_id)
    except (TypeError, ValueError):
        return False
    base = base_url.rstrip("/")
    try:
        r = await client.delete(f"{base}/timers/{tid}")
        return r.status_code in (200, 404)
    except Exception:  # noqa: BLE001 - cancel is best-effort
        return False


async def scale_current_recipe(client: Any, base_url: str, factor: float) -> str:
    """Scale the active Current Recipe by ``factor``. Returns a short face.

    Posts the factor to /current-recipe/scale. Returns a brief confirmation
    ("0.5x") on success, "No recipe" when nothing is active (the endpoint 404s),
    and "Failed" on any other error, so a press always degrades to a readable
    face rather than crashing the controller.
    """
    base = base_url.rstrip("/")
    try:
        r = await client.post(f"{base}/current-recipe/scale", json={"factor": factor})
    except Exception:  # noqa: BLE001 - never crash a press
        return "Failed"
    if r.status_code == 404:
        return "No recipe"
    if r.status_code == 200:
        # Render the factor compactly: "0.5x", "1x", "2x".
        text = f"{factor:g}"
        return f"{text}x"
    return "Failed"


# Where the host bridge drops its shared auth token (FoodAssistant-pxcm).
# The bridge writes INSTALL_DIR/data/bridge-token at startup; the deck runs on
# the same host, so it reads the same well-known path on both a pi_hosted and
# a pi_remote install. Overridable per device via bridge_token_path in
# config.toml.
DEFAULT_BRIDGE_TOKEN_PATH = "/opt/foodassistant/data/bridge-token"

# One cached token per path; a 401 from the bridge clears it so the next call
# re-reads a rotated token from disk. A missing file is never cached (the
# bridge may write it after the deck starts).
_bridge_token_cache: dict[str, str] = {}


def bridge_headers(token_path: str = "") -> dict:
    """Auth header for a bridge call: the shared token when readable, else {}."""
    path = token_path or DEFAULT_BRIDGE_TOKEN_PATH
    token = _bridge_token_cache.get(path, "")
    if not token:
        try:
            token = Path(path).read_text().strip()
        except OSError:
            return {}
        if token:
            _bridge_token_cache[path] = token
    return {"X-Bridge-Token": token} if token else {}


def invalidate_bridge_token(token_path: str = "") -> None:
    """Drop the cached token so the next bridge call re-reads the file."""
    _bridge_token_cache.pop(token_path or DEFAULT_BRIDGE_TOKEN_PATH, None)


async def bridge_post(client: Any, host_bridge_url: str, path: str,
                      timeout: float = 0.0, token_path: str = "") -> str:
    """POST to a host-bridge path, best-effort. Returns a short face.

    ``host_bridge_url`` is the bridge base (empty off-Pi, so the call is a
    no-op returning "No bridge"). A short ``timeout`` lets slow operations
    (reboot, update) fire without blocking the deck: a read timeout is treated
    as a successful "Sent" rather than a failure, since the request reached the
    bridge even if the reply never comes. Other errors return "Failed".
    """
    base = (host_bridge_url or "").rstrip("/")
    if not base:
        return "No bridge"
    kwargs: dict[str, Any] = {"json": {}, "headers": bridge_headers(token_path)}
    if timeout:
        kwargs["timeout"] = timeout
    try:
        r = await client.post(f"{base}{path}", **kwargs)
        if r.status_code == 401:
            # Stale token: forget it so the next press reads the fresh one.
            invalidate_bridge_token(token_path)
        return "OK" if r.status_code == 200 else "Failed"
    except Exception:  # noqa: BLE001
        # A timeout on a slow op (reboot/update) means the request reached the
        # bridge; treat it as sent rather than a hard failure.
        if timeout:
            return "Sent"
        return "Failed"


_HEALTH_COLOR_OK = "#15803d"
_HEALTH_COLOR_WARN = "#b45309"
_HEALTH_COLOR_UNKNOWN = "#6b7280"


class HealthState:
    """Caches the host bridge's system-health summary for the health key.

    Refreshed from GET {host_bridge_url}/system/health on the status loop. The
    key is green when the bridge reports ok with no warnings, amber when any
    warning is present, and a neutral grey when the bridge is unreachable (off-Pi
    or not yet up), so the key is never misleading and never crashes the loop.
    """

    def __init__(self) -> None:
        self._fetched_at: float = 0.0
        self._reachable: bool = False
        self._warnings: int = 0

    def age_seconds(self) -> float:
        return time.monotonic() - self._fetched_at

    def label(self, base_label: str) -> str:
        if not self._fetched_at or not self._reachable:
            return base_label
        return "OK" if self._warnings == 0 else f"Warn {self._warnings}"

    def color(self, base_color: str) -> str:
        if not self._fetched_at or not self._reachable:
            return _HEALTH_COLOR_UNKNOWN
        return _HEALTH_COLOR_OK if self._warnings == 0 else _HEALTH_COLOR_WARN

    def apply(self, reachable: bool, warnings: int) -> None:
        """Record a poll result. Pure setter so the colour/label logic is testable."""
        self._reachable = bool(reachable)
        self._warnings = max(0, int(warnings))
        self._fetched_at = time.monotonic()

    async def refresh(self, host_bridge_url: str) -> None:
        base = (host_bridge_url or "").rstrip("/")
        if not base:
            self.apply(False, 0)
            return
        try:
            import httpx
            async with httpx.AsyncClient(timeout=2.0) as client:
                r = await client.get(f"{base}/system/health",
                                     headers=bridge_headers())
            if r.status_code == 200:
                warnings = (r.json() or {}).get("warnings") or []
                self.apply(True, len(warnings))
            else:
                self.apply(False, 0)
        except Exception:  # noqa: BLE001 - unreachable -> neutral, never crash
            self.apply(False, 0)


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
    # Handle a timer key press (kind=="timer"); args are the pressed spec's
    # name and whether it was a long press. May return an awaitable (the
    # controller's handler talks to the shared server registry); run_action
    # awaits it when it does, so a plain sync callable also works in tests.
    timer_press: Callable[[str, bool], Any] = field(default=lambda _name, _long=False: None)
    weather_refresh: Callable[[], Awaitable[None]] = field(
        default=lambda: __import__("asyncio").sleep(0)
    )
    # Advance the weather stat / forecast day cycle for a pressed widget key.
    # The arg is the pressed spec's name so per-key override widgets cycle their
    # own WeatherState. Default no-ops keep unit contexts that omit them valid.
    weather_cycle: Callable[[str], None] = field(default=lambda _name: None)
    forecast_cycle: Callable[[str], None] = field(default=lambda _name: None)
    # Paint a new scanner-mode label on the scan_mode key right away, without
    # waiting for the full status refresh round-trip. Default no-op keeps unit
    # contexts that omit it valid.
    scanner_label_set: Callable[[str], None] = field(default=lambda _label: None)
    ha_base_url: str = ""
    ha_token: str = ""
    # Base URL of the host bridge (empty off-Pi). Bridge actions POST here via
    # ctx.client; display_power, health, and bridge_action keys all use it.
    host_bridge_url: str = ""
    # Path of the bridge's shared auth token file ("" = the well-known
    # default). Passed through to bridge_post so pressed keys authenticate.
    bridge_token_path: str = ""
    ha_entity_refresh: Callable[[], Awaitable[None]] = field(
        default=lambda: __import__("asyncio").sleep(0)
    )
    # Enter the on-deck PIN keypad (kind=="pin").
    keypad_enter: Callable[[], None] = field(default=lambda: None)
    # Enter the dynamic shopping-check page (kind=="shopping_check_page").
    shopping_check_enter: Callable[[], Awaitable[None]] = field(
        default=lambda: __import__("asyncio").sleep(0)
    )
    # Check off the item bound to a dynamic shopping-check key (kind==
    # "shopping_check"). The arg is the pressed spec's name; returns a face.
    shopping_check_press: Callable[[str], Awaitable[str]] = field(
        default=lambda _name: __import__("asyncio").sleep(0, result="")
    )
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
        result = ctx.timer_press(spec.name, long_press)
        if inspect.isawaitable(result):
            await result
        return f"{spec.name} {'reset' if long_press else 'pressed'}"

    if spec.kind == "pin":
        ctx.keypad_enter()
        return "keypad"

    if spec.kind == "keypad":
        await ctx.keypad_press(spec.keypad_key)
        return f"keypad {spec.keypad_key}"

    if spec.kind == "weather":
        # A press cycles to the next stat (temp, feels-like, humidity, wind) on
        # the key face and also opens the full weather page on the attached kiosk
        # display, so the deck doubles as a remote for the screen. The data itself
        # is refreshed on its own timer, not on every tap.
        ctx.weather_cycle(spec.name)
        await ctx.navigate("ui/weather")
        return "weather"

    if spec.kind == "forecast":
        # A press advances to the next forecast day on the key face and opens the
        # full weather page on the attached kiosk display.
        ctx.forecast_cycle(spec.name)
        await ctx.navigate("ui/weather")
        return "forecast"

    if spec.kind in ("ha_entity", "ha_service"):
        # "ha_entity" toggles a stateful entity (its face tracks on/off); the
        # stateless "ha_service" (media transport keys) just fires the service.
        entity_id = spec.ha_entity_id
        service = spec.ha_service
        if not entity_id or not service or not ctx.ha_base_url or not ctx.ha_token:
            return "ha: not configured"
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
            if spec.kind == "ha_entity":
                await ctx.ha_entity_refresh()
            return f"{entity_id} -> {service} ({r.status_code})"
        except Exception as e:  # noqa: BLE001
            return f"ha error: {e}"

    if spec.kind == "recipe_scale":
        # Scale the active Current Recipe by the spec's factor. Defensive: no
        # active recipe 404s, surfaced as a short face, never a crash.
        return await scale_current_recipe(ctx.client, base, spec.scale_factor)

    if spec.kind == "display_power":
        # Wake or blank the kiosk display via the host bridge.
        path = "/display/wake" if spec.power_on else "/display/blank"
        face = await bridge_post(ctx.client, ctx.host_bridge_url, path,
                                 token_path=ctx.bridge_token_path)
        if face in ("OK", "Sent"):
            return "On" if spec.power_on else "Off"
        return face

    if spec.kind == "scan_mode":
        # Cycle the barcode scanner mode on the app; the face shows the active
        # mode. On a satellite the app forwards this to the main server, the
        # single owner of the mode, so the same press changes how every scanner
        # on the network routes. The new label is painted immediately from the
        # response; the poll loop keeps it in step afterwards.
        if ctx.client is None:
            return "scan: no client"
        try:
            r = await ctx.client.post(f"{base}/pending/scanner-mode/cycle")
            if r.status_code != 200:
                # A main server too old for the cycle endpoint, or a satellite
                # that cannot reach it, must not look like a successful press:
                # scans would keep their old routing while the key face lied.
                return f"mode cycle failed: HTTP {r.status_code}"
            data = r.json() or {}
            label = data.get("label", "")
            if label:
                ctx.scanner_label_set(label)
                # The scan_mode key face changes, but the kitchen screen does
                # not, so confirm the new mode on screen too (FoodAssistant-rdlo).
                await post_deck_confirmation(ctx.client, base, f"Scanner: {label}")
            await ctx.refresh()
            return label or "Scan"
        except Exception as e:  # noqa: BLE001
            return f"scan err: {e}"

    if spec.kind == "health":
        # The health key's live state comes from the poll loop; a press just
        # deep-links to Setup so the user can read the detail.
        opened = await ctx.navigate(spec.target_path) if spec.target_path else False
        return "opened" if opened else "no display"

    if spec.kind == "bridge_action":
        # One dispatch for kiosk_restart / update / reboot. Update and reboot are
        # slow, so fire them with a short client timeout and return immediately
        # rather than blocking the deck (a timeout is treated as "Sent").
        slow = spec.bridge_path in ("/update", "/reboot")
        timeout = 2.0 if slow else 0.0
        return await bridge_post(
            ctx.client, ctx.host_bridge_url, spec.bridge_path, timeout=timeout,
            token_path=ctx.bridge_token_path,
        )

    if spec.kind == "camera":
        # The key face is a live snapshot the controller paints; a press opens
        # the full feed on the kiosk display. The configured camera name is
        # carried through as a ?cam= query param so the kiosk opens the requested
        # camera rather than always camera 0 (FoodAssistant-f230). A missing
        # display is not an error.
        target = camera_target_path(spec.target_path or "ui/camera", spec.camera_name)
        opened = await ctx.navigate(target)
        return "opened" if opened else "no display"

    if spec.kind == "camera_full":
        # The controller toggles the full-deck overlay on press (see
        # _on_key); the handler itself is a no-op marker so dispatch never fails.
        return "Camera"

    if spec.kind == "shopping_add":
        face = await add_shopping_item(ctx.client, base, spec.item)
        if face == "Added":
            # The screen does not otherwise change, so tell the user it worked
            # (FoodAssistant-rdlo). Names the item so the toast is unambiguous.
            item = str(spec.item or "").strip()
            await post_deck_confirmation(
                ctx.client, base,
                f"Added {item} to shopping list" if item else "Added to shopping list",
            )
        await ctx.refresh()
        return face

    if spec.kind == "shopping_check_page":
        # Open the dynamic shopping quick-check page. The controller populates it
        # from the current shopping list and swaps the visible page.
        await ctx.shopping_check_enter()
        return "check"

    if spec.kind == "shopping_check":
        # A dynamic key bound to one shopping-list item: check it off (via the
        # controller, which holds the item dicts keyed by spec name) and let the
        # controller refresh the page so the checked item drops out.
        return await ctx.shopping_check_press(spec.name)

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
