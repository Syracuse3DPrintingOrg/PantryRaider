"""Shared Jinja2 environment so base.html globals work on every page."""
from fastapi import Request
from fastapi.templating import Jinja2Templates

from .config import settings, theme_info, ui_scale_factor, resolve_custom_colors, nav_chrome_hidden, timer_chips_hidden, APP_NAME, APP_VERSION
from .hardware import is_raspberry_pi
from .ingress import template_globals
from .navigation import visible_tabs, auto_hidden_groups, build_nav_tree


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
    # Resolve the active custom palette (live "custom" swatches or a saved
    # "custom:<id>" theme). None for built-in themes. base.html emits its inline
    # override <style> whenever custom_theme_active is true, using these values.
    cc = resolve_custom_colors(settings.ui_theme)
    custom_active = cc is not None
    if cc is None:
        cc = {
            "base": settings.custom_theme_base,
            "primary": settings.custom_theme_primary,
            "accent": settings.custom_theme_accent,
            "bg": settings.custom_theme_bg,
            "surface": settings.custom_theme_surface,
            "text": settings.custom_theme_text,
        }
    return {
        "ui_theme": settings.ui_theme,
        "theme_mode": info["mode"],
        "theme_css": info["stylesheet"],
        "theme_overlay": info.get("overlay"),
        # Custom theme builder (FoodAssistant-hatd, named themes -nw49). Exposed
        # on every render so base.html can emit an inline <style> from the active
        # palette when a custom theme (live or saved) is selected.
        "custom_theme_active": custom_active,
        "custom_theme_base": cc["base"],
        "custom_theme_primary": cc["primary"],
        "custom_theme_accent": cc["accent"],
        "custom_theme_bg": cc["bg"],
        "custom_theme_surface": cc["surface"],
        "custom_theme_text": cc["text"],
        # Background image (FoodAssistant-e2t6): an optional fixed image layer
        # behind the whole UI, with a 0-1 opacity for readability.
        "background_image_url": settings.background_image_url,
        "background_opacity": max(0, min(100, settings.background_opacity)) / 100.0,
        "ui_scale": settings.ui_scale,
        "ui_scale_factor": ui_scale_factor(settings.ui_scale),
        # 12/24-hour clock reading, stamped on <html data-clock-format> so any
        # browser-side clock (the screensaver, browser-rendered timestamps)
        # follows the same setting as the server-rendered ones.
        "clock_format": settings.clock_format,
        "display_rotation": settings.display_rotation,
        "is_pi": is_raspberry_pi(),
        "features": settings.features(),
        "deployment_mode": settings.deployment_mode,
        "barcode_global_capture": settings.barcode_global_capture,
        # Quiet mode silences the audible timer chime (FoodAssistant-soj1).
        "quiet_mode": settings.quiet_mode,
        # Global has-LLM flag (FoodAssistant-9vgx): true when a vision/LLM
        # provider is configured. Templates hide AI-only affordances when false.
        "ai_configured": settings.ai_configured(),
        # Label / document printing master gate (FoodAssistant-fb8x). True only
        # when the user has turned printing on. Templates hide every print
        # affordance when false ({% if printing_enabled %}), the same way
        # ai_configured gates AI-only UI.
        "printing_enabled": settings.printing_enabled,
        "pin_readonly": getattr(request.state, "pin_readonly", False),
        # On-screen floating navigation menu (FoodAssistant-bzuu).
        "floating_nav_position": settings.floating_nav_position,
        "floating_nav_orientation": settings.floating_nav_orientation,
        "floating_nav_autohide_streamdeck": settings.floating_nav_autohide_streamdeck,
        # Whether the on-screen nav chrome is suppressed for this device
        # (FoodAssistant-vbfp follow-up): a Stream-Deck kiosk at large scale
        # defaults to hidden. base.html reads this to drop the floating nav.
        "nav_visibility": settings.nav_visibility,
        "hide_nav_chrome": nav_chrome_hidden(
            settings.nav_visibility, settings.has_streamdeck, settings.ui_scale),
        "has_streamdeck": settings.has_streamdeck,
        # Floating timer chips (FoodAssistant-kfda): per-timer overlay chips on
        # every page. "auto" resolves against the interface scale the same way
        # nav_visibility does; base.html passes the resolved flag to the chips
        # script so a hidden device never even starts the poller.
        "timer_chips": settings.timer_chips,
        "hide_timer_chips": timer_chips_hidden(settings.timer_chips, settings.ui_scale),
        # Cameras for the kiosk camera page (FoodAssistant-oewn).
        "cameras": settings.streamdeck_cameras,
        # On-screen Home Assistant event channel (notifications + camera pop-ups).
        "ha_events_enabled": settings.ha_events_enabled,
        "ha_camera_popup_seconds": settings.ha_camera_popup_seconds,
        # Kiosk screensaver (FoodAssistant-y65x): minutes idle before the soft
        # on-screen clock layer shows; 0 keeps it off. Read by screensaver.js.
        "screensaver_minutes": settings.screensaver_minutes,
        "screensaver_speed": settings.screensaver_speed,
        "screensaver_pill_scale": settings.screensaver_pill_scale,
        "screensaver_photo_seconds": settings.screensaver_photo_seconds,
        "screensaver_ken_burns": settings.screensaver_ken_burns,
        "screensaver_ken_burns_speed": settings.screensaver_ken_burns_speed,
        "screensaver_mode": settings.screensaver_mode,
        # When on, the saver's idle behaviour runs in every browser viewing
        # this install, not just kiosk-mode ones (FoodAssistant-xlb3).
        "screensaver_all_clients": settings.screensaver_all_clients,
        # On-screen keyboard for kiosk touchscreens (FoodAssistant-wo9j),
        # rendered into #osk-config and read by osk.js. Kiosk-gated in the
        # script itself; this flag lets a kiosk with an attached keyboard
        # turn the on-screen one off.
        "osk_enabled": settings.osk_enabled,
        # Cache-buster for static assets so a kiosk browser fetches fresh CSS/JS
        # after an update instead of serving a stale cached copy.
        "app_version": APP_VERSION,
        "app_name": APP_NAME,
        # Read-only DEMO MODE (FoodAssistant-pxp0). Surfaced on every render so
        # base.html can show the demo banner and templates can hide write
        # affordances with {% if not demo_mode %}. False on any normal install,
        # so the banner is absent and nothing else changes.
        "demo_mode": settings.demo_mode,
    }


# context_processors run per request, so ingress_path/theme reflect live state
templates = Jinja2Templates(
    directory="app/templates",
    context_processors=[template_globals, theme_context],
)
# Called per render, so nav reflects settings changes without a restart
templates.env.globals["nav_tabs"] = visible_tabs
templates.env.globals["nav_tree"] = build_nav_tree
templates.env.globals["auto_hidden_groups"] = auto_hidden_groups
