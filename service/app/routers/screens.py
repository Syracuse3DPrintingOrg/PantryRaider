"""Screens: the list, events for one screen, and the pages a foreign tablet shows.

  GET    /api/screens                every screen (a screen key sees its own)
  POST   /api/screens/register       a foreign screen signs up or updates itself
  DELETE /api/screens/{id}           forget a foreign screen
  GET    /api/screens/poll           a screen's own events (long poll, the heartbeat)
  POST   /api/screens/session        a one-use address that signs a screen's web view in
  POST   /api/screens/show-recipe    find a recipe by what was said and show it
  GET    /api/screens/recipe         the recipe open on a screen, for questions
  POST   /api/screens/recipe/step    move the step that screen is on, and read it
  POST   /api/screens/recipe/timer   start one of that recipe's timers
  POST   /api/screens/recipe/cooked  mark it cooked (uses up the stock)
  GET    /screen/open                redeems that one-use address
  GET    /screen/, /screen/timers, /screen/recipe, /screen/cook   the pages themselves

Which scope may call which route is decided in services/access.py like every
other route. What is checked here is ownership: a screen key (or a web view
signed in as a screen) reaches only the screens it registered.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..services import (access, current_recipe, recipe_find, recipe_timers, screen_recipes,
                        screens, timers, upstream)
from ..templating import templates

router = APIRouter(prefix="/api/screens", tags=["screens"])
# A screen that draws its own timer bar is told when the list changes.
timers.add_listener(screens.nudge_timers)
pages = APIRouter(prefix="/screen", tags=["screens"])

_NOT_YOURS = "That screen belongs to another key."
_NO_SCREEN = "There is no screen with that id."
_MAIN_ONLY = "Screens are kept on the main server. Ask it instead."


def _refuse(status: int, error: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": error}, status_code=status)


def _owner(request: Request) -> str | None:
    """The key a screen-scoped caller acts for, or None for any other caller
    (who may reach every screen)."""
    p = access.principal(request)
    if p.scope == "screen":
        return p.credential_id or "-"
    return None


def _may_touch(request: Request, screen_id: str) -> bool:
    owner = _owner(request)
    return owner is None or screens.owner_of(screen_id) == owner


class RegisterIn(BaseModel):
    screen_id: str = ""
    label: str = ""
    kind: str = ""
    width: int = 0
    height: int = 0


class SessionIn(BaseModel):
    screen: str = ""
    page: str = "home"


class ShowRecipeIn(BaseModel):
    query: str = ""
    slug: str = ""
    screen: str = ""
    # The answer to "which one?" after an ambiguous call for the same screen.
    choice: str = ""
    # "recipe" shows the whole recipe; "cook" opens cook mode at step 1
    # ("let's cook ..."). Empty: whatever the question being answered asked for.
    page: str = ""


@router.get("")
async def list_screens(request: Request):
    return {"screens": await run_in_threadpool(screens.list_screens, _owner(request))}


@router.post("/register")
async def register(body: RegisterIn, request: Request):
    p = access.principal(request)
    if p.kind != "key" or not p.credential_id:
        return _refuse(403, "A screen registers itself with its own key.")
    try:
        screen = await run_in_threadpool(
            screens.register, p.credential_id, body.screen_id, body.label, body.kind,
            body.width, body.height)
    except screens.ScreenError as exc:
        return _refuse(400, str(exc))
    return {"ok": True, "screen": screen,
            "poll": f"/api/screens/poll?screen={screen['id']}"}


@router.delete("/{screen_id}")
async def forget(screen_id: str, request: Request):
    if not _may_touch(request, screen_id):
        return _refuse(403, _NOT_YOURS)
    if not await run_in_threadpool(screens.forget, screen_id):
        return _refuse(404, _NO_SCREEN)
    await run_in_threadpool(screen_recipes.clear, screen_id)
    return {"ok": True}


@router.get("/poll")
async def poll(request: Request, screen: str = "", since: int = 0, wait: float = 0):
    sid = screen or str(request.session.get("screen_id") or "")
    known = await run_in_threadpool(screens.get, sid)
    if known is None or sid == screens.SERVER_ID:
        return _refuse(404, _NO_SCREEN)
    if not _may_touch(request, sid):
        return _refuse(403, _NOT_YOURS)
    if wait and wait > 0:
        out = await screens.wait_for_events(sid, since, timeout=min(float(wait), 30.0),
                                            is_gone=request.is_disconnected)
    else:
        out = screens.poll(sid, since)
    out["screen"] = {"id": known["id"], "label": known["label"]}
    return out


@router.post("/session")
async def session_ticket(body: SessionIn, request: Request):
    sid = body.screen.strip()
    if screens.owner_of(sid) is None:
        # Only a foreign screen signs in this way; a Bandit's display and the
        # main install's own are signed in by the device itself.
        return _refuse(404, _NO_SCREEN)
    if not _may_touch(request, sid):
        return _refuse(403, _NOT_YOURS)
    page = body.page.strip().lower() or "home"
    path = screens.page_path("foreign", page)
    if not path:
        return _refuse(400, "page is one of home, recipe, cook, timers or app.")
    ticket = screens.mint_ticket(sid, app=page == "app")
    # Every page draws its own Exit (the /screen pages, cook mode and the
    # whole interface), and one page moves to another by itself, so the app
    # hosting them never draws a close button of its own.
    out = {"ok": True, "url": f"/screen/open?ticket={ticket}&next={path}",
           "expires_in": int(screens.TICKET_TTL), "exit": "page"}
    return JSONResponse(
        out,
        headers={"Cache-Control": "no-store"})


# --- Show a recipe by name ---------------------------------------------------


async def _recipe_names(db: Session, query: str) -> list[dict]:
    from ..services import recipe_source, recipe_store
    if recipe_source.active_backend() == recipe_source.BACKEND_NATIVE:
        return await run_in_threadpool(recipe_store.list_recipes, db, "", 100000)
    from ..services.mealie import MealieClient
    client = MealieClient()
    words = " ".join(recipe_find.tokens(query))
    rows = await client.search_recipes(words, per_page=50) if words else []
    if not rows:
        rows = await client.search_recipes("", per_page=200)
    return rows


async def _recipe_detail(db: Session, slug: str) -> dict | None:
    from ..services import recipe_source, recipe_store
    if recipe_source.active_backend() == recipe_source.BACKEND_NATIVE:
        return await run_in_threadpool(recipe_store.detail, db, slug)
    from ..services.mealie import MealieClient
    return await MealieClient().get_recipe(slug)


def _where(screen: dict | None) -> str:
    return screen_recipes._where(screen)


def _which_one(titles: list[str]) -> str:
    if len(titles) == 1:
        return f"Did you mean {titles[0]}?"
    return f"I found {recipe_find.join_titles(titles)}. Which one?"


@router.post("/show-recipe")
async def show_recipe(body: ShowRecipeIn, db: Session = Depends(get_db)):
    """Find a recipe by what was said and make it the one cooking on a
    screen. When several fit, the answer lists them (result "ambiguous") and
    they are kept for that screen for a few minutes, so the next call can
    name one: by slug, or with ``choice`` (or ``query``) set to what was said
    back ("the second one", "the soup")."""
    if settings.is_satellite():
        return _refuse(409, _MAIN_ONLY)
    query = " ".join(body.query.split())[:200]
    choice = " ".join(body.choice.split())[:200]
    slug = body.slug.strip()
    target = None
    if body.screen.strip():
        target = await run_in_threadpool(screens.get, body.screen.strip())
        if target is None:
            return _refuse(404, _NO_SCREEN)
        if not target.get("targetable"):
            return _refuse(409, f"{target['label']} needs an update before it can be "
                                "sent a recipe.")
    key = target["id"] if target else ""
    title = ""
    asked_page = ""
    try:
        if not slug:
            offered, asked_page = screen_recipes.recent_candidates_page(key)
            picked = None
            if offered:
                picked = (recipe_find.resolve_choice(choice, offered) if choice else None) \
                    or (recipe_find.resolve_choice(query, offered) if query else None)
            if picked is None and choice and offered:
                # An answer that picks none of them: ask again, same list.
                return {"ok": False, "result": "ambiguous", "candidates": offered,
                        "speech": _which_one([c["title"] for c in offered])}
            if picked is not None:
                slug, title = picked["slug"], picked["title"]
            else:
                said = query or choice
                if not said:
                    return _refuse(400, "Say which recipe: send query or slug.")
                found = recipe_find.choose(said, await _recipe_names(db, said))
                if found["result"] == "not_found":
                    return {"ok": False, "result": "not_found",
                            "speech": f"I could not find a recipe called {said}."}
                if found["result"] == "ambiguous":
                    screen_recipes.remember_candidates(key, found["candidates"],
                                                       body.page.strip().lower())
                    titles = [c["title"] for c in found["candidates"]]
                    return {"ok": False, "result": "ambiguous",
                            "candidates": found["candidates"], "speech": _which_one(titles)}
                slug, title = found["recipe"]["slug"], found["recipe"]["title"]
        detail = await _recipe_detail(db, slug)
    except Exception:
        return JSONResponse({"ok": False, "result": "error",
                             "speech": "I could not reach the recipe library."},
                            status_code=502)
    if not detail:
        return {"ok": False, "result": "not_found",
                "speech": f"I could not find a recipe called {query or choice or slug}."}
    screen_recipes.forget_candidates(key)
    # Cook mode when asked for, or when the question this answers was asked
    # from "let's cook ...".
    page = body.page.strip().lower() or asked_page
    page = "cook" if page == "cook" else "recipe"
    normalized = current_recipe.from_mealie_detail(detail, slug)
    # The server's current recipe follows what was last asked for, as it
    # always has; a named screen also keeps it as its own.
    recipe = await run_in_threadpool(current_recipe.set_active, normalized)
    title = recipe.get("title") or title or slug
    shown = None
    if target is not None and target["kind"] != "server":
        await run_in_threadpool(screen_recipes.set_recipe, target["id"], normalized)
    if target is None or target["kind"] == "server":
        # Where the cook was up to belonged to the recipe that was open.
        await run_in_threadpool(screen_recipes.forget_step, screens.SERVER_ID)
    first = None
    if page == "cook" and recipe.get("steps"):
        # Cook mode starts at step 1.
        await run_in_threadpool(screen_recipes.set_step, key, recipe, 0)
        first = str(recipe["steps"][0])
    if target is not None:
        if target["kind"] == "bandit":
            # A Bandit keeps its own current recipe, so it is handed this one
            # along with the page change.
            await run_in_threadpool(screens.send, target["id"],
                                    {"type": "recipe", "recipe": normalized})
            if first is not None:
                await run_in_threadpool(screens.send, target["id"], {"type": "step", "step": 1})
        shown = await run_in_threadpool(
            lambda: screens.send_navigate(target["id"], page=page))
    out = {"ok": True, "result": "shown", "recipe": {"slug": slug, "title": title},
           "screen": shown, "page": page, "speech": f"Showing {title}{_where(shown)}."}
    if first is not None:
        out["step"] = 1
        out["speech"] = (f"Let's cook {title}{_where(shown)}. "
                         f"Step 1 of {len(recipe['steps'])}: {first}")
    return out


# --- The recipe open on a screen, for questions and its timers ---------------


def _cr_router():
    from . import current_recipe as cr
    return cr


async def _timer_list() -> list[dict]:
    """Every timer, from wherever timers live (the main server)."""
    cr = _cr_router()
    if cr._upstream():
        import json as _json
        resp = await upstream.send(cr._fwd_client, "GET", f"{cr._upstream()}/timers")
        try:
            return list((_json.loads(resp.body) or {}).get("timers") or [])
        except (ValueError, AttributeError):
            return []
    return timers.list_timers()


async def _start_timer(label: str, seconds: float, origin: str,
                       note: str = "") -> dict | None:
    cr = _cr_router()
    if cr._upstream():
        import json as _json
        resp = await upstream.send(cr._fwd_client, "POST", f"{cr._upstream()}/timers",
                                   json={"label": label, "seconds": seconds, "origin": origin,
                                         "note": note})
        if resp.status_code != 200:
            return None
        cr._timers_cache.invalidate()
        try:
            return (_json.loads(resp.body) or {}).get("timer")
        except (ValueError, AttributeError):
            return None
    return await run_in_threadpool(timers.create_timer, label, seconds, origin, note)


async def _screen_for(request: Request, screen: str):
    """(screen or None, refusal or None) for the screen a call names, or the
    one a screen's own web view is signed in as. None with no refusal means
    no screen: the server's current recipe."""
    sid = screen.strip() or str(request.session.get("screen_id") or "")
    if _owner(request) is not None and (not sid or settings.is_satellite()):
        # A screen key acts for the screens it registered and nothing else:
        # never for the kitchen's own current recipe (that is the main
        # display's), and never on a Bandit, which keeps no screens.
        if settings.is_satellite():
            return None, _refuse(409, _MAIN_ONLY)
        return None, _refuse(400, "Say which screen: send screen.")
    if not sid or settings.is_satellite():
        # A Bandit answers about its own current recipe; screens live on the
        # main server.
        return None, None
    known = await run_in_threadpool(screens.get, sid)
    if known is None:
        return None, _refuse(404, _NO_SCREEN)
    if not _may_touch(request, sid):
        return None, _refuse(403, _NOT_YOURS)
    return known, None


