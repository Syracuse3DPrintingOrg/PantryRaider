"""Settings, Bandit Remotes, Screens: the tablets and wall panels that signed
up with a screen key.

  GET    /setup/screens          every such screen, with its wall tablet profile
  POST   /setup/screens/{id}     rename it, or turn the profile on or off
  DELETE /setup/screens/{id}     forget it (its app signs up again on its next start)

Admin only, like everything under /setup (services/access.py). The main
install's own display and the Bandits are listed elsewhere in the pane, so
only the screens kept in services/screens.py appear here.
"""
from __future__ import annotations

from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ._common import router

_NO_SCREEN = "There is no screen with that id."

# What a kiosk app calls itself when it signs up, in words.
_KIND_LABELS = {
    "kiosk_satellite": "Kiosk Satellite",
    "fully_kiosk": "Fully Kiosk",
    "view_assist": "View Assist",
    "browser": "Browser",
}


def kind_label(kind) -> str:
    text = str(kind or "").strip()
    if text in _KIND_LABELS:
        return _KIND_LABELS[text]
    words = text.replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else "Browser"


def _row(screen: dict) -> dict:
    from ...services import screens
    return {
        "id": screen["id"], "label": screen["label"], "kind": screen["kind"],
        "kind_label": kind_label(screen["kind"]),
        "online": bool(screen.get("online")), "last_seen": screen.get("last_seen"),
        "width": screen.get("width", 0), "height": screen.get("height", 0),
        "wall": screens.wall_profile(screen["id"]),
        "wall_default": screens.wall_default(screen["kind"]),
    }


def _rows() -> list[dict]:
    from ...services import screens
    return [_row(s) for s in screens.list_screens()
            if s["kind"] not in ("server", "bandit") and screens.owner_of(s["id"])]


def _one(screen_id: str) -> dict | None:
    for row in _rows():
        if row["id"] == screen_id:
            return row
    return None


class ScreenEditIn(BaseModel):
    # Each field is applied only when it is sent.
    label: str | None = None
    # True or false sets the profile; null goes back to the default for the
    # screen's kind.
    wall: bool | None = None


@router.get("/screens")
async def screens_list():
    return {"ok": True, "screens": await run_in_threadpool(_rows)}


@router.post("/screens/{screen_id}")
async def screens_edit(screen_id: str, body: ScreenEditIn):
    from ...services import screens
    sent = body.model_dump(exclude_unset=True)
    if screens.owner_of(screen_id) is None:
        return JSONResponse({"ok": False, "error": _NO_SCREEN}, status_code=404)
    if "label" in sent:
        await run_in_threadpool(screens.set_label, screen_id, sent["label"] or "")
    if "wall" in sent:
        await run_in_threadpool(screens.set_wall_profile, screen_id, sent["wall"])
    return {"ok": True, "screen": await run_in_threadpool(_one, screen_id)}


@router.delete("/screens/{screen_id}")
async def screens_forget(screen_id: str):
    from ...services import screen_recipes, screens
    if not await run_in_threadpool(screens.forget, screen_id):
        return JSONResponse({"ok": False, "error": _NO_SCREEN}, status_code=404)
    # The recipe it was cooking and its place in it go with it.
    await run_in_threadpool(screen_recipes.clear, screen_id)
    return {"ok": True}
