"""Interface mode, theme, custom theme, background image, and AI usage."""

import re
from pathlib import Path
from fastapi import UploadFile, File
from fastapi.responses import JSONResponse, FileResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from ...config import (
    settings, _DEFAULT_THEME, DEPLOYMENT_MODES, _DEFAULT_DEPLOYMENT_MODE,
)
from ...hardware import is_raspberry_pi
from ._common import router


class ModePayload(BaseModel):
    deployment_mode: str = _DEFAULT_DEPLOYMENT_MODE
    remote_server_url: str = ""


@router.post("/mode")
async def save_mode(payload: ModePayload):
    """Persist the deployment mode chosen on wizard step 1.

    Saved on its own (before the rest of setup) so the wizard can branch and,
    on a Pi, the provisioner can read the choice to decide what to install.
    """
    mode = payload.deployment_mode
    if mode not in DEPLOYMENT_MODES:
        return JSONResponse({"ok": False, "error": "Unknown deployment mode."})
    data = {"deployment_mode": mode}
    if mode == "pi_remote":
        data["remote_server_url"] = payload.remote_server_url.rstrip("/")
    settings.save(data)
    return {"ok": True, "mode": mode}


class ThemePayload(BaseModel):
    ui_theme: str = _DEFAULT_THEME


@router.post("/theme")
async def save_theme(payload: ThemePayload):
    settings.save({"ui_theme": payload.ui_theme})
    # Recolour an attached Stream Deck to match the new theme (gxl). Best-effort
    # and Pi-only: pushes the theme into the controller config.toml via the
    # bridge so the running deck updates without a manual Stream Deck save.
    if is_raspberry_pi() and settings.has_streamdeck:
        from ...services.satellite import _push_streamdeck_settings
        # Two blocking host-bridge calls: they must not run on the event loop.
        await run_in_threadpool(_push_streamdeck_settings)
    return {"ok": True}


