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
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

# Preset durations (minutes) cycled through on each timer key press.
TIMER_PRESETS: tuple[int, ...] = (5, 10, 15, 30, 60)


class TimerState:
    """Mutable per-key countdown timer.

    Pressing cycles: idle -> 5 min -> 10 min -> 15 min -> 30 min -> 60 min -> idle.
    While counting down, ``label()`` returns MM:SS remaining. When expired,
    ``alerting`` flips to True; the next press dismisses it.
    """

    def __init__(self) -> None:
        self._preset_idx: int = -1   # -1 = idle
        self._deadline: float = 0.0  # monotonic clock target
        self.alerting: bool = False

    def is_running(self) -> bool:
        return self._preset_idx >= 0 and not self.alerting

    def remaining_seconds(self) -> int:
        if not self.is_running():
            return 0
        return max(0, int(self._deadline - time.monotonic()))

    def label(self, base_label: str) -> str:
        if self.alerting:
            return "Done!"
        if self._preset_idx < 0:
            return base_label
        secs = self.remaining_seconds()
        if secs <= 0:
            return "Done!"
        return f"{secs // 60}:{secs % 60:02d}"

    def color(self, base_color: str) -> str:
        if self.alerting:
            return "#ef4444"
        if self._preset_idx < 0:
            return base_color
        secs = self.remaining_seconds()
        return "#f59e0b" if secs < 60 else "#0d9488"

    def alert_active(self) -> bool:
        return self.alerting

    def press(self) -> None:
        if self.alerting:
            self.alerting = False
            self._preset_idx = -1
            return
        self._preset_idx += 1
        if self._preset_idx >= len(TIMER_PRESETS):
            self._preset_idx = -1
            self._deadline = 0.0
        else:
            self._deadline = time.monotonic() + TIMER_PRESETS[self._preset_idx] * 60

    def tick(self) -> bool:
        """Return True (and set alerting) if the timer just expired."""
        if self.is_running() and self.remaining_seconds() <= 0:
            self.alerting = True
            self._preset_idx = -1
            return True
        return False


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
    timer_press: Callable[[str], None] = field(default=lambda _name: None)


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

    if spec.kind == "timer":
        ctx.timer_press(spec.name)
        return f"{spec.name} pressed"

    return ""
