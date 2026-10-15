"""Which Home Assistant tokens the integration removes, run against a fake hub.

The integration hands each Pantry Raider install a token of its own and, once
an install has a limited account, removes the tokens it replaced. Older
versions handed every install the same owner token (named "Pantry Raider"),
which must stay while any install that ever received it still lacks a
limited account: removing it then would cut that install off from Home
Assistant. Only tokens this entry made are ever removed.

The integration imports Home Assistant itself, which this suite does not
install, so the package is loaded here with small stand-ins for the parts of
Home Assistant it touches at import time, and driven with a fake hub that
records every token removal.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_COMPONENT = (Path(__file__).resolve().parents[1] / "homeassistant" / "custom_components"
              / "pantry_raider")
_PKG = "_pr_ha_integration_under_test"
_LONG_LIVED = "long_lived_access_token"


class _StubMeta(type):
    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _stub_class(name)


def _stub_class(name):
    return _StubMeta(name, (Exception,), {
        "__init__": lambda self, *a, **k: None,
        "__class_getitem__": classmethod(lambda cls, item: cls),
    })


class _StubModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        value = _stub_class(name)
        setattr(self, name, value)
        return value


_STUB_NAMES = [
    "homeassistant", "homeassistant.auth", "homeassistant.auth.const",
    "homeassistant.auth.models", "homeassistant.config_entries", "homeassistant.const",
    "homeassistant.core", "homeassistant.exceptions", "homeassistant.helpers",
    "homeassistant.helpers.config_validation", "homeassistant.helpers.device_registry",
    "homeassistant.helpers.issue_registry", "homeassistant.helpers.aiohttp_client",
    "homeassistant.helpers.network", "homeassistant.helpers.update_coordinator",
    "homeassistant.loader", "voluptuous", "aiohttp", "async_timeout",
]


@pytest.fixture
def integration():
    saved = {name: sys.modules.get(name) for name in _STUB_NAMES + [_PKG]}
    saved.update({name: mod for name, mod in sys.modules.items() if name.startswith(_PKG + ".")})
    try:
        for name in _STUB_NAMES:
            mod = _StubModule(name)
            mod.__path__ = []          # a package, so dotted children import
            sys.modules[name] = mod
        sys.modules["homeassistant.core"].callback = lambda fn: fn
        sys.modules["homeassistant.auth.const"].GROUP_ID_ADMIN = "system-admin"
        sys.modules["homeassistant.auth.models"].TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN = _LONG_LIVED
        const = sys.modules["homeassistant.const"]
        const.CONF_API_KEY, const.CONF_HOST, const.CONF_PORT = "api_key", "host", "port"
        spec = importlib.util.spec_from_file_location(
            _PKG, _COMPONENT / "__init__.py", submodule_search_locations=[str(_COMPONENT)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[_PKG] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        for name in [n for n in sys.modules if n == _PKG or n.startswith(_PKG + ".")]:
            del sys.modules[name]
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


class _Token:
    def __init__(self, token_id, client_name, token_type=_LONG_LIVED):
        self.id = token_id
        self.client_name = client_name
        self.token_type = token_type


class _User:
    def __init__(self, tokens, is_owner=True):
        self.refresh_tokens = {t.id: t for t in tokens}
        self.is_owner = is_owner


class _Auth:
    def __init__(self, owner):
        self.owner = owner
        self.removed = []

    async def async_get_owner(self):
        return self.owner

    async def async_get_users(self):
        return [self.owner, *getattr(self, "others", [])]

    def async_get_refresh_token(self, token_id):       # a plain callback, as in HA
        return self.owner.refresh_tokens.get(token_id)

    def async_remove_refresh_token(self, token):
        self.removed.append(token.id)
        self.owner.refresh_tokens.pop(token.id, None)


class _Entry:
    def __init__(self, entry_id, options):
        self.entry_id = entry_id
        self.options = dict(options)
        self.title = entry_id


class _ConfigEntries:
    def __init__(self, entries):
        self.entries = entries

    def async_entries(self, domain):
        assert domain == "pantry_raider"
        return list(self.entries)

    def async_update_entry(self, entry, options=None, **kw):
        if options is not None:
            entry.options = dict(options)


class _Hass:
    def __init__(self, entries, owner):
        self.config_entries = _ConfigEntries(entries)
        self.auth = _Auth(owner)


def _iso(delta_hours):
    return (datetime.now(timezone.utc) + timedelta(hours=delta_hours)).isoformat()


def test_the_shared_owner_token_stays_while_another_install_needs_it(integration):
    m = integration
    shared = _Token("shared", "Pantry Raider")
    others = [_Token("mine-old", "Pantry Raider (Kitchen)"), _Token("frigate", "Frigate"),
              _Token("pending-due", "Pantry Raider (Kitchen) 2"),
              _Token("pending-later", "Pantry Raider (Kitchen) 3"),
              _Token("browser", "Pantry Raider", token_type="normal")]
    owner = _User([shared, *others])
    dedicated = _Entry("a", {
        "connect_done": True, "ha_token_kind": "dedicated", "ha_token_id": "ded-1",
        "ha_legacy_remove_after": _iso(-1),
        "ha_pending_removals": [{"token_id": "pending-due", "after": _iso(-1)},
                                {"token_id": "pending-later", "after": _iso(+1)}],
    })
    # A second install that received the shared token under 0.20 and whose
    # app has no limited account yet.
    legacy = _Entry("b", {"connect_done": True, "ha_token_kind": "owner"})
    hass = _Hass([dedicated, legacy], owner)

    asyncio.run(m._async_process_removals(hass, dedicated))
    assert hass.auth.removed == ["pending-due"]
    assert "shared" in owner.refresh_tokens
    # Still due once the other install moves over.
    assert dedicated.options["ha_legacy_remove_after"]
    assert [i["token_id"] for i in dedicated.options["ha_pending_removals"]] == ["pending-later"]
    assert dedicated.options["ha_removed_refs"]

    # An install that never received a token does not hold the shared one up.
    hass.config_entries.entries.append(_Entry("c", {}))
    asyncio.run(m._async_process_removals(hass, dedicated))
    assert "shared" in owner.refresh_tokens

    # The second install moves to a limited account: now the shared token goes,
    # and only that one.
    legacy.options.update({"ha_token_kind": "dedicated", "ha_token_id": "ded-2"})
    asyncio.run(m._async_process_removals(hass, dedicated))
    assert hass.auth.removed == ["pending-due", "shared"]
    assert set(owner.refresh_tokens) == {"mine-old", "frigate", "pending-later", "browser"}
    assert "ha_legacy_remove_after" not in dedicated.options


def test_an_entry_with_history_but_no_kind_counts_as_needing_the_token(integration):
    """An entry set up under 0.20 has connect_done but no recorded kind."""
    m = integration
    shared = _Token("shared", "Pantry Raider")
    owner = _User([shared])
    mine = _Entry("a", {"connect_done": True, "ha_token_kind": "dedicated",
                        "ha_legacy_remove_after": _iso(-2)})
    old = _Entry("b", {"connect_done": True})
    hass = _Hass([mine, old], owner)
    asyncio.run(m._async_process_removals(hass, mine))
    assert hass.auth.removed == [] and "shared" in owner.refresh_tokens


def test_nothing_is_removed_before_its_time(integration):
    m = integration
    shared = _Token("shared", "Pantry Raider")
    owner = _User([shared, _Token("later", "x")])
    mine = _Entry("a", {"connect_done": True, "ha_token_kind": "dedicated",
                        "ha_legacy_remove_after": _iso(+1),
                        "ha_pending_removals": [{"token_id": "later", "after": _iso(+1)}]})
    hass = _Hass([mine], owner)
    asyncio.run(m._async_process_removals(hass, mine))
    assert hass.auth.removed == []


class _Client:
    async def async_disconnect(self):
        return {"ok": True}


def test_an_entry_that_disconnected_no_longer_holds_up_the_shared_token(integration):
    """Connect-back turned off: the install forgot the connection and this
    entry's tokens are gone, so it does not keep the shared token alive."""
    m = integration
    shared = _Token("shared", "Pantry Raider")
    own = _Token("own-b", "Pantry Raider (b)")
    owner = _User([shared, own])
    mine = _Entry("a", {"connect_done": True, "ha_token_kind": "dedicated",
                        "ha_token_id": "ded-1", "ha_legacy_remove_after": _iso(-1)})
    off = _Entry("b", {"connect_done": True, "ha_token_kind": "owner",
                       "ha_owner_token_id": "own-b"})
    off.runtime_data = types.SimpleNamespace(main=types.SimpleNamespace(client=_Client()))
    hass = _Hass([mine, off], owner)
    asyncio.run(m._async_disconnect_own(hass, off))
    assert hass.auth.removed == ["own-b"]
    assert "connect_done" not in off.options and "ha_token_kind" not in off.options
    asyncio.run(m._async_process_removals(hass, mine))
    assert hass.auth.removed == ["own-b", "shared"]


