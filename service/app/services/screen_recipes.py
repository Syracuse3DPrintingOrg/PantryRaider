"""The recipe open on each screen, the step each screen is on, and what was
last offered to it.

Two kitchen tablets can cook different things, so a screen keeps its own
recipe: asking "what do I do first" at one tablet answers for the recipe that
tablet is cooking, and a tablet that never opened one says so rather than
answering for another screen's. Its pages may still show the kitchen's
current recipe (``source`` "kitchen" in what GET /api/screens/recipe
returns); tapping a step or a timer there, or opening cook mode on it, makes
that recipe the tablet's own. The main install's own display (id "server")
is the server's current recipe (services/current_recipe.py) and is not kept
here. A Bandit keeps its own current recipe on the device; the copy here is
what the server sent it, so a question asked about the Bandit's screen can be
answered.

Each screen also has one current step, the step the cook is on. Tapping a
step on the screen moves it, and so does voice ("next step", "go back",
"what's step 4"), so the screen and the speaker always agree. It belongs to
the recipe it was set on: open another recipe and there is no current step
until one is chosen. The server's own current recipe (the main display, the
full interface, a request that names no screen) keeps its step under
"server".

What is kept where:

- Each screen's recipe and current step: data_dir/screen_recipes.json, the
  usual small state file (atomic write from a thread, state_write_lock,
  mtime-cached reads). Anything nobody has touched for RECIPE_HOURS is
  treated as gone, so a tablet does not offer yesterday's dinner.
- The recipes offered after "which one?": memory only, for a few minutes. It
  is about the conversation going on now, and a restart is a fine time to
  forget it.

The answer shapes built here (``describe``, ``step_answer``) are pure, so a
language model's view of a recipe is tested without a server.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict
from pathlib import Path

from . import current_recipe
from .state_lock import state_write_lock
from .state_writer import StateWriter

RECIPE_HOURS = 12
CANDIDATE_SECONDS = 300.0
MAX_SCREENS = 128
SERVER_KEY = "server"

_lock = threading.Lock()
_rows: dict[str, dict] = {}
# key -> {"step": index from 0, "of": which recipe, "set": epoch}
_steps: dict[str, dict] = {}
_mtime: int | None = None
_writer = StateWriter("screen-recipes")
# key -> (monotonic time, [{"slug", "title"}]); key is a screen id or "server".
_candidates: dict[str, tuple[float, list[dict], str]] = {}


def _key(screen_id) -> str:
    return str(screen_id or "") or SERVER_KEY


def _state_file() -> Path:
    from ..config import settings
    return Path(settings.data_dir) / "screen_recipes.json"


def _load_locked() -> None:
    global _rows, _steps, _mtime
    if _writer.busy():
        return
    try:
        sf = _state_file()
        mtime = sf.stat().st_mtime_ns
    except OSError:
        if _mtime is not None:
            _rows, _steps, _mtime = {}, {}, None
        return
    if mtime == _mtime:
        return
    try:
        data = json.loads(sf.read_text())
        rows = {}
        for sid, raw in (data.get("screens") or {}).items():
            if not (isinstance(raw, dict) and isinstance(raw.get("recipe"), dict)):
                continue
            try:
                # A row that no longer reads back as a recipe (a hand edit, a
                # damaged file) is dropped here, so a read never fails on it.
                current_recipe._serialize(current_recipe._from_stored(raw["recipe"]))
                rows[str(sid)] = {"recipe": raw["recipe"],
                                  "set": float(raw.get("set") or 0)}
            except (ValueError, TypeError, AttributeError):
                continue
        steps = {}
        for sid, raw in (data.get("steps") or {}).items():
            if not isinstance(raw, dict):
                continue
            try:
                steps[str(sid)] = {"step": int(raw.get("step", 0)),
                                   "of": str(raw.get("of") or ""),
                                   "set": float(raw.get("set") or 0)}
            except (ValueError, TypeError):
                continue
    except (OSError, ValueError, TypeError, AttributeError):
        return
    _rows, _steps = rows, steps
    _mtime = mtime


def _written(mtime: int | None) -> None:
    global _mtime
    if mtime is not None:
        _mtime = mtime


def _save_locked() -> None:
    try:
        blob = {"screens": {k: _rows[k] for k in sorted(_rows)},
                "steps": {k: _steps[k] for k in sorted(_steps)}}
        _writer.write(_state_file(),
                      json.dumps(blob).encode("utf-8") if (_rows or _steps) else None,
                      _written)
    except OSError:
        pass


def _fresh(row: dict | None, now: float) -> bool:
    return bool(row) and now - float(row.get("set") or 0) <= RECIPE_HOURS * 3600


def _prune_locked(now: float) -> None:
    for table in (_rows, _steps):
        for sid in [k for k, v in table.items() if not _fresh(v, now)]:
            del table[sid]
        while len(table) > MAX_SCREENS:
            del table[min(table, key=lambda k: table[k]["set"])]


def recipe_identity(recipe: dict | None) -> str:
    """What a current step is tied to: the recipe's id, else its title."""
    if not recipe:
        return ""
    return str(recipe.get("id") or "") or "title:" + str(recipe.get("title") or "")