@router.get("/recipe")
async def screen_recipe_now(request: Request, screen: str = "", full: bool = False,
                            own: bool = False):
    """The recipe open on a screen, in a shape a language model can answer
    any question from: title, servings, ingredients as lines with scaled
    amounts, steps numbered from 1, the timers each step suggests (with
    whether one is already running) and the step the cook is on (``step``,
    from 1, or null). ``full`` adds the recipe as stored and the raw timer
    suggestions, for the screen's own page.

    ``source`` says whose recipe it is: "screen" (that screen's own) or
    "kitchen" (the kitchen's current one, which a screen's pages show when
    it has none of its own). ``own`` leaves the kitchen's out: an answer
    about one screen (Home Assistant asking what to do next there) is "none"
    for a screen that never opened a recipe."""
    target, refusal = await _screen_for(request, screen)
    if refusal is not None:
        return refusal
    return JSONResponse(await _describe(target, full, own),
                        headers={"Cache-Control": "no-store"})


async def _recipe_of(target: dict | None, adopt: bool = False) -> tuple[dict | None, str]:
    """The recipe a step or timer request acts on: that screen's own (the
    kitchen's when no screen is named). ``adopt`` is the screen's own page
    asking: a page that was showing the kitchen's recipe makes it the
    screen's own first, so its steps and timers, and voice at that screen,
    follow the recipe being cooked there."""
    recipe, source = await run_in_threadpool(screen_recipes.recipe_for, target, True)
    if (recipe is None and adopt and target is not None
            and target.get("kind") not in ("server", "bandit")):
        kitchen = await run_in_threadpool(current_recipe.get_active)
        if kitchen:
            recipe = await run_in_threadpool(screen_recipes.set_recipe, target["id"],
                                             kitchen, True)
            source = "screen"
    return recipe, source


