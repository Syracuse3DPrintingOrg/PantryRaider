"""The Home Assistant key policy (services/ha_policy.py and the deck's copy)."""
import ast
import subprocess
import sys
from pathlib import Path

import pytest

from app.services import ha_policy as hp

REPO = Path(__file__).resolve().parent.parent
APP_COPY = REPO / "service" / "app" / "services" / "ha_policy.py"
DECK_COPY = REPO / "streamdeck" / "foodassistant_streamdeck" / "ha_policy.py"


# ---------- classify: the matrix ----------

OK_CALLS = [
    ("light.x", "light.toggle"),
    ("light.x", "homeassistant.toggle"),
    ("scene.x", "scene.turn_on"),
    ("script.goodnight", "script.goodnight"),
    ("script.x", "script.turn_on"),
    ("media_player.den", "media_player.media_play_pause"),
    ("switch.x", "switch.turn_on"),
    ("button.x", "button.press"),
    ("light.a, light.b", "homeassistant.toggle"),
    ("Light.Kitchen", "Light.Toggle"),
    (" light.a , light.b ", "homeassistant.toggle"),
    ("input_boolean.guest", "homeassistant.turn_off"),
    ("vacuum.downstairs", "vacuum.return_to_base"),
]

HIGH_RISK_CALLS = [
    ("lock.front_door", "lock.unlock"),
    ("alarm_control_panel.home", "alarm_control_panel.alarm_disarm"),
    ("cover.garage", "cover.open_cover"),
    ("cover.garage", "homeassistant.toggle"),
    ("light.x", "homeassistant.restart"),
    ("light.x", "hassio.host_shutdown"),
    ("shell_command.x", "shell_command.x"),
    ("light.a, lock.b", "homeassistant.toggle"),
    ("lock.front_door", "script.goodnight"),
    ("lock.front_door", "script.turn_on"),
    ("light.x", "notify.mobile_app"),
]

UNLISTED_CALLS = [
    ("foo.baz", "foo.bar"),
    ("light.x", "switch.turn_on"),
    ("light.x", "script.goodnight"),
    ("sensor.temp", "homeassistant.toggle"),
    ("light.x", "light.set_effect"),
    # script.reload is Home Assistant's reload action, never a script.
    ("script.reload", "script.reload"),
    ("script.x", "script.reload"),
]

INVALID_ENTITY = [
    "all", "none", "", "   ", "light./../config", "light.a,,light.b", "light.a,",
    ",light.a", "light.a b", "light", "light.", ".x", "light.a/b", "light.a?b=1",
    "light.a\nlight.b", "light.é",
]


@pytest.mark.parametrize("entity,service", OK_CALLS)
def test_ok_calls(entity, service):
    assert hp.classify(entity, service) == ("ok", "")


@pytest.mark.parametrize("entity,service", HIGH_RISK_CALLS)
def test_high_risk_calls(entity, service):
    assert hp.classify(entity, service) == ("elevated", "high_risk")


@pytest.mark.parametrize("entity,service", UNLISTED_CALLS)
def test_unlisted_calls(entity, service):
    assert hp.classify(entity, service) == ("elevated", "unlisted")


@pytest.mark.parametrize("entity", INVALID_ENTITY)
def test_invalid_entities(entity):
    assert hp.classify(entity, "homeassistant.toggle") == ("invalid", "entity")


@pytest.mark.parametrize("service", [
    "light/toggle", "light.toggle/../x", "", "toggle", "light.", "light.to ggle",
    "light.toggle.extra", "../api.x",
])
def test_invalid_services(service):
    assert hp.classify("light.x", service) == ("invalid", "service")


def test_entity_limit_is_max_entities():
    assert hp.MAX_ENTITIES == 20
    twenty = ",".join(f"light.l{i}" for i in range(20))
    assert hp.classify(twenty, "homeassistant.toggle") == ("ok", "")
    twenty_one = ",".join(f"light.l{i}" for i in range(21))
    assert hp.classify(twenty_one, "homeassistant.toggle") == ("invalid", "entity")


def test_invalid_wins_over_elevated():
    assert hp.classify("lock.a,all", "homeassistant.toggle") == ("invalid", "entity")


