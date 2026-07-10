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
from app.services import best_by_provenance  # noqa: E402
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

    async def get(self, url, *a, **k):
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


# -- Layout designer endpoints (FoodAssistant-bwl1 / -or5e) -----------------


def test_presets_endpoint_lists_formats(client):
    r = client.get("/printing/label/presets")
    assert r.status_code == 200
    presets = r.json()["presets"]
    keys = {p["key"] for p in presets}
    assert {"2x1", "4x6_shipping", "spice_square"} <= keys
    for p in presets:
        # Each preset carries its size, dpi, and a starting layout so the
        # designer can both resize the stock and seed a design in one pick.
        assert set(p.keys()) == {"key", "name", "width_in", "height_in",
                                 "dpi", "layout"}
        layout = p["layout"]
        assert isinstance(layout, dict)
        assert isinstance(layout.get("elements"), list) and layout["elements"]
        for el in layout["elements"]:
            assert "field" in el
            for frac in ("x", "y", "w", "h"):
                assert 0.0 <= el[frac] <= 1.0


def test_layout_preview_renders_png(client):
    layout = {
        "width_in": 2.0, "height_in": 1.0, "dpi": 203,
        "elements": [
            {"field": "name", "x": 0, "y": 0, "w": 1, "h": 0.4, "bold": True},
            {"field": "best_by_date", "x": 0, "y": 0.5, "w": 1, "h": 0.4},
        ],
    }
    r = client.post("/printing/label/layout/preview", json={"layout": layout})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_layout_preview_empty_body_is_400(client):
    r = client.post("/printing/label/layout/preview", json={"layout": {}})
    assert r.status_code == 400


def test_layout_preview_malformed_layout_never_500(client):
    # Garbage sizes and bad elements are normalized (or skipped), so the preview
    # returns a clean image, never a 500.
    bad = {"width_in": "wide", "dpi": "lots",
           "elements": ["nope", {"field": "unknown"},
                        {"field": "name", "x": 9, "y": -2, "w": 0.5, "h": 0.5}]}
    r = client.post("/printing/label/layout/preview", json={"layout": bad})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"


def test_layout_preview_rejects_non_dict_layout(client):
    # A layout that is not an object is a clean 4xx (pydantic 422 or our 400),
    # never a 500.
    r = client.post("/printing/label/layout/preview", json={"layout": "oops"})
    assert 400 <= r.status_code < 500


def test_layout_save_then_clear(client):
    layout = {
        "width_in": 2.0, "height_in": 1.0, "dpi": 203,
        "elements": [{"field": "name", "x": 0, "y": 0.3, "w": 1, "h": 0.4,
                      "align": "center", "bold": True}],
    }
    r = client.post("/printing/label/layout", json={"layout": layout})
    assert r.status_code == 200
    assert r.json()["ok"] is True and r.json()["elements"] == 1
    assert client._settings.label_layout_json  # persisted

    # An empty layout clears the custom design.
    r2 = client.post("/printing/label/layout", json={"layout": {}})
    assert r2.status_code == 200
    assert r2.json()["cleared"] is True
    assert client._settings.label_layout_json == ""


def test_layout_save_all_invalid_elements_is_400(client):
    r = client.post("/printing/label/layout",
                    json={"layout": {"elements": [{"field": "unknown"}, "junk"]}})
    assert r.status_code == 400


def test_saved_layout_drives_label_render(client, monkeypatch):
    # With a custom layout saved, the label print path renders through the layout
    # engine (not the default renderer) but still sends a valid PNG.
    client._settings.printing_enabled = True
    client._settings.label_printer_queue = "Zebra"
    monkeypatch.setattr(printing_service, "printing_available", lambda: True)
    save = client.post("/printing/label/layout", json={"layout": {
        "elements": [{"field": "name", "x": 0, "y": 0, "w": 1, "h": 1,
                      "align": "center", "bold": True}]}})
    assert save.status_code == 200
    sent = {}

    def _fake_print(queue, data, *, options=None):
        sent["len"] = len(data)
        return printing_service.PrintResult(ok=True, job_id="Zebra-2")

    monkeypatch.setattr(printing_service, "print_bytes", _fake_print)
    r = client.post("/printing/label", json={"name": "Chili Powder"})
    assert r.status_code == 200
    assert r.json()["ok"] is True and sent["len"] > 0
    # Clean up so the saved layout does not leak into other tests.
    client._settings.label_layout_json = ""


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


