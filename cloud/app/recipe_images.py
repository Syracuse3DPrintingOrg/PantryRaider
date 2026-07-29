"""Forager-hosted recipe photos (afx1).

When a kitchen shares a recipe, the device now uploads the photo's raw bytes so
Forager keeps its own copy. The community browse page (on the website) and the
private share page (served here) then show that copy, independent of whether the
origin kitchen is online or publicly reachable. This module is the storage and
validation seam both share paths use.

Security matters here, because an image host that trusts its input becomes an
arbitrary-file store:

- The content type is decided by sniffing the file's magic bytes, never the
  client's Content-Type header or filename.
- Only jpg / png / webp / gif are accepted. SVG is refused outright: it is XML
  and can carry script, so serving one back would be a stored-XSS vector.
- The upload is size-capped before it is stored.
- The stored filename is built only from an integer id or a token Forager
  minted, under one fixed directory, and any name with a path separator or a
  leading dot is refused.

The served responses ride the app-wide ``X-Content-Type-Options: nosniff``
header, so a browser honours the sniffed type and never re-interprets a stored
image as HTML.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, UploadFile

from .config import settings

# The accepted formats: raster images a browser renders inline and that carry
# no active content. The extension we store under is the one the sniff proved,
# and the same map gives the media type we serve back.
MEDIA_TYPES: dict[str, str] = {
    "jpg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
}


def sniff_image_ext(data: bytes) -> str | None:
    """The image type from the file's leading magic bytes, or None when it is
    not one of the formats we accept. Header-only and pure, so it is cheap and
    unit-testable. Deliberately rejects SVG (it has no binary signature and is
    script-capable) and anything else."""
    if len(data) < 12:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def image_root() -> Path:
    """The directory shared-recipe photos are stored in. The configured value
    if set, otherwise a "recipe-images" directory beside the backup store (both
    live on the same VPS data volume)."""
    configured = (settings.recipe_image_dir or "").strip()
    if configured:
        return Path(configured)
    return Path(settings.backup_storage_dir).parent / "recipe-images"


def _guard_prefix(prefix: str) -> str:
    """The filename stem, refusing anything that could escape the image dir.
    The callers only ever pass "community-<int>" or "share-<token>", but this is
    the last line before a path is built, so it checks anyway."""
    if not prefix or "/" in prefix or "\\" in prefix or prefix.startswith("."):
        raise ValueError("unsafe image name")
    return prefix


def store_image(prefix: str, data: bytes, ext: str) -> None:
    """Write the photo for ``prefix`` as ``<prefix>.<ext>`` under the image dir,
    dropping any earlier copy stored under a different extension."""
    prefix = _guard_prefix(prefix)
    root = image_root()
    root.mkdir(parents=True, exist_ok=True)
    for other in MEDIA_TYPES:
        if other == ext:
            continue
        stale = root / f"{prefix}.{other}"
        if stale.exists():
            try:
                stale.unlink()
            except OSError:
                pass
    (root / f"{prefix}.{ext}").write_bytes(data)


def find_image(prefix: str) -> Path | None:
    """The stored photo for ``prefix``, or None. Tries the accepted extensions
    in turn, so the serving route needs no record of which one was written."""
    try:
        prefix = _guard_prefix(prefix)
    except ValueError:
        return None
    root = image_root()
    for ext in MEDIA_TYPES:
        candidate = root / f"{prefix}.{ext}"
        if candidate.is_file():
            return candidate
    return None


def media_type_for(path: Path) -> str:
    """The Content-Type to serve a stored photo with, from its extension."""
    return MEDIA_TYPES.get(path.suffix.lstrip(".").lower(),
                           "application/octet-stream")


async def read_image_upload(upload: UploadFile | None) -> tuple[bytes, str]:
    """Read an uploaded photo, enforce the size cap, and return (bytes, ext)
    where ext is the sniffed real type. Raises a clear HTTPException on a
    missing, oversized, or non-image upload, so the routes stay thin."""
    if upload is None:
        raise HTTPException(400, detail="No image was uploaded.")
    # Read in chunks with a running total and stop the moment the cap is passed,
    # so a client cannot make the server buffer a huge body (up to an upstream
    # proxy's own limit) into memory before we reject it.
    max_bytes = settings.recipe_image_max_bytes
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(413, detail="That image is too large. Please "
                                            "share a photo under a few megabytes.")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise HTTPException(400, detail="No image was uploaded.")
    ext = sniff_image_ext(data)
    if not ext:
        raise HTTPException(400, detail="Please upload a real photo "
                                        "(JPG, PNG, WEBP, or GIF).")
    return data, ext
