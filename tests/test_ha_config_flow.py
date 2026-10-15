"""The integration's config flow, run against the same stand-ins for Home
Assistant that test_ha_integration_removals.py uses.

Each case here was found on a real Home Assistant 2026.10 with two Pantry
Raider installs on its network:

- adding an install by hand while its discovered card waited on the
  integrations page aborted with "already in progress" after the pairing had
  been approved, so the key the install handed over was lost;
- a zeroconf announcement carrying a container's own address and the
  published port moved a working entry there, and the entry never loaded
  again;
- the options flow reloads the entry itself on current Home Assistant, so the
  integration adds no update listener there (Home Assistant warns about one on
  every reauth, and it stops working in 2026.12).
"""
from __future__ import annotations

import asyncio
import importlib
import sys
import types

import pytest

from test_ha_integration_removals import _PKG, integration  # noqa: F401


class _AbortFlow(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class _FakeConfigFlow:
    """The parts of Home Assistant's ConfigFlow the flow calls, recorded."""

    def __init_subclass__(cls, domain=None, **kwargs):
        super().__init_subclass__(**kwargs)

    def __init__(self, entries=()):
        self.entries = list(entries)
        self.context = {}
        self.unique_id = None
        self.unique_id_calls = []
        self.abort_updates = []

    async def async_set_unique_id(self, unique_id, *, raise_on_progress=True):
        self.unique_id_calls.append((unique_id, raise_on_progress))
        self.unique_id = unique_id

    def _async_current_entries(self, include_ignore=None):
        return list(self.entries)

    def _abort_if_unique_id_configured(self, updates=None, **kw):
        self.abort_updates.append(updates)
        for entry in self.entries:
            if entry.unique_id == self.unique_id:
                if updates:
                    entry.data.update(updates)
                raise _AbortFlow("already_configured")

    def async_create_entry(self, *, title, data):
        return {"type": "create_entry", "title": title, "data": data}


class _Entry:
    def __init__(self, unique_id, host, port, api_key="entry-key"):
        self.unique_id = unique_id
        self.data = {"host": host, "port": port, "api_key": api_key}


class _StateClient:
    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    async def async_get_state(self):
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def flow_module(integration, monkeypatch):  # noqa: F811
    config_entries = sys.modules["homeassistant.config_entries"]
    monkeypatch.setattr(config_entries, "ConfigFlow", _FakeConfigFlow, raising=False)
    for name in ("homeassistant.helpers.service_info",
                 "homeassistant.helpers.service_info.zeroconf"):
        mod = types.ModuleType(name)
        mod.__path__ = []
        mod.ZeroconfServiceInfo = object
        monkeypatch.setitem(sys.modules, name, mod)
    return importlib.import_module(_PKG + ".config_flow")


def _flow(module, entries=(), client=None):
    flow = module.PantryRaiderConfigFlow(entries)
    clients = []

    async def _client(host, port, api_key):
        clients.append((host, port, api_key))
        return client

    flow._async_client = _client
    flow.clients = clients
    return flow


def test_a_paired_flow_finishes_while_a_discovered_card_waits(flow_module):
    flow = _flow(flow_module)
    result = asyncio.run(flow._async_finish(
        "pantry.lan", 9284, "new-key", {"device_id": "dev-1", "hostname": "Kitchen"}))
    assert result["type"] == "create_entry"
    assert result["data"]["api_key"] == "new-key"
    # Home Assistant raises "already in progress" from async_set_unique_id
    # unless told otherwise; the waiting card is closed when the entry is made.
    assert flow.unique_id_calls == [("dev-1", False)]


def test_a_discovery_does_not_move_a_working_entry_to_an_address_that_fails(flow_module):
    m = flow_module
    entry = _Entry("dev-1", "pantry-server", 8000)
    flow = _flow(m, [entry], _StateClient(m.PantryRaiderConnectionError("refused")))
    info = types.SimpleNamespace(host="172.28.0.2", port=9284,
                                 properties={"device_id": "dev-1", "hostname": "Kitchen"})
    with pytest.raises(_AbortFlow):
        asyncio.run(flow.async_step_zeroconf(info))
    assert entry.data["host"] == "pantry-server" and entry.data["port"] == 8000
    assert flow.abort_updates == [None]
    # The announced address was tried with the entry's own key.
    assert flow.clients == [("172.28.0.2", 9284, "entry-key")]


def test_a_discovery_moves_an_entry_when_the_new_address_is_the_same_install(flow_module):
    m = flow_module
    entry = _Entry("dev-1", "192.168.1.20", 9284)
    flow = _flow(m, [entry], _StateClient({"device_id": "dev-1"}))
    info = types.SimpleNamespace(host="192.168.1.31", port=9284,
                                 properties={"device_id": "dev-1"})
    with pytest.raises(_AbortFlow):
        asyncio.run(flow.async_step_zeroconf(info))
    assert entry.data["host"] == "192.168.1.31"


@pytest.mark.parametrize("answer", [
    {"device_id": "someone-else"},
    "auth",
])
def test_a_discovery_leaves_the_entry_when_the_address_is_not_confirmed(flow_module, answer):
    m = flow_module
    if answer == "auth":
        answer = m.PantryRaiderAuthError("401")
    entry = _Entry("dev-1", "192.168.1.20", 9284)
    flow = _flow(m, [entry], _StateClient(answer))
    info = types.SimpleNamespace(host="192.168.1.31", port=9284,
                                 properties={"device_id": "dev-1"})
    with pytest.raises(_AbortFlow):
        asyncio.run(flow.async_step_zeroconf(info))
    assert entry.data["host"] == "192.168.1.20"


def test_a_discovery_at_the_known_address_asks_nothing(flow_module):
    m = flow_module
    client = _StateClient({"device_id": "dev-1"})
    entry = _Entry("dev-1", "192.168.1.20", 9284)
    flow = _flow(m, [entry], client)
    info = types.SimpleNamespace(host="192.168.1.20", port=9284,
                                 properties={"device_id": "dev-1"})
    with pytest.raises(_AbortFlow):
        asyncio.run(flow.async_step_zeroconf(info))
    assert client.calls == 0


def test_options_flow_reloads_the_entry_itself(flow_module, integration):  # noqa: F811
    config_entries = sys.modules["homeassistant.config_entries"]
    assert issubclass(flow_module.PantryRaiderOptionsFlow,
                      config_entries.OptionsFlowWithReload)
    assert integration.OPTIONS_FLOW_RELOADS is True