def set_recipe(screen_id: str, recipe_dict: dict, keep_step: bool = False) -> dict:
    """Make ``recipe_dict`` the recipe open on one screen; the serialized
    form (scaled amounts included) comes back. The screen has no current
    step until one is chosen, unless ``keep_step`` and the step it was on
    belongs to this same recipe (a tablet taking up the kitchen's recipe it
    was already showing)."""
    normalized = current_recipe._normalize(recipe_dict)
    serialized = current_recipe._serialize(normalized)
    now = time.time()
    key = _key(screen_id)
    with _lock, state_write_lock(_state_file()):
        _load_locked()
        _rows[key] = {"recipe": asdict(normalized), "set": now}
        step = _steps.get(key)
        if not (keep_step and step and step.get("of") == recipe_identity(serialized)):
            _steps.pop(key, None)
        _prune_locked(now)
        _save_locked()
    return serialized


def get_recipe(screen_id: str) -> dict | None:
    """The recipe open on one screen (serialized), or None."""
    with _lock:
        _load_locked()
        row = _rows.get(_key(screen_id))
        if not _fresh(row, time.time()):
            return None
        return current_recipe._serialize(current_recipe._from_stored(row["recipe"]))


def clear(screen_id: str) -> bool:
    key = _key(screen_id)
    with _lock, state_write_lock(_state_file()):
        _load_locked()
        gone = _rows.pop(key, None) is not None
        gone = _steps.pop(key, None) is not None or gone
        if gone:
            _save_locked()
    return gone


def recipe_for(screen: dict | None, own: bool = False) -> tuple[dict | None, str]:
    """(recipe, where it came from) for a screen as screens.get returns it,
    or None for "no screen named". Where is "screen" for the screen's own
    recipe and "kitchen" for the kitchen's current one: the main display, no
    screen named, or a tablet that has none of its own, whose pages show the
    kitchen's. With ``own`` (an answer about one screen: its steps, its
    timers, what to do next) a tablet with none of its own has none. A Bandit
    is never given the kitchen's: it keeps its own on the device."""
    if not screen or screen.get("kind") == "server":
        return current_recipe.get_active(), "kitchen"
    mine = get_recipe(screen["id"])
    if mine is not None:
        return mine, "screen"
    if own or screen.get("kind") == "bandit":
        return None, "screen"
    return current_recipe.get_active(), "kitchen"


# --- The step each screen is on ----------------------------------------------


def current_step(screen_id, recipe: dict | None) -> int | None:
    """The step (from 0) a screen is on in ``recipe``, or None when no step
    has been chosen for that recipe there."""
    if not recipe:
        return None
    with _lock:
        _load_locked()
        row = _steps.get(_key(screen_id))
    if not _fresh(row, time.time()) or row.get("of") != recipe_identity(recipe):
        return None
    count = len(recipe.get("steps") or [])
    idx = int(row.get("step", 0))
    return idx if 0 <= idx < count else None


def set_step(screen_id, recipe: dict | None, index: int | None) -> int | None:
    """Put a screen on step ``index`` (from 0) of ``recipe``; None clears it.
    Out-of-range steps are clamped. Returns the step now current."""
    key = _key(screen_id)
    now = time.time()
    count = len((recipe or {}).get("steps") or [])
    with _lock, state_write_lock(_state_file()):
        _load_locked()
        if index is None or not count:
            if _steps.pop(key, None) is not None:
                _save_locked()
            return None
        idx = max(0, min(int(index), count - 1))
        _steps[key] = {"step": idx, "of": recipe_identity(recipe), "set": now}
        _prune_locked(now)
        _save_locked()
    return idx