def test_high_risk_wins_over_unlisted():
    assert hp.classify("sensor.a, lock.b", "homeassistant.toggle") == ("elevated", "high_risk")


def test_none_inputs_are_invalid_not_errors():
    assert hp.classify(None, None) == ("invalid", "service")
    assert hp.classify(None, "light.toggle") == ("invalid", "entity")


def test_switch_and_button_stay_standard():
    assert hp.classify("switch.kettle", "homeassistant.toggle") == ("ok", "")
    assert hp.classify("button.doorbell", "button.press") == ("ok", "")
    assert hp.classify("input_button.reset", "input_button.press") == ("ok", "")


def test_policy_version():
    assert hp.HA_POLICY_VERSION == 1


# ---------- normalize and split_entities ----------

@pytest.mark.parametrize("entity,service,expected", [
    ("Light.Kitchen", "Light.Toggle", ("light.kitchen", "light.toggle")),
    (" light.a , light.b ", " homeassistant.toggle ", ("light.a,light.b", "homeassistant.toggle")),
    ("light.a,,light.b", "x.y", ("light.a,,light.b", "x.y")),
    ("light.a,", "x.y", ("light.a,", "x.y")),
    ("", "", ("", "")),
    (None, None, ("", "")),
])
def test_normalize(entity, service, expected):
    assert hp.normalize(entity, service) == expected


def test_normalize_reads_a_list_of_ids_like_a_comma_string():
    assert hp.normalize(["Light.A", " light.b "], "light.toggle") == (
        "light.a,light.b", "light.toggle")
    assert hp.normalize(("light.a",), "light.toggle") == ("light.a", "light.toggle")
    assert hp.classify(["light.a", "light.b"], "light.toggle") == ("ok", "")
    assert hp.classify(["light.a", "lock.b"], "homeassistant.toggle") == (
        "elevated", "high_risk")
    # An item that hides a second id is still seen.
    assert hp.classify(["light.a,lock.b"], "homeassistant.toggle") == (
        "elevated", "high_risk")
    for bad in ([], [None], ["light.a", ""], ["all"], [["light.a"]]):
        assert hp.classify(bad, "light.toggle") == ("invalid", "entity"), bad
    assert hp.classify([f"light.l{i}" for i in range(21)], "light.toggle") == (
        "invalid", "entity")


def test_split_entities_drops_empty_items():
    assert hp.split_entities(" Light.A ,, light.b, ") == ["light.a", "light.b"]
    assert hp.split_entities("") == []


# ---------- permitted ----------

def test_permitted_ok_any_source():
    for source in hp.SOURCES:
        assert hp.permitted("light.x", "light.toggle", None, source, enforce=True) == (True, "")


def test_elevated_refused_without_allow_elevated():
    ok, msg = hp.permitted("lock.front_door", "lock.unlock", {}, "deck", enforce=True)
    assert (ok, msg) == (False, hp.MSG_HIGH_RISK)
    ok, msg = hp.permitted("foo.baz", "foo.bar", {"allow_elevated": "yes"}, "screen", enforce=True)
    assert (ok, msg) == (False, hp.MSG_UNLISTED)


def test_elevated_allowed_with_allow_elevated():
    entry = {"allow_elevated": True}
    for source in ("deck", "screen", "esp", "satellite", "proxy"):
        assert hp.permitted("lock.front_door", "lock.unlock", entry, source,
                            enforce=True) == (True, "")


def test_ble_never_runs_elevated():
    entry = {"allow_elevated": True}
    for enforce in (True, False):
        assert hp.permitted("lock.front_door", "lock.unlock", entry, "ble",
                            enforce=enforce) == (False, hp.MSG_BLE)
    assert hp.permitted("light.x", "light.toggle", None, "ble", enforce=True) == (True, "")


def test_enforce_false_allows_elevated_except_ble():
    for source in ("deck", "screen", "esp", "satellite", "proxy", "something-new"):
        assert hp.permitted("cover.garage", "cover.open_cover", None, source,
                            enforce=False) == (True, "")


