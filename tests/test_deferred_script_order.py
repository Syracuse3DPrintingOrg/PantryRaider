"""Script-order guard for the deferred shared kiosk scripts.

base.html loads its shared kiosk scripts with ``defer`` (kiosk-status.js,
screensaver.js, timer-chips.js and friends). Deferred scripts run after the
whole document has parsed, in document order. A page script loaded WITHOUT
defer therefore runs before them, so any load-time check like
``if (window.PRKioskStatus)`` finds nothing, silently takes its fallback, and
nobody notices until a downstream feature goes quiet. That is exactly how the
NeoKey stopped taking the Manage screen's colors after the defer change
(0.18.97): manage-pantry.js was a plain end-of-body script, so the Manage
heartbeat never left the page.

The rule pinned here is the precise one: on a given page, a script loaded
without defer must not READ a global whose PRODUCER is loaded with defer on
that same page. Plain scripts in document order are fine (split.html loads
kiosk-idle.js then screensaver.js, both plain), a producer defining its own
global is fine (kiosk-status.js defines PRKioskStatus), and a read that waits
for DOMContentLoaded is fine. The check is static (template tags plus the
static file text), so it needs no browser and runs in milliseconds.
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATES = _ROOT / "service" / "app" / "templates"
_STATIC = _ROOT / "service" / "app" / "static"

# Global -> the static file that defines it. A page that loads the producer
# with defer makes that global unavailable to every plain script on the page.
PRODUCERS = {
    "PRKioskStatus": "js/kiosk-status.js",
    "PRScreensaver": "js/screensaver.js",
    "prTimerChips": "js/timer-chips.js",
    "PRHaEvents": "js/ha-events.js",
    "__prKioskActivity": "js/kiosk-idle.js",
}

_SCRIPT_TAG = re.compile(r"<script\b([^>]*)\bsrc=[\"']([^\"']+)[\"']([^>]*)>", re.I)
_EXTENDS = re.compile(r"{%\s*extends\s+[\"']([^\"']+)[\"']\s*%}")
_VERSION_SUFFIX = re.compile(r"\?v=.*$")


def _static_rel(src: str) -> str | None:
    """The static-relative path a script tag points at, or None if external."""
    path = _VERSION_SUFFIX.sub("", src)
    if "://" in path or path.startswith("//") or "static/" not in path:
        return None
    return path.split("static/", 1)[1]


def _tags(template: Path) -> list[tuple[str, bool]]:
    """(static-relative src, deferred) for every local script tag on the page,
    including the ones inherited from the template it extends."""
    text = template.read_text()
    out: list[tuple[str, bool]] = []
    parent = _EXTENDS.search(text)
    if parent:
        base = _TEMPLATES / parent.group(1)
        if base.exists():
            out.extend(_tags(base))
    for m in _SCRIPT_TAG.finditer(text):
        attrs = f" {m.group(1)} {m.group(3)} ".lower()
        deferred = " defer " in attrs or " defer=" in attrs or " async " in attrs
        rel = _static_rel(m.group(2))
        if rel:
            out.append((rel, deferred))
    return out


def _reads(js: str, name: str) -> bool:
    """True when the script reads ``name`` at load time.

    Definitions (``window.name =``) do not count: that is the producer. A
    reference after a DOMContentLoaded or load listener has been registered
    is treated as waiting, which is the pattern base.html and timers.html use.
    """
    for m in re.finditer(r"\b" + re.escape(name) + r"\b", js):
        after = js[m.end(): m.end() + 40]
        if re.match(r"\s*=[^=]", after):
            continue                                  # a definition
        before = js[: m.start()]
        if re.search(r"addEventListener\(\s*['\"](DOMContentLoaded|load)['\"]", before):
            continue                                  # waits for the page
        return True
    return False


def _offenders() -> list[str]:
    found = []
    for template in sorted(_TEMPLATES.rglob("*.html")):
        tags = _tags(template)
        deferred_files = {rel for rel, deferred in tags if deferred}
        for rel, deferred in tags:
            if deferred:
                continue
            path = _STATIC / rel
            if not path.exists():
                continue
            js = path.read_text()
            for name, producer in PRODUCERS.items():
                if producer in deferred_files and producer != rel and _reads(js, name):
                    found.append(f"{template.relative_to(_ROOT)}: {rel} is loaded "
                                 f"without defer but reads {name}, whose producer "
                                 f"{producer} is deferred on that page")
    return found


def test_no_plain_script_reads_a_global_its_page_defers():
    assert not _offenders(), "\n".join(_offenders())


def test_the_rule_catches_the_manage_regression():
    """Strip defer from the Manage script in memory and the rule must fire."""
    add = _TEMPLATES / "add.html"
    original = add.read_text()
    broken = original.replace(
        'manage-pantry.js?v={{ app_version }}" defer>',
        'manage-pantry.js?v={{ app_version }}">')
    assert broken != original
    try:
        add.write_text(broken)
        hits = [o for o in _offenders() if "manage-pantry.js" in o]
        assert hits and "PRKioskStatus" in hits[0]
    finally:
        add.write_text(original)


def test_manage_pantry_script_is_deferred():
    """The specific regression: the Manage page must keep sending the kiosk
    heartbeat that colors the NeoKey, which needs PRKioskStatus to exist."""
    add = (_TEMPLATES / "add.html").read_text()
    tag = re.search(r"<script[^>]*manage-pantry\.js[^>]*>", add)
    assert tag is not None and "defer" in tag.group(0)