# -- Best-by provenance on printed labels (FoodAssistant-cidz) --------------


def test_spec_from_fields_uses_recorded_provenance_when_fresh(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    best_by_provenance.clear_all()
    best_by_provenance.record(7, "Chicken Stock", "default", "2026-07-22")
    item = {"product_id": 7, "name": "Chicken Stock",
            "added_date": "2026-07-01 08:00:00", "best_before_date": "2026-07-22"}
    body = printing_router.LabelIn(product_id=7)
    spec = printing_router._spec_from_fields(body, item)
    assert spec.best_by_source == "default"
    best_by_provenance.clear_all()


def test_spec_from_fields_ignores_stale_provenance(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    best_by_provenance.clear_all()
    # Recorded for a date that no longer matches the item (edited in Grocy
    # since): the badge must not lie about today's date.
    best_by_provenance.record(7, "Chicken Stock", "default", "2026-07-01")
    item = {"product_id": 7, "name": "Chicken Stock",
            "added_date": "2026-07-01 08:00:00", "best_before_date": "2026-07-22"}
    body = printing_router.LabelIn(product_id=7)
    spec = printing_router._spec_from_fields(body, item)
    assert spec.best_by_source == "manual"
    best_by_provenance.clear_all()


def test_spec_from_fields_explicit_source_wins_over_provenance(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    best_by_provenance.clear_all()
    best_by_provenance.record(7, "Chicken Stock", "default", "2026-07-22")
    item = {"product_id": 7, "name": "Chicken Stock",
            "added_date": "2026-07-01 08:00:00", "best_before_date": "2026-07-22"}
    body = printing_router.LabelIn(product_id=7, best_by_source="llm")
    spec = printing_router._spec_from_fields(body, item)
    assert spec.best_by_source == "llm"
    best_by_provenance.clear_all()


def test_print_label_renders_recorded_source_end_to_end(client, monkeypatch):
    # A full round trip through the /printing/label route: an item with a
    # "default" (estimate) provenance recorded prints with that source, which
    # is visible as the LabelSpec the renderer receives.
    client._settings.printing_enabled = True
    client._settings.label_printer_queue = "Zebra"
    monkeypatch.setattr(printing_service, "printing_available", lambda: True)
    best_by_provenance.clear_all()
    best_by_provenance.record(11, "Yogurt", "default", "2026-07-22")

    async def _fake_item_for_id(product_id):
        return {"product_id": 11, "name": "Yogurt",
                "added_date": "2026-07-01 08:00:00", "best_before_date": "2026-07-22"}

    monkeypatch.setattr(printing_router, "_item_for_id", _fake_item_for_id)
    captured = {}

    def _fake_render(spec):
        captured["source"] = spec.best_by_source
        return printing_router.label_render.render_label(spec)

    monkeypatch.setattr(printing_router, "_render_label_image", _fake_render)

    def _fake_print(queue, data, *, options=None):
        return printing_service.PrintResult(ok=True, job_id="Zebra-1")

    monkeypatch.setattr(printing_service, "print_bytes", _fake_print)
    r = client.post("/printing/label", json={"product_id": 11})
    assert r.status_code == 200
    assert captured["source"] == "default"
    best_by_provenance.clear_all()


# -- Batch print confirmation over 5 labels (FoodAssistant-np6o) ------------


def test_batch_over_five_needs_confirmation(client, monkeypatch):
    client._settings.printing_enabled = True
    client._settings.label_printer_queue = "Zebra"
    monkeypatch.setattr(printing_service, "printing_available", lambda: True)
    r = client.post("/printing/label/batch", json={"product_ids": [1, 2, 3, 4, 5, 6]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["needs_confirmation"] is True
    assert body["count"] == 6


def test_batch_of_five_or_fewer_prints_without_confirmation(client, monkeypatch):
    from app.services.grocy import GrocyClient
    client._settings.printing_enabled = True
    client._settings.label_printer_queue = "Zebra"
    monkeypatch.setattr(printing_service, "printing_available", lambda: True)

    async def _fake_stock(self):
        return [{"product_id": i, "name": f"Item {i}",
                  "added_date": "2026-07-01 08:00:00",
                  "best_before_date": "2026-07-22"} for i in range(1, 6)]

    monkeypatch.setattr(GrocyClient, "get_full_stock", _fake_stock)

    def _fake_print(queue, data, *, options=None):
        return printing_service.PrintResult(ok=True, job_id="Zebra-2")

    monkeypatch.setattr(printing_service, "print_bytes", _fake_print)
    r = client.post("/printing/label/batch", json={"product_ids": [1, 2, 3, 4, 5]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["printed"] == 5


def test_batch_over_five_prints_when_confirmed(client, monkeypatch):
    from app.services.grocy import GrocyClient
    client._settings.printing_enabled = True
    client._settings.label_printer_queue = "Zebra"
    monkeypatch.setattr(printing_service, "printing_available", lambda: True)

    async def _fake_stock(self):
        return [{"product_id": i, "name": f"Item {i}",
                  "added_date": "2026-07-01 08:00:00",
                  "best_before_date": "2026-07-22"} for i in range(1, 7)]

    monkeypatch.setattr(GrocyClient, "get_full_stock", _fake_stock)

    def _fake_print(queue, data, *, options=None):
        return printing_service.PrintResult(ok=True, job_id="Zebra-3")

    monkeypatch.setattr(printing_service, "print_bytes", _fake_print)
    r = client.post("/printing/label/batch",
                    json={"product_ids": [1, 2, 3, 4, 5, 6], "confirmed": True})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["printed"] == 6


# -- Bluetooth label printer setup (FoodAssistant-h2j6) ---------------------
# The Supvan T50M family connects over Bluetooth, so a small on-device bridge
# is needed before Find printers can see it. Server mode gets honest guidance
# and never touches the bridge; a Pi kicks it off as a background task and
# reports status separately. The bridge is always mocked here.


def test_bluetooth_setup_server_mode_returns_guidance_and_skips_bridge(client, monkeypatch):
    client._settings.deployment_mode = "server"
    from app.services import bluetooth_print_setup as bps
    called = {"n": 0}

    def _boom():
        called["n"] += 1
        raise AssertionError("bridge should not be touched in server mode")

    monkeypatch.setattr(bps, "mark_installing", _boom)
    r = client.post("/printing/bluetooth/setup")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "appliance" in body["message"].lower()
    assert called["n"] == 0


def test_bluetooth_status_server_mode_is_unsupported(client):
    client._settings.deployment_mode = "server"
    r = client.get("/printing/bluetooth/status")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "unsupported"


def test_bluetooth_setup_pi_mode_kicks_off_background_task(client, monkeypatch):
    client._settings.deployment_mode = "pi_hosted"
    from app.services import bluetooth_print_setup as bps
    bps._state.update({**bps._DEFAULT_STATE, "mtime": None})
    # Swap the real long-running background coroutine for a no-op so the test
    # does not depend on scheduling races with the test event loop; the
    # coroutine itself is exercised directly below.
    started = {"n": 0}

    async def _fake_run():
        started["n"] += 1

    monkeypatch.setattr(printing_router, "_run_bluetooth_print_setup", _fake_run)
    r = client.post("/printing/bluetooth/setup")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "several minutes" in body["message"]
    assert bps.current()["status"] == bps.INSTALLING


def test_bluetooth_setup_pi_mode_dedupes_when_already_installing(client, monkeypatch):
    client._settings.deployment_mode = "pi_hosted"
    from app.services import bluetooth_print_setup as bps
    bps._state.update({**bps._DEFAULT_STATE, "mtime": None})
    bps.mark_installing()

    async def _fake_run():
        raise AssertionError("should not start a second run while one is in flight")

    monkeypatch.setattr(printing_router, "_run_bluetooth_print_setup", _fake_run)
    r = client.post("/printing/bluetooth/setup")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "already running" in body["message"].lower()


def test_bluetooth_status_pi_mode_ready_when_bridge_confirms_active(client, monkeypatch):
    client._settings.deployment_mode = "pi_hosted"
    from app.services import bluetooth_print_setup as bps
    from app.services import bridge as bridge_mod
    bps._state.update({**bps._DEFAULT_STATE, "mtime": None})
    fake = _FakeBridgeClient(resp=_FakeResp(200, {"ok": True, "supvan_active": True}))
    monkeypatch.setattr(bridge_mod, "bridge_client", fake)
    r = client.get("/printing/bluetooth/status")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


def test_bluetooth_status_pi_mode_installing_when_bridge_unreachable(client, monkeypatch):
    client._settings.deployment_mode = "pi_hosted"
    from app.services import bluetooth_print_setup as bps
    from app.services import bridge as bridge_mod
    bps._state.update({**bps._DEFAULT_STATE, "mtime": None})
    bps.mark_installing()
    fake = _FakeBridgeClient(exc=RuntimeError("connection refused"))
    monkeypatch.setattr(bridge_mod, "bridge_client", fake)
    r = client.get("/printing/bluetooth/status")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "installing"
    assert body["started_at"] is not None


def test_bluetooth_status_pi_mode_failed_carries_log_tail(client, monkeypatch):
    client._settings.deployment_mode = "pi_hosted"
    from app.services import bluetooth_print_setup as bps
    from app.services import bridge as bridge_mod
    bps._state.update({**bps._DEFAULT_STATE, "mtime": None})
    bps.mark_result(False, "cargo build failed: missing libdbus-1-dev")
    fake = _FakeBridgeClient(resp=_FakeResp(200, {"ok": True, "supvan_active": False}))
    monkeypatch.setattr(bridge_mod, "bridge_client", fake)
    r = client.get("/printing/bluetooth/status")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed"
    assert "cargo build failed" in body["log_tail"]


def test_run_bluetooth_print_setup_records_success(monkeypatch, tmp_path):
    import asyncio
    from app.config import settings as app_settings
    from app.services import bluetooth_print_setup as bps
    from app.services import bridge as bridge_mod
    monkeypatch.setattr(app_settings, "data_dir", str(tmp_path), raising=False)
    bps._state.update({**bps._DEFAULT_STATE, "mtime": None})
    fake = _FakeBridgeClient(resp=_FakeResp(200, {"ok": True, "log": "installed via source"}))
    monkeypatch.setattr(bridge_mod, "bridge_client", fake)
    asyncio.run(printing_router._run_bluetooth_print_setup())
    state = bps.current()
    assert state["status"] == bps.READY
    assert state["log_tail"] == "installed via source"


def test_run_bluetooth_print_setup_records_failure_on_bridge_error(monkeypatch, tmp_path):
    import asyncio
    from app.config import settings as app_settings
    from app.services import bluetooth_print_setup as bps
    from app.services import bridge as bridge_mod
    monkeypatch.setattr(app_settings, "data_dir", str(tmp_path), raising=False)
    bps._state.update({**bps._DEFAULT_STATE, "mtime": None})
    fake = _FakeBridgeClient(exc=RuntimeError("no route to host"))
    monkeypatch.setattr(bridge_mod, "bridge_client", fake)
    asyncio.run(printing_router._run_bluetooth_print_setup())
    state = bps.current()
    assert state["status"] == bps.FAILED
    assert "no route to host" in state["log_tail"]
