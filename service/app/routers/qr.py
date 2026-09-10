"""QR code endpoint: returns an SVG QR code for the phone deep-link."""
import io
import re
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ..config import _LOCALHOST_HOSTS, _lan_ip, settings

router = APIRouter(tags=["ui"])


def phone_base_url(request_host: str, mode: str = "auto", public_url: str = "",
                   lan_ip: str = "") -> str:
    """Base URL (scheme://host[:port]) a phone can actually reach.

    The kiosk browser hits the app at localhost, so a QR built from the raw
    request Host header encodes an address that goes nowhere on a phone
    (FoodAssistant-75ak). In "auto" mode a loopback request host is swapped for
    the device's LAN IP, keeping the port; any other host is kept as is. In
    "public" mode the configured external URL is used when one is set,
    otherwise it falls back to the auto behavior.
    """
    public_url = (public_url or "").strip().rstrip("/")
    if mode == "public" and public_url:
        return public_url
    host = (request_host or "").strip()
    try:
        parts = urlsplit(f"//{host}")
        hostname = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        hostname, port = "", None
    if (not hostname or hostname in _LOCALHOST_HOSTS) and lan_ip:
        netloc = lan_ip if not port or port in (80, 443) else f"{lan_ip}:{port}"
        return f"http://{netloc}"
    return f"http://{host}"


def lan_url_for(request: Request, path: str) -> str:
    """A same-network (LAN) URL to `path` a phone or laptop can open.

    The kiosk browser reaches the app over localhost, so a link built from the
    raw request host goes nowhere on another device. This swaps a loopback host
    for the device's LAN IP (keeping the port) and always uses the LAN address,
    never a public tunnel, so the QR points at the reachable local page. `path`
    should start with a slash, e.g. "/ui/scanner-setup".
    """
    host = request.headers.get("host", request.url.netloc)
    base = phone_base_url(host, "auto", "", _lan_ip())
    return f"{base}/{path.lstrip('/')}"


def _default_qr_url(request: Request) -> str:
    """The phone deep-link the QR encodes when no explicit ?url= is given."""
    host = request.headers.get("host", request.url.netloc)
    public = settings.qr_public_url or settings.tunnel_url
    base = phone_base_url(host, settings.qr_url_mode, public, _lan_ip())
    return f"{base}/ui/add"


@router.get("/ui/qr/url")
def qr_url(request: Request) -> dict:
    """The URL the default QR code encodes, so the modal caption can show the
    same phone-reachable address instead of the kiosk's own localhost."""
    return {"url": _default_qr_url(request)}


@router.get("/ui/qr")
def qr_code(request: Request, url: str = "") -> Response:
    """Return an SVG QR code. Defaults to the phone deep link to /ui/add on a
    phone-reachable base address (see phone_base_url); pass ?url= to encode a
    specific http(s) URL instead, e.g. the LAN setup link shown on the kiosk
    (FoodAssistant-cssj).

    The URL is also embedded as a <title> element inside the SVG so tests and
    screen-readers can confirm which URL is encoded.
    """
    import qrcode
    import qrcode.image.svg

    # Only honor an explicit http(s) URL so the parameter cannot encode arbitrary
    # schemes (javascript:, data:, etc.) into a scannable code.
    if not (url.startswith("http://") or url.startswith("https://")):
        url = _default_qr_url(request)

    factory = qrcode.image.svg.SvgPathImage
    qr = qrcode.make(url, image_factory=factory)

    buf = io.BytesIO()
    qr.save(buf)
    svg_bytes = buf.getvalue()

    svg_text = svg_bytes.decode("utf-8")
    # qrcode's SVG sizes the root in millimetres (width="37mm" height="37mm").
    # Kiosk browsers (cage/Wayland Chromium) resolve physical mm units poorly in
    # an <img>, which showed as a broken/blank code on the setup splash. The SVG
    # also carries a viewBox, so swap the mm dimensions for unitless pixels and
    # let the viewBox drive scaling; the <img> CSS controls the display size.
    svg_text = re.sub(
        r'(<svg\b[^>]*?)\swidth="[\d.]+mm"\s+height="[\d.]+mm"',
        r'\1 width="300" height="300"',
        svg_text,
        count=1,
    )
    svg_text = svg_text.replace(
        "<svg ",
        f'<svg aria-label="{url}" ',
        1,
    )
    insert_at = svg_text.index(">", svg_text.index("<svg ")) + 1
    # White background ensures the code is scannable on dark themes.
    svg_text = (
        svg_text[:insert_at]
        + f"<title>{url}</title><rect width='100%' height='100%' fill='white'/>"
        + svg_text[insert_at:]
    )

    return Response(content=svg_text.encode("utf-8"), media_type="image/svg+xml")
