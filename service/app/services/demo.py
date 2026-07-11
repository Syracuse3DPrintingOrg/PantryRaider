"""Read-only DEMO MODE helpers (FoodAssistant-pxp0).

When settings.demo_mode is on, the app is a public, fully-navigable demo that
must never be changed by a visitor. The request-blocking decision lives here as
pure functions so it is trivial to unit-test and reason about; main.py wires the
actual middleware around them.

The rule is deliberately blunt: every state-changing HTTP method is refused,
with a tiny allowlist of paths that only touch session-local or display-only
state (signing in, unlocking a kiosk PIN, the kiosk display-wake ping). Nothing
on the allowlist writes app data, Grocy/Mealie, or settings, so when in doubt a
path is simply left off it and blocked.
"""
from __future__ import annotations

# HTTP methods that can change state. GET/HEAD/OPTIONS are always safe to serve.
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Exact paths that stay usable in demo mode. Each changes only the browser
# session or a display-wake signal, never app data, inventory, recipes, or
# settings, so the demo stays useful without ever being mutated:
#   /ui/login        - sign in: validates the password and sets the session cookie
#   /ui/pin/verify   - kiosk PIN unlock: sets a session flag only
#   /setup/kiosk/activity - kiosk display-wake ping: pokes the host bridge's
#                     blank timer; writes no app or settings state
_DEMO_ALLOWED_PATHS = frozenset({
    "/ui/login",
    "/ui/pin/verify",
    "/setup/kiosk/activity",
})

# Friendly, consistent copy for both surfaces.
DEMO_MESSAGE = "This is a live demo, changes are turned off here."
DEMO_ERROR_CODE = "demo_read_only"


def is_mutating_method(method: str) -> bool:
    """True for the HTTP methods that can change state (POST/PUT/PATCH/DELETE)."""
    return (method or "").upper() in _MUTATING_METHODS


def is_allowed_in_demo(path: str) -> bool:
    """True when this exact path is on the demo allowlist (session/display only)."""
    return path in _DEMO_ALLOWED_PATHS


def is_blocked_in_demo(method: str, path: str) -> bool:
    """True when a request must be refused while demo_mode is on.

    A request is blocked when it uses a state-changing method and its path is
    not on the small allowlist. Read requests (GET/HEAD/OPTIONS) are never
    blocked, so every page and API read still works and the demo is fully
    explorable.
    """
    return is_mutating_method(method) and not is_allowed_in_demo(path)
