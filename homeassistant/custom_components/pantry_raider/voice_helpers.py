"""Pure helpers for voice, timers and per-satellite routing.

Free of any Home Assistant import, like helpers.py, so the repo's pure-logic
tests cover the parsing and the routing rules without Home Assistant
installed. voice.py wraps these in services, intents and sentences.
"""

from __future__ import annotations

import re
from typing import Any

# Where a voice satellite's timers go.
TIMERS_HOME_ASSISTANT = "home_assistant"
TIMERS_PANTRY_RAIDER = "pantry_raider"

# Keys inside entry.options["voice"].
OPT_VOICE = "voice"
OPT_RECIPE_SENTENCES = "recipe_sentences"
OPT_TIMER_SENTENCES = "timer_sentences"
OPT_RING = "ring"
OPT_SATELLITES = "satellites"
SAT_TIMERS = "timers"
SAT_RECIPES = "recipes"
SAT_SCREEN = "screen"

# How a finished timer is announced: every RING_EVERY seconds, RING_TIMES at
# most (ten minutes), until it is dismissed or cancelled anywhere.
RING_EVERY = 20
RING_TIMES = 30

_NUMBER_WORDS = {
    "zero": 0, "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
_UNITS = {
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600, "h": 3600,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60, "m": 60,
    "second": 1, "seconds": 1, "sec": 1, "secs": 1, "s": 1,
}
_FILLER = {"and", "a", "an", "the", "my", "for", "of", "timer", "timers", "please",
           "set", "start", "create", "called", "named", "to", "on", "in"}
MAX_TIMER_SECONDS = 24 * 3600


def voice_options(options: Any) -> dict[str, Any]:
    """The voice block of an entry's options, with every default filled in."""
    raw = options.get(OPT_VOICE) if isinstance(options, dict) or hasattr(options, "get") else None
    raw = raw if isinstance(raw, dict) else {}
    sats = raw.get(OPT_SATELLITES)
    return {
        OPT_RECIPE_SENTENCES: raw.get(OPT_RECIPE_SENTENCES, True) is not False,
        # Off unless chosen: many houses already have something answering
        # "set a timer", and two listeners for one sentence collide.
        OPT_TIMER_SENTENCES: raw.get(OPT_TIMER_SENTENCES, False) is True,
        OPT_RING: raw.get(OPT_RING, True) is not False,
        OPT_SATELLITES: dict(sats) if isinstance(sats, dict) else {},
    }


def routing_for(options: Any, satellite: str | None) -> dict[str, Any]:
    """What one voice satellite is set to do. A satellite nobody has set up
    keeps its timers in Home Assistant, may ask for recipes, and has no
    screen of its own."""
    sats = voice_options(options)[OPT_SATELLITES]
    row = sats.get(str(satellite or "")) if satellite else None
    row = row if isinstance(row, dict) else {}
    timers = row.get(SAT_TIMERS)
    return {
        SAT_TIMERS: timers if timers in (TIMERS_HOME_ASSISTANT, TIMERS_PANTRY_RAIDER)
        else TIMERS_HOME_ASSISTANT,
        SAT_RECIPES: row.get(SAT_RECIPES, True) is not False,
        SAT_SCREEN: str(row.get(SAT_SCREEN) or ""),
    }


def with_satellite(options: Any, satellite: str, timers: str, recipes: bool,
                   screen: str) -> dict[str, Any]:
    """A copy of the entry options with one satellite's choices saved."""
    out = dict(options)
    voice = voice_options(options)
    sats = dict(voice[OPT_SATELLITES])
    sats[satellite] = {
        SAT_TIMERS: timers if timers == TIMERS_PANTRY_RAIDER else TIMERS_HOME_ASSISTANT,
        SAT_RECIPES: bool(recipes),
        SAT_SCREEN: str(screen or ""),
    }
    voice[OPT_SATELLITES] = sats
    out[OPT_VOICE] = voice
    return out


def _words(text: Any) -> list[str]:
    text = str(text or "").lower().replace("-", " ")
    # "1h30m", "90s": pull digits and letters apart.
    text = re.sub(r"(\d)([a-z])", r"\1 \2", text)
    text = re.sub(r"([a-z])(\d)", r"\1 \2", text)
    return re.findall(r"[a-z]+|\d+(?:\.\d+)?", text)


def split_timer_request(text: Any) -> tuple[int, str]:
    """(seconds, name) from what was said: "10 minute pasta", "an hour and a
    half", "pasta for ten minutes". Seconds is 0 when no length was found; the
    name is whatever words are left once the length and filler are gone."""
    words = _words(text)
    total = 0.0
    used: set[int] = set()
    i = 0
    while i < len(words):
        # A run of number words or one figure, then a unit.
        j = i
        value: float | None = None
        if re.fullmatch(r"\d+(?:\.\d+)?", words[j]):
            value = float(words[j])
            j += 1
        else:
            acc = None
            while j < len(words) and words[j] in _NUMBER_WORDS:
                if words[j] in ("a", "an") and acc is not None:
                    break
                acc = (acc or 0) + _NUMBER_WORDS[words[j]]
                j += 1
            value = None if acc is None else float(acc)
        if value is not None and j < len(words) and words[j] in _UNITS:
            unit = _UNITS[words[j]]
            total += value * unit
            used.update(range(i, j + 1))
            j += 1
            # "an hour and a half", "two and a half minutes" said the long way.
            if words[j:j + 3] == ["and", "a", "half"]:
                total += unit / 2
                used.update(range(j, j + 3))
                j += 3
            i = j
            continue
        if words[i:i + 2] == ["half", "an"] and words[i + 2:i + 3] == ["hour"]:
            total += 1800
            used.update(range(i, i + 3))
            i += 3
            continue
        if (value is not None and words[j:j + 3] == ["and", "a", "half"]
                and j + 3 < len(words) and words[j + 3] in _UNITS):
            total += (value + 0.5) * _UNITS[words[j + 3]]
            used.update(range(i, j + 4))
            i = j + 4
            continue
        i += 1
    name = " ".join(w for k, w in enumerate(words) if k not in used and w not in _FILLER)
    seconds = int(round(total))
    if seconds > MAX_TIMER_SECONDS:
        seconds = 0
    return seconds, name.strip()


def parse_duration(value: Any) -> int:
    """Seconds from a number (already seconds), "HH:MM:SS", or spoken text.
    0 when there is nothing usable."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value) if 0 < value <= MAX_TIMER_SECONDS else 0
    if isinstance(value, dict):
        try:
            total = (int(value.get("hours", 0) or 0) * 3600
                     + int(value.get("minutes", 0) or 0) * 60
                     + int(value.get("seconds", 0) or 0))
        except (TypeError, ValueError):
            return 0
        return total if 0 < total <= MAX_TIMER_SECONDS else 0
    text = str(value or "").strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return parse_duration(float(text))
    clock = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
    if clock:
        a, b, c = int(clock.group(1)), int(clock.group(2)), clock.group(3)
        total = a * 3600 + b * 60 + int(c) if c is not None else a * 60 + b
        return total if 0 < total <= MAX_TIMER_SECONDS else 0
    return split_timer_request(text)[0]


def spoken_duration(seconds: Any) -> str:
    """"1 hour and 5 minutes", "45 seconds": a length said aloud."""
    try:
        total = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        total = 0
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    # Past a minute nobody wants the seconds unless that is all there is.
    if h and s:
        m, s = m + (1 if s >= 30 else 0), 0
    parts = []
    for count, word in ((h, "hour"), (m, "minute"), (s, "second")):
        if count:
            parts.append(f"{count} {word}{'' if count == 1 else 's'}")
    if not parts:
        return "0 seconds"
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def timer_label(name: Any, seconds: int) -> str:
    """What a timer is called on screen: its name, or its length."""
    text = " ".join(str(name or "").split())
    if text:
        return text[:1].upper() + text[1:]
    # "10 minute timer", the way it is said, not "10 minutes timer".
    said = re.sub(r"(hour|minute|second)s\b", r"\1", spoken_duration(seconds))
    return said.replace(" and ", " ").replace(",", "").capitalize() + " timer"


def _plain(text: Any) -> str:
    return " ".join(w for w in _words(text) if w not in ("the", "my", "a", "an", "timer"))


def pick_timers(timers: Any, name: Any = "", origin: str = "",
                ringing: bool | None = None) -> list[dict[str, Any]]:
    """The timers a spoken request most likely means.

    ``ringing`` True keeps only finished timers, False only running ones.
    With a name, the timers whose label matches it. Without one, the timers
    this satellite set if it set any, else all of them."""
    rows = [t for t in (timers or []) if isinstance(t, dict)]
    if ringing is not None:
        rows = [t for t in rows if bool(t.get("expired")) is ringing]
    want = _plain(name)
    if want:
        exact = [t for t in rows if _plain(t.get("label")) == want]
        if exact:
            return exact
        return [t for t in rows if want in _plain(t.get("label"))
                or _plain(t.get("label")) in want and _plain(t.get("label"))]
    if origin:
        mine = [t for t in rows if t.get("origin") == origin]
        if mine:
            return mine
    return rows


def status_speech(timers: Any) -> str:
    """What to say for "how long is left"."""
    rows = [t for t in (timers or []) if isinstance(t, dict)]
    running = sorted((t for t in rows if not t.get("expired")),
                     key=lambda t: t.get("remaining_seconds", 0))
    done = [t for t in rows if t.get("expired")]
    if not running and not done:
        return "No timers are running."
    said = [f"{t.get('label', 'Timer')} has {spoken_duration(t.get('remaining_seconds', 0))} left"
            for t in running[:4]]
    said += [f"{t.get('label', 'Timer')} is done" for t in done[:2]]
    return ". ".join(said) + "."


def should_ring(event: Any, options: Any) -> str:
    """The assist_satellite entity to ring for a "finished" timer event, or
    "" when this event is not one to ring for."""
    if not isinstance(event, dict) or event.get("type") != "finished":
        return ""
    if not voice_options(options)[OPT_RING]:
        return ""
    timer = event.get("timer")
    origin = str(timer.get("origin") or "") if isinstance(timer, dict) else ""
    return origin if origin.startswith("assist_satellite.") else ""


def ring_message(timer: Any) -> str:
    label = str(timer.get("label") or "").strip() if isinstance(timer, dict) else ""
    if not label:
        return "Your timer is done."
    if label.lower().endswith("timer"):
        return f"Your {label[:1].lower() + label[1:]} is done."
    return f"Your {label} timer is done."
