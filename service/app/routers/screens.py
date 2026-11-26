"""Screens: the list, events for one screen, and the pages a foreign tablet shows.

  GET    /api/screens                every screen (a screen key sees its own)
  POST   /api/screens/register       a foreign screen signs up or updates itself
  DELETE /api/screens/{id}           forget a foreign screen
  GET    /api/screens/poll           a screen's own events (long poll, the heartbeat)
  POST   /api/screens/session        a one-use address that signs a screen's web view in
  POST   /api/screens/show-recipe    find a recipe by what was said and show it
  GET    /screen/open                redeems that one-use address
  GET    /screen/, /screen/timers, /screen/recipe   the pages themselves

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
from ..services import access, current_recipe, recipe_find, screens, timers
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
        return _refuse(400, "page is one of home, recipe or timers.")
    ticket = screens.mint_ticket(sid)
    return JSONResponse(
        {"ok": True, "url": f"/screen/open?ticket={ticket}&next={path}",
         "expires_in": int(screens.TICKET_TTL)},
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
    if not screen:
        return ""
    label = screen["label"]
    return f" on {label}" if label.lower().startswith("the ") else f" on the {label}"


@router.post("/show-recipe")
async def show_recipe(body: ShowRecipeIn, db: Session = Depends(get_db)):
    if settings.is_satellite():
        return _refuse(409, _MAIN_ONLY)
    query = " ".join(body.query.split())[:200]
    slug = body.slug.strip()
    target = None
    if body.screen.strip():
        target = await run_in_threadpool(screens.get, body.screen.strip())
        if target is None:
            return _refuse(404, _NO_SCREEN)
        if not target.get("targetable"):
            return _refuse(409, f"{target['label']} needs an update before it can be "
                                "sent a recipe.")
    title = ""
    try:
        if not slug:
            if not query:
                return _refuse(400, "Say which recipe: send query or slug.")
            found = recipe_find.choose(query, await _recipe_names(db, query))
            if found["result"] == "not_found":
                return {"ok": False, "result": "not_found",
                        "speech": f"I could not find a recipe called {query}."}
            if found["result"] == "ambiguous":
                names = recipe_find.join_titles(c["title"] for c in found["candidates"])
                return {"ok": False, "result": "ambiguous",
                        "candidates": found["candidates"],
                        "speech": f"I found {names}. Which one?"}
            slug, title = found["recipe"]["slug"], found["recipe"]["title"]
        detail = await _recipe_detail(db, slug)
    except Exception:
        return JSONResponse({"ok": False, "result": "error",
                             "speech": "I could not reach the recipe library."},
                            status_code=502)
    if not detail:
        return {"ok": False, "result": "not_found",
                "speech": f"I could not find a recipe called {query or slug}."}
    normalized = current_recipe.from_mealie_detail(detail, slug)
    recipe = await run_in_threadpool(current_recipe.set_active, normalized)
    title = recipe.get("title") or title or slug
    shown = None
    if target is not None:
        if target["kind"] == "bandit":
            # A Bandit keeps its own current recipe, so it is handed this one
            # along with the page change.
            await run_in_threadpool(screens.send, target["id"],
                                    {"type": "recipe", "recipe": normalized})
        shown = await run_in_threadpool(
            lambda: screens.send_navigate(target["id"], page="recipe"))
    return {"ok": True, "result": "shown", "recipe": {"slug": slug, "title": title},
            "screen": shown, "speech": f"Showing {title}{_where(shown)}."}


# --- The pages a foreign screen shows ---------------------------------------


@pages.get("/open")
async def open_screen(request: Request, ticket: str = "", next: str = ""):
    """Sign this web view in as the screen the ticket was made for, then go to
    the page. A bad, used or expired ticket gets a page that says the screen
    needs to reconnect (401, header X-PR-Screen: reconnect), never a password
    form: a wall tablet has nobody to type one."""
    target = next if next in ("screen/", "screen/recipe", "screen/timers") else "screen/"
    sid = screens.redeem_ticket(ticket)
    owner = screens.owner_of(sid) if sid else None
    if sid and owner:
        request.session.clear()
        request.session["authed"] = True
        request.session["role"] = "screen"
        request.session["screen_id"] = sid
        request.session["screen_owner"] = owner
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
    known = screens.get(sid) if sid else None
    return templates.TemplateResponse(request, "screen.html", {
        "request": request, "page": page, "screen_id": sid,
        # Some kiosk apps draw their own close button over the top right
        # corner of the page; the header keeps that corner clear for them.
        "corner_clear": bool(known and known["kind"] == "kiosk_satellite"),
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
