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

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional


@dataclass(frozen=True)
class ActionSpec:
    """Static description of one bindable action."""

    name: str
    label: str
    color: str            # key background, "#rrggbb"
    kind: str             # "status" | "trigger" | "nav" | "system"
    status_field: str = ""   # for kind=="status": which polled count to show
    target_path: str = ""    # for kind=="nav": app path to open in the kiosk
    description: str = ""


# The actions a key can be bound to. status_field names must match the keys
# produced by poll_status() below.
ACTIONS: dict[str, ActionSpec] = {
    "expiring": ActionSpec(
        name="expiring",
        label="Expiring",
        color="#b54708",
        kind="status",
        status_field="expiring",
        description="Count of items expired or expiring within the soon window. "
        "Press to refresh now.",
    ),
    "pending": ActionSpec(
        name="pending",
        label="Pending",
        color="#1d4ed8",
        kind="status",
        status_field="pending",
        description="Count of scanned items waiting to be committed. "
        "Press to refresh now.",
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
}

# Order used when no explicit key list is configured. The controller trims or
# paginates this to fit the connected deck.
DEFAULT_ORDER: list[str] = [
    "expiring",
    "pending",
    "commit",
    "add",
    "inventory",
    "cook",
    "brightness",
]


def resolve(name: str) -> Optional[ActionSpec]:
    """Look up an action by name, or None if it is not known."""
    return ACTIONS.get(name)


async def poll_status(client: Any, base_url: str, soon_days: int = 7) -> dict[str, int]:
    """Fetch the live counts shown on status keys.

    Returns a flat mapping of status_field -> integer. Network or service
    errors collapse to zeros so a key never shows a stale or crashing value.
    """
    out = {"expiring": 0, "pending": 0}
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
    return out


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


async def run_action(spec: ActionSpec, ctx: ActionContext) -> str:
    """Perform the side effect for a pressed key. Returns a short status line.

    Handlers are intentionally forgiving: a failed HTTP call returns a readable
    message rather than raising, so one bad press cannot take the daemon down.
    """
    base = ctx.base_url.rstrip("/")

    if spec.kind == "status":
        await ctx.refresh()
        return "refreshed"

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

    if spec.kind == "nav":
        ok = await ctx.navigate(spec.target_path)
        return "opened" if ok else "no display"

    if spec.kind == "system" and spec.name == "brightness":
        pct = ctx.cycle_brightness()
        return f"brightness {pct}%"

    if spec.kind == "system" and spec.name == "page_next":
        ctx.page_next()
        return "next page"

    if spec.kind == "system" and spec.name == "page_prev":
        ctx.page_prev()
        return "prev page"

    return ""
