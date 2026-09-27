"""Pure duration extraction for the active recipe (FoodAssistant-96h0).

Cooking steps name durations in loose natural language ("simmer 20 minutes",
"bake for 1 hour", "1 hr 30 min", "rest 1.5 hours", "an hour"). This module
pulls those durations out of a step string and turns them into ready-to-start
timer suggestions (a short label plus a duration in seconds) that a surface can
hand to the timer service. It does NOT create timers and never touches process
state, so it stays pure and unit-testable.

Conservative by design: we only fire on a number that is glued to a time unit
(seconds/minutes/hours and their abbreviations), so oven temperatures
("350 degrees"), quantities ("2 cups"), and servings never become timers. When
a value is a range ("10-12 minutes") we take the UPPER bound, since a cook would
rather the timer ring a little late than pull food early. A borderline phrase
that does not clearly carry a time unit is intentionally skipped.
"""
from __future__ import annotations

import re

# Unit names (and common abbreviations) mapped to seconds-per-unit. Longer keys
# are matched first so "hr" does not swallow the "h" of "hour" mid-word.
_UNIT_SECONDS = {
    "hours": 3600.0,
    "hour": 3600.0,
    "hrs": 3600.0,
    "hr": 3600.0,
    "h": 3600.0,
    "minutes": 60.0,
    "minute": 60.0,
    "mins": 60.0,
    "min": 60.0,
    "m": 60.0,
    "seconds": 1.0,
    "second": 1.0,
    "secs": 1.0,
    "sec": 1.0,
    "s": 1.0,
}

# A number that may be a decimal ("1.5") or a range ("10-12", "10 to 12").
_NUMBER = r"\d+(?:\.\d+)?"
_RANGE = rf"{_NUMBER}(?:\s*(?:-|–|to)\s*{_NUMBER})?"

# Build a unit alternation, longest first, so "hours" wins over "hour"/"hr"/"h".
_UNIT_ALT = "|".join(sorted(_UNIT_SECONDS, key=len, reverse=True))

# One "<number><unit>" chunk, e.g. "90 seconds", "1.5 hours", "30 min", "1 hr".
# The unit must be a whole word (\b) so "minutes" does not match inside a longer
# token and "m" does not match the "m" in "marinade".
_CHUNK_RE = re.compile(
    rf"(?P<value>{_RANGE})\s*(?P<unit>{_UNIT_ALT})\b",
    re.IGNORECASE,
)

# A bare worded duration with no digits, e.g. "an hour", "half an hour".
_WORDED_RE = re.compile(
    r"\b(?P<qty>an?|half\s+an?)\s+(?P<unit>hour|minute|hours|minutes)\b",
    re.IGNORECASE,
)

# Filler words we drop when building a label so it reads as an action phrase.
_LABEL_STOP = {
    "for", "the", "a", "an", "about", "approximately", "until", "at", "to",
    "of", "on", "in", "degrees", "degree",
}


def _range_upper(value: str) -> float:
    """Return the chosen number from a value token. For a range ("10-12",
    "10 to 12") we deliberately take the UPPER bound (cook a touch longer rather
    than under). A single number returns itself."""
    parts = re.split(r"\s*(?:-|–|to)\s*", value.strip())
    return float(parts[-1])


def _label_from_step(step: str, max_words: int = 4, max_chars: int = 40) -> str:
    """Derive a short human label from a step: the leading verb plus object,
    skipping filler words, truncated to a few words / a char cap. Falls back to
    "Timer" when the step has no usable words."""
    # The action phrase lives before the first number; everything after is
    # durations, temps, or quantities. Cut there so "degrees"/units never leak.
    head = re.split(r"\d", step, maxsplit=1)[0]
    words = re.findall(r"[A-Za-z']+", head)
    kept: list[str] = []
    for w in words:
        lw = w.lower()
        # Stop at a time-unit word: "Simmer the sauce" beats "...for minutes".
        if lw in _UNIT_SECONDS:
            break
        # Skip filler ("for", "the", "an"...) anywhere; it never earns a slot.
        if lw in _LABEL_STOP:
            continue
        kept.append(w)
        if len(kept) >= max_words:
            break
    label = " ".join(kept).strip()
    if not label:
        return "Timer"
    label = label[0].upper() + label[1:]
    if len(label) > max_chars:
        label = label[:max_chars].rstrip()
    return label


def parse_step_durations(step: str) -> list[tuple[str, int]]:
    """Extract cookable durations from a single step string.

    Returns a list of (label, seconds) suggestions, one per distinct duration
    found, in the order they appear. Adjacent hour+minute chunks ("1 hr 30 min",
    "1 hour 30 minutes") are summed into one suggestion. Returns [] when the step
    carries no time-bearing number.

    seconds is an int (durations are whole seconds for a timer). Temperatures and
    quantities are ignored because they are not glued to a time unit.
    """
    text = (step or "").strip()
    if not text:
        return []

    label = _label_from_step(text)
    suggestions: list[tuple[str, int]] = []

    # Walk numeric "<value><unit>" chunks left to right, merging chunks that are
    # directly adjacent (only whitespace/"and" between them) into one duration so
    # "1 hr 30 min" is a single 5400s timer, not two.
    matches = list(_CHUNK_RE.finditer(text))
    i = 0
    while i < len(matches):
        total = 0.0
        j = i
        while j < len(matches):
            m = matches[j]
            total += _range_upper(m.group("value")) * _UNIT_SECONDS[m.group("unit").lower()]
            if j + 1 < len(matches):
                gap = text[m.end():matches[j + 1].start()]
                # Merge only across trivial connectors, never across other words.
                if re.fullmatch(r"[\s,]*(?:and)?[\s,]*", gap, re.IGNORECASE):
                    j += 1
                    continue
            break
        seconds = int(round(total))
        if seconds > 0:
            suggestions.append((label, seconds))
        i = j + 1

    # Worded durations only when no numeric chunk fired, to avoid double-counting
    # something like "an hour (60 minutes)".
    if not suggestions:
        for m in _WORDED_RE.finditer(text):
            unit = m.group("unit").lower().rstrip("s")
            base = _UNIT_SECONDS["hour"] if unit == "hour" else _UNIT_SECONDS["minute"]
            factor = 0.5 if m.group("qty").lower().startswith("half") else 1.0
            seconds = int(round(base * factor))
            if seconds > 0:
                suggestions.append((label, seconds))

    return suggestions


def suggestions_for_recipe(active: dict | None) -> list[dict]:
    """Walk an active recipe's steps and return ordered timer suggestions.

    `active` is the serialized active recipe (as from current_recipe.get_active())
    or None. Returns a list of {label, seconds, step_index} in step order, empty
    when no recipe is active or no step carries a duration. This stays a pure
    function of its argument so the caller owns the registry lookup.
    """
    if not active:
        return []
    out: list[dict] = []
    for idx, step in enumerate(active.get("steps") or []):
        for label, seconds in parse_step_durations(str(step)):
            out.append({"label": label, "seconds": seconds, "step_index": idx})
    return out
