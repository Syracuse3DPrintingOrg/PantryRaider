"""Printing router + document formatting (FoodAssistant-fb8x).

Two halves, neither needing a real printer or the network:
  * the pure document/label shaping helpers (recipe_to_blocks, html_to_text,
    the label spec builder), checked directly;
  * the router's disabled-state behaviour through a TestClient, so a request
    with printing turned off (or no queue chosen) answers with a clean 4xx JSON
    body and never a 500.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import print_document  # noqa: E402
from app.services import printing as printing_service  # noqa: E402
from app.routers import printing as printing_router  # noqa: E402


# -- Pure document formatting ----------------------------------------------


def test_fmt_qty_trims_whole_and_decimals():
    assert print_document._fmt_qty(2) == "2"
    assert print_document._fmt_qty(2.0) == "2"
    assert print_document._fmt_qty(1.5) == "1.5"
    assert print_document._fmt_qty(None) == ""
    assert print_document._fmt_qty("a pinch") == "a pinch"


def test_ingredient_line_shapes():
    assert print_document._ingredient_line("2 eggs") == "2 eggs"
    assert print_document._ingredient_line(
        {"name": "flour", "quantity": 1.5, "unit": "cup"}) == "1.5 cup flour"
    # A scaled quantity wins over the base quantity.
    assert print_document._ingredient_line(
        {"name": "sugar", "quantity": 1, "scaled_quantity": 2, "unit": "tbsp"}
    ) == "2 tbsp sugar"
    assert print_document._ingredient_line({"name": "salt"}) == "salt"


def test_recipe_to_blocks_covers_sections():
    recipe = {
        "title": "Test Bake",
        "servings": 4,
        "ingredients": [{"name": "flour", "quantity": 2, "unit": "cup"},
                        {"name": "salt"}],
        "steps": ["Mix.", "Bake."],
        "notes": "Rest 5 min.",
    }
    blocks = print_document.recipe_to_blocks(recipe)
    styles = [b.style for b in blocks]
    texts = [b.text for b in blocks]
    assert blocks[0].style == "title" and blocks[0].text == "Test Bake"
    assert "Serves 4" in texts
    assert "Ingredients" in texts and "Steps" in texts and "Notes" in texts
    assert "- 2 cup flour" in texts
    # Steps are numbered.
    assert any(t.startswith("1. Mix.") for t in texts)
    assert "step" in styles


def test_recipe_to_blocks_tolerates_empty():
    blocks = print_document.recipe_to_blocks({})
    assert blocks[0].style == "title"
    assert blocks[0].text == "Recipe"


def test_html_to_text_breaks_on_block_tags():
    html = "<h1>Soup</h1><p>Warm</p><ul><li>Salt</li><li>Pepper</li></ul>"
    text = print_document.html_to_text(html)
    lines = [ln for ln in text.splitlines() if ln]
    assert lines == ["Soup", "Warm", "Salt", "Pepper"]


def test_html_to_text_unescapes_entities():
    assert "Salt & Pepper" in print_document.html_to_text("<p>Salt &amp; Pepper</p>")


def test_text_to_blocks_keeps_title_and_lines():
    blocks = print_document.text_to_blocks("line one\n\nline two", title="Doc")
    assert blocks[0].style == "title" and blocks[0].text == "Doc"
    assert [b.text for b in blocks[1:]] == ["line one", "", "line two"]


def test_render_recipe_pdf_is_pdf_bytes():
    pdf = print_document.render_recipe_pdf_bytes(
        {"title": "T", "ingredients": [{"name": "x"}], "steps": ["do it"]})
    assert pdf[:5] == b"%PDF-"


def test_render_document_paginates_long_input():
    # Many lines force a second page; the output is still a single valid PDF.
    blocks = [print_document.Block("body", f"line {i}") for i in range(400)]
    pdf = print_document.render_document_pdf_bytes(blocks)
    assert pdf[:5] == b"%PDF-"


# -- Pure router helpers ----------------------------------------------------


def test_short_date_trims_timestamp():
    assert printing_router._short_date("2026-07-01 12:34:56") == "2026-07-01"
    assert printing_router._short_date("2026-07-01T09:00:00") == "2026-07-01"
    assert printing_router._short_date("") == ""


def test_normalize_source_only_known_values():
    assert printing_router._normalize_source("default") == "default"
    assert printing_router._normalize_source("llm") == "llm"
    assert printing_router._normalize_source("manual") == "manual"
    # Anything unexpected reads as user-entered (no badge), never garbage.
    assert printing_router._normalize_source("bogus") == "manual"
    assert printing_router._normalize_source("") == "manual"


def test_spec_from_fields_prefers_explicit_over_item():
    item = {"name": "Milk", "added_date": "2026-07-01 08:00:00",
            "best_before_date": "2026-07-10"}
    body = printing_router.LabelIn(product_id=1)
    spec = printing_router._spec_from_fields(body, item)
    assert spec.name == "Milk"
    assert spec.added == "2026-07-01"
    assert spec.best_by == "2026-07-10"
    # No source is tracked in Grocy, so an item-derived label defaults to manual.
    assert spec.best_by_source == "manual"

    # Explicit fields win.
    body2 = printing_router.LabelIn(name="Oat Milk", best_by_source="llm")
    spec2 = printing_router._spec_from_fields(body2, item)
    assert spec2.name == "Oat Milk"
    assert spec2.best_by_source == "llm"


# -- Router behaviour through a TestClient ----------------------------------


@pytest.fixture()
def client(tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    from app.config import settings
    from app.main import app
    saved = {k: getattr(settings, k) for k in (
        "data_dir", "grocy_base_url", "grocy_api_key", "vision_provider",
        "gemini_api_key", "auth_required", "auth_password", "printing_enabled",
        "label_printer_queue", "document_printer_queue")}
    settings.data_dir = str(tmp_path)
    settings.grocy_base_url = "http://grocy.test"
    settings.grocy_api_key = "k"
    settings.vision_provider = "gemini"
    settings.gemini_api_key = "k"
    settings.auth_required = False
    settings.auth_password = ""
    settings.printing_enabled = False
    settings.label_printer_queue = ""
    settings.document_printer_queue = ""
    try:
        with TestClient(app) as c:
            c._settings = settings
            yield c
    finally:
        for k, v in saved.items():
            setattr(settings, k, v)
        os.chdir(cwd)


def test_queues_endpoint_never_500(client):
    r = client.get("/printing/queues")
    assert r.status_code == 200
    body = r.json()
    assert "queues" in body and "available" in body
    assert isinstance(body["queues"], list)


def test_label_refused_when_printing_disabled(client):
    r = client.post("/printing/label", json={"name": "Milk"})
    # A clean 409 with a user-facing message, never a 500.
    assert r.status_code == 409
    assert "turned off" in r.json()["detail"].lower()


def test_document_refused_when_disabled(client):
    r = client.post("/printing/document", json={"text": "hello"})
    assert r.status_code == 409


def test_label_refused_when_no_queue_chosen(client, monkeypatch):
    # Printing on, print stack present, but no label queue picked yet.
    client._settings.printing_enabled = True
    monkeypatch.setattr(printing_service, "printing_available", lambda: True)
    r = client.post("/printing/label", json={"name": "Milk"})
    assert r.status_code == 409
    assert "label printer" in r.json()["detail"].lower()


def test_preview_allowed_while_disabled(client):
    # A preview is just an image; it works before printing is turned on.
    r = client.post("/printing/label/preview", json={"name": "Sample"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_decorative_preview_allowed_while_disabled(client):
    r = client.post("/printing/decorative/preview", json={"text": "Paprika"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"


def test_label_prints_when_enabled(client, monkeypatch):
    client._settings.printing_enabled = True
    client._settings.label_printer_queue = "Zebra"
    monkeypatch.setattr(printing_service, "printing_available", lambda: True)
    sent = {}

    def _fake_print(queue, data, *, options=None):
        sent["queue"] = queue
        sent["len"] = len(data)
        return printing_service.PrintResult(ok=True, job_id="Zebra-1")

    monkeypatch.setattr(printing_service, "print_bytes", _fake_print)
    r = client.post("/printing/label", json={"name": "Milk", "best_by": "2026-07-10"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["job_id"] == "Zebra-1"
    assert sent["queue"] == "Zebra" and sent["len"] > 0


def test_label_prints_to_inherited_fleet_default(client, monkeypatch):
    # A device with NO local label queue but an inherited fleet default (pulled
    # from the main server) prints to the inherited queue (FoodAssistant-7u7z).
    client._settings.printing_enabled = True
    client._settings.label_printer_queue = ""  # no local choice
    monkeypatch.setattr(client._settings, "fleet_label_printer_queue", "FleetZebra", raising=False)
    monkeypatch.setattr(printing_service, "printing_available", lambda: True)
    sent = {}

    def _fake_print(queue, data, *, options=None):
        sent["queue"] = queue
        return printing_service.PrintResult(ok=True, job_id="FleetZebra-3")

    monkeypatch.setattr(printing_service, "print_bytes", _fake_print)
    r = client.post("/printing/label", json={"name": "Milk"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert sent["queue"] == "FleetZebra"


def test_local_queue_overrides_inherited_fleet_default(client, monkeypatch):
    # When the device has its own queue, it wins over the inherited fleet default.
    client._settings.printing_enabled = True
    client._settings.label_printer_queue = "MyLocalZebra"
    monkeypatch.setattr(client._settings, "fleet_label_printer_queue", "FleetZebra", raising=False)
    monkeypatch.setattr(printing_service, "printing_available", lambda: True)
    sent = {}

    def _fake_print(queue, data, *, options=None):
        sent["queue"] = queue
        return printing_service.PrintResult(ok=True, job_id="MyLocalZebra-1")

    monkeypatch.setattr(printing_service, "print_bytes", _fake_print)
    r = client.post("/printing/label", json={"name": "Milk"})
    assert r.status_code == 200
    assert sent["queue"] == "MyLocalZebra"


def test_document_text_prints_when_enabled(client, monkeypatch):
    client._settings.printing_enabled = True
    client._settings.document_printer_queue = "OfficeLaser"
    monkeypatch.setattr(printing_service, "printing_available", lambda: True)
    captured = {}

    def _fake_print(queue, data, *, options=None):
        captured["queue"] = queue
        return printing_service.PrintResult(ok=True, job_id="OfficeLaser-9")

    monkeypatch.setattr(printing_service, "print_bytes", _fake_print)
    r = client.post("/printing/document",
                    json={"title": "Soup", "text": "Ingredients\n- water"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert captured["queue"] == "OfficeLaser"


def test_document_empty_body_is_400(client, monkeypatch):
    client._settings.printing_enabled = True
    client._settings.document_printer_queue = "OfficeLaser"
    monkeypatch.setattr(printing_service, "printing_available", lambda: True)
    r = client.post("/printing/document", json={})
    assert r.status_code == 400


# -- Install-now endpoint (FoodAssistant-gyri) ------------------------------
# The button appears only when no print stack is present. On a Pi it drives the
# host bridge; on a server it returns guidance. The bridge is always mocked here.


class _FakeResp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _FakeBridgeClient:
    """Async-context-manager stand-in for services.bridge.bridge_client."""
    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc

    def __call__(self, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, *a, **k):
        if self._exc:
            raise self._exc
        return self._resp


def test_install_noop_when_already_available(client, monkeypatch):
    monkeypatch.setattr(printing_service, "printing_available", lambda: True)
    r = client.post("/printing/install")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_install_server_mode_returns_guidance(client, monkeypatch):
    monkeypatch.setattr(printing_service, "printing_available", lambda: False)
    monkeypatch.setattr(client._settings, "deployment_mode", "server")
    r = client.post("/printing/install")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    # User-forward guidance mentions the with-printing profile.
    assert "with-printing" in body["message"]


def test_install_pi_calls_bridge_success(client, monkeypatch):
    monkeypatch.setattr(printing_service, "printing_available", lambda: False)
    monkeypatch.setattr(client._settings, "deployment_mode", "pi_hosted")
    from app.services import bridge as bridge_mod
    fake = _FakeBridgeClient(resp=_FakeResp(200, {"ok": True, "log": "installed"}))
    monkeypatch.setattr(bridge_mod, "bridge_client", fake)
    r = client.post("/printing/install")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["log"] == "installed"


def test_install_pi_bridge_failure_is_502(client, monkeypatch):
    # pi_hosted is used here (not pi_remote) only to avoid the satellite setup
    # redirect; the bridge-failure branch is identical for both Pi modes.
    monkeypatch.setattr(printing_service, "printing_available", lambda: False)
    monkeypatch.setattr(client._settings, "deployment_mode", "pi_hosted")
    from app.services import bridge as bridge_mod
    fake = _FakeBridgeClient(resp=_FakeResp(500, {"ok": False, "error": "apt failed"}))
    monkeypatch.setattr(bridge_mod, "bridge_client", fake)
    r = client.post("/printing/install")
    assert r.status_code == 502
    assert r.json()["ok"] is False


def test_install_pi_bridge_unreachable_is_502(client, monkeypatch):
    monkeypatch.setattr(printing_service, "printing_available", lambda: False)
    monkeypatch.setattr(client._settings, "deployment_mode", "pi_hosted")
    from app.services import bridge as bridge_mod
    fake = _FakeBridgeClient(exc=RuntimeError("connection refused"))
    monkeypatch.setattr(bridge_mod, "bridge_client", fake)
    r = client.post("/printing/install")
    assert r.status_code == 502
    assert r.json()["ok"] is False


# -- Discover + add printers (FoodAssistant-r9a4) ---------------------------
# Discovery and add branch by deployment mode: a Pi appliance drives the host
# bridge, a plain server acts locally. The bridge is always mocked here.


def test_discover_refused_when_no_stack(client, monkeypatch):
    # No print service on the device: a clean, user-facing note, never a 500.
    monkeypatch.setattr(printing_service, "printing_available", lambda: False)
    r = client.get("/printing/discover")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["printers"] == [] and body["message"]


def test_discover_server_mode_runs_locally(client, monkeypatch):
    monkeypatch.setattr(printing_service, "printing_available", lambda: True)
    monkeypatch.setattr(client._settings, "deployment_mode", "server")
    monkeypatch.setattr(printing_service, "discover_printers", lambda: [
        {"name": "Brother", "uri": "ipp://h/ipp/print", "kind": "driverless",
         "driver": "everywhere", "info": "Brother"}])
    r = client.get("/printing/discover")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["printers"][0]["name"] == "Brother"


def test_discover_pi_mode_calls_bridge(client, monkeypatch):
    monkeypatch.setattr(printing_service, "printing_available", lambda: True)
    monkeypatch.setattr(client._settings, "deployment_mode", "pi_hosted")
    from app.services import bridge as bridge_mod
    fake = _FakeBridgeClient(resp=_FakeResp(200, {
        "ok": True, "printers": [{"name": "Zebra", "uri": "socket://h:9100",
                                  "kind": "socket", "driver": "raw"}]}))
    monkeypatch.setattr(bridge_mod, "bridge_client", fake)
    r = client.get("/printing/discover")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["printers"][0]["uri"] == "socket://h:9100"


def test_add_refused_when_no_stack(client, monkeypatch):
    monkeypatch.setattr(printing_service, "printing_available", lambda: False)
    r = client.post("/printing/add", json={
        "name": "Brother", "connection": "ipp://h/ipp/print", "model": "everywhere"})
    assert r.status_code == 409


def test_add_rejects_unsafe_name(client, monkeypatch):
    monkeypatch.setattr(printing_service, "printing_available", lambda: True)
    monkeypatch.setattr(client._settings, "deployment_mode", "server")
    r = client.post("/printing/add", json={
        "name": "bad name", "connection": "ipp://h/ipp/print"})
    assert r.status_code == 400


def test_add_rejects_unsafe_connection(client, monkeypatch):
    monkeypatch.setattr(printing_service, "printing_available", lambda: True)
    monkeypatch.setattr(client._settings, "deployment_mode", "server")
    r = client.post("/printing/add", json={
        "name": "Good", "connection": "socket://h;reboot"})
    assert r.status_code == 400


def test_add_server_mode_success_then_queue_appears(client, monkeypatch):
    monkeypatch.setattr(printing_service, "printing_available", lambda: True)
    monkeypatch.setattr(client._settings, "deployment_mode", "server")
    registry: list[dict] = []

    def _fake_add(name, connection, model="everywhere"):
        registry.append({"name": name, "state": "idle", "is_default": False})
        return printing_service.PrintResult(ok=True)

    monkeypatch.setattr(printing_service, "add_printer", _fake_add)
    monkeypatch.setattr(printing_service, "list_queues", lambda: list(registry))
    r = client.post("/printing/add", json={
        "name": "Brother", "connection": "ipp://h/ipp/print", "model": "everywhere"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # The new queue now shows up in the list the dropdowns read.
    q = client.get("/printing/queues")
    assert "Brother" in [x["name"] for x in q.json()["queues"]]


def test_add_pi_mode_calls_bridge(client, monkeypatch):
    monkeypatch.setattr(printing_service, "printing_available", lambda: True)
    monkeypatch.setattr(client._settings, "deployment_mode", "pi_hosted")
    from app.services import bridge as bridge_mod
    fake = _FakeBridgeClient(resp=_FakeResp(200, {"ok": True}))
    monkeypatch.setattr(bridge_mod, "bridge_client", fake)
    r = client.post("/printing/add", json={
        "name": "Brother", "connection": "ipp://h/ipp/print", "model": "everywhere"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_add_pi_mode_bridge_failure_is_502(client, monkeypatch):
    monkeypatch.setattr(printing_service, "printing_available", lambda: True)
    monkeypatch.setattr(client._settings, "deployment_mode", "pi_hosted")
    from app.services import bridge as bridge_mod
    fake = _FakeBridgeClient(resp=_FakeResp(200, {"ok": False, "error": "lpadmin failed"}))
    monkeypatch.setattr(bridge_mod, "bridge_client", fake)
    r = client.post("/printing/add", json={
        "name": "Brother", "connection": "ipp://h/ipp/print"})
    assert r.status_code == 502
    assert r.json()["ok"] is False


def test_setup_payload_round_trips_fleet_printer_queues():
    # Regression: the fleet default fields were missing from SetupPayload, so
    # /setup/save silently dropped them and a server could never set the fleet
    # default printer (FoodAssistant-7u7z).
    from app.routers.setup import SetupPayload
    p = SetupPayload(fleet_label_printer_queue="Brother",
                     fleet_document_printer_queue="Zebra")
    assert p.fleet_label_printer_queue == "Brother"
    assert p.fleet_document_printer_queue == "Zebra"
    dumped = SetupPayload(fleet_label_printer_queue="Brother").model_dump(exclude_unset=True)
    assert dumped == {"fleet_label_printer_queue": "Brother"}