def test_invalid_refused_even_without_enforcement():
    for source in hp.SOURCES:
        assert hp.permitted("all", "light.turn_off", {"allow_elevated": True}, source,
                            enforce=False) == (False, hp.MSG_INVALID)


def test_unknown_source_is_treated_like_proxy():
    assert hp.permitted("lock.a", "lock.unlock", None, "mystery", enforce=True) == (
        False, hp.MSG_HIGH_RISK)


def test_permitted_normalizes_first():
    assert hp.permitted("Lock.Front_Door", "LOCK.UNLOCK", {"allow_elevated": True},
                        "deck", enforce=True) == (True, "")
    assert hp.permitted("Light.Kitchen", "Light.Toggle", None, "ble", enforce=True) == (True, "")


def test_permitted_requires_enforce():
    with pytest.raises(TypeError):
        hp.permitted("light.x", "light.toggle", None, "deck")  # missing enforce
    with pytest.raises(TypeError):
        hp.permitted("light.x", "light.toggle", None, "deck", True)  # positional enforce


def test_messages_point_to_settings_and_have_no_em_dashes():
    for msg in (hp.MSG_INVALID, hp.MSG_HIGH_RISK, hp.MSG_UNLISTED):
        assert "Settings, Start Page" in msg
    for msg in (hp.MSG_INVALID, hp.MSG_HIGH_RISK, hp.MSG_UNLISTED, hp.MSG_BLE):
        assert chr(0x2014) not in msg and chr(0x2013) not in msg


# ---------- the deck copy and the standard library ----------

def test_deck_copy_is_byte_identical():
    assert APP_COPY.read_bytes() == DECK_COPY.read_bytes()


def test_parses_as_python_3_9():
    ast.parse(APP_COPY.read_text(encoding="utf-8"), feature_version=(3, 9))


@pytest.mark.parametrize("path", [APP_COPY, DECK_COPY])
def test_imports_with_only_the_standard_library(path):
    code = (
        "import importlib.util, sys\n"
        "before = set(sys.modules)\n"
        f"spec = importlib.util.spec_from_file_location('hp_probe', {str(path)!r})\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "new = {m.split('.')[0] for m in set(sys.modules) - before}\n"
        "std = getattr(sys, 'stdlib_module_names', None)\n"
        "extra = sorted(m for m in new if std is not None and m not in std and m != 'hp_probe')\n"
        "assert not extra, extra\n"
        "assert mod.classify('light.x', 'light.toggle') == ('ok', '')\n"
        "print('ok')\n"
    )
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True,
                            text=True, timeout=60, cwd=str(path.parent.parent))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


# ---------- every caller states enforce ----------

def _ha_policy_aliases(tree: ast.AST):
    """(module aliases, function aliases) bound to ha_policy in one file."""
    modules, funcs = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for alias in node.names:
                if alias.name == "ha_policy":
                    modules.add(alias.asname or alias.name)
                elif mod.split(".")[-1] == "ha_policy" and alias.name == "permitted":
                    funcs.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[-1] == "ha_policy":
                    modules.add(alias.asname or alias.name)
    return modules, funcs


def _dotted(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value)
        return f"{inner}.{node.attr}" if inner else ""
    return ""


def _permitted_calls():
    found = []
    roots = [REPO / "service" / "app", REPO / "streamdeck"]
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if path.name == "ha_policy.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            modules, funcs = _ha_policy_aliases(tree)
            if not modules and not funcs:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                hit = False
                if isinstance(fn, ast.Attribute) and fn.attr == "permitted":
                    owner = _dotted(fn.value)
                    hit = owner in modules or owner.split(".")[-1] == "ha_policy"
                elif isinstance(fn, ast.Name) and fn.id in funcs:
                    hit = True
                if hit:
                    found.append((path.relative_to(REPO), node))
    return found


def test_every_permitted_call_passes_enforce():
    calls = _permitted_calls()
    files = {str(p) for p, _ in calls}
    assert any(f.endswith("ha_allowlist.py") for f in files), files
    missing = [f"{p}:{n.lineno}" for p, n in calls
               if not any(kw.arg == "enforce" for kw in n.keywords)]
    assert not missing, f"permitted() called without enforce= at {missing}"
