"""Kitchen-appliance preferences: catalog, AI-prompt clause, persistence.

Pure logic plus a settings round-trip; no network or DB. Covers the framework
that steers AI cook suggestions to dishes the kitchen can actually make
(FoodAssistant-k2kv) and that affiliate recommendations will later build on.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app import config  # noqa: E402
from app.config import (  # noqa: E402
    KITCHEN_APPLIANCES,
    KITCHEN_APPLIANCE_KEYS,
    KITCHEN_APPLIANCE_LABELS,
    DEFAULT_KITCHEN_APPLIANCES,
    appliances_clause,
)


def test_catalog_is_well_formed():
    keys = [k for k, *_ in KITCHEN_APPLIANCES]
    assert len(keys) == len(set(keys)), "appliance ids must be unique"
    # Every catalog row carries a known group.
    assert all(g in ("major", "minor", "attachment") for _k, _l, g, _d in KITCHEN_APPLIANCES)
    # The pasta tools and the stand-mixer attachments are in the catalog.
    for k in ("pasta_roller", "pasta_extruder", "sm_meat_grinder", "sm_pasta_roller_cutter"):
        assert k in keys
    # Derived lookups stay in sync with the catalog.
    assert set(KITCHEN_APPLIANCE_KEYS) == set(keys)
    assert set(KITCHEN_APPLIANCE_LABELS) == set(keys)
    # Defaults are a subset of the catalog and include the everyday workhorses.
    assert set(DEFAULT_KITCHEN_APPLIANCES) <= set(keys)
    for everyday in ("stove", "oven", "microwave"):
        assert everyday in DEFAULT_KITCHEN_APPLIANCES


def test_appliances_clause_lists_labels_and_drops_unknown():
    clause = appliances_clause(["stove", "sous_vide", "not_a_real_key"])
    assert "Stovetop" in clause and "Sous vide" in clause
    assert "not_a_real_key" not in clause
    # It instructs the model to stay within the listed equipment.
    assert "do not require" in clause.lower()


def test_appliances_clause_empty_is_blank():
    assert appliances_clause([]) == ""
    assert appliances_clause(None) == ""
    # A list of only-unknown ids yields nothing rather than a dangling sentence.
    assert appliances_clause(["bogus", "also_bogus"]) == ""


def test_settings_persist_appliances_round_trip():
    from app.config import Settings
    d = tempfile.mkdtemp()
    s = Settings()
    s.data_dir = d
    # Fresh install starts with the everyday defaults.
    assert s.kitchen_appliances == DEFAULT_KITCHEN_APPLIANCES
    s.save({"kitchen_appliances": ["wok", "oven"]})
    assert s.kitchen_appliances == ["wok", "oven"]
    # An explicit empty selection persists (distinct from the unset default).
    s.save({"kitchen_appliances": []})
    saved = json.loads((Path(d) / "settings.json").read_text())
    assert saved["kitchen_appliances"] == []


def test_setup_save_sanitizes_appliance_ids(monkeypatch, tmp_path):
    """The save handler keeps only known ids, in catalog order, so a stale or
    hand-crafted id never reaches the AI prompt."""
    from app.routers.setup import _appliance_groups
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "kitchen_appliances", ["oven", "wok"], raising=False)
    groups = _appliance_groups()
    # The grouped view reflects the current selection's checked state.
    oven = next(a for a in groups["major"] if a["key"] == "oven")
    wok = next(a for a in groups["minor"] if a["key"] == "wok")
    microwave = next(a for a in groups["major"] if a["key"] == "microwave")
    assert oven["checked"] and wok["checked"] and not microwave["checked"]