async def _describe(target: dict | None, full: bool = False, own: bool = False) -> dict:
    recipe, source = await run_in_threadpool(screen_recipes.recipe_for, target, own)
    suggestions = recipe_timers.suggestions_for_recipe(recipe)
    try:
        running = await _timer_list() if suggestions else []
    except Exception:
        running = []
    key = target["id"] if target else ""
    cur = await run_in_threadpool(screen_recipes.current_step, key, recipe)
    out = screen_recipes.describe(recipe, suggestions, running, target, source, cur)
    if recipe:
        # Always {"title": ...} at least; the screen's own page gets it whole.
        out["recipe"] = recipe if full else {"title": out["title"], "id": recipe.get("id")}
    if full:
        out["suggestions"] = suggestions
    return out


class RecipeStepIn(BaseModel):
    screen: str = ""
    # first, next, back, repeat, last, a number from 1, or "none" to clear.
    step: str | int = "next"
    full: bool = False
    # The screen's own page, which may be showing the kitchen's recipe:
    # that recipe becomes the screen's own (see _recipe_of).
    adopt: bool = False


@router.post("/recipe/step")
async def move_recipe_step(body: RecipeStepIn, request: Request):
    """Move the step a screen is on (a tap on the step, or "next step" said
    out loud) and read it: the answer is the recipe as GET /recipe gives it,
    with ``current`` (the step now current) and a ``speech`` line that reads
    it. The screen is told, so its page follows. Only the screen's own
    recipe moves: one that never opened a recipe answers that none is open
    there, rather than reading another screen's."""
    target, refusal = await _screen_for(request, body.screen)
    if refusal is not None:
        return refusal
    recipe, _src = await _recipe_of(target, body.adopt)
    out = await _describe(target, body.full, own=True)
    if not out.get("ok"):
        return out
    key = target["id"] if target else ""
    word = str(body.step).strip().lower()
    if word in ("none", "clear", "off"):
        await run_in_threadpool(screen_recipes.set_step, key, recipe, None)
        out.update(step=None, result="cleared", speech="Done.")
        out.pop("current", None)
    else:
        last = out["step"] - 1 if out.get("step") else None
        extra, now = screen_recipes.step_answer(out, word, last)
        out.update(extra)
        if now is not None:
            now = await run_in_threadpool(screen_recipes.set_step, key, recipe, now)
            out["step"] = now + 1
    if target is not None and target["kind"] != "server":
        await run_in_threadpool(screens.send, target["id"],
                                {"type": "step", "step": out.get("step")})
    return out


