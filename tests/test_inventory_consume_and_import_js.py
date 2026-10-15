"""Inventory consume and Import all report what really happened.

FoodAssistant-51o2x follow-ups: the inventory "Mark consumed" button posted to
the redirecting form endpoint with redirect: 'manual', so it could never see a
Grocy refusal and always said it worked. "Import all" kept every line queued
after a partial failure, so retrying added the successful ones twice.
"""
from __future__ import annotations

import re
from pathlib import Path

_JS = Path(__file__).resolve().parents[1] / "service" / "app" / "static" / "js"


def _function(src: str, name: str) -> str:
    start = src.index(f"async function {name}(")
    nxt = re.search(r"\n(async )?function ", src[start + 1:])
    return src[start: start + 1 + nxt.start()] if nxt else src[start:]


def test_consume_checks_the_response_before_claiming_success():
    body = _function((_JS / "pages" / "inventory.js").read_text(), "consume")
    assert "redirect: 'manual'" not in body
    assert "inventory/consume/" in body
    assert body.index("if (!r.ok)") < body.index("Consumed:")


def test_import_all_keeps_only_the_failed_lines_queued():
    body = _function((_JS / "manage-pantry.js").read_text(), "importAll")
    assert "queue = sent.filter((_, i) => failed.has(i))" in body
    assert "r.index" in body
    assert "PR.esc(errs)" in body


def test_inventory_import_reports_each_lines_index():
    src = (Path(__file__).resolve().parents[1] / "service" / "app" / "routers"
           / "inventory.py").read_text()
    assert '"index": i, "status": "ok"' in src
    assert '"index": i, "status": "error"' in src
