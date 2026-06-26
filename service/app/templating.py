"""Shared Jinja2 environment so base.html globals work on every page."""
from fastapi import Request
from fastapi.templating import Jinja2Templates

from .config import settings, theme_info, ui_scale_factor
from .hardware import is_raspberry_pi
from .ingress import template_globals
from .navigation import visible_tabs, auto_hidden_groups


def theme_context(request: Request) -> dict:
    """Context processor: expose the current UI theme to every render.

    ``ui_theme``     : the selected theme key (for the setup <select>).
    ``theme_mode``   : "light"/"dark" for the <html data-bs-theme> attribute.
    ``theme_css``    : a vendored Bootswatch stylesheet href, or None to use
                        the default Bootstrap CSS. Resolved per request so a
                        settings change applies on the next page load.
    ``theme_overlay``: a second CSS href loaded after the main stylesheet
                        (used by overlay themes like Synthwave), or None.
    ``pin_readonly`` : True when the user is browsing in read-only mode because
                        kiosk_readonly_when_locked is set and no PIN session exists.
    """
    info = theme_info(settings.ui_theme)
    return {
        "ui_theme": settings.ui_theme,
        "theme_mode": info["mode"],
        "theme_css": info["stylesheet"],
        "theme_overlay": info.get("overlay"),
        # Custom theme builder (FoodAssistant-hatd). Exposed on every render so
        # base.html can emit an inline <style> from the stored swatches when
        # ui_theme == "custom". Harmless for other themes (base.html only reads
        # them in that branch).
        "custom_theme_base": settings.custom_theme_base,
        "custom_theme_primary": settings.custom_theme_primary,
        "custom_theme_accent": settings.custom_theme_accent,
        "custom_theme_bg": settings.custom_theme_bg,
        "custom_theme_surface": settings.custom_theme_surface,
        "custom_theme_text": settings.custom_theme_text,
        "ui_scale": settings.ui_scale,
        "ui_scale_factor": ui_scale_factor(settings.ui_scale),
        "display_rotation": settings.display_rotation,
        "is_pi": is_raspberry_pi(),
        "features": settings.features(),
        "deployment_mode": settings.deployment_mode,
        "barcode_global_capture": settings.barcode_global_capture,
        # Global has-LLM flag (FoodAssistant-9vgx): true when a vision/LLM
        # provider is configured. Templates hide AI-only affordances when false.
        "ai_configured": settings.ai_configured(),
        "pin_readonly": getattr(request.state, "pin_readonly", False),
        # On-screen floating navigation menu (FoodAssistant-bzuu).
        "floating_nav_position": settings.floating_nav_position,
        "floating_nav_orientation": settings.floating_nav_orientation,
        "floating_nav_autohide_streamdeck": settings.floating_nav_autohide_streamdeck,
        "has_streamdeck": settings.has_streamdeck,
    }


# context_processors run per request, so ingress_path/theme reflect live state
templates = Jinja2Templates(
    directory="app/templates",
    context_processors=[template_globals, theme_context],
)
# Called per render, so nav reflects settings changes without a restart
templates.env.globals["nav_tabs"] = visible_tabs
templates.env.globals["auto_hidden_groups"] = auto_hidden_groups
