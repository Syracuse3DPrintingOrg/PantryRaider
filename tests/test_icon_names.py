"""Every Bootstrap Icons name the pages use exists in the vendored icon font.

An unknown name renders nothing at all, with no error anywhere: bi-fridge
left the sign-in page, the Settings header and the Inventory entries with a
blank space where the icon should be.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "service" / "app"
ICON_CSS = ROOT / "static" / "vendor" / "bootstrap-icons.min.css"

# Names a script finishes at run time ('bi-chevron-' + 'up' or 'down'); the
# finished names are checked below.
_BUILT_AT_RUN_TIME = {"chevron": ("chevron-up", "chevron-down")}

_NAME_RE = re.compile(r"(?<![\w-])bi-([a-z0-9]+(?:-[a-z0-9]+)*)")


def _known() -> set[str]:
    return set(re.findall(r"\.bi-([a-z0-9-]+)::before", ICON_CSS.read_text()))


def _sources():
    yield from ROOT.joinpath("templates").rglob("*.html")
    for path in ROOT.joinpath("static", "js").rglob("*.js"):
        if "vendor" not in path.parts:
            yield path


def test_every_icon_name_exists():
    known = _known()
    assert len(known) > 1000  # the real font, not an empty file
    missing = {}
    for path in _sources():
        for name in _NAME_RE.findall(path.read_text(errors="ignore")):
            if name in known:
                continue
            if name in _BUILT_AT_RUN_TIME and all(n in known for n in _BUILT_AT_RUN_TIME[name]):
                continue
            missing.setdefault(name, set()).add(str(path.relative_to(ROOT)))
    assert not missing, f"Icon names the icon font does not have: {missing}"
