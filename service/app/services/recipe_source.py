"""Recipe source badges (FoodAssistant-5frk).

The app now browses recipes from several places at once: recipes the user made
in Mealie, recipes imported into Mealie from a webpage, one-off web results from
TheMealDB or Spoonacular, and recipes shared through the Forager community. Every
recipe already carries a ``source``; a Mealie recipe that came from the web also
keeps its original source URL, which is what tells an imported recipe apart from
one the user wrote themselves.

``source_badge`` turns that (source, has-original-URL) pair into a small labeled
chip the browse UI can show on every card, so at a glance you can tell where a
recipe came from. It is pure and total: an unknown or missing source still gets a
sensible chip rather than an error, so a new source added later never breaks the
recipe list.

The colours are Bootstrap 5.3 subtle utility classes, deliberately staying off
the pink brand accent so the chips read as quiet metadata, not calls to action.

This module is also the recipe BACKEND seam (FoodAssistant-zwwe):
``active_backend`` decides whether the recipe library lives in Pantry Raider's
own store ("native") or in a configured Mealie ("mealie"). The endpoints in
routers/mealie.py consult it, so the browse, save, suggest, and cook flows work
identically over either backend.
"""
from __future__ import annotations

BACKEND_NATIVE = "native"
BACKEND_MEALIE = "mealie"


def active_backend() -> str:
    """Which recipe backend this install uses: "native" or "mealie".

    An explicit recipes_backend setting wins. When unset, an install that has
    Mealie configured keeps using it (existing installs are production and must
    not change behavior on upgrade); everything else gets the native store, so
    a new install never needs Mealie for recipes. The one-click migration
    flips the setting to "native" on success.
    """
    from ..config import settings
    value = (getattr(settings, "recipes_backend", "") or "").strip().lower()
    if value in (BACKEND_NATIVE, BACKEND_MEALIE):
        return value
    return BACKEND_MEALIE if settings.mealie_configured() else BACKEND_NATIVE

# Subtle, distinct chips per source. Each value is the extra class string added
# to a Bootstrap ``badge`` span; none use the danger/pink accent.
_MINE = {"label": "My recipes", "css_class": "bg-success-subtle text-success-emphasis border"}
_IMPORTED = {"label": "Mealie (imported)", "css_class": "bg-primary-subtle text-primary-emphasis border"}
_NATIVE_IMPORTED = {"label": "Imported", "css_class": "bg-primary-subtle text-primary-emphasis border"}
_WEB = {"label": "Web", "css_class": "bg-secondary-subtle text-secondary-emphasis border"}
_FORAGER = {"label": "Forager cloud", "css_class": "bg-info-subtle text-info-emphasis border"}

_WEB_SOURCES = ("themealdb", "spoonacular")


def source_badge(source: str | None, has_source_url: bool = False) -> dict:
    """Map a recipe's ``source`` (and whether a Mealie recipe kept an original
    source URL) to a ``{"label", "css_class"}`` badge. Pure and total.

    Rules:
      * mealie WITH an original source URL -> "Mealie (imported)"
      * mealie WITHOUT one                 -> "My recipes" (the user's own)
      * native WITH an original source URL -> "Imported"
      * native WITHOUT one                 -> "My recipes"
      * themealdb / spoonacular            -> "Web"
      * forager                            -> "Forager cloud"
      * anything else                      -> "Web" (a safe generic for a source
                                              added later)
    """
    src = (source or "").strip().lower()
    if src == "mealie":
        return dict(_IMPORTED if has_source_url else _MINE)
    if src == "native":
        return dict(_NATIVE_IMPORTED if has_source_url else _MINE)
    if src == "forager":
        return dict(_FORAGER)
    if src in _WEB_SOURCES:
        return dict(_WEB)
    return dict(_WEB)