class RecipeTimerIn(BaseModel):
    screen: str = ""
    # Which suggested timer: a step number (from 1), words said about it
    # ("baking it", "the crust"), or a button's own label. Any mix.
    step: int | None = None
    query: str = ""
    label: str = ""
    # Who asked (a screen id, a Home Assistant voice device).
    origin: str = ""
    # Move the screen to its timers page once started. Off: the recipe stays up.
    show: bool = False
    # The screen's own page, which may be showing the kitchen's recipe:
    # that recipe becomes the screen's own (see _recipe_of).
    adopt: bool = False


def _choice(row: dict) -> dict:
    """One timer to choose from: {"step", "label", "seconds", "speech"}."""
    return {"step": row["step"], "label": row["label"], "seconds": int(row["seconds"]),
            "speech": f"{row['label']}, {screen_recipes.spoken(row['seconds'])}"}


def _timer_choices_speech(choices: list[dict]) -> str:
    said = [f"{c['label'].lower()} for {screen_recipes.spoken(c['seconds'])} in step {c['step']}"
            for c in choices[:4]]
    return "Which timer: " + (" or ".join(said) if len(said) <= 2
                              else ", ".join(said[:-1]) + " or " + said[-1]) + "?"


def _ambiguous(title: str, rows: list[dict], lead: str = "") -> dict:
    choices = [_choice(r) for r in rows]
    return {"ok": False, "result": "ambiguous", "recipe": {"title": title},
            "choices": choices, "speech": lead + _timer_choices_speech(choices)}


