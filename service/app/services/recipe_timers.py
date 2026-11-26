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

# Cooking actions a timer can belong to, in their plain (imperative) form. The
# label names the one nearest before the duration, so "Heat the oven and boil
# the noodles for 8 minutes" is "Boil noodles", not "Heat oven".
_ACTIONS = frozenset({
    "bake", "blanch", "blend", "boil", "braise", "brine", "broil", "brown", "char",
    "chill", "cook", "cool", "cure", "deep-fry", "defrost", "dehydrate", "fry",
    "grill", "heat", "infuse", "knead", "marinate", "melt", "microwave", "mix",
    "parboil", "poach", "preheat", "process", "proof", "prove", "pulse",
    "reduce", "refrigerate", "reheat", "rest", "rise", "roast", "saute", "scald",
    "sear", "set", "simmer", "sit", "smoke", "soak", "stand", "steam", "steep",
    "stew", "stir", "stir-fry", "toast", "beat", "whip", "whisk", "freeze",
    "thaw", "caramelize", "caramelise", "sweat", "soften", "bloom", "drain",
    "dry", "warm", "air-fry", "pressure-cook", "slow-cook", "ferment",
    "sauté", "griddle", "barbecue", "bbq", "flip", "turn", "cover",
})
# Words that end the object of an action: "Boil the noodles until tender".
_OBJECT_END = frozenset({
    "for", "until", "at", "to", "about", "approximately", "or", "and", "then",
    "over", "under", "with", "without", "in", "on", "into", "onto", "while",
    "before", "after", "uncovered", "covered", "per", "each", "a", "side",
    "minutes", "minute", "hours", "hour", "seconds", "second", "more", "another",
    "longer", "if", "when", "so", "till", "from", "by", "as", "of", "is", "are",
})
# Where one instruction ends and the next begins inside a step.
_CLAUSE_SPLIT = re.compile(r"[.;:!?]\s+|[.;:!?]$|,\s*then\b|\bthen\b|,\s*and\b|\(|\)", re.I)
_WORD_RE = re.compile(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'-]*|,")
_LET = frozenset({"let", "allow", "leave"})


def _action_word(word: str) -> str | None:
    """The plain form of a cooking action, or None. "Bake" and "sauté" are
    actions; "baking" is not (a participle is a detail, "cook, stirring")."""
    w = word.lower().strip("'")
    if w in _ACTIONS:
        return "saute" if w == "sauté" else w
    return None


def _clause_head(text: str, at: int) -> str:
    """The words of the instruction a duration at ``at`` belongs to, from
    where that instruction starts up to the duration."""
    start = 0
    for m in _CLAUSE_SPLIT.finditer(text, 0, at):
        start = m.end()
    return text[start:at]


def _label_at(text: str, at: int, max_object: int = 2, max_chars: int = 40) -> str:
    """Name the action a duration at ``at`` belongs to: the cooking verb
    nearest before it in the same instruction, plus a word or two of what it
    is done to ("Boil noodles", "Bake", "Let dough rise"). Without a known
    verb it is the instruction's first word and its object, then "Timer"."""
    # Numbers never match a word, so a temperature ahead of the duration
    # ("Heat the oven to 375 F and boil ...") does not end the search early.
    words = _WORD_RE.findall(_clause_head(text, at))
    verb_at = None
    for i in range(len(words) - 1, -1, -1):
        if _action_word(words[i]):
            verb_at = i
            break
    if verb_at is None:
        verb_at = next((i for i, w in enumerate(words)
                        if w != "," and w.lower() not in _LABEL_STOP
                        and w.lower() not in _UNIT_SECONDS), None)
        if verb_at is None:
            return "Timer"
    start = verb_at
    # "Let the dough rise": the leading "let" belongs to the action.
    for k in range(max(0, verb_at - 4), verb_at):
        if words[k].lower() in _LET and "," not in words[k:verb_at]:
            start = k
            break
    kept = [(_action_word(words[start]) or words[start].lower())]
    kept += [w.lower() for w in words[start + 1:verb_at + 1]
             if w != "," and w.lower() not in _LABEL_STOP]
    taken = 0
    for w in words[verb_at + 1:]:
        lw = w.lower()
        if (taken >= max_object or lw == "," or lw in _OBJECT_END or lw in _UNIT_SECONDS
                or lw.endswith("ly") or lw.endswith("ing")
                or lw in ("it", "them", "everything", "all")):
            break
        if lw in _LABEL_STOP or lw == "mixture":
            continue
        kept.append(lw)
        taken += 1
    label = " ".join(kept).strip()
    label = label[:1].upper() + label[1:]
    if len(label) > max_chars:
        label = label[:max_chars].rsplit(" ", 1)[0].rstrip()
    return label or "Timer"


def _range_upper(value: str) -> float:
    """Return the chosen number from a value token. For a range ("10-12",
    "10 to 12") we deliberately take the UPPER bound (cook a touch longer rather
    than under). A single number returns itself."""
    parts = re.split(r"\s*(?:-|–|to)\s*", value.strip())
    return float(parts[-1])


def _label_from_step(step: str, max_words: int = 4, max_chars: int = 40) -> str:
    """The label for a step's first duration (or the whole step when it has
    none). Kept for callers that label a step rather than one duration."""
    text = step or ""
    m = _CHUNK_RE.search(text) or _WORDED_RE.search(text)
    return _label_at(text, m.start() if m else len(text), max_chars=max_chars)


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
            suggestions.append((_label_at(text, matches[i].start()), seconds))
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
                suggestions.append((_label_at(text, m.start()), seconds))

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


# --- Picking one suggestion from what was said --------------------------------

# Words around a timer request that do not say which timer.
_ASK_FILLER = frozenset({
    "the", "a", "an", "my", "our", "this", "that", "it", "its", "them", "timer",
    "timers", "for", "start", "set", "begin", "run", "please", "now", "one", "step",
    "to", "of", "on", "in", "and", "i", "we", "want", "need", "let", "lets", "let's",
    "go", "do", "can", "you", "time", "clock", "countdown",
})


def _stem(word: str) -> str:
    """A rough stem so "baking", "baked" and "bake" meet: ing/ed/es/s and a
    trailing e come off, and a doubled last letter is undone ("chopping")."""
    w = word.lower().strip("'")
    for suffix in ("ing", "ed", "es", "s"):
        if len(w) > len(suffix) + 2 and w.endswith(suffix):
            w = w[: -len(suffix)]
            break
    if len(w) > 3 and w[-1] == w[-2] and w[-1] not in "aeiouls":
        w = w[:-1]
    if len(w) > 3 and w.endswith("e"):
        w = w[:-1]
    return w


def _ask_words(text) -> list[str]:
    words = re.findall(r"[a-zà-ÿ']+", str(text or "").lower())
    return [_stem(w) for w in words if w not in _ASK_FILLER]


def _hits(asked: list[str], words: list[str]) -> int:
    have = set(words)
    return sum(1 for w in asked if w in have
               or any(len(w) >= 4 and len(o) >= 4 and (o.startswith(w) or w.startswith(o))
                      for o in have))


def choice_row(s: dict) -> dict:
    """A suggestion as one of several to choose from (steps from 1)."""
    return {"label": s["label"], "seconds": s["seconds"], "step": s["step_index"] + 1}


def choose_suggestion(suggestions, steps, step: int | None = None, query: str = "",
                      label: str = "") -> dict:
    """Pick the one suggested timer a request means. ``step`` counts from 1;
    ``query`` is the words that were said ("baking it", "the crust");
    ``label`` names a button's own timer exactly.

    {"result": "match", "suggestion": s} for one, {"result": "ambiguous",
    "choices": [...]} for several, {"result": "not_found", "reason": ...}
    where reason is no_timers, no_timer_in_step or no_match. Pure."""
    rows = [s for s in (suggestions or []) if isinstance(s, dict)]
    if not rows:
        return {"result": "not_found", "reason": "no_timers"}
    if step is not None:
        rows = [s for s in rows if s.get("step_index") == int(step) - 1]
        if not rows:
            return {"result": "not_found", "reason": "no_timer_in_step", "step": int(step)}
    if label:
        exact = [s for s in rows if str(s.get("label", "")).lower() == str(label).lower()]
        if exact:
            rows = exact
    asked = _ask_words(query)
    if asked:
        scored = []
        for s in rows:
            on_label = _hits(asked, _ask_words(s.get("label")))
            idx = s.get("step_index", -1)
            text = steps[idx] if isinstance(steps, list) and 0 <= idx < len(steps) else ""
            on_step = _hits(asked, _ask_words(text))
            scored.append((on_label * 2 + on_step, s))
        best = max(sc[0] for sc in scored)
        if best > 0:
            rows = [sc[1] for sc in scored if sc[0] == best]
        elif step is None:
            return {"result": "not_found", "reason": "no_match"}
    if len(rows) == 1:
        return {"result": "match", "suggestion": rows[0]}
    return {"result": "ambiguous", "choices": [choice_row(s) for s in rows[:6]]}


def running_timer_for(suggestion: dict, timers) -> dict | None:
    """The timer already counting down for this suggestion (same name, same
    length), or None. A finished one does not count: it can be started again."""
    want = str(suggestion.get("label", "")).strip().lower()
    for t in timers or []:
        if not isinstance(t, dict) or t.get("expired") or t.get("state") == "ringing":
            continue
        if (str(t.get("label", "")).strip().lower() == want
                and abs(float(t.get("total_seconds") or 0)
                        - float(suggestion.get("seconds") or 0)) < 1):
            return t
    return None


def next_note(steps, step_index: int, max_chars: int = 120) -> str:
    """What the "Time is up" screen says after a step's timer: the step that
    follows ("Next: Lower the oven to 350 F."), or "" after the last."""
    if not isinstance(steps, list) or not 0 <= step_index + 1 < len(steps):
        return ""
    text = " ".join(str(steps[step_index + 1]).split())
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0].rstrip(",;:") + "..."
    return f"Next: {text}" if text else ""
