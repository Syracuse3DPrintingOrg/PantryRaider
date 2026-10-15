"""The printer search shows what the network reports as plain text.

A printer advertises its own name and description over the network, so a
device on the LAN could put markup in them. The discovery card wrote the
label and URI into innerHTML as they came (security review, Oct 2026).
"""
import re
from pathlib import Path

PANES = Path(__file__).resolve().parents[1] / "service" / "app" / "static" / "js" / "setup" / "panes.js"


def _find_printers() -> str:
    src = PANES.read_text()
    start = src.index("async function findPrinters(")
    end = src.index("\n}\n", start)
    return src[start:end]


def test_discovered_fields_are_escaped():
    body = _find_printers()
    assert "${PR.esc(label || '')}" in body
    assert "${PR.esc(p.uri || '')}" in body
    # No interpolation of a printer field or a message without PR.esc.
    for m in re.finditer(r"\$\{([^}]*)\}", body):
        expr = m.group(1).strip()
        if re.search(r"\b(label|p\.(uri|info|name)|e\.message|d\.(message|detail))\b", expr):
            assert expr.startswith("PR.esc"), expr