def _all_running(title: str, rows: list[dict], running: list[dict]) -> dict:
    """Every timer that could be meant is already counting down."""
    on = [t for t in (recipe_timers.running_timer_for(r, running) for r in rows) if t]
    out = {"ok": True, "result": "running", "recipe": {"title": title}, "timers": on}
    if len(rows) == 1 and on:
        left = screen_recipes.spoken(on[0].get("remaining_seconds") or 0)
        out.update(timer=on[0], step=rows[0]["step"],
                   speech=f"The {rows[0]['label'].lower()} timer is already running, "
                          f"with {left} left.")
    else:
        out["speech"] = "Those timers are already running."
    return out


@router.post("/recipe/timer")
async def start_recipe_timer(body: RecipeTimerIn, request: Request):
    """Start one of the timers the recipe on a screen suggests, picked by step
    number (from 1), by words ("the crust", "baking it"), or by a button's
    label. With none of those it is the current step's timer.

    Answers {"ok", "result", "speech", "recipe": {"title"}} where result is
    started or running (with "timer"), ambiguous (with "choices", each
    {"step", "label", "seconds", "speech"}: ask, then call again with the
    step), no_recipe or not_found. A timer already counting down for that
    step is never started twice, and is never offered as a choice: when
    every timer that could be meant is running, the answer is "running"
    (with "timers"). Only the screen's own recipe is used."""
    target, refusal = await _screen_for(request, body.screen)
    if refusal is not None:
        return refusal
    recipe, _source = await _recipe_of(target, body.adopt)
    if not recipe:
        return {"ok": False, "result": "no_recipe",
                "speech": f"No recipe is open{_where(target)}. What are you cooking?"}
    title = recipe.get("title") or "This recipe"
    steps = recipe.get("steps") or []
    about = {"title": title}
    if body.step is not None and not 1 <= body.step <= len(steps):
        return {"ok": False, "result": "not_found", "reason": "no_such_step", "recipe": about,
                "speech": f"{title} has {len(steps)} step{'' if len(steps) == 1 else 's'}."}
    suggestions = recipe_timers.suggestions_for_recipe(recipe)
    if not suggestions:
        return {"ok": False, "result": "not_found", "reason": "no_timers", "recipe": about,
                "speech": f"{title} has no step with a time in it."}
    every = [recipe_timers.choice_row(x) for x in suggestions]
    query, label = body.query.strip(), body.label.strip()
    running: list[dict] | None = None

    async def now_running() -> list[dict]:
        nonlocal running
        if running is None:
            try:
                running = await _timer_list()
            except Exception:
                running = []
        return running

    async def ask(rows: list[dict], lead: str = "") -> dict:
        # The timers already counting down are left out. A question with only
        # one left is still a question: the caller decides whether to ask.
        on = await now_running()
        idle = [r for r in rows if recipe_timers.running_timer_for(r, on) is None]
        if not idle:
            return _all_running(title, rows, on)
        return _ambiguous(title, idle, lead)

    if body.step is None and not query and not label:
        # "Start the timer": the current step's, when it has exactly one.
        # Otherwise the person is asked which (the caller asks on the device
        # and calls again with the step).
        cur = await run_in_threadpool(screen_recipes.current_step,
                                      target["id"] if target else "", recipe)
        if cur is not None:
            picked = recipe_timers.choose_suggestion(suggestions, steps, cur + 1)
            if picked["result"] == "not_found":
                return await ask(every, f"Step {cur + 1} has no timer. ")
        elif len(suggestions) == 1:
            picked = {"result": "match", "suggestion": suggestions[0]}
        else:
            return await ask(every)
    else:
        picked = recipe_timers.choose_suggestion(suggestions, steps, body.step, query, label)
        if picked["result"] == "ambiguous" and query:
            # Of several that fit the words, the one not already counting
            # down is meant ("start the timer for baking it" once the crust
            # is in).
            on = await now_running()
            idle = [x for x in suggestions
                    if any(c["step"] == x["step_index"] + 1 and c["label"] == x["label"]
                           and c["seconds"] == x["seconds"] for c in picked["choices"])
                    and recipe_timers.running_timer_for(x, on) is None]
            if len(idle) == 1:
                picked = {"result": "match", "suggestion": idle[0]}
    if picked["result"] == "ambiguous":
        return await ask(picked["choices"])
    if picked["result"] == "not_found":
        reason = picked.get("reason")
        if reason == "no_timer_in_step":
            speech = f"Step {body.step} has no time in it."
        else:
            names = recipe_find.join_titles(sorted({x["label"].lower() for x in suggestions}))
            speech = (f"I could not find a timer for {query or 'that'} in {title}. "
                      f"It has timers to {names}.")
        return {"ok": False, "result": "not_found", "reason": reason, "recipe": about,
                "choices": [_choice(r) for r in every[:6]], "speech": speech}
    sug = picked["suggestion"]
    step_no = sug["step_index"] + 1
    already = recipe_timers.running_timer_for(sug, await now_running())
    if already is not None:
        left = screen_recipes.spoken(already.get("remaining_seconds") or 0)
        return {"ok": True, "result": "running", "timer": already, "step": step_no,
                "recipe": about,
                "speech": f"The {sug['label'].lower()} timer is already running, "
                          f"with {left} left."}
    origin = body.origin.strip() or (target["id"] if target else "")
    if not origin:
        p = access.principal(request)
        if p.scope == "screen":
            origin = str(request.session.get("screen_id") or "")
    note = recipe_timers.next_note(steps, sug["step_index"])
    try:
        timer = await _start_timer(sug["label"], sug["seconds"], origin, note)
    except ValueError as exc:
        return _refuse(400, str(exc))
    if timer is None:
        return JSONResponse({"ok": False, "result": "unreachable",
                             "speech": "I could not reach the main server."}, status_code=502)
    shown = None
    if body.show and target is not None:
        shown = await run_in_threadpool(
            lambda: screens.send_navigate(target["id"], page="timers"))
    return {"ok": True, "result": "started", "timer": timer, "step": step_no,
            "recipe": about, "screen": shown,
            "speech": f"{sug['label']} timer started for "
                      f"{screen_recipes.spoken(sug['seconds'])}."}


