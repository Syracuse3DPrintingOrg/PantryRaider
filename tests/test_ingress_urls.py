"""No root-absolute request URLs in the browser code (FoodAssistant-zh3l9).

Under Home Assistant Ingress every page sets <base href="<prefix>/">, so a
relative URL stays inside the add-on while a leading slash escapes to the Home
Assistant host and 404s. Pages and scripts must call fetch() with relative
paths, never "/inventory/...".

The Bandit Cubs page and the web app manifest link need absolute URLs (the
flasher resolves its manifest against the page address, not the base tag), so
they are written as "{{ ingress_path }}/...": absolute, with the Ingress prefix
when there is one (FoodAssistant-iaa0s, test_cub_flasher.py covers the page).
"""
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "service" / "app"
_ABSOLUTE_FETCH = re.compile(r"""fetch\(\s*(['"`])/(?!/)""")
_EXEMPT: set[str] = set()


def test_no_root_absolute_fetch_in_templates_or_scripts():
    offenders = []
    for path in [*APP.glob("templates/**/*.html"), *APP.glob("static/js/**/*.js")]:
        if "vendor" in path.parts or path.relative_to(APP).as_posix() in _EXEMPT:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _ABSOLUTE_FETCH.search(line):
                offenders.append(f"{path.relative_to(APP)}:{n}: {line.strip()}")
    assert not offenders, "root-absolute fetch() breaks under Ingress:\n" + "\n".join(offenders)


# An attribute URL with a leading slash (but not "//" and not a Jinja
# expression) escapes the Ingress prefix the same way a fetch() would.
_ABSOLUTE_ATTR = re.compile(r"""\b(?:href|src|action|manifest)=(['"])/(?![/{])""")


def test_no_root_absolute_link_attributes_in_templates():
    offenders = []
    for path in APP.glob("templates/**/*.html"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _ABSOLUTE_ATTR.search(line):
                offenders.append(f"{path.relative_to(APP)}:{n}: {line.strip()}")
    assert not offenders, "root-absolute link breaks under Ingress:\n" + "\n".join(offenders)


def test_manifest_link_carries_the_ingress_prefix():
    for name in ("base.html", "login.html"):
        text = (APP / "templates" / name).read_text(encoding="utf-8")
        assert 'rel="manifest" href="{{ ingress_path }}/manifest.webmanifest"' in text, name


def test_prefix_manifest_moves_root_urls_under_the_prefix():
    import sys
    sys.path.insert(0, str(APP.parent))
    from app.ingress import prefix_manifest
    raw = {"id": "/", "start_url": "/", "scope": "/", "name": "Pantry Raider",
           "icons": [{"src": "/static/pwa/icon-192.png", "sizes": "192x192"},
                     {"src": "https://cdn.example/x.png"}]}
    out = prefix_manifest(raw, "/api/hassio_ingress/tok/")
    assert out["start_url"] == "/api/hassio_ingress/tok/"
    assert out["scope"] == "/api/hassio_ingress/tok/"
    assert out["id"] == "/api/hassio_ingress/tok/"
    assert out["icons"][0]["src"] == "/api/hassio_ingress/tok/static/pwa/icon-192.png"
    assert out["icons"][1]["src"] == "https://cdn.example/x.png"
    assert out["name"] == "Pantry Raider"
    # No prefix: unchanged, and the input is never mutated.
    assert prefix_manifest(raw, "") is raw
    assert raw["start_url"] == "/"
