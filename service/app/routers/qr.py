"""QR code endpoint: returns an SVG QR code for the phone deep-link."""
import io

from fastapi import APIRouter, Request
from fastapi.responses import Response

router = APIRouter(tags=["ui"])


@router.get("/ui/qr")
def qr_code(request: Request, url: str = "") -> Response:
    """Return an SVG QR code. Defaults to http://{host}/ui/add (the phone deep
    link); pass ?url= to encode a specific http(s) URL instead, e.g. the LAN
    setup link shown on the kiosk (FoodAssistant-cssj).

    The URL is also embedded as a <title> element inside the SVG so tests and
    screen-readers can confirm which URL is encoded.
    """
    import qrcode
    import qrcode.image.svg

    # Only honor an explicit http(s) URL so the parameter cannot encode arbitrary
    # schemes (javascript:, data:, etc.) into a scannable code.
    if not (url.startswith("http://") or url.startswith("https://")):
        host = request.headers.get("host", request.url.netloc)
        url = f"http://{host}/ui/add"

    factory = qrcode.image.svg.SvgPathImage
    qr = qrcode.make(url, image_factory=factory)

    buf = io.BytesIO()
    qr.save(buf)
    svg_bytes = buf.getvalue()

    svg_text = svg_bytes.decode("utf-8")
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