class RecipeCookedIn(BaseModel):
    screen: str = ""


@router.post("/recipe/cooked")
async def recipe_cooked(body: RecipeCookedIn, request: Request,
                        db: Session = Depends(get_db)):
    """Mark the recipe open on a screen cooked: the same as Cooked on the
    recipe page (the matching stock is used up and a "save to leftovers?"
    item is raised), then that screen's recipe is put away."""
    if settings.is_satellite():
        return _refuse(409, _MAIN_ONLY)
    target, refusal = await _screen_for(request, body.screen)
    if refusal is not None:
        return refusal
    recipe, source = await run_in_threadpool(screen_recipes.recipe_for, target)
    if not recipe:
        return {"ok": False, "result": "no_recipe", "speech": "No recipe is open."}
    cr = _cr_router()
    if source == "kitchen":
        out = await cr._cook_recipe(recipe, current_recipe.PRIMARY_SLOT, db)
        # The kitchen's recipe is put away, and with it where this screen
        # was up to in it.
        await run_in_threadpool(screen_recipes.forget_step, target["id"] if target else "")
    else:
        out = await cr._cook_recipe(
            recipe, None, db, clear=lambda: screen_recipes.clear(target["id"]))
    n = len(out.get("consumed") or [])
    out["speech"] = (f"{recipe.get('title') or 'The recipe'} is cooked. "
                     f"{n} item{'' if n == 1 else 's'} used up.")
    return out


