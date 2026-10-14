"""Discover cameras from a Frigate NVR (FoodAssistant-7ror).

Frigate publishes its configuration at ``GET /api/config``: a JSON object with a
``cameras`` map of name to per-camera config. Each camera has a still image at
``GET /api/<name>/latest.jpg`` and a browser-friendly MJPEG stream at
``GET /api/<name>`` (the go2rtc/RTSP feeds are not playable in a plain page).

This mirrors the Home Assistant camera discovery: a probe that lists the camera
names, and URL builders that turn a name into the snapshot and stream URLs an
added camera entry stores. Frigate needs no login by default, so the URLs carry
no secret and the existing manual-camera resolve/proxy path serves them.

The HTTP fetch is injectable so the probe is unit-testable without a network,
and every failure is soft: an unreachable or empty Frigate yields a clear error,
never an exception.
"""
from __future__ import annotations


import httpx


def normalize_base(base_url: str) -> str:
    """Trim a Frigate base URL and default the scheme to http.

    Users type ``frigate.local:5000`` as often as a full URL; assume http so the
    probe still works. Returns "" for an empty input."""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if not base.startswith(("http://", "https://")):
        base = "http://" + base
    return base


def snapshot_url(base_url: str, name: str) -> str:
    """The still-image URL for a Frigate camera: ``<base>/api/<name>/latest.jpg``."""
    base = normalize_base(base_url)
    name = (name or "").strip()
    if not base or not name:
        return ""
    return f"{base}/api/{name}/latest.jpg"


def stream_url(base_url: str, name: str) -> str:
    """The MJPEG stream URL for a Frigate camera: ``<base>/api/<name>``."""
    base = normalize_base(base_url)
    name = (name or "").strip()
    if not base or not name:
        return ""
    return f"{base}/api/{name}"


def parse_config(config: object) -> list[str]:
    """Camera names from a parsed Frigate ``/api/config`` body.

    Frigate keys its cameras under a ``cameras`` map; the names are the keys.
    Anything unexpected yields an empty list so the caller fails soft."""
    if not isinstance(config, dict):
        return []
    cams = config.get("cameras")
    if not isinstance(cams, dict):
        return []
    return [str(name) for name in cams.keys() if str(name).strip()]


def discover(base_url: str, fetch=None) -> dict:
    """List the cameras a Frigate instance exposes.

    Returns ``{"ok": True, "base_url": <normalized>, "cameras": [...]}`` where
    each camera is ``{name, snapshot_url, stream_url}``, or ``{"ok": False,
    "error": <message>}``. ``fetch`` is injectable for tests; by default it does
    a short blocking GET of ``/api/config``. Never raises: an unreachable host or
    an empty camera list comes back as a friendly, actionable error."""
    base = normalize_base(base_url)
    if not base:
        return {"ok": False, "error": "Enter the Frigate address, e.g. http://frigate.local:5000."}

    def _default_fetch(url: str):
        return httpx.get(url, timeout=8.0, follow_redirects=True)

    fetch = fetch or _default_fetch
    try:
        resp = fetch(f"{base}/api/config")
    except Exception as exc:
        return {"ok": False,
                "error": f"Could not reach Frigate at {base}: {exc}"}
    code = getattr(resp, "status_code", 0)
    if code != 200:
        return {"ok": False,
                "error": f"Frigate returned HTTP {code} from {base}/api/config."}
    try:
        config = resp.json()
    except Exception:
        return {"ok": False,
                "error": "Frigate did not return the expected configuration."}
    names = parse_config(config)
    if not names:
        return {"ok": False,
                "error": f"No cameras found on Frigate at {base}."}
    cameras = [
        {"name": name,
         "snapshot_url": snapshot_url(base, name),
         "stream_url": stream_url(base, name)}
        for name in names
    ]
    cameras.sort(key=lambda c: c["name"].lower())
    return {"ok": True, "base_url": base, "cameras": cameras}
