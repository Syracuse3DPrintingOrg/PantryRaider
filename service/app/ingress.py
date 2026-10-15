"""Home Assistant Ingress support.

When Pantry Raider runs as an HA add-on, the Supervisor proxies requests at
``/api/hassio_ingress/<token>/`` and strips that prefix before forwarding, so
routing is unchanged. The browser, however, still sees the prefixed URL, so any
link the app emits must carry it. HA tells us the prefix via the
``X-Ingress-Path`` request header.

Strategy: templates set ``<base href="{{ ingress_path }}/">`` and use
root-relative links (no leading slash), which the browser resolves against the
base. Server-side HTTP redirects are not affected by the base tag, so those are
prefixed explicitly via :func:`ingress_redirect`.

Outside HA the header is absent, ``ingress_path`` is "" and everything behaves
exactly as before.
"""
from fastapi import Request
from fastapi.responses import RedirectResponse


def ingress_path(request: Request) -> str:
    """The browser-facing path prefix for this request ("" when not via HA)."""
    return request.headers.get("X-Ingress-Path", "").rstrip("/")


def ingress_redirect(request: Request, path: str, status_code: int = 303) -> RedirectResponse:
    """RedirectResponse that keeps the HA Ingress prefix when present."""
    target = path if path.startswith("http") else ingress_path(request) + path
    return RedirectResponse(target, status_code=status_code)


def template_globals(request: Request) -> dict:
    """Context processor: expose ingress_path to every template render."""
    return {"ingress_path": ingress_path(request)}


def prefix_manifest(manifest: dict, prefix: str) -> dict:
    """The web app manifest with its root-absolute URLs moved under ``prefix``.

    The manifest's start_url, scope, id and icon paths are written for the
    site root. Under Ingress the browser resolves them against the Home
    Assistant host instead, so each one gets the Ingress prefix. With no
    prefix the manifest comes back unchanged. Pure.
    """
    prefix = (prefix or "").rstrip("/")
    if not prefix:
        return manifest

    def _fix(url):
        if isinstance(url, str) and url.startswith("/") and not url.startswith("//"):
            return prefix + url
        return url

    out = dict(manifest)
    for key in ("id", "start_url", "scope"):
        if key in out:
            out[key] = _fix(out[key])
    if isinstance(out.get("icons"), list):
        out["icons"] = [{**i, "src": _fix(i.get("src"))} if isinstance(i, dict) else i
                        for i in out["icons"]]
    return out
