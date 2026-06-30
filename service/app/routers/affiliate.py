"""Recommended kitchen products with Amazon affiliate links (FoodAssistant-k2kv).

Serves the Shop page at /ui/shop. Recommendations are tied to what the user does
NOT already own (their kitchen appliance selection) and, when a recipe is active
on the Current Recipe page, to the equipment that recipe needs but the user lacks.
This is not an AI feature, so nothing here is gated on a provider being set.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..config import settings, AMAZON_ASSOCIATES_TAG
from ..services import affiliate, current_recipe, utensils
from ..templating import templates

router = APIRouter(prefix="/ui", tags=["affiliate"])


def _recipe_missing() -> list[str]:
    """Equipment names the active recipe needs but the user does not own.

    Empty when no recipe is active or the user never set their kitchen. Failures
    here are non-fatal: the Shop page still renders general recommendations.
    """
    try:
        recipe = current_recipe.get_active()
        if not recipe:
            return []
        equipment = utensils.detect_equipment(recipe)
        return utensils.missing_appliances(equipment, settings.kitchen_appliances)
    except Exception:
        return []


@router.get("/shop", response_class=HTMLResponse)
async def shop_page(request: Request):
    missing = _recipe_missing()
    # The Associates tag is the project owner's static tag (not a per-user
    # setting), so the links earn for the project on every deployment.
    groups = affiliate.grouped_recommendations(
        settings.kitchen_appliances,
        AMAZON_ASSOCIATES_TAG,
        recipe_missing=missing,
    )
    return templates.TemplateResponse(request, "shop.html", {
        "request": request,
        "active": "shop",
        "groups": groups,
        "recipe_missing": missing,
        "disclosure": affiliate.DISCLOSURE,
        "affiliate_tag": AMAZON_ASSOCIATES_TAG,
    })
