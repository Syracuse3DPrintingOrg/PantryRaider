"""Screensaver photo sources (FoodAssistant-af1l).

The photo slideshow originally only read a USB drive on a Pi appliance.
This layer adds sources that work everywhere the app runs:

  folder  a directory of images, by default data_dir/photos, so photos
          dropped into the data directory (or a share mounted there) show up
          with no extra setup.
  immich  an album on a self-hosted Immich server, listed through its API
          with an API key. The images themselves are proxied thumbnail-size
          by the ui router so the browser never needs the key.
  urls    a plain newline-separated list of direct image links, passed
          through untouched.

Google Photos and iCloud are deliberately NOT here: Google removed the
Library API read scopes for third-party apps on March 31, 2025 (the Picker
API that replaced them needs a human tapping a picker every session, which
is useless for an unattended kiosk), and iCloud has no public API at all.
Scraping shared-album web pages breaks without notice, so we do not do it.

Everything that can be pure is pure (parsing, filtering, path guarding) so
the tests cover it without a network or a real Immich server. list_photos()
returns browser-servable src strings and never raises; any failure is an
empty list, which the screensaver turns into the bouncing-logo fallback.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .ttl_cache import TTLCache

# Extensions the slideshow will show. Lowercase; matching is case-insensitive.
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

PHOTO_SOURCES = ("built-in", "folder", "immich", "urls")

# Immich asset ids are UUIDs; anything else is rejected before it can reach
# the proxy URL (belt and braces with the router's own check).
_IMMICH_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")

# Album listings are cheap but the saver can restart often; hold the asset
# list for a minute so repeated starts cost one API call.
_immich_cache = TTLCache(ttl=60.0)


def normalize_photo_source(value: Any) -> str:
    """Only known source names persist; anything else is the built-in."""
    return value if value in PHOTO_SOURCES else "built-in"


def parse_photo_urls(text: Any) -> list[str]:
    """Direct image links from the newline-separated setting text.

    One URL per line (commas and whitespace also split, so a pasted list
    still works). Only http(s) links are kept and duplicates collapse while
    preserving order. Pure.
    """
    if not isinstance(text, str) or not text.strip():
        return []
    out: list[str] = []
    seen: set[str] = set()
    for tok in re.split(r"[\s,]+", text):
        tok = tok.strip()
        if not tok or not tok.lower().startswith(("http://", "https://")):
            continue
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def is_image_name(name: str) -> bool:
    """Whether a bare file name looks like a showable image. Pure."""
    if not name or name.startswith("."):
        return False
    dot = name.rfind(".")
    if dot <= 0:
        return False
    return name[dot:].lower() in IMAGE_EXTENSIONS


def effective_photo_folder(settings) -> Path:
    """The folder the "folder" source reads: the setting, or data_dir/photos."""
    configured = (getattr(settings, "photo_folder", "") or "").strip()
    if configured:
        return Path(configured)
    return Path(settings.data_dir) / "photos"


def list_folder_photos(folder: Path) -> list[str]:
    """Image file names in `folder`, sorted; hidden files and non-images are
    skipped. Never raises: a missing or unreadable folder is an empty list."""
    try:
        entries = list(folder.iterdir())
    except OSError:
        return []
    names: list[str] = []
    for p in entries:
        try:
            if p.is_file() and is_image_name(p.name):
                names.append(p.name)
        except OSError:
            continue
    return sorted(names)


def safe_photo_path(folder: Path, name: str) -> Path | None:
    """Resolve `name` inside `folder`, or None when it is not a plain image
    file directly in the folder.

    The traversal guard for the serve route: only a bare file name is
    accepted (no separators, no dot-dot, nothing hidden), it must carry an
    image extension, and the resolved path must still live in the folder
    (symlinked folders resolve first so a legitimate mount works while an
    escaping name cannot). Pure aside from the filesystem checks.
    """
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    if not is_image_name(name):
        return None
    try:
        base = folder.resolve()
        target = (base / name).resolve()
        if target.parent != base or not target.is_file():
            return None
    except OSError:
        return None
    return target


def parse_immich_album(data: Any) -> list[str]:
    """Image asset ids from an Immich GET /api/albums/{id} response. Pure.

    Only assets typed IMAGE with a sane-looking id are kept (the slideshow
    cannot play videos, and an odd id must never reach a proxy URL).
    """
    if not isinstance(data, dict):
        return []
    assets = data.get("assets")
    if not isinstance(assets, list):
        return []
    ids: list[str] = []
    for a in assets:
        if not isinstance(a, dict):
            continue
        if str(a.get("type", "")).upper() != "IMAGE":
            continue
        aid = a.get("id")
        if isinstance(aid, str) and _IMMICH_ID_RE.match(aid):
            ids.append(aid)
    return ids


def immich_headers(api_key: str) -> dict[str, str]:
    """The auth header Immich expects. Pure."""
    return {"x-api-key": api_key, "Accept": "application/json"}


async def immich_album_asset_ids(base_url: str, api_key: str,
                                 album_id: str) -> list[str]:
    """Image asset ids in the configured Immich album, briefly cached.

    Never raises: connection trouble, a bad key, or a wrong album id all
    come back as an empty list (the saver falls back to the logo)."""
    base = (base_url or "").strip().rstrip("/")
    album = (album_id or "").strip()
    if not base or not api_key or not album or not _IMMICH_ID_RE.match(album):
        return []
    cache_key = (base, album)
    cached = _immich_cache.get()
    if isinstance(cached, tuple) and cached[0] == cache_key:
        return cached[1]
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(f"{base}/api/albums/{quote(album)}",
                            headers=immich_headers(api_key))
        if r.status_code != 200:
            return []
        ids = parse_immich_album(r.json())
    except Exception:
        return []
    _immich_cache.set((cache_key, ids))
    return ids


def invalidate_immich_cache() -> None:
    """Drop the cached album listing (settings save, tests)."""
    _immich_cache.invalidate()


def folder_photo_src(name: str) -> str:
    """Browser src for one folder photo, via the guarded serve route. Pure."""
    return "ui/screensaver/photo/local?name=" + quote(name)


def immich_photo_src(asset_id: str) -> str:
    """Browser src for one Immich asset, via the app's proxy. Pure."""
    return "ui/screensaver/photo/immich?id=" + quote(asset_id)


async def list_photos(settings) -> list[str]:
    """Servable image src strings for the configured photo source.

    "built-in" (and any unknown value) returns [] here; the ui router keeps
    handling that case itself (the Pi USB-drive path). Never raises.
    """
    source = normalize_photo_source(getattr(settings, "photo_source", ""))
    if source == "folder":
        folder = effective_photo_folder(settings)
        return [folder_photo_src(n) for n in list_folder_photos(folder)]
    if source == "immich":
        ids = await immich_album_asset_ids(
            getattr(settings, "immich_base_url", ""),
            getattr(settings, "immich_api_key", ""),
            getattr(settings, "immich_album_id", ""))
        return [immich_photo_src(i) for i in ids]
    if source == "urls":
        return parse_photo_urls(getattr(settings, "photo_urls", ""))
    return []
