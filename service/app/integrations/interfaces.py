"""The four integration contracts (FoodAssistant-pjtq).

Each interface below is the minimal shape its current inhabitants already
satisfy; nothing here is speculative surface. The contracts are deliberately
small and, where possible, pure (no clocks, no I/O in the methods that decide
things), because that is what has kept the underlying seams testable:

  * ``RecipeBackend``: a place the recipe library can live. The seam's
    contract today is identity plus readiness; the read operations stay
    module-level (services.recipe_store deliberately answers the same wire
    shapes as MealieClient), and the resolver in ``integrations.recipes``
    picks the active one.
  * ``ShoppingBackend``: a store the shopping list can live in. Carries the
    two operations every surface already routes through the seam
    (``quick_add`` and ``autocheck``) plus readiness.
  * ``SensorDecoder``: one device family the gadgets ingest recognizes. Each
    decoder owns its wire ``kind`` token, how a pushed reading and a
    discovery land in the shared state file, and its prune windows, so
    adding a family means registering a decoder, not editing a routing
    switch.
  * ``ActionProvider``: a resolver for the named-action vocabulary (the
    tokens the Start Page, Stream Deck, ESP buttons, and BLE buttons fire).
    A provider answers the tokens it owns and passes on the rest.

Every inhabitant also answers ``enabled()``: whether this integration is
switched on for this install, always derived from the existing settings
(recipes_backend, shopping_backend, the gadget class gates). Enablement is
descriptive; the hot paths keep their own rules (the gadgets ingest, for
example, stores whatever the reader pushes exactly as it always has).
"""
from __future__ import annotations

from abc import ABC, abstractmethod

# Registry families. Exclusive kinds (recipes, shopping) resolve to one
# active inhabitant; additive kinds (sensors, actions) run all enabled ones.
KIND_RECIPES = "recipes"
KIND_SHOPPING = "shopping"
KIND_SENSORS = "sensors"
KIND_ACTIONS = "actions"
KINDS = (KIND_RECIPES, KIND_SHOPPING, KIND_SENSORS, KIND_ACTIONS)


class Integration(ABC):
    """One registered inhabitant of a seam.

    Subclasses set three class attributes: ``kind`` (the registry family),
    ``name`` (unique within the kind; for the exclusive kinds this is the
    value the matching settings field stores), and ``label`` (a short human
    name for status surfaces).
    """

    kind: str = ""
    name: str = ""
    label: str = ""

    @abstractmethod
    def enabled(self) -> bool:
        """Whether this integration is switched on for this install."""

    def describe(self) -> dict:
        """A status row: {"kind", "name", "label", "enabled"}."""
        return {"kind": self.kind, "name": self.name, "label": self.label,
                "enabled": bool(self.enabled())}


class RecipeBackend(Integration):
    """A place the recipe library can live ("native" or "mealie" today).

    The seam contract is identity plus readiness: ``configured()`` is what
    the auto rule in ``integrations.recipes.resolve_active`` consumes (an
    install with Mealie configured keeps using it until the user migrates).
    The read operations deliberately stay module-level for now:
    services.recipe_store answers the same wire shapes as MealieClient (its
    docstring documents the mirror), so every consumer works on either
    backend without the interface having to carry a method nobody calls
    through it yet. Folding the shared reads into this contract is the
    third-party step (FoodAssistant-czui), not this one.
    """

    kind = KIND_RECIPES

    @abstractmethod
    def configured(self) -> bool:
        """Whether this backend has what it needs to serve the library."""


class ShoppingBackend(Integration):
    """A store the shopping list can live in ("grocy" or "mealie" today).

    ``quick_add`` and ``autocheck`` are the two operations every surface
    already routes through the seam (the scanner shopping mode, the Start
    Page and BLE button quick-add, the stock-and-tick-off flow). The richer
    /mealie/shopping* endpoints keep their existing per-backend branches and
    wire shapes; they are the wire surface, not the seam.
    """

    kind = KIND_SHOPPING

    @abstractmethod
    def configured(self) -> bool:
        """Whether this backend has a reachable-in-principle list store."""

    @abstractmethod
    async def quick_add(self, item: str, quantity: float = 1.0) -> str:
        """Add one item to this backend's list; returns the list's name.

        Raises the backend's own error (GrocyError / MealieError /
        ValueError) for the caller to report, exactly as the seam always
        has."""

    @abstractmethod
    async def autocheck(self, item_name: str) -> None:
        """Check off list items that token-match a just-stocked item name."""


class SensorDecoder(Integration):
    """One device family the gadgets readings push can carry.

    ``wire_kind`` is the ``kind`` token entries carry on POST
    /gadgets/readings ("thermometer", "hygrometer", "contact", "button",
    "stemma"); the decoder whose ``fallback`` is True also takes entries
    with no or an unknown kind (the original wire contract: everything
    defaulted to a thermometer, so nothing existing ever changed shape).

    The three store/prune methods mutate the already-loaded gadgets state
    dict and nothing else: no clocks (``now`` comes in), no file I/O (the
    caller holds the lock and saves), so each family stays as testable as
    the pure normalize functions it wraps. ``enabled()`` mirrors the
    family's settings gate and is descriptive only; ingest stores whatever
    the reader pushes, exactly as it always has, because the reader is the
    component the gates actually throttle.
    """

    kind = KIND_SENSORS
    wire_kind: str = ""
    fallback: bool = False

    @abstractmethod
    def configured_ids(self) -> set[str]:
        """Normalized ids of this family's configured devices (settings)."""

    @abstractmethod
    def store_reading(self, state: dict, entry, now: float, source: str) -> None:
        """Land one pushed reading in the shared state (or drop junk)."""

    @abstractmethod
    def store_discovered(self, state: dict, entry: dict, dev_id: str,
                         protocol: str, now: float, source: str,
                         configured_ids: set[str]) -> None:
        """Land one discovery sighting (already id-validated by ingest)."""

    @abstractmethod
    def prune(self, state: dict, now: float, configured_ids: set[str]) -> None:
        """Drop this family's long-gone readings and stale discoveries."""


class ActionProvider(Integration):
    """A resolver for named action tokens.

    ``fire`` executes a token and returns the familiar {"ok", "detail"}
    dict, or None when the token is not one of its own so the seam can ask
    the next provider (and report an unknown key when nobody claims it).
    """

    kind = KIND_ACTIONS

    @abstractmethod
    async def fire(self, name: str, long: bool = False) -> dict | None:
        """Execute a token this provider owns, or return None to pass."""
