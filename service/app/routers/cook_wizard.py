"""Cook wizard support endpoints.

The wizard itself is a front-end state machine (static/js/cook-wizard.js) that
reuses the Cook page's suggestion, quick-view, and AI-generate flows. The only
server piece it needs is the list of guided-path buttons, served here so the
options live in one testable place instead of being duplicated in JavaScript.
"""
from __future__ import annotations

from fastapi import APIRouter

from ..services.cook_wizard import wizard_options

router = APIRouter(prefix="/cook-wizard", tags=["cook-wizard"])


@router.get("/options")
async def options() -> dict:
    """Cuisine, dish-type, and dietary button lists for the guided path."""
    return wizard_options()
