"""Pure decision helpers for the Pi host page in Settings.

The host page (Devices and Connections panes) shows a couple of things that
depend on the appliance's live state, reported by the host bridge:

- whether to offer "Start Mealie on this device" or show that Mealie is
  already running (FoodAssistant-mvke), and
- which link actually carries the network, so a Pi on Ethernet does not read
  as if Wi-Fi has failed (FoodAssistant-1idf).

The bridge itself is a standalone root script that is awkward to unit test,
so the branching lives here as small pure functions the app calls when it
renders, and the tests exercise these directly.
"""
from __future__ import annotations


def mealie_action_state(installed: bool, running: bool, *, available: bool = True) -> str:
    """Which Mealie affordance the Pi host card should show.

    Mealie is a legacy, optional recipe backend: recipes, the meal plan, and the
    shopping list are built into Pantry Raider. We deliberately do NOT offer to
    install or start Mealie from the UI (FoodAssistant-9mu5 / Dan 2026-07-10):
    that keeps the appliance consistent and light. Someone who genuinely wants
    Mealie starts it themselves over SSH (docker compose --profile with-mealie),
    and the app then detects it and offers to connect.

    Returns one of:
      "none"    -> no local Mealie action (not a Pi appliance, a satellite, or
                   Mealie simply is not running here). The URL + token connect
                   fields still let a user point at any Mealie they run.
      "running" -> Mealie is already up on this device; show a running indicator
                   and the "connect / set up for me" affordance.

    ``installed`` is accepted for a complete, self-documenting signature but no
    longer changes the result: a stopped or absent Mealie is "none", never an
    offer to start or download it.
    """
    if not available:
        return "none"
    if running:
        return "running"
    # Installed-but-stopped or not installed: we never offer to start or
    # download Mealie from the UI. Connect an existing one via URL + token.
    return "none"


def _iface_kind(name: str) -> str:
    """Classify a network interface name as "wired", "wifi", or "none".

    Wired NICs on a Pi appear as eth0 or, on newer boards, end0/eno1/enp2s0/
    enx<mac>. Wi-Fi shows up as wlan0 (or the predictable wlp* form). Virtual
    interfaces (docker, veth, bridges, tunnels, loopback) count as neither.
    """
    if not name:
        return "none"
    # Check Wi-Fi first so a future "en"-style rule cannot shadow "wl*".
    if name.startswith(("wlan", "wlp", "wl")):
        return "wifi"
    if name.startswith(("eth", "en")):
        return "wired"
    return "none"


def classify_active_connection(default_route_out: str) -> str:
    """Which link carries the default route: "wired", "wifi", or "none".

    Takes the raw output of ``ip route show default``. Each default line names
    its interface after ``dev`` and may carry a ``metric``; the lowest metric
    wins when several defaults exist (a box on both Ethernet and Wi-Fi prefers
    the wired route). Returns "none" when there is no usable default route, so
    a genuinely offline device (or the fallback hotspot, which has no default
    route) is not mislabelled as connected.
    """
    best_metric = None
    best_kind = "none"
    for line in (default_route_out or "").splitlines():
        parts = line.split()
        if not parts or parts[0] != "default":
            continue
        iface = ""
        metric = 0
        for i, tok in enumerate(parts):
            if tok == "dev" and i + 1 < len(parts):
                iface = parts[i + 1]
            elif tok == "metric" and i + 1 < len(parts):
                try:
                    metric = int(parts[i + 1])
                except ValueError:
                    metric = 0
        kind = _iface_kind(iface)
        if kind == "none":
            continue
        if best_metric is None or metric < best_metric:
            best_metric = metric
            best_kind = kind
    return best_kind
