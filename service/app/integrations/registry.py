"""The integrations registry (FoodAssistant-pjtq).

Register an inhabitant, list them, resolve the active one. Discovery is
explicit registration and first-party only: the built-in inhabitants are
loaded lazily on the first query (so importing a service module alone, as
the tests do, still finds a populated registry), and nothing here does
dynamic loading, entry-point scanning, or trust decisions. Those belong to
the third-party follow-up (FoodAssistant-czui), which would build on exactly
this surface.

Two resolution styles, matching the seams as they exist:

  * Exclusive kinds (recipes, shopping) have ONE active inhabitant at a
    time. Each registers a resolver that reproduces the seam's long-standing
    rule (explicit setting wins, then the auto rule); ``active_name`` /
    ``active`` are the single place that answers "which backend is live".
  * Additive kinds (sensors, actions) run every enabled inhabitant;
    ``enabled_for`` lists them in registration order.

Enable/disable: an inhabitant's own ``enabled()`` is settings-driven (the
existing recipes_backend / shopping_backend values and gadget class gates).
``set_override`` can force one on or off at the registry level; nothing in
the app sets overrides today (behavior is unchanged by construction), but it
is the mechanism a per-plugin toggle would persist through later.
"""
from __future__ import annotations

import threading

from .interfaces import (Integration, KINDS, KIND_ACTIONS, KIND_RECIPES,
                         KIND_SENSORS, KIND_SHOPPING)

__all__ = [
    "KINDS", "KIND_ACTIONS", "KIND_RECIPES", "KIND_SENSORS", "KIND_SHOPPING",
    "register", "unregister", "get", "names", "all_for", "enabled_for",
    "is_enabled", "set_override", "set_resolver", "active_name", "active",
    "snapshot", "reset",
]

_lock = threading.RLock()
# kind -> {name -> Integration}, insertion-ordered (registration order).
_registered: dict[str, dict[str, Integration]] = {}
# kind -> zero-arg callable returning the active inhabitant's name.
_resolvers: dict[str, object] = {}
# (kind, name) -> forced enablement; absent means follow the inhabitant.
_overrides: dict[tuple[str, str], bool] = {}
_builtins_loaded = False


def _ensure_builtins() -> None:
    """Load the first-party inhabitants once, on the first registry query.

    The flag flips before the load so ``register`` calls made during it do
    not recurse; a failed load leaves the flag set (a broken builtin module
    should surface as its own ImportError, not as an empty registry that
    half-works)."""
    global _builtins_loaded
    if _builtins_loaded:
        return
    with _lock:
        if _builtins_loaded:
            return
        _builtins_loaded = True
        from . import actions, recipes, sensors, shopping
        for module in (recipes, shopping, sensors, actions):
            module.register_builtins()


def register(integration: Integration, *, replace: bool = False) -> None:
    """Add one inhabitant to its kind's bucket.

    ``kind`` must be one of the known families and ``name`` unique within
    it; pass replace=True to swap an existing registration (tests use this
    to substitute a fake)."""
    kind = getattr(integration, "kind", "")
    name = getattr(integration, "name", "")
    if kind not in KINDS:
        raise ValueError(f"Unknown integration kind: {kind!r}")
    if not name or not isinstance(name, str):
        raise ValueError("An integration needs a non-empty name.")
    with _lock:
        bucket = _registered.setdefault(kind, {})
        if name in bucket and not replace:
            raise ValueError(f"{kind} integration {name!r} is already registered.")
        bucket[name] = integration


def unregister(kind: str, name: str) -> None:
    """Remove one inhabitant (a no-op when it is not registered)."""
    with _lock:
        _registered.get(kind, {}).pop(name, None)
        _overrides.pop((kind, name), None)


def get(kind: str, name: str) -> Integration | None:
    """The registered inhabitant, or None."""
    _ensure_builtins()
    with _lock:
        return _registered.get(kind, {}).get(name)


def names(kind: str) -> tuple[str, ...]:
    """Registered names for a kind, in registration order."""
    _ensure_builtins()
    with _lock:
        return tuple(_registered.get(kind, {}))


def all_for(kind: str) -> list[Integration]:
    """Every registered inhabitant of a kind, in registration order."""
    _ensure_builtins()
    with _lock:
        return list(_registered.get(kind, {}).values())


def is_enabled(kind: str, name: str) -> bool:
    """Whether one inhabitant is on: a registry override when set, else the
    inhabitant's own settings-driven answer. Unregistered names are off."""
    integration = get(kind, name)
    if integration is None:
        return False
    with _lock:
        override = _overrides.get((kind, name))
    if override is not None:
        return override
    return bool(integration.enabled())


def enabled_for(kind: str) -> list[Integration]:
    """The enabled inhabitants of a kind, in registration order."""
    return [i for i in all_for(kind) if is_enabled(kind, i.name)]


def set_override(kind: str, name: str, enabled: bool | None) -> None:
    """Force one inhabitant on or off, or None to follow its settings again.

    Nothing in the app sets overrides today; this is the enable/disable
    mechanism itself (and what a persisted per-plugin toggle would call)."""
    with _lock:
        if enabled is None:
            _overrides.pop((kind, name), None)
        else:
            _overrides[(kind, name)] = bool(enabled)


def set_resolver(kind: str, resolver) -> None:
    """Install the zero-arg callable that names a kind's active inhabitant.

    Only the exclusive kinds have one; the seam module that registers a
    kind's builtins installs its resolver alongside them."""
    with _lock:
        _resolvers[kind] = resolver


def active_name(kind: str) -> str:
    """The active inhabitant's name for an exclusive kind ("" when the kind
    has no resolver, i.e. it is additive)."""
    _ensure_builtins()
    with _lock:
        resolver = _resolvers.get(kind)
    return str(resolver()) if resolver is not None else ""


def active(kind: str) -> Integration:
    """The active inhabitant for an exclusive kind.

    Raises LookupError when the kind has no resolver or the resolved name is
    not registered; with the first-party inhabitants both are unreachable,
    but a clear error beats an AttributeError somewhere downstream."""
    name = active_name(kind)
    integration = get(kind, name) if name else None
    if integration is None:
        raise LookupError(f"No active {kind} integration ({name!r}).")
    return integration


def snapshot() -> list[dict]:
    """Status rows for every registered inhabitant: kind, name, label,
    enabled, and (on exclusive kinds) whether it is the active one."""
    _ensure_builtins()
    rows: list[dict] = []
    for kind in KINDS:
        current = active_name(kind) if kind in (KIND_RECIPES, KIND_SHOPPING) else ""
        for integration in all_for(kind):
            row = integration.describe()
            row["enabled"] = is_enabled(kind, integration.name)
            if current:
                row["active"] = integration.name == current
            rows.append(row)
    return rows


def reset() -> None:
    """Test helper: forget everything and reload builtins on the next query."""
    global _builtins_loaded
    with _lock:
        _registered.clear()
        _resolvers.clear()
        _overrides.clear()
        _builtins_loaded = False