def _ref(token_id):
    import hashlib
    return hashlib.sha256(token_id.encode()).hexdigest()[:16]


def test_a_token_pasted_before_021_is_told_apart_from_a_handover(integration):
    m = integration
    shared = _Token("shared", "Pantry Raider")
    pasted = _Token("pasted", "Kitchen tablet")
    on_other = _Token("helper", "Pantry Raider")
    owner = _User([shared, pasted])
    hass = _Hass([], owner)
    hass.auth.others = [_User([on_other], is_owner=False)]
    entry = _Entry("a", {"ha_owner_token_id": "mine"})
    owner.refresh_tokens["mine"] = _Token("mine", "Pantry Raider (a)")

    def pasted_check(token_id):
        return asyncio.run(m._async_install_token_pasted(hass, entry, _ref(token_id)))

    assert pasted_check("pasted") is True
    # A token named like the shared one on another account was made by hand.
    assert pasted_check("helper") is True
    # The shared owner token and this entry's own are handovers.
    assert pasted_check("shared") is False
    assert pasted_check("mine") is False
    # A token this Home Assistant does not have is not counted as pasted.
    assert pasted_check("gone") is False
    # Nothing is ever removed by the check.
    assert hass.auth.removed == []


class _MintingAuth(_Auth):
    def __init__(self, owner):
        super().__init__(owner)
        self.minted = []

    async def async_create_refresh_token(self, user, client_name=None, token_type=None,
                                         access_token_expiration=None):
        names = {t.client_name for t in user.refresh_tokens.values()
                 if t.token_type == _LONG_LIVED}
        if client_name in names:      # Home Assistant refuses a repeated name
            raise ValueError(client_name)
        token = _Token(f"new-{len(self.minted) + 1}", client_name, token_type)
        user.refresh_tokens[token.id] = token
        self.minted.append(token)
        return token

    def async_create_access_token(self, refresh):
        return "access-" + refresh.id


