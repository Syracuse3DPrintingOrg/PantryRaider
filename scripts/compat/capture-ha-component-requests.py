#!/usr/bin/env python3
"""Record the requests every fielded Home Assistant integration sends.

Build-time only: run it from the repository root when the fixture needs to be
made again. It reads homeassistant/custom_components/pantry_raider/api.py as
it was at each listed revision (git show), loads it with stand-ins for aiohttp
and async_timeout, drives every client method with a recording session, and
writes tests/data/compat_ha_component_requests.json. The test suite replays
that file against the current app and never runs git itself.

    python3 scripts/compat/capture-ha-component-requests.py

Secrets never reach the file: header values are replaced by placeholders and
only the header names are kept.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
API_PATH = "homeassistant/custom_components/pantry_raider/api.py"
OUT = ROOT / "tests" / "data" / "compat_ha_component_requests.json"

# The first integration (2026-07-14) and the newest published release.
REVISIONS = (("c9517f9", "c9517f9"), ("v0.20.0", "v0.20.0"))

BASE_URL = "http://192.168.1.50:9284"
KEY_PLACEHOLDER = "<api-key>"
TOKEN_PLACEHOLDER = "<ha-token>"


class _Response:
    def __init__(self, url: str):
        self.status = 200
        self._url = url

    async def json(self):
        if "/api/pairing/request" in self._url:
            return {"ok": True, "request_id": "req-1", "code": "1234"}
        if "/api/pairing/status/" in self._url:
            return {"status": "pending"}
        return {"ok": True}


class _Session:
    """Records each call the client makes instead of sending it."""

    def __init__(self, calls: list):
        self.calls = calls

    def _record(self, method: str, url: str, headers=None, json=None):
        path = url[len(BASE_URL):] if url.startswith(BASE_URL) else url
        names = sorted((headers or {}).keys())
        self.calls.append({
            "method": method,
            "path": path,
            "headers": names,
            "json": json,
        })
        return _Response(url)

    async def get(self, url, headers=None, **_kw):
        return self._record("GET", url, headers=headers)

    async def post(self, url, headers=None, json=None, **_kw):
        return self._record("POST", url, headers=headers, json=json)


class _Timeout:
    def __init__(self, *_a, **_kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def _install_stand_ins() -> None:
    aiohttp = types.ModuleType("aiohttp")

    class ClientError(Exception):
        pass

    aiohttp.ClientError = ClientError
    aiohttp.ClientSession = object
    async_timeout = types.ModuleType("async_timeout")
    async_timeout.timeout = _Timeout
    sys.modules["aiohttp"] = aiohttp
    sys.modules["async_timeout"] = async_timeout


def _load(source: str, name: str):
    module = types.ModuleType(name)
    module.__file__ = f"<{name}>"
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


async def _drive(module, calls: list) -> None:
    """Call every client method the integration uses, keyless and keyed."""
    for key in (None, KEY_PLACEHOLDER):
        client = module.PantryRaiderClient(_Session(calls), BASE_URL, key)
        for name, args in (
            ("async_get_state", ()),
            ("async_post_settings", ({"display_idle_timeout": 10},)),
            ("async_display_action", ("wake",)),
            ("async_notify", ("Dinner is ready", "Kitchen", "info", 0)),
            ("async_camera_popup", ("Front Door", 20)),
            ("async_camera_detect", ("Front Door", "person", 0)),
            ("async_connect", ("http://192.168.1.40:8123", TOKEN_PLACEHOLDER)),
            ("async_request_pairing", ()),
            ("async_pairing_status", ("req-1",)),
        ):
            method = getattr(client, name, None)
            if method is None:
                continue
            start = len(calls)
            await method(*args)
            for call in calls[start:]:
                call["keyed"] = key is not None
                call["client_method"] = name


def main() -> int:
    _install_stand_ins()
    out = {"generated_by": "scripts/compat/capture-ha-component-requests.py",
           "versions": []}
    for label, rev in REVISIONS:
        source = subprocess.run(
            ["git", "-C", str(ROOT), "show", f"{rev}:{API_PATH}"],
            check=True, capture_output=True, text=True).stdout
        module = _load(source, f"pantry_raider_api_{label.replace('.', '_')}")
        calls: list = []
        asyncio.run(_drive(module, calls))
        for call in calls:
            call["version"] = label
        out["versions"].append({"version": label, "calls": calls})
    OUT.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
