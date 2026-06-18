"""Tunnel router — manage Cloudflare Tunnel and FoodAssistant Cloud connections."""

from fastapi import APIRouter
from pydantic import BaseModel

from ..config import settings
from ..services.tunnel import TunnelService

router = APIRouter(prefix="/tunnel", tags=["tunnel"])

_svc = TunnelService()


class TunnelStartPayload(BaseModel):
    mode: str
    token: str = ""


@router.post("/start")
async def tunnel_start(payload: TunnelStartPayload):
    """Start a tunnel. Saves mode and token to config, then starts the tunnel."""
    # Persist mode and token before attempting to start
    settings.save({"tunnel_mode": payload.mode, "tunnel_token": payload.token})
    result = _svc.start(payload.mode, payload.token)
    if result.get("ok") and result.get("url"):
        settings.save({"tunnel_url": result["url"]})
    return result


@router.post("/stop")
async def tunnel_stop():
    """Stop the running tunnel and clear the stored URL."""
    result = _svc.stop()
    # Clear the URL regardless of whether docker reported an error (container
    # may already be gone; the user intent is "disconnected").
    settings.save({"tunnel_url": ""})
    return result


@router.get("/status")
async def tunnel_status():
    """Return current tunnel status. Saves URL to config when first discovered."""
    st = _svc.status()
    url = st.get("url", "")
    # If logs reveal a URL that isn't stored yet, persist it now
    if not url and st.get("running"):
        url = _svc.get_url_from_cloudflare_logs()
    if url and url != settings.tunnel_url:
        settings.save({"tunnel_url": url})
    return {
        "running": st.get("running", False),
        "url": url or settings.tunnel_url,
        "mode": settings.tunnel_mode,
    }
