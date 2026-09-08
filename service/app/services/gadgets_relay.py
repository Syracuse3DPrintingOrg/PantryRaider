"""Satellite gadget relay (FoodAssistant-me3t).

A satellite (deployment_mode=pi_remote) usually sits in a different room, or
a different building, than the main server, and the main server often has no
Bluetooth radio at all. The satellite's host reader
(gadgets/foodassistant_gadgets) hears the nearby thermometers, hygrometers,
door sensors, and shelf buttons and POSTs them to the satellite's own app;
this module forwards a tagged copy of every one of those pushes to the main
server's POST /gadgets/readings, authenticated with the satellite's existing
upstream API key, so sensors near any satellite appear on the server and are
managed there.

Rules of the road:

* Never block or break local ingest. Forwarding is a fire-and-forget queue
  drained by one background thread; a slow or absent server costs the local
  POST nothing.
* Never lose a button press to a blip. A failed delivery stays queued and is
  retried with backoff; queued snapshots coalesce (newest reading per device
  wins) but event entries always survive a merge.
* The server owns the truth while relaying. The satellite skips its own
  alarm evaluation and button-mapping execution when the relay is active
  (see services/gadgets.py and gadgets_buttons.py), so one breach or press
  never fires twice across the fleet.
* Config flows back down. The satellite sync mirrors the server's gadget
  device lists into settings.upstream_gadget_config, and GET /gadgets/config
  hands the local reader the merged union, so a device added on the server
  is scanned for by every satellite's radio.

The payload shaping, merging, and list-union logic are pure functions so
they test without a network or a running app.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from collections import deque

import httpx

log = logging.getLogger("foodassistant.gadgets.relay")

# The forwarding queue is bounded: when the server is unreachable for a
# while, queued snapshots merge together rather than growing without limit.
QUEUE_MAX = 30
# One upstream POST's timeout: generous enough for a sleepy server, short
# enough that a dead one cannot stall the drain loop for long.
POST_TIMEOUT = 6.0
# Retry backoff bounds for a failed delivery.
BACKOFF_MIN = 5.0
BACKOFF_MAX = 60.0
# Event entries kept across payload merges: enough for any real burst of
# presses during an outage without letting the queue hoard stale edges.
MERGE_EVENT_MAX = 100

# The keys of the server's gadget config block a satellite mirrors
# (settings.upstream_gadget_config). cub_ble_advertise and device_id ride
# along so the reader's Cub broadcast keeps carrying the SERVER's flag and
# install tag, the same values it saw when /gadgets/config was proxied.
UPSTREAM_CONFIG_KEYS = (
    "gadgets_enabled", "gadget_devices",
    "hygrometers_enabled", "hygrometer_devices",
    "buttons_enabled", "button_devices",
    "contacts_enabled", "contact_devices",
    "cub_ble_advertise", "device_id",
)

_lock = threading.Lock()
_queue: deque = deque()
_wake = threading.Event()
_worker: threading.Thread | None = None


def _norm_id(value) -> str:
    return str(value or "").strip().upper()


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

def tag_payload(payload: dict, source: str) -> dict:
    """Shape one local reader push for the upstream POST. Pure.

    Keeps the devices and discovered lists (shallow-copied so the queue never
    aliases the caller's dicts), drops the local Bluetooth-adapter block (the
    server's radio health is its own), and stamps the payload with ``source``,
    the tag the server stores per device so its UI can say which satellite
    heard it."""
    payload = payload if isinstance(payload, dict) else {}
    out = {
        "devices": [dict(e) for e in payload.get("devices") or []
                    if isinstance(e, dict)],
        "discovered": [dict(e) for e in payload.get("discovered") or []
                       if isinstance(e, dict)],
    }
    source = str(source or "").strip()[:60]
    if source:
        out["source"] = source
    return out


def merge_payloads(older: dict, newer: dict) -> dict:
    """Coalesce two queued pushes into one, newest data winning. Pure.

    Plain readings and discovered entries dedupe by (kind, id) with the later
    entry replacing the earlier one; entries carrying an ``event`` (a button
    press) are edges, not levels, so every one survives the merge (bounded by
    MERGE_EVENT_MAX, newest kept)."""
    older = older if isinstance(older, dict) else {}
    newer = newer if isinstance(newer, dict) else {}
    devices: list = []
    events: list = []
    index: dict = {}
    for entry in list(older.get("devices") or []) + list(newer.get("devices") or []):
        if not isinstance(entry, dict):
            continue
        if entry.get("event"):
            events.append(entry)
            continue
        key = (str(entry.get("kind") or ""), _norm_id(entry.get("id")))
        if key in index:
            devices[index[key]] = entry
        else:
            index[key] = len(devices)
            devices.append(entry)
    discovered: dict = {}
    for entry in list(older.get("discovered") or []) + list(newer.get("discovered") or []):
        if isinstance(entry, dict) and _norm_id(entry.get("id")):
            discovered[(str(entry.get("kind") or ""),
                        _norm_id(entry.get("id")))] = entry
    out = {"devices": devices + events[-MERGE_EVENT_MAX:],
           "discovered": list(discovered.values())}
    source = newer.get("source") or older.get("source")
    if source:
        out["source"] = source
    return out


def merge_device_lists(local, upstream) -> list:
    """Union two configured-device lists by normalized id. Pure.

    Local entries keep their position; a server (upstream) entry with the
    same id replaces the local one, because the server is where the fleet's
    gadget config is managed while the relay is on."""
    out: list = []
    index: dict = {}
    for dev in local or []:
        if isinstance(dev, dict) and _norm_id(dev.get("id")):
            index[_norm_id(dev["id"])] = len(out)
            out.append(dict(dev))
    for dev in upstream or []:
        if not (isinstance(dev, dict) and _norm_id(dev.get("id"))):
            continue
        key = _norm_id(dev["id"])
        if key in index:
            out[index[key]] = dict(dev)
        else:
            index[key] = len(out)
            out.append(dict(dev))
    return out


def normalize_upstream_gadget_config(raw) -> dict:
    """Sanitize the server's gadget_config block before it is persisted. Pure.

    Only the known keys survive: the enable flags coerce to booleans, the
    device lists keep only dicts with an id, and device_id stays a short
    string. Anything else (an older or newer server's extras) is dropped so
    the stored dict round-trips through settings cleanly."""
    out: dict = {}
    if not isinstance(raw, dict):
        return out
    for key in UPSTREAM_CONFIG_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if key.endswith("_devices"):
            if isinstance(value, list):
                out[key] = [dict(d) for d in value
                            if isinstance(d, dict) and _norm_id(d.get("id"))]
        elif key == "device_id":
            out[key] = str(value or "")[:80]
        else:
            out[key] = bool(value)
    return out


# --------------------------------------------------------------------------
# Settings glue
# --------------------------------------------------------------------------

def relay_active() -> bool:
    """Whether this install forwards its gadget pushes to a main server:
    a satellite (pi_remote) with the relay toggle on and a working upstream
    link (server URL + API key). Everything else is a no-op."""
    from ..config import settings
    return bool(settings.is_satellite()
                and settings.relay_gadgets_upstream
                and settings.remote_server_url
                and settings.upstream_api_key)


def relay_source() -> str:
    """The tag stamped on forwarded payloads: this device's hostname (the
    friendly name a fleet admin recognizes), falling back to its device_id."""
    from ..config import settings
    host = ""
    try:
        host = socket.gethostname()
    except OSError:
        pass
    return str(host or settings.device_id or "satellite")[:60]


def upstream_config() -> dict:
    """The server's mirrored gadget config, or {} when the relay is off (so a
    plain server, or a satellite that opted out, sees only its own lists)."""
    from ..config import settings
    if not relay_active():
        return {}
    return normalize_upstream_gadget_config(settings.upstream_gadget_config)


# --------------------------------------------------------------------------
# Queue and drain
# --------------------------------------------------------------------------

def enqueue(payload: dict) -> bool:
    """Queue one local push for upstream delivery. Fire-and-forget: returns
    immediately, never raises, and returns False when there is nothing to
    forward (relay off, or an empty payload)."""
    try:
        if not relay_active():
            return False
        item = tag_payload(payload, relay_source())
        if not item["devices"] and not item["discovered"]:
            return False
        with _lock:
            _queue.append(item)
            while len(_queue) > QUEUE_MAX:
                oldest = _queue.popleft()
                nxt = _queue.popleft()
                _queue.appendleft(merge_payloads(oldest, nxt))
        _ensure_worker()
        _wake.set()
        return True
    except Exception as exc:  # noqa: BLE001 - forwarding never breaks ingest
        log.warning("Could not queue a gadget push for the server: %s", exc)
        return False


def pending() -> int:
    """How many pushes wait for delivery (for tests and diagnostics)."""
    with _lock:
        return len(_queue)


def _post(item: dict) -> bool:
    """Deliver one payload upstream. True when the server answered at all
    with a non-5xx status: a 4xx (bad key, older server) will not improve by
    retrying the same payload, so it counts as done and is logged instead."""
    from ..config import settings
    base = (settings.remote_server_url or "").rstrip("/")
    key = settings.upstream_api_key
    if not base or not key:
        return False
    resp = httpx.post(f"{base}/gadgets/readings", json=item,
                      headers={"X-API-Key": key}, timeout=POST_TIMEOUT)
    if resp.status_code >= 500:
        return False
    if resp.status_code != 200:
        log.warning("Server declined a relayed gadget push (%s); dropping it",
                    resp.status_code)
        return True
    _mirror_alarms_from_reply(resp)
    return True


def _mirror_alarms_from_reply(resp) -> None:
    """Show the server's live alarms on this device's own screen.

    The server owns the alarm call while we relay, so its fridge and door
    warnings would otherwise only reach its own screens, never the kiosk
    standing in the kitchen. Its reply to our push carries them back, which
    makes this the quickest path there is: the toast lands seconds after the
    reading that raised it, with no extra request. Deduped by alarm onset in
    services/satellite.py, and best-effort: an unreadable reply just means no
    toast, never a lost delivery."""
    try:
        alarms = (resp.json() or {}).get("alarms")
        if not alarms:
            return
        from . import satellite
        satellite.mirror_alarms(alarms)
    except Exception as exc:  # noqa: BLE001 - the push itself already landed
        log.debug("Could not mirror the server's alarms: %s", exc)


def _drain_once() -> str:
    """Handle the head of the queue: "sent", "failed", or "idle".

    A failed head merges into the next queued push (when there is one) so the
    queue always retries the freshest combined view instead of replaying a
    stale snapshot first."""
    with _lock:
        if not _queue:
            return "idle"
        item = _queue[0]
    if not relay_active():
        with _lock:
            _queue.clear()
        return "idle"
    try:
        delivered = _post(item)
    except Exception as exc:  # noqa: BLE001 - network errors are retried
        log.debug("Gadget relay delivery failed: %s", exc)
        delivered = False
    with _lock:
        if delivered:
            if _queue and _queue[0] is item:
                _queue.popleft()
            return "sent"
        if len(_queue) > 1 and _queue[0] is item:
            _queue.popleft()
            nxt = _queue.popleft()
            _queue.appendleft(merge_payloads(item, nxt))
    return "failed"


def _run() -> None:
    backoff = BACKOFF_MIN
    while True:
        try:
            result = _drain_once()
        except Exception as exc:  # noqa: BLE001 - the worker must not die
            log.warning("Gadget relay worker error: %s", exc)
            result = "failed"
        if result == "sent":
            backoff = BACKOFF_MIN
        elif result == "failed":
            time.sleep(backoff)
            backoff = min(BACKOFF_MAX, backoff * 2)
        else:
            _wake.wait(timeout=5.0)
            _wake.clear()


def _ensure_worker() -> None:
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    _worker = threading.Thread(target=_run, name="gadgets-relay", daemon=True)
    _worker.start()


def reset() -> None:
    """Clear the queue (used by tests). The worker thread, if any, idles."""
    with _lock:
        _queue.clear()
    _wake.clear()