_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _theme_slug(name: str) -> str:
    """Stable id from a display name: lowercased, non-alphanumerics to '_'."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return slug or "theme"


class CustomThemePayload(BaseModel):
    name: str = ""
    base: str = "dark"
    primary: str = ""
    accent: str = ""
    bg: str = ""
    surface: str = ""
    text: str = ""


@router.post("/custom-theme")
async def save_custom_theme(payload: CustomThemePayload):
    """Save (or update) a named custom theme and make it active.

    Stores it in settings.custom_themes keyed by a slug of the name, then sets
    ui_theme to "custom:<id>". Resaving with the same name updates that theme.
    """
    name = (payload.name or "").strip()
    if not name:
        return {"ok": False, "error": "Give the theme a name."}
    base = payload.base if payload.base in ("light", "dark") else "dark"
    colors = {}
    fallback = {
        "primary": settings.custom_theme_primary, "accent": settings.custom_theme_accent,
        "bg": settings.custom_theme_bg, "surface": settings.custom_theme_surface,
        "text": settings.custom_theme_text,
    }
    for k in ("primary", "accent", "bg", "surface", "text"):
        v = (getattr(payload, k) or "").strip()
        if v and not _HEX_RE.match(v):
            return {"ok": False, "error": f"{k.title()} must be a #rrggbb colour."}
        colors[k] = v or fallback[k]
    theme_id = _theme_slug(name)
    entry = {"id": theme_id, "name": name, "base": base, **colors}
    existing = [t for t in (settings.custom_themes or []) if isinstance(t, dict) and t.get("id")]
    replaced = False
    for i, t in enumerate(existing):
        if t.get("id") == theme_id:
            existing[i] = entry
            replaced = True
            break
    if not replaced:
        existing.append(entry)
    settings.save({"custom_themes": existing, "ui_theme": f"custom:{theme_id}"})
    if is_raspberry_pi() and settings.has_streamdeck:
        from ...services.satellite import _push_streamdeck_settings
        # Two blocking host-bridge calls: they must not run on the event loop.
        await run_in_threadpool(_push_streamdeck_settings)
    return {"ok": True, "id": theme_id}


@router.post("/custom-theme/delete")
async def delete_custom_theme():
    """Delete the currently-active saved custom theme; fall back to the default."""
    name = getattr(settings, "ui_theme", "")
    if not (isinstance(name, str) and name.startswith("custom:")):
        return {"ok": False, "error": "No saved custom theme is active."}
    theme_id = name.split(":", 1)[1]
    remaining = [t for t in (settings.custom_themes or [])
                 if isinstance(t, dict) and t.get("id") and t.get("id") != theme_id]
    settings.save({"custom_themes": remaining, "ui_theme": _DEFAULT_THEME})
    return {"ok": True}


# -- Background image (FoodAssistant-e2t6) ----------------------------------

# Bitmap formats a browser renders as a CSS background, mapped to the on-disk
# extension we save them under. SVG is intentionally excluded: a background SVG
# can carry script, and it would be served same-origin.
_BG_TYPES = {
    "image/jpeg": ".jpg", "image/png": ".png",
    "image/webp": ".webp", "image/gif": ".gif",
}
_BG_MAX_BYTES = 8 * 1024 * 1024  # 8 MB: plenty for a full-screen photo.


def _bg_path() -> Path | None:
    """The stored background image file, or None if none is uploaded."""
    d = Path(settings.data_dir)
    for ext in (".jpg", ".png", ".webp", ".gif"):
        p = d / f"background{ext}"
        if p.exists():
            return p
    return None


@router.post("/background")
async def upload_background(file: UploadFile = File(...)):
    """Store an uploaded background image and point the setting at it.

    Saves to data_dir/background.<ext> (replacing any previous upload) and sets
    background_image_url to the internal serve route with a content hash for
    cache-busting, so a re-upload of a different image refreshes immediately.
    """
    ctype = (file.content_type or "").split(";", 1)[0].strip().lower()
    ext = _BG_TYPES.get(ctype)
    if not ext:
        return {"ok": False, "error": "Use a JPG, PNG, WebP, or GIF image."}
    data = await file.read()
    if not data:
        return {"ok": False, "error": "The uploaded file was empty."}
    if len(data) > _BG_MAX_BYTES:
        return {"ok": False, "error": "Image is larger than 8 MB."}
    import hashlib
    d = Path(settings.data_dir)
    d.mkdir(parents=True, exist_ok=True)
    # Remove any previous upload (possibly a different extension) so only one
    # background file ever exists on disk.
    for old in (".jpg", ".png", ".webp", ".gif"):
        op = d / f"background{old}"
        if op.exists():
            try:
                op.unlink()
            except OSError:
                pass
    (d / f"background{ext}").write_bytes(data)
    token = hashlib.sha256(data).hexdigest()[:12]
    settings.save({"background_image_url": f"setup/background/image?v={token}"})
    return {"ok": True, "url": settings.background_image_url}


@router.get("/background/image")
async def serve_background():
    """Serve the uploaded background image (FileResponse), or 404 if none."""
    p = _bg_path()
    if not p:
        return JSONResponse({"ok": False, "error": "no background"}, status_code=404)
    media = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp",
             "gif": "image/gif"}.get(p.suffix.lstrip("."), "application/octet-stream")
    return FileResponse(str(p), media_type=media)


@router.post("/background/clear")
async def clear_background():
    """Remove the background image (uploaded file and/or URL)."""
    p = _bg_path()
    if p:
        try:
            p.unlink()
        except OSError:
            pass
    settings.save({"background_image_url": ""})
    return {"ok": True}


@router.get("/ai-usage")
async def ai_usage():
    """Current AI token usage + budget for the AI settings panel (Pantry Raider).

    Also attaches an approximate dollar cost for this month and all time,
    priced with the currently selected provider's model (services/ai_pricing).
    The tracker stores combined input+output totals, so the figures are
    blended estimates; an unrecognised model gets no estimate at all.
    """
    from ...services import ai_pricing, usage
    data = usage.get_usage()
    provider = getattr(settings, "vision_provider", "") or ""
    model = getattr(settings, f"{provider}_model", "") or ""
    data["cost_model"] = model
    data["cost_month"] = ai_pricing.estimate_cost(data["month"], model)
    data["cost_total"] = ai_pricing.estimate_cost(data["total"], model)
    return {"ok": True, **data}


@router.post("/ai-usage/reset")
async def ai_usage_reset():
    """Clear the recorded AI token usage."""
    from ...services import usage
    usage.reset()
    return {"ok": True}
