"""QR code endpoint — returns an SVG QR code for the phone deep-link."""
import io

from fastapi import APIRouter, Request
from fastapi.responses import Response

router = APIRouter(tags=["ui"])


@router.get("/ui/qr")
def qr_code(request: Request) -> Response:
    """Return an SVG QR code whose encoded URL is http://{host}/ui/add.

    The URL is also embedded as a <title> element inside the SVG so tests and
    screen-readers can confirm which URL is encoded.
    """
    import qrcode
    import qrcode.image.svg

    host = request.headers.get("host", request.url.netloc)
    url = f"http://{host}/ui/add"

    factory = qrcode.image.svg.SvgPathImage
    qr = qrcode.make(url, image_factory=factory)

    buf = io.BytesIO()
    qr.save(buf)
    svg_bytes = buf.getvalue()

    # Inject the target URL as a <title> element so it appears in the SVG body.
    # This lets clients (tests, accessibility tools) confirm the encoded URL.
    svg_text = svg_bytes.decode("utf-8")
    svg_text = svg_text.replace(
        "<svg ",
        f'<svg aria-label="{url}" ',
        1,
    )
    # Insert <title> after the opening <svg ...> tag
    insert_at = svg_text.index(">", svg_text.index("<svg ")) + 1
    svg_text = svg_text[:insert_at] + f"<title>{url}</title>" + svg_text[insert_at:]

    return Response(content=svg_text.encode("utf-8"), media_type="image/svg+xml")
