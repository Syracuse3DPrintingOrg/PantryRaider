"""The integrations registry and its first-party inhabitants (FoodAssistant-pjtq).

Covers:
  * Registry mechanics: register/list/get, duplicate protection, replace,
    unregister, enable/disable overrides, snapshot rows, reset.
  * Interface conformance: every built-in inhabitant satisfies its contract
    (RecipeBackend, ShoppingBackend, SensorDecoder, ActionProvider) and the
    registry buckets hold exactly the expected names.
  * Behavior preservation: the shims (recipe_source.active_backend,
    shopping_source.active_backend / shopping_available, gadgets.ingest
    routing, start_actions.fire_key) answer exactly what the pre-registry
    code did, including the auto rules, the thermometer wire fallback, the
    contact open-since bookkeeping, and the "Unknown key." reply.
  * The lazy-Mealie property: resolving backends, listing the registry, and
    ingesting readings never import services.mealie on a native install
    (checked in a subprocess so the suite's own imports cannot mask it).

Pure logic plus the shared state file under a tmp data_dir; no network.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import settings
from app.integrations import registry
from app.integrations.interfaces import (ActionProvider, RecipeBackend,
                                         SensorDecoder, ShoppingBackend)
from app.services import gadgets, recipe_source, shopping_source

SERVICE = Path(__file__).resolve().parents[1] / "service"


@pytest.fixture(autouse=True)
def _fresh():
    registry.reset()
    gadgets.reset()
    yield
    registry.reset()
    gadgets.reset()


def _native_settings():
    settings.recipes_backend = ""
    settings.shopping_backend = ""
    settings.mealie_base_url = ""
    settings.mealie_api_key = ""


def _state_file(tmp_path) -> dict:
    return json.loads((tmp_path / "gadgets.json").read_text())


# -- Registry mechanics -------------------------------------------------------

class _FakeRecipes(RecipeBackend):
    name = "fake"
    label = "Fake library"

    def enabled(self):
        return True

    def configured(self):
        return True


def test_register_rejects_duplicates_and_unknown_kinds():
    registry.register(_FakeRecipes())
    with pytest.raises(ValueError):
        registry.register(_FakeRecipes())
    registry.register(_FakeRecipes(), replace=True)  # explicit swap is fine

    class Nameless(_FakeRecipes):
        name = ""
    with pytest.raises(ValueError):
        registry.register(Nameless())

    class WrongKind(_FakeRecipes):
        kind = "nonsense"
        name = "x"
    with pytest.raises(ValueError):
        registry.register(WrongKind())


def test_get_names_and_unregister():
    assert "native" in registry.names(registry.KIND_RECIPES)
    assert registry.get(registry.KIND_RECIPES, "native") is not None
    registry.register(_FakeRecipes())
    assert registry.names(registry.KIND_RECIPES) == ("native", "mealie", "fake")
    registry.unregister(registry.KIND_RECIPES, "fake")
    assert "fake" not in registry.names(registry.KIND_RECIPES)
    registry.unregister(registry.KIND_RECIPES, "fake")  # a no-op, never raises
    assert registry.get(registry.KIND_RECIPES, "missing") is None


def test_enable_disable_overrides():
    _native_settings()
    # The native library is always on; an override can force it off and a
    # None override hands control back to the inhabitant's own answer.
    assert registry.is_enabled(registry.KIND_RECIPES, "native") is True
    registry.set_override(registry.KIND_RECIPES, "native", False)
    assert registry.is_enabled(registry.KIND_RECIPES, "native") is False
    assert all(i.name != "native"
               for i in registry.enabled_for(registry.KIND_RECIPES))
    registry.set_override(registry.KIND_RECIPES, "native", None)
    assert registry.is_enabled(registry.KIND_RECIPES, "native") is True
    # Mealie reads as enabled exactly when it is configured.
    assert registry.is_enabled(registry.KIND_RECIPES, "mealie") is False
    settings.mealie_base_url = "http://mealie.test"
    settings.mealie_api_key = "key"
    assert registry.is_enabled(registry.KIND_RECIPES, "mealie") is True
    # An unregistered name is simply off.
    assert registry.is_enabled(registry.KIND_RECIPES, "missing") is False


def test_snapshot_marks_the_active_backends():
    _native_settings()
    rows = {(r["kind"], r["name"]): r for r in registry.snapshot()}
    assert rows[("recipes", "native")]["active"] is True
    assert rows[("recipes", "mealie")]["active"] is False
    assert rows[("shopping", "grocy")]["active"] is True
    # Additive kinds carry no active flag; they run all enabled inhabitants.
    assert "active" not in rows[("sensors", "thermometer")]
    assert "active" not in rows[("actions", "start_page")]
    for row in rows.values():
        assert row["label"]


def test_reset_reloads_builtins_on_next_query():
    registry.register(_FakeRecipes())
    registry.reset()
    assert registry.names(registry.KIND_RECIPES) == ("native", "mealie")


# -- Interface conformance ------------------------------------------------------

def test_builtin_buckets_and_contracts():
    expectations = (
        (registry.KIND_RECIPES, RecipeBackend, ("native", "mealie")),
        (registry.KIND_SHOPPING, ShoppingBackend, ("grocy", "mealie")),
        (registry.KIND_SENSORS, SensorDecoder,
         ("thermometer", "hygrometer", "contact", "button", "stemma")),
        (registry.KIND_ACTIONS, ActionProvider, ("start_page",)),
    )
    for kind, contract, expected_names in expectations:
        inhabitants = registry.all_for(kind)
        assert tuple(i.name for i in inhabitants) == expected_names
        for inhabitant in inhabitants:
            assert isinstance(inhabitant, contract)
            assert inhabitant.kind == kind
            assert inhabitant.label
            assert isinstance(inhabitant.enabled(), bool)


def test_sensor_decoders_cover_the_wire_kinds_with_one_fallback():
    from app.integrations import sensors as sensor_seam
    decoders = sensor_seam.ingest_decoders()
    kinds = [d.wire_kind for d in decoders]
    assert kinds == ["thermometer", "hygrometer", "contact", "button", "stemma"]
    assert [d.wire_kind for d in decoders if d.fallback] == ["thermometer"]
    assert sensor_seam.fallback_decoder(decoders).wire_kind == "thermometer"


def test_sensor_decoder_gates_mirror_the_settings(monkeypatch):
    from app.integrations import sensors as sensor_seam
    flags = {"thermometer": "gadgets_enabled", "hygrometer": "hygrometers_enabled",
             "contact": "contacts_enabled", "button": "buttons_enabled",
             "stemma": "stemma_enabled"}
    for decoder in sensor_seam.ingest_decoders():
        field = flags[decoder.wire_kind]
        monkeypatch.setattr(settings, field, False, raising=False)
        assert decoder.enabled() is False
        monkeypatch.setattr(settings, field, True, raising=False)
        assert decoder.enabled() is True


# -- Behavior preservation: the backend rules ---------------------------------

def test_recipes_resolution_matches_the_original_rule():
    _native_settings()
    assert recipe_source.active_backend() == "native"
    assert registry.active_name(registry.KIND_RECIPES) == "native"

    settings.mealie_base_url = "http://mealie.test"
    settings.mealie_api_key = "key"
    assert recipe_source.active_backend() == "mealie"

    settings.recipes_backend = "native"
    assert recipe_source.active_backend() == "native"

    # An explicit choice wins even with Mealie unconfigured, as it always has.
    settings.recipes_backend = "mealie"
    settings.mealie_base_url = ""
    settings.mealie_api_key = ""
    assert recipe_source.active_backend() == "mealie"

    # Junk in the setting falls back to the auto rule, whitespace and case
    # are forgiven, exactly as before.
    settings.recipes_backend = "  NATIVE  "
    assert recipe_source.active_backend() == "native"
    settings.recipes_backend = "paprika"
    assert recipe_source.active_backend() == "native"

    assert registry.active(registry.KIND_RECIPES).name == "native"


def test_shopping_resolution_matches_the_original_rule():
    _native_settings()
    assert shopping_source.active_backend() == "grocy"

    settings.mealie_base_url = "http://mealie.test"
    settings.mealie_api_key = "key"
    settings.recipes_backend = "mealie"
    assert shopping_source.active_backend() == "mealie"

    settings.recipes_backend = "native"
    assert shopping_source.active_backend() == "grocy"

    settings.shopping_backend = "mealie"
    assert shopping_source.active_backend() == "mealie"
    settings.shopping_backend = "grocy"
    settings.recipes_backend = "mealie"
    assert shopping_source.active_backend() == "grocy"


def test_shopping_available_matches_the_original_rule(monkeypatch):
    _native_settings()
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    settings.grocy_base_url = ""
    settings.grocy_api_key = ""
    assert shopping_source.shopping_available() is False
    settings.grocy_base_url = "http://grocy.test"
    settings.grocy_api_key = "key"
    assert shopping_source.shopping_available() is True
    # Mealie-backed shopping needs the Mealie connection instead.
    settings.shopping_backend = "mealie"
    assert shopping_source.shopping_available() is False
    settings.mealie_base_url = "http://mealie.test"
    settings.mealie_api_key = "key"
    assert shopping_source.shopping_available() is True


# -- Behavior preservation: ingest routing ------------------------------------

def test_ingest_routes_every_family_like_the_old_if_chain(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    now = 1_000_000.0
    result = gadgets.ingest({
        "source": "bandit-1",
        "devices": [
            {"id": "aa:bb", "probes": [{"index": 1, "temp_c": 55.5}]},
            {"id": "cc:dd", "kind": "hygrometer", "temp_c": 4.0, "humidity": 40},
            {"id": "ee:ff", "kind": "contact", "open": True,
             "protocol": "bthome_contact"},
            {"id": "11:22", "kind": "button", "events": [{"button": 1,
                                                          "type": "single"}]},
            {"id": "i2c:1:0x30", "kind": "stemma", "model": "neokey"},
            {"id": "33:44", "kind": "who-knows",
             "probes": [{"index": 1, "temp_c": 20.0}]},
            "junk",
        ],
        "discovered": [
            {"id": "d1:aa", "name": "Probe", "protocol": "inkbird"},
            {"id": "d2:bb", "kind": "hygrometer", "protocol": "govee_hygro"},
            {"id": "d3:cc", "kind": "contact", "protocol": "switchbot_contact"},
            {"id": "d4:dd", "kind": "button"},
        ],
    }, now=now)
    # The reply reports the pushed entries, exactly as before.
    assert result["ok"] is True and result["stored"] == 7

    state = _state_file(tmp_path)
    # Thermometers: the default AND the unknown-kind fallback, tagged via.
    assert set(state["devices"]) == {"AA:BB", "33:44"}
    assert state["devices"]["AA:BB"]["via"] == "bandit-1"
    # Each family landed in its own bucket.
    assert set(state["hygrometers"]) == {"CC:DD"}
    assert set(state["contacts"]) == {"EE:FF"}
    assert state["contacts"]["EE:FF"]["open_since"] == now
    # Buttons never land in the gadgets state (gadgets_buttons owns them).
    assert "11:22" not in state["devices"]
    assert set(state["stemma"]) == {"i2c:1:0x30"}
    # Discoveries routed the same way; buttons skipped here too.
    assert set(state["discovered"]) == {"D1:AA"}
    assert state["discovered"]["D1:AA"]["supported"] is True
    assert set(state["hygro_discovered"]) == {"D2:BB"}
    assert set(state["contact_discovered"]) == {"D3:CC"}
    assert "D4:DD" not in state["discovered"]


def test_ingest_contact_open_since_survives_and_clears(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    door = {"id": "ee:ff", "kind": "contact", "open": True}
    gadgets.ingest({"devices": [door]}, now=100.0)
    gadgets.ingest({"devices": [door]}, now=160.0)
    entry = _state_file(tmp_path)["contacts"]["EE:FF"]
    assert entry["open_since"] == 100.0  # the first opening, not the last push
    gadgets.ingest({"devices": [{**door, "open": False}]}, now=200.0)
    entry = _state_file(tmp_path)["contacts"]["EE:FF"]
    assert entry["open_since"] is None


def test_ingest_prunes_each_family_on_its_own_window(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    start = 1_000_000.0
    gadgets.ingest({"devices": [
        {"id": "aa:bb", "probes": [{"index": 1, "temp_c": 50.0}]},
        {"id": "ee:ff", "kind": "contact", "open": False},
    ]}, now=start)
    # Past the probe prune window but inside the contacts' longer one.
    later = start + gadgets.PRUNE_SECONDS + 60
    gadgets.ingest({"devices": []}, now=later)
    state = _state_file(tmp_path)
    assert "AA:BB" not in state["devices"]
    assert "EE:FF" in state["contacts"]


# -- Behavior preservation: the action vocabulary ------------------------------

def test_unknown_token_still_reports_unknown_key():
    from app.services import start_actions
    assert asyncio.run(start_actions.fire_key("never-registered-token")) == {
        "ok": False, "detail": "Unknown key."}


def test_a_second_provider_is_asked_in_turn():
    class Extra(ActionProvider):
        name = "extra"
        label = "Extra tokens"

        def enabled(self):
            return True

        async def fire(self, name, long=False):
            if name == "extra_token":
                return {"ok": True, "detail": "Extra ran."}
            return None

    registry.register(Extra())
    from app.services import start_actions
    assert asyncio.run(start_actions.fire_key("extra_token"))["detail"] == "Extra ran."
    # Built-in tokens are unaffected by the extra provider.
    assert asyncio.run(start_actions.fire_key("nope"))["detail"] == "Unknown key."


def test_disabled_provider_is_skipped():
    from app.integrations import actions as action_seam
    registry.set_override(registry.KIND_ACTIONS, "start_page", False)
    assert asyncio.run(action_seam.fire_key("timer_1")) == {
        "ok": False, "detail": "Unknown key."}


# -- The lazy-Mealie property ---------------------------------------------------

def test_native_install_never_imports_mealie(tmp_path):
    """Importing the app, resolving both backends, walking the registry, and
    ingesting a reading must not import the Mealie backend when the install
    is native. Run in a subprocess so this suite's own earlier imports cannot
    mask a regression."""
    code = """
import sys
sys.path.insert(0, {service!r})
from app.main import app
from app.config import settings
settings.recipes_backend = ""
settings.shopping_backend = ""
settings.mealie_base_url = ""
settings.mealie_api_key = ""
settings.data_dir = {data_dir!r}
from app.services import gadgets, recipe_source, shopping_source
assert recipe_source.active_backend() == "native"
assert shopping_source.active_backend() == "grocy"
from app.integrations import registry
assert registry.snapshot()
gadgets.ingest({{"devices": [{{"id": "aa:bb",
                              "probes": [{{"index": 1, "temp_c": 20.0}}]}}]}},
               now=5.0)
assert "app.services.mealie" not in sys.modules, "Mealie was imported"
print("LAZY_OK")
""".format(service=str(SERVICE), data_dir=str(tmp_path))
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(SERVICE),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "LAZY_OK" in proc.stdout