class _ConnectClient:
    async def async_connect(self, *a, **kw):
        return {"ok": True, "verified": True}


def test_an_owner_token_handover_never_touches_another_entrys_token(integration):
    """Two entries Home Assistant made minutes apart share the leading
    characters of their ids (seen on a real 2026.10 install). Each one's
    owner token is still named apart, and a second handover replaces only
    this entry's own earlier token, even one named the older way."""
    m = integration
    first_id, second_id = "01M4YZWRN9JK3Y99D373H6MH75", "01M4YZQWDJHP35Z6S1K1QWGFZF"
    # The other entry's token, named the way older versions named it: the
    # same name this entry's would have had under that scheme.
    other = _Token("other", "Pantry Raider (pr 01M4YZ)")
    mine_old = _Token("mine-old", "Pantry Raider (pr 01M4YZ) old")
    owner = _User([other, mine_old])
    entry = _Entry(first_id, {"connect_done": False, "ha_owner_token_id": "mine-old"})
    entry.title = "pr"
    second = _Entry(second_id, {})
    second.title = "pr"
    entry.runtime_data = types.SimpleNamespace(
        main=types.SimpleNamespace(client=_ConnectClient()), version="1.0.0")
    hass = _Hass([entry, second], owner)
    hass.auth = _MintingAuth(owner)

    asyncio.run(m._async_hand_over_owner_token(hass, entry, "http://ha.lan:8123"))
    assert hass.auth.removed == ["mine-old"]
    assert "other" in owner.refresh_tokens
    made = hass.auth.minted[0]
    assert made.client_name != m.owner_token_name("pr", second_id)
    assert entry.options["ha_owner_token_id"] == made.id

    # Handed over again (the install went back to an older version): the
    # earlier token goes, the other entry's stays.
    asyncio.run(m._async_hand_over_owner_token(hass, entry, "http://ha.lan:8123"))
    assert hass.auth.removed == ["mine-old", made.id]
    assert "other" in owner.refresh_tokens
