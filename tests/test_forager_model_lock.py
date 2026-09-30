"""Forager locks the LLM model choice (FoodAssistant-rgwa).

When the AI provider is Forager (the managed proxy, stored as "cloud"), Forager
picks and runs the model, so the direct-provider model and key controls do not
apply. The AI & Scanning pane gates them: the vision section hides its
model/key blocks and shows a managed-model note, and the barcode-enrichment
Model override collapses to the same note whenever the effective enrichment
provider is Forager. A direct provider is unaffected. Saving under Forager
never requires the direct-provider model/key fields.

Three checks:
  1. The rendered pane carries the note markup + gating hooks (always runs).
  2. The client gating logic actually toggles them (node, skipped if absent).
  3. Saving under Forager does not require the model/key fields (server-side).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(_SERVICE))

from app.config import settings  # noqa: E402

_HELPERS = _SERVICE / "app" / "static" / "js" / "setup" / "helpers.js"


# --------------------------------------------------------------------------- #
# 1. Render: the note markup and gating hooks are in the pane.
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app

    cwd = os.getcwd()
    os.chdir(_SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "auth_password", "")
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _render(client) -> str:
    with patch.object(type(settings), "is_configured", lambda self: True), \
         patch("app.routers.setup.is_raspberry_pi", return_value=False), \
         patch("app.templating.is_raspberry_pi", return_value=False):
        r = client.get("/setup")
    assert r.status_code == 200
    return r.text


def test_pane_has_managed_note_and_gating_hooks(client):
    html = _render(client)
    # The managed-model note (user-forward copy, no jargon) is present for both
    # the vision section and the enrichment override, hidden until Forager is
    # the effective provider.
    assert 'id="forager-managed-note"' in html
    assert 'id="enrich-forager-note"' in html
    assert html.count("Forager manages the AI model for you") == 2
    # The enrichment override row has a wrapper the gate can collapse.
    assert 'id="enrich-model-row"' in html
    # Both notes ship hidden (d-none) so a direct provider never sees them.
    for anchor in ('id="forager-managed-note"', 'id="enrich-forager-note"'):
        tag = html[html.index(anchor):html.index(anchor) + 200]
        assert "d-none" in tag


def test_gating_logic_references_forager():
    """The client logic keys off the "cloud" (Forager) value and toggles the
    note + row, so the wiring cannot silently drop out of the template."""
    src = _HELPERS.read_text()
    assert "forager-managed-note" in src
    assert "enrich-forager-note" in src
    assert "enrich-model-row" in src
    assert "syncEnrichForagerGate" in src
    # showProvider must consult syncEnrichForagerGate so the enrichment override
    # follows a vision provider left on "same as vision provider".
    show = src[src.index("function showProvider"):]
    show = show[:show.index("\n}\n") + 3]
    assert "forager-managed-note" in show
    assert "syncEnrichForagerGate" in show


# --------------------------------------------------------------------------- #
# 2. Behavior: node runs the real gating functions against a DOM stub.
# --------------------------------------------------------------------------- #
_NODE = shutil.which("node")

_HARNESS_PRELUDE = r"""
function mkClassList(initial) {
  const s = new Set(initial || []);
  return {
    add: c => s.add(c),
    remove: c => s.delete(c),
    contains: c => s.has(c),
    toggle: (c, force) => {
      if (force === undefined) { if (s.has(c)) { s.delete(c); return false; } s.add(c); return true; }
      if (force) { s.add(c); return true; } s.delete(c); return false;
    },
  };
}
function mkEl(id, cls) {
  return { id, value: '', classList: mkClassList(cls || []), style: {}, innerHTML: '' };
}
const _byId = {};
const _providerFields = [];
function reg(el, isProviderField) {
  _byId[el.id] = el;
  if (isProviderField) _providerFields.push(el);
  return el;
}
var document = {
  getElementById: id => _byId[id] || null,
  querySelectorAll: sel => (sel === '.provider-fields' ? _providerFields : []),
  addEventListener: () => {},
  documentElement: { getAttribute: () => null },
};
var window = {};
var AI_MODELS = {
  gemini: [{ id: 'gemini-2.5-flash', note: 'x' }],
  openai: [{ id: 'gpt-4o-mini', note: 'x' }],
  anthropic: [{ id: 'claude-haiku-4-5-20251001', note: 'x' }],
  ollama: [{ id: 'llava:7b', note: 'x' }],
};

// Register the elements the gate touches.
reg(mkEl('vision_provider'));
reg(mkEl('forager-managed-note', ['d-none']));
reg(mkEl('enrich_provider'));
reg(mkEl('enrich-model-row'));
reg(mkEl('enrich-forager-note', ['d-none']));
['gemini', 'openai', 'anthropic', 'ollama'].forEach(p =>
  reg(mkEl('fields-' + p, ['provider-fields']), true));

function visible(id) {
  const el = document.getElementById(id);
  return !el.classList.contains('d-none') && el.style.display !== 'none';
}
function scenario(vision, enrich) {
  document.getElementById('vision_provider').value = vision;
  document.getElementById('enrich_provider').value = enrich;
  showProvider();
  return {
    foragerNote: visible('forager-managed-note'),
    geminiFields: visible('fields-gemini'),
    enrichRow: visible('enrich-model-row'),
    enrichNote: visible('enrich-forager-note'),
  };
}
"""

_HARNESS_ASSERT = r"""
const out = {
  forager: scenario('cloud', ''),          // Forager for vision, enrich follows it
  gemini: scenario('gemini', ''),          // direct provider, enrich follows it
  enrichForager: scenario('gemini', 'cloud'),  // vision direct, enrich = Forager
};
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(_NODE is None, reason="node is not available")
def test_forager_selection_gates_model_controls(tmp_path):
    harness = tmp_path / "gate_harness.js"
    harness.write_text(_HARNESS_PRELUDE + _HELPERS.read_text() + _HARNESS_ASSERT)
    proc = subprocess.run(
        [_NODE, str(harness)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    res = json.loads(proc.stdout.strip().splitlines()[-1])

    # Forager as the vision provider: the managed note shows, the direct-provider
    # model/key block is hidden, and the enrichment override (following it) is
    # collapsed to the same note.
    assert res["forager"] == {
        "foragerNote": True, "geminiFields": False,
        "enrichRow": False, "enrichNote": True,
    }
    # A direct provider: its model/key fields show, no managed note anywhere,
    # and the enrichment override is available.
    assert res["gemini"] == {
        "foragerNote": False, "geminiFields": True,
        "enrichRow": True, "enrichNote": False,
    }
    # Forager chosen only for enrichment: the vision fields stay, but the
    # enrichment Model override collapses to the managed note.
    assert res["enrichForager"]["enrichRow"] is False
    assert res["enrichForager"]["enrichNote"] is True
    assert res["enrichForager"]["geminiFields"] is True


# --------------------------------------------------------------------------- #
# 3. Save: posting under Forager does not require the model/key fields.
# --------------------------------------------------------------------------- #
def test_save_under_forager_omits_model_and_key_fields():
    from app.routers.setup import SetupPayload

    payload = SetupPayload(vision_provider="cloud")
    data = payload.model_dump(exclude_unset=True)
    # Only the field actually posted is present; the direct-provider model and
    # key fields are absent, so a Forager save cannot clobber the stored values
    # the user set before linking (they are kept by exclude_unset).
    assert data == {"vision_provider": "cloud"}
    for field in ("gemini_model", "openai_model", "anthropic_model", "ollama_model",
                  "gemini_api_key", "openai_api_key", "anthropic_api_key"):
        assert field not in data