# --- The pages a foreign screen shows ---------------------------------------


@pages.get("/open")
async def open_screen(request: Request, ticket: str = "", next: str = ""):
    """Sign this web view in as the screen the ticket was made for, then go to
    the page. A bad, used or expired ticket gets a page that says the screen
    needs to reconnect (401, header X-PR-Screen: reconnect), never a password
    form: a wall tablet has nobody to type one."""
    target = next if next in ("screen/", "screen/recipe", "screen/timers", "screen/cook",
                              screens.APP_PATH) else "screen/"
    sid, whole = screens.redeem_ticket(ticket) or (None, False)
    owner = screens.owner_of(sid) if sid else None
    if target == screens.APP_PATH and not whole:
        target = "screen/"
    if sid and owner:
        request.session.clear()
        request.session["authed"] = True
        request.session["role"] = "screen"
        request.session["screen_id"] = sid
        request.session["screen_owner"] = owner
        # The whole interface, as a household member sees it (no Settings),
        # only when the ticket was asked for with page "app".
        request.session["screen_app"] = bool(whole)
        from ..services import sessions
        sessions.new_id(request.session)
        from ..ingress import ingress_redirect
        return ingress_redirect(request, "/" + target)
    if not ticket and (not settings.auth_password
                       or access.principal(request).kind != "anonymous"):
        # Nothing to redeem and no sign-in needed (or one already held).
        from ..ingress import ingress_redirect
        return ingress_redirect(request, "/" + target)
    # A missing, used or expired ticket. There is no form here on purpose: the
    # screen's app asks for a new ticket when it sees this answer.
    return templates.TemplateResponse(
        request, "screen_reconnect.html", {"request": request}, status_code=401,
        headers={"X-PR-Screen": "reconnect", "Cache-Control": "no-store"})


def _page(request: Request, page: str, screen: str = "") -> HTMLResponse:
    sid = str(request.session.get("screen_id") or "") or screen.strip()
    # Each page draws its own Exit for its screen (top right), so no corner
    # is kept clear for a kiosk app's close button.
    return templates.TemplateResponse(request, "screen.html", {
        "request": request, "page": page, "screen_id": sid,
    })


@pages.get("/", response_class=HTMLResponse)
async def screen_home(request: Request, screen: str = ""):
    return _page(request, "home", screen)


@pages.get("", include_in_schema=False)
async def screen_home_bare():
    return RedirectResponse("screen/", status_code=307)


@pages.get("/timers", response_class=HTMLResponse)
async def screen_timers(request: Request, screen: str = ""):
    return _page(request, "timers", screen)


@pages.get("/recipe", response_class=HTMLResponse)
async def screen_recipe(request: Request, screen: str = ""):
    return _page(request, "recipe", screen)


@pages.get("/cook", response_class=HTMLResponse)
async def screen_cook(request: Request, screen: str = ""):
    return _page(request, "cook", screen)
