"""First-party integrations: the app's pluggable seams (FoodAssistant-pjtq).

Pantry Raider grew several places where more than one implementation already
stands behind one stable surface: the recipe library (the native store or
Mealie), the shopping list (Grocy or Mealie), the sensor families the gadgets
ingest recognizes (probes, hygrometers, contacts, buttons, STEMMA boards),
and the named-action vocabulary the Start Page, Stream Deck, ESP buttons, and
BLE buttons all fire through. This package formalizes those seams:

  * ``interfaces``: the four small contracts (RecipeBackend, ShoppingBackend,
    SensorDecoder, ActionProvider) that current implementations already
    satisfy. Nothing here invents capability; each interface is the minimal
    shape its inhabitants share today.
  * ``registry``: register an inhabitant, list them, resolve the active one.
    Discovery is explicit registration, first-party only: the mechanism a
    plugin system needs, without dynamic loading or a trust model
    (FoodAssistant-czui is the follow-up that would add those).
  * ``recipes`` / ``shopping`` / ``sensors`` / ``actions``: the existing
    implementations, registered as inhabitants. Mealie is the proof case: a
    full backend living behind the seam, imported only when it is actually
    used.

The long-standing public functions (services.recipe_source.active_backend,
services.shopping_source.active_backend, services.gadgets.ingest,
services.start_actions.fire_key) keep working unchanged; they are thin shims
over this package, so no caller moved and no behavior changed.
"""
from __future__ import annotations
