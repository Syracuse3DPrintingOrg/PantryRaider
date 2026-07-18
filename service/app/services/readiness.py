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

The gate holds until the inventory is CONNECTED, not merely answering
(Dan, 2026-07-16). Grocy answering its first HTTP request is the start of the
work, not the end: first-run provisioning still has to sign in and create the
API key. Releasing on the first answer handed the user a wizard whose
inventory pane was still empty, and if provisioning then failed there was
nothing on screen saying so. So "ready" now means the same thing the user
means by it: the inventory is connected and the wizard can actually finish.

The gate is deliberately narrow and one-way:

  * It can only engage on a pi_hosted install whose inventory is not connected
    yet (no Grocy API key). Server installs, satellites, and any install with a
    key short-circuit to "no gate" without a single network probe. It keys on
    the API key, NOT is_configured(): a Pi seeds its own Grocy URL, so a wizard
    password makes is_configured() true while the inventory is still empty, and
    releasing there is exactly what dropped users into a Grocy-less app.
  * It releases for good the moment the inventory connects (an API key is
    saved, which ends gate_possible outright), the moment first-run
    provisioning reports it has stopped trying (a Grocy that is someone
    else's, so no key is coming), or on a hard deadline, so a wedged or
    crashed provisioner can never wall the user out.
  * The user can always dismiss it ("continue to setup without waiting"),
    which is remembered the same way.

Every release reason that is not a settings read is remembered in a tiny
state file under data_dir (same atomic-write pattern as the other shared
state files), so the gate can never come back: not on a reload, not after a
restart.

Probes are cached for a few seconds so the middleware never turns page loads
into a Grocy health check storm, and each probe has a hard sub-2s timeout so
a silent backend cannot slow navigation down.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import httpx

logger = logging.getLogger("foodassistant.readiness")

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

# Backstop: the longest the gate may hold before it lets the user through
# regardless. Only reachable if first-run provisioning dies without reporting
# (it normally releases the gate itself, within seconds of Grocy answering, by
# connecting or by giving up), so this is a safety net against a wedged
# provisioner walling someone out of their own device, not a normal wait. It
# sits just past the provisioner's own retry budget (~28 minutes) so it can
# never fire while provisioning is still genuinely trying.
_GATE_MAX_WAIT = 35 * 60

# In-process view of the persisted flags plus the probe cache. "loaded" flips
# after the first state-file read so steady-state calls cost nothing.
_state: dict = {
    "loaded": False,
    "answered": False,    # the local Grocy has answered at least once (sticky)
    "dismissed": False,   # the user chose to continue to setup anyway (sticky)
    "provisioned": False,  # first-run provisioning has stopped trying (sticky)
    "first_seen": 0.0,    # wall-clock of the first gate evaluation (deadline base)
    "probe_at": 0.0,
    "probe_result": False,
    "deadline_logged": False,  # the backstop release is logged once, not per request
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
        _state["provisioned"] = bool(data.get("provisioned"))
        try:
            _state["first_seen"] = float(data.get("first_seen") or 0.0)
        except (TypeError, ValueError):
            _state["first_seen"] = 0.0


def _save() -> None:
    """Persist the sticky flags (atomic replace, best effort). An unwritable
    data_dir degrades to in-memory flags, which still hold for this process."""
    sf = _state_file()
    try:
        tmp = sf.with_name(sf.name + ".tmp")
        tmp.write_text(json.dumps({"answered": _state["answered"],
                                   "dismissed": _state["dismissed"],
                                   "provisioned": _state["provisioned"],
                                   "first_seen": _state["first_seen"]}))
        os.replace(tmp, sf)
    except OSError:
        pass


def reset() -> None:
    """Forget everything (tests only)."""
    _state.update({"loaded": False, "answered": False, "dismissed": False,
                   "provisioned": False, "first_seen": 0.0,
                   "probe_at": 0.0, "probe_result": False,
                   "deadline_logged": False})


def mark_answered() -> None:
    """Remember, permanently, that the local inventory service has answered.

    This drives the progress display only. It is deliberately NOT a release
    reason: see the module docstring."""
    _load()
    if not _state["answered"]:
        _state["answered"] = True
        _save()
        logger.info("First boot: the local inventory service answered its "
                    "first request; provisioning connects it next")


def dismiss() -> None:
    """The user chose the wizard without waiting; never gate them again."""
    _load()
    if not _state["dismissed"]:
        _state["dismissed"] = True
        _save()
        logger.info("First-boot gate released: the user chose to continue "
                    "to setup without waiting")


def mark_provisioning_done() -> None:
    """Remember that first-run provisioning has stopped trying to connect the
    inventory, so the gate must let the user through.

    Called by services.first_run when the Grocy attempt reaches a definite end
    that is not a connection: most often a Grocy that is already someone's own
    (its sign-in has been changed), where no key is ever coming and waiting
    would be a lie. Sticky, like every other release reason."""
    _load()
    if not _state["provisioned"]:
        _state["provisioned"] = True
        _save()
        logger.info("First-boot gate released: provisioning has finished "
                    "with the inventory (see the first-run lines above for "
                    "how it ended)")


def gate_possible() -> bool:
    """Whether this install is in the only state the gate may engage in:
    a Pi-hosted appliance whose inventory is not connected yet.

    The test is deliberately CONNECTED (an API key is saved), not
    is_configured(). A Pi appliance seeds its own Grocy URL, so the wizard
    saving a password flips is_configured() true while grocy_api_key, and thus
    the inventory, is still empty: gating on is_configured() there released the
    user into a half-configured app the moment they finished the wizard, before
    first-run provisioning had connected Grocy (Dan, 2026-07-16). Keying on the
    API key holds the gate until the inventory is genuinely connected, which is
    what the user means by "ready". Pure settings reads, safe on every
    request; pi_hosted-only, so servers and satellites never gate."""
    from ..config import settings
    return (settings.deployment_mode == "pi_hosted"
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
                    # service is serving, which is the progress page's second
                    # step (the third, connecting it, is provisioning's job).
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


def _deadline_passed() -> bool:
    """Whether the gate has held for longer than it may (see _GATE_MAX_WAIT).

    The clock starts at the first gate evaluation and is persisted, so a
    restart loop cannot keep resetting it and hold the user forever."""
    now = time.time()
    if not _state["first_seen"]:
        _state["first_seen"] = now
        _save()
        return False
    # A clock that jumped backwards (a Pi has no RTC; NTP lands mid-boot)
    # would otherwise freeze the deadline. Re-base and keep waiting.
    if now < _state["first_seen"]:
        _state["first_seen"] = now
        _save()
        return False
    return (now - _state["first_seen"]) > _GATE_MAX_WAIT


async def gate_active() -> bool:
    """Whether navigation should land on the getting-ready page instead of
    the setup wizard.

    False the moment any of these hold: wrong mode, already configured, the
    inventory is connected (both via gate_possible), first-run provisioning
    has stopped trying, the user dismissed the page, or the backstop deadline
    passed. Note what is deliberately NOT a release: the backend merely
    answering. Provisioning connects seconds after that first answer, and
    handing the wizard over in between is what left the user staring at an
    inventory pane with nothing in it."""
    if not gate_possible():
        return False
    _load()
    if _state["dismissed"] or _state["provisioned"]:
        return False
    # Keep the progress display honest while we wait (sticky "answered").
    await grocy_answering()
    if _deadline_passed():
        if not _state["deadline_logged"]:
            _state["deadline_logged"] = True
            logger.info("First-boot gate released: nothing connected the "
                        "inventory within the %d minute backstop deadline",
                        _GATE_MAX_WAIT // 60)
        return False
    return True


async def status() -> dict:
    """The getting-ready page's poll payload: honest, user-forward progress.

    ``ready`` means the page should hand off to the wizard now. Each step is
    {label, state} with state one of done / working / waiting.
    """
    from ..config import settings
    connected = bool(settings.grocy_api_key)
    serving = connected or await grocy_answering()
    # One source of truth with the middleware: the page hands off exactly when
    # navigation would no longer be steered here, never before.
    ready = not await gate_active()
    steps = [
        {"label": "Pantry Raider is running", "state": "done"},
        {"label": "Starting the inventory service",
         "state": "done" if serving else "working"},
        {"label": "Connecting the inventory",
         "state": "done" if connected else ("working" if serving else "waiting")},
    ]
    return {"ok": True, "ready": ready, "grocy_serving": serving,
            "grocy_connected": connected, "steps": steps}