def forget_step(screen_id) -> None:
    key = _key(screen_id)
    with _lock, state_write_lock(_state_file()):
        _load_locked()
        if _steps.pop(key, None) is not None:
            _save_locked()


# --- What was offered after "which one?" -------------------------------------


def remember_candidates(key: str, candidates: list[dict], page: str = "") -> None:
    """Keep what was offered after "which one?", and the page it was asked
    for ("cook" for "let's cook ..."), for the answer that comes next."""
    rows = [{"slug": str(c.get("slug")), "title": str(c.get("title") or "")}
            for c in candidates or [] if isinstance(c, dict) and c.get("slug")]
    if rows:
        _candidates[_key(key)] = (time.monotonic(), rows, str(page or ""))


def recent_candidates_page(key: str) -> tuple[list[dict], str]:
    hit = _candidates.get(_key(key))
    if not hit or time.monotonic() - hit[0] > CANDIDATE_SECONDS:
        _candidates.pop(_key(key), None)
        return [], ""
    return list(hit[1]), hit[2]


def recent_candidates(key: str) -> list[dict]:
    return recent_candidates_page(key)[0]


def forget_candidates(key: str) -> None:
    _candidates.pop(_key(key), None)


def reset() -> None:
    """Forget everything and drop the state file (tests)."""
    global _rows, _steps, _mtime
    _writer.wait_idle()
    with _lock:
        _rows = {}
        _steps = {}
        _mtime = None
        _candidates.clear()
        try:
            _state_file().unlink(missing_ok=True)
        except OSError:
            pass


# --- What a language model (or a speaker) is told ---------------------------

_FRACTIONS = ((0.125, "1/8"), (0.25, "1/4"), (1 / 3, "1/3"), (0.375, "3/8"), (0.5, "1/2"),
              (0.625, "5/8"), (2 / 3, "2/3"), (0.75, "3/4"), (0.875, "7/8"))


def amount_text(q) -> str:
    """1.5 -> "1 1/2", 0.333 -> "1/3", 2.0 -> "2", None -> ""."""
    if q is None:
        return ""
    try:
        q = float(q)
    except (TypeError, ValueError):
        return ""
    if q <= 0:
        return ""
    whole = int(q)
    part = q - whole
    if part < 0.02:
        return str(whole)
    if part > 0.98:
        return str(whole + 1)
    for value, text in _FRACTIONS:
        if abs(part - value) < 0.02:
            return f"{whole} {text}" if whole else text
    return f"{round(q, 2):g}"


def ingredient_line(ing: dict) -> str:
    """"2 cups flour", with the amount scaled to the servings chosen."""
    qty = ing.get("scaled_quantity")
    if qty is None:
        qty = ing.get("quantity")
    parts = [amount_text(qty), str(ing.get("unit") or "").strip(),
             str(ing.get("name") or "").strip()]
    line = " ".join(p for p in parts if p)
    section = str(ing.get("section") or "").strip()
    return f"{section}: {line}" if section else line


def spoken(seconds) -> str:
    """45 -> "45 seconds", 5400 -> "1 hour and 30 minutes"."""
    try:
        total = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        total = 0
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    if h or m:
        m += 1 if s >= 30 else 0
        s = 0
        if m == 60:
            h, m = h + 1, 0
    parts = [f"{n} {w}{'' if n == 1 else 's'}"
             for n, w in ((h, "hour"), (m, "minute"), (s, "second")) if n]
    if not parts:
        return "0 seconds"
    return parts[0] if len(parts) == 1 else " and ".join(parts)


def _as_adjective(duration: str) -> str:
    """"10 minutes" -> "10 minute", as in "a 10 minute timer"."""
    import re
    return re.sub(r"\b(hour|minute|second)s\b", r"\1", str(duration))


def _where(screen: dict | None) -> str:
    if not screen:
        return ""
    label = str(screen.get("label") or "")
    return f" on {label}" if label.lower().startswith("the ") else f" on the {label}"


