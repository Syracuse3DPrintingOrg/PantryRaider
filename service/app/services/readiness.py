"""First-boot readiness gate for a Pi-hosted appliance (FoodAssistant-0m61).

On a freshly flashed Pi the app container comes up minutes before the
co-hosted Grocy does: first boot is still pulling the inventory image and
Grocy's own first start unpacks and migrates its database. The kiosk used to
paint the setup wizard into that gap, so the user walked steps that could not
finish and Settings then claimed the inventory was broken. This module owns
the honest alternative: while the install is brand new AND its local
inventory service has never answered, browser navigation is steered to
/ui/getting-ready, a live progress page that hands off to the wizard the
moment the backend answers.

The gate is deliberately narrow and one-way:

  * It can only engage on a pi_hosted install that is not configured yet and
    has no Grocy API key. Server installs, satellites, and every configured
    install short-circuit to "no gate" without a single network probe.
  * The first time the local Grocy answers, that fact is remembered in a tiny
    state file under data_dir (same atomic-write pattern as the other shared
    state files), so the gate can never come back: not on a reload, not after
    a restart, not if Grocy later goes down mid-setup.
  * The user can always dismiss it ("continue to setup without waiting"),
    which is remembered the same way.

Probes are cached for a few seconds so the middleware never turns page loads
into a Grocy health check storm, and each probe has a hard sub-2s timeout so
a silent backend cannot slow navigation down.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

# Addresses the appliance's own Grocy may answer on, mirroring the first-run
# provisioner's candidates: the seeded loopback port on a Pi (host networking)
# and the compose service name for a plain Docker stack.
_PROBE_CANDIDATES = [
    "http://localhost:9383",
    "http://127.0.0.1:9383",
    "http://grocy:80",
]

_PROBE_TTL = 3.0        # seconds a probe result is reused for
_PROBE_TIMEOUT = 1.5    # hard per-request timeout

# In-process view of the persisted flags plus the probe cache. "loaded" flips
# after the first state-file read so steady-state calls cost nothing.
_state: dict = {
    "loaded": False,
    "answered": False,    # the local Grocy has answered at least once (sticky)
    "dismissed": False,   # the user chose to continue to setup anyway (sticky)
    "probe_at": 0.0,
    "probe_result": False,
}


def _state_file() -> Path:
    # Resolved per call (not at import) so tests that repoint data_dir work.
    from ..config import settings
    return Path(settings.data_dir) / "first-boot-ready.json"


def _load() -> None:
    if _state["loaded"]:
        return
    _state["loaded"] = True
    try:
        data = json.loads(_state_file().read_text())
    except (OSError, ValueError):
        return  # no file yet, or unreadable: keep the in-memory defaults
    if isinstance(data, dict):
        _state["answered"] = bool(data.get("answered"))
        _state["dismissed"] = bool(data.get("dismissed"))


def _save() -> None:
    """Persist the sticky flags (atomic replace, best effort). An unwritable
    data_dir degrades to in-memory flags, which still hold for this process."""
    sf = _state_file()
    try:
        tmp = sf.with_name(sf.name + ".tmp")
        tmp.write_text(json.dumps({"answered": _state["answered"],
                                   "dismissed": _state["dismissed"]}))
        os.replace(tmp, sf)
    except OSError:
        pass


def reset() -> None:
    """Forget everything (tests only)."""
    _state.update({"loaded": False, "answered": False, "dismissed": False,
                   "probe_at": 0.0, "probe_result": False})


def mark_answered() -> None:
    """Remember, permanently, that the local inventory service has answered."""
    _load()
    if not _state["answered"]:
        _state["answered"] = True
        _save()


def dismiss() -> None:
    """The user chose the wizard without waiting; never gate them again."""
    _load()
    if not _state["dismissed"]:
        _state["dismissed"] = True
        _save()


def gate_possible() -> bool:
    """Whether this install is in the only state the gate may engage in:
    a Pi-hosted appliance that has never completed setup nor connected its
    inventory. Pure settings reads, so it is safe on every request."""
    from ..config import settings
    return (settings.deployment_mode == "pi_hosted"
            and not settings.is_configured()
            and not settings.grocy_api_key)


def _probe_urls() -> list[str]:
    from ..config import settings
    urls = [settings.grocy_base_url] if settings.grocy_base_url else []
    for u in _PROBE_CANDIDATES:
        if u not in urls:
            urls.append(u)
    return urls


async def grocy_answering() -> bool:
    """Whether the local Grocy answers HTTP right now (cached for a few
    seconds). The first positive answer is remembered permanently."""
    _load()
    if _state["answered"]:
        return True
    now = time.monotonic()
    if now - _state["probe_at"] < _PROBE_TTL:
        return _state["probe_result"]
    _state["probe_at"] = now
    serving = False
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
            for url in _probe_urls():
                try:
                    r = await client.get(f"{url.rstrip('/')}/api/system/info")
                    # 200 = open, 401 = up and asking for a key: both mean the
                    # service answers, which is all the gate cares about.
                    if r.status_code in (200, 401):
                        serving = True
                        break
                except Exception:
                    continue
    except Exception:
        serving = False
    _state["probe_result"] = serving
    if serving:
        mark_answered()
    return serving


async def gate_active() -> bool:
    """Whether navigation should land on the getting-ready page instead of
    the setup wizard. False the moment any of these hold: wrong mode,
    already configured, inventory connected, the backend has answered once,
    or the user dismissed the page."""
    if not gate_possible():
        return False
    _load()
    if _state["answered"] or _state["dismissed"]:
        return False
    return not await grocy_answering()


async def status() -> dict:
    """The getting-ready page's poll payload: honest, user-forward progress.

    ``ready`` means the page should hand off to the wizard now. Each step is
    {label, state} with state one of done / working / waiting.
    """
    from ..config import settings
    connected = bool(settings.grocy_api_key)
    serving = connected or await grocy_answering()
    ready = serving or not gate_possible()
    steps = [
        {"label": "Pantry Raider is running", "state": "done"},
        {"label": "Starting the inventory service",
         "state": "done" if serving else "working"},
        {"label": "Connecting the inventory",
         "state": "done" if connected else ("working" if serving else "waiting")},
    ]
    return {"ok": True, "ready": ready, "grocy_serving": serving,
            "grocy_connected": connected, "steps": steps}
