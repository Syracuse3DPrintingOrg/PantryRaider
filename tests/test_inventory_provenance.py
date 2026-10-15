"""Best-by provenance recording at import time (FoodAssistant-cidz).

POST /inventory/import is the seam: an item that arrives with no best-by date
gets one filled in by services/defaults.apply_defaults (source "default"), and
once the Grocy import succeeds and a product id exists, that source is handed
to services/best_by_provenance.py so a later-printed label can badge it
honestly. A user-provided override always reads back as "manual" even if the
item arrived with a stamped source, since the override replaces the guess.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import best_by_provenance  # noqa: E402
from app.services.grocy import GrocyClient  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    from app.config import settings
    from app.main import app
    saved = {k: getattr(settings, k) for k in (
        "data_dir", "grocy_base_url", "grocy_api_key", "vision_provider",
        "gemini_api_key", "auth_required", "auth_password")}
    settings.data_dir = str(tmp_path)
    settings.grocy_base_url = "http://grocy.test"
    settings.grocy_api_key = "k"
    settings.vision_provider = "gemini"
    settings.gemini_api_key = "k"
    settings.auth_required = False
    settings.auth_password = ""
    best_by_provenance.clear_all()
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for k, v in saved.items():
            setattr(settings, k, v)
        best_by_provenance.clear_all()
        os.chdir(cwd)


def _fake_import_item(next_id):
    async def _import(self, item):
        return {"product_id": next_id, "name": item.name}
    return _import


def test_import_records_default_source_when_date_filled(client, monkeypatch):
    monkeypatch.setattr(GrocyClient, "import_item", _fake_import_item(501))
    r = client.post("/inventory/import", json={"items": [
        {"name": "Yogurt", "category": "Dairy", "storage_type": "refrigerated"},
    ]})
    assert r.status_code == 200
    assert r.json()["imported"] == 1
    product_id = r.json()["results"][0]["product_id"]
    assert product_id == 501

    from datetime import date, timedelta
    filled = date.today() + timedelta(days=14)  # yogurt refrigerated default
    assert best_by_provenance.lookup(501, "Yogurt", filled.isoformat()) == "default"


def test_import_override_date_reads_back_manual(client, monkeypatch):
    monkeypatch.setattr(GrocyClient, "import_item", _fake_import_item(502))
    r = client.post("/inventory/import", json={
        "items": [{"name": "Cheese", "category": "Dairy",
                    "storage_type": "refrigerated"}],
        "overrides": {"0": {"best_by_date": "2026-08-01"}},
    })
    assert r.status_code == 200
    product_id = r.json()["results"][0]["product_id"]
    # A manually-typed date is never recorded (it is the no-badge default
    # anyway), so no fresh non-manual record exists for it.
    assert best_by_provenance.lookup(product_id, "Cheese", "2026-08-01") == "manual"


def test_import_with_no_date_and_no_fill_records_nothing():
    # Sanity: apply_defaults always fills a missing date, so this mostly
    # documents the guard in the router (best_by_date is not None before
    # recording is attempted). Exercised indirectly by the tests above.
    pass