def timer_rows(suggestions, timers) -> list[dict]:
    """Each suggested timer, numbered by step from 1, and whether one is
    already counting down (with its time left)."""
    from .recipe_timers import running_timer_for
    out = []
    for s in suggestions or []:
        t = running_timer_for(s, timers)
        row = {"label": s["label"], "seconds": int(s["seconds"]),
               "duration": spoken(s["seconds"]), "step": int(s["step_index"]) + 1,
               "running": t is not None}
        if t is not None:
            row["remaining_seconds"] = int(round(float(t.get("remaining_seconds") or 0)))
            row["timer_id"] = t.get("id")
        out.append(row)
    return out


def describe(recipe: dict | None, suggestions=None, timers=None, screen: dict | None = None,
             source: str = "kitchen", step_index: int | None = None) -> dict:
    """A recipe in the shape a language model answers from. Pure."""
    shown = {"id": screen["id"], "label": screen.get("label", "")} if screen else None
    if not recipe:
        where = _where(screen)
        return {"ok": False, "result": "none", "screen": shown,
                "speech": (f"No recipe is open{where}. What are you cooking?")}
    rows = timer_rows(suggestions, timers)
    steps = []
    for i, text in enumerate(recipe.get("steps") or []):
        steps.append({"number": i + 1, "text": str(text),
                      "timers": [{"label": r["label"], "seconds": r["seconds"],
                                  "duration": r["duration"], "running": r["running"]}
                                 for r in rows if r["step"] == i + 1]})
    servings = recipe.get("scaled_servings") or recipe.get("servings")
    title = recipe.get("title") or "The recipe"
    out = {
        "ok": True, "result": "open", "screen": shown, "source": source,
        "title": title,
        "servings": servings,
        "ingredients": [ingredient_line(i) for i in recipe.get("ingredients") or []
                        if isinstance(i, dict)],
        "steps": steps,
        "step_count": len(steps),
        # The step the cook is on (from 1), or None before one is chosen.
        "step": step_index + 1 if step_index is not None and 0 <= step_index < len(steps)
        else None,
        "timers": rows,
        "notes": recipe.get("notes") or "",
        "speech": (f"{title} is open{_where(screen)}. It has {len(steps)} "
                   f"step{'' if len(steps) == 1 else 's'}."),
    }
    if out["step"]:
        out["current"] = steps[out["step"] - 1]
        out["speech"] += f" You are on step {out['step']}."
    return out


def step_answer(described: dict, which, last: int | None) -> tuple[dict, int | None]:
    """Move to and read one step: ``which`` is first, next, back, repeat,
    last or a number from 1. ``last`` is the current step (from 0) or None.
    Returns the fields to add to the answer and the step now current (from
    0), or None when it did not move. Pure."""
    steps = described.get("steps") or []
    count = len(steps)
    title = described.get("title") or "The recipe"
    word = str(which or "").strip().lower()
    if not count:
        return {"result": "no_steps", "speech": f"{title} has no steps written down."}, None
    if word in ("first", "start", "1st", "begin"):
        idx = 0
    elif word == "next":
        idx = 0 if last is None else last + 1
        if idx >= count:
            return {"result": "last_step", "current": steps[-1],
                    "speech": f"That was the last step. {title} has {count} steps."}, last
    elif word in ("previous", "back", "before", "prev"):
        idx = 0 if last is None else max(0, last - 1)
    elif word in ("again", "repeat", "current", "this", "same"):
        idx = 0 if last is None else last
    elif word in ("last", "final"):
        idx = count - 1
    else:
        try:
            idx = int(word) - 1
        except ValueError:
            return {"result": "bad_step", "speech": "Which step?"}, None
        if not 0 <= idx < count:
            return {"result": "no_such_step",
                    "speech": f"{title} has {count} step{'' if count == 1 else 's'}."}, None
    step = steps[idx]
    speech = f"Step {idx + 1} of {count}: {step['text']}"
    timers = [t for t in step.get("timers") or [] if not t.get("running")]
    if len(timers) == 1:
        speech += (f" This step has a {_as_adjective(timers[0]['duration'])} timer: "
                   "say start the timer.")
    elif timers:
        speech += (f" This step has {len(timers)} timers: "
                   + " and ".join(f"{t['label'].lower()} for {t['duration']}" for t in timers[:3])
                   + ".")
    if idx == count - 1:
        speech += " That is the last step."
    return {"result": "step", "current": step, "speech": speech}, idx
