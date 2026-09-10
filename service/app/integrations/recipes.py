"""RecipeBackend inhabitants: the native store and Mealie (FoodAssistant-pjtq).

This is the proof case for the registry: a FULL backend living behind the
seam. The native library (services.recipe_store) and Mealie
(services.mealie) both answer the same read shapes, so the only question the
seam has ever had to answer is "which one is live", and that rule now lives
in ``resolve_active`` here, installed as the registry's resolver for the
recipes kind. services.recipe_source.active_backend() is a thin shim over
it, so every existing caller keeps working unchanged.

Mealie is imported only when it is actually used: nothing in this module
(or in the registry) imports services.mealie at load time, and the Mealie
inhabitant reaches for it inside its methods. That matches how the app now
treats Mealie generally (an optional backend, collapsed under Advanced), and
it is the shape a third-party backend would take later (FoodAssistant-czui).
"""
from __future__ import annotations

from .interfaces import KIND_RECIPES, RecipeBackend

# The two backend names, the exact strings the recipes_backend setting
# stores. services.recipe_source re-exports them as BACKEND_NATIVE /
# BACKEND_MEALIE for its long-standing callers.
NAME_NATIVE = "native"
NAME_MEALIE = "mealie"


class NativeRecipeBackend(RecipeBackend):
    """Pantry Raider's own recipe library (services.recipe_store): SQLite
    rows plus images under data_dir, always available, no setup needed."""

    name = NAME_NATIVE
    label = "Pantry Raider recipe library"

    def enabled(self) -> bool:
        return True

    def configured(self) -> bool:
        return True


class MealieRecipeBackend(RecipeBackend):
    """A configured Mealie as the recipe library (services.mealie).

    services.mealie is imported inside the methods, never at module load, so
    an install that does not use Mealie never imports it."""

    name = NAME_MEALIE
    label = "Mealie"

    def enabled(self) -> bool:
        return self.configured()

    def configured(self) -> bool:
        from ..config import settings
        return bool(settings.mealie_configured())


def resolve_active() -> str:
    """Which recipe backend this install uses: "native" or "mealie".

    The seam's long-standing rule, unchanged: an explicit recipes_backend
    setting wins (any registered backend name counts). When unset, an
    install with Mealie configured keeps using it (existing installs are
    production and must not change behavior on upgrade); everything else
    gets the native store, so a new install never needs Mealie for recipes.
    The one-click migration flips the setting to "native" on success.
    """
    from ..config import settings
    from . import registry
    value = (getattr(settings, "recipes_backend", "") or "").strip().lower()
    if value in registry.names(KIND_RECIPES):
        return value
    mealie = registry.get(KIND_RECIPES, NAME_MEALIE)
    return NAME_MEALIE if mealie is not None and mealie.configured() else NAME_NATIVE


def register_builtins() -> None:
    """Register both backends and the resolver (called by the registry)."""
    from . import registry
    registry.register(NativeRecipeBackend(), replace=True)
    registry.register(MealieRecipeBackend(), replace=True)
    registry.set_resolver(KIND_RECIPES, resolve_active)
