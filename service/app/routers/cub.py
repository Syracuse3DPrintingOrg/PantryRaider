"""Bandit Cub endpoints (docs/design/bandit-cub.md).

GET /cub/summary is the one URL a Cub polls: the server decides what the
device should show (view takeovers, idle rotation) and hands back every block
plus the effective content settings, so a policy change never needs a
reflash. Auth rides the same X-API-Key headless-client path /ha/state uses
(the require_auth middleware in main.py; /cub is deliberately in no bypass
list). Every block degrades to a calm empty/zero on a backend failure rather
than a 500, per the /ha/state precedent: a display polling every 15 seconds
must never see the endpoint itself go down because Grocy did.

The poll doubles as the device's heartbeat: the X-Cub-* headers upsert the
registry row (services/cub.py). No headers, no row, so curl testing works.
The remaining routes back the Bandit Cubs section of the Devices pane.
"""
from __future__ import annotations

import os
import time
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from ..config import settings, APP_VERSION, GITHUB_REPO
from ..services import cub as cub_svc

router = APIRouter(prefix="/cub", tags=["cub"])


# -- GET /cub/summary ----------------------------------------------------------


async def _expiring_items() -> tuple[list[dict] | None, bool]:
    """The expiring items list, riding the same /expiring/count cache
    /ha/state uses so Cub polls never add their own Grocy pull. (None, False)
    on any failure."""
    from . import expiring as expiring_router
    from ..services.grocy import GrocyClient

    try:
        all_items = expiring_router._count_items_cache.get()
        if all_items is None:
            grocy = GrocyClient()
            all_items = await grocy.get_expiring(days=30)
            expiring_router._count_items_cache.set(all_items)
        return all_items, True
    except Exception:
        return None, False


def _timers() -> list[dict]:
    from ..services import timers as timers_svc
    try:
        return timers_svc.list_timers()
    except Exception:
        return []


def _gadget_devices() -> list[dict]:
    from ..services import gadgets
    try:
        state = gadgets.get_state()
        devices = state.get("devices", []) if isinstance(state, dict) else []
        return devices if isinstance(devices, list) else []
    except Exception:
        return []


def _hygro_devices() -> list[dict]:
    from ..services import gadgets
    try:
        state = gadgets.get_state()
        devices = state.get("hygrometers", []) if isinstance(state, dict) else []
        return devices if isinstance(devices, list) else []
    except Exception:
        return []


def _alarms() -> list[dict]:
    """The live protection alarms (fridge/freezer thresholds, doors left
    open; FoodAssistant-5c61), degrading to an empty list on any trouble."""
    from ..services import gadgets
    try:
        alarms = gadgets.active_alarms()
        return alarms if isinstance(alarms, list) else []
    except Exception:
        return []


def _counts() -> dict:
    # Same pending/action-items counts the HA snapshot reports; the builder
    # already degrades to zeros on any DB trouble.
    from .ha import _counts_block
    try:
        return _counts_block()
    except Exception:
        return {"pending": 0, "action_items": 0}


@router.get("/summary")
async def cub_summary(request: Request):
    """Everything a Cub shows, in one poll. See the design doc's contract."""
    cub_id = (request.headers.get("X-Cub-Id") or "").strip()
    overrides: dict = {}
    if cub_id:
        try:
            client_ip = request.client.host if request.client else None
            overrides = cub_svc.record_cub_heartbeat(
                cub_id,
                name=(request.headers.get("X-Cub-Name") or "").strip() or None,
                hardware_profile=(request.headers.get("X-Cub-Profile") or "").strip() or None,
                firmware_version=(request.headers.get("X-Cub-Version") or "").strip() or None,
                ip=client_ip,
            )
        except Exception:
            overrides = {}  # a registry hiccup never blocks the poll

    merged = cub_svc.merge_cub_settings(cub_svc.global_cub_settings(settings), overrides)
    body: dict

    timers = cub_svc.timers_block(_timers())
    probes = cub_svc.probes_block(_gadget_devices())
    alerts = cub_svc.alerts_block(_alarms())
    items, ok = await _expiring_items()
    window_days = settings.expiring_soon_days

    body = {
        "v": 1,
        "generated": int(time.time()),
        "view": cub_svc.decide_view(timers, probes, merged, alerts),
        "rotation": cub_svc.rotation_blocks(merged),
        # Active protection alarms (FoodAssistant-5c61): additive, so older
        # firmware simply ignores it; [] when nothing is alarming. The
        # "alert" view this block backs ships in a later firmware release.
        "alerts": alerts,
        "expiring": cub_svc.expiring_block(items, window_days, ok=ok),
        "counts": _counts(),
        "timers": timers,
        "probes": probes,
        # Fridge/room hygrometers (FoodAssistant-q97i): additive, so older
        # firmware simply ignores it; [] whenever none are configured.
        "hygrometers": cub_svc.hygrometers_block(_hygro_devices()),
        "settings": cub_svc.settings_block(
            merged,
            units=settings.streamdeck_weather_units,
            clock_24h=settings.clock_format == "24",
        ),
    }
    # The BLE relay allowlist (FoodAssistant-nn3u): what this Cub should
    # listen for and forward. Additive and absent unless the relay is on, so
    # a Cub only ever spends radio time on it when the server asked.
    relay = cub_svc.ble_relay_block(settings)
    if relay:
        body["ble_relay"] = relay
    return body


# -- POST /cub/ble-adv (the BLE advertisement relay) ---------------------------


@router.post("/ble-adv")
async def cub_ble_adv(request: Request):
    """Take a batch of BLE advertisements a Cub heard and decode them here.

    This is what gives a server with no Bluetooth radio the sensors in the
    kitchen: the Cub is only a radio, and every reading it forwards lands in
    the ordinary gadgets ingest, tagged with the Cub it came through, so the
    cards, the "via" line, and the fridge alarms all work exactly as they do
    for a reader on the machine itself.

    The body is {packets: [{mac, rssi, adv}]} with adv as hex. It arrives from
    a device on the LAN, so nothing here trusts it: the batch is capped, each
    packet is validated, and anything malformed is a counted drop. The
    endpoint answers calmly whatever it is sent and never 500s.
    """
    from ..services import gadgets, gadgets_buttons

    if not cub_svc.ble_relay_enabled(settings):
        # Off, or on but with no decoders on this install: either way there is
        # nothing useful to do with the packets, and saying so plainly beats
        # accepting them into a void.
        return JSONResponse(
            {"ok": False, "reason": "relay_off",
             "decoders": cub_svc.ble_relay_available()},
            status_code=403)

    cub_id = (request.headers.get("X-Cub-Id") or "").strip()
    if not cub_svc.relay_rate_ok(cub_id):
        return JSONResponse({"ok": False, "reason": "rate_limited"},
                            status_code=429)

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - a bad body is the client's problem
        return JSONResponse({"ok": False, "reason": "bad_json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "reason": "bad_body"}, status_code=400)
    packets = payload.get("packets")
    if not isinstance(packets, list):
        return JSONResponse({"ok": False, "reason": "bad_packets"}, status_code=400)
    if len(packets) > cub_svc.BLE_RELAY_MAX_PACKETS:
        return JSONResponse(
            {"ok": False, "reason": "too_many_packets",
             "max": cub_svc.BLE_RELAY_MAX_PACKETS},
            status_code=400)

    # "via Cub Kitchen" is what a card should say, so the tag is the Cub's
    # name when it sent one and its id otherwise.
    name = (request.headers.get("X-Cub-Name") or "").strip()
    source = f"Cub {name or cub_id or 'unknown'}"[:60]

    try:
        result = cub_svc.decode_packets(packets, source)
    except Exception:  # noqa: BLE001 - decoding never takes the endpoint down
        return JSONResponse({"ok": False, "reason": "decode_failed"},
                            status_code=400)

    events = 0
    stored = result["payload"]
    if stored:
        try:
            gadgets.ingest(stored, mark_reader=False)
            buttons = await gadgets_buttons.handle_payload(stored)
            events = buttons.get("events", 0)
        except Exception:  # noqa: BLE001 - a storage hiccup is not a 500 here
            return JSONResponse({"ok": False, "reason": "ingest_failed"},
                                status_code=400)

    return {
        "ok": True,
        "accepted": result["accepted"],
        "dropped": result["dropped"],
        "matched": result["matched"],
        "readings": len(stored.get("devices", [])) if stored else 0,
        "events": events,
    }


# -- firmware source (the Bandit Cubs flasher page, /ui/cubs) --------------------
# The page flashes over Web Serial with ESP Web Tools, which requires the
# manifest and the firmware parts to be same-origin (a cross-origin part is
# refused). So the server both serves the manifest and proxies the binary from
# the public repo's GitHub release, caching it under data_dir so a repeat flash
# is local and offline-friendly. The proxy pins the upstream to the known GitHub
# release host (URL built from constants, host-validated on the API-discovered
# fallback), so there is no user-controlled URL and no SSRF surface.

# GitHub serves release assets from github.com, which 302-redirects to its
# object store; both are allowed redirect targets for the pinned fetch.
_FIRMWARE_HOSTS = {
    "github.com", "objects.githubusercontent.com",
    "release-assets.githubusercontent.com", "codeload.github.com",
}


def _is_github_asset_url(url: str) -> bool:
    """True only for a download URL on a known GitHub host, so the
    API-discovered fallback below can never be pointed elsewhere."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host in _FIRMWARE_HOSTS or host.endswith(".githubusercontent.com")


async def _fetch_release_firmware(profile: str, version: str):
    """Fetch a profile's factory image from the public repo's GitHub releases.

    Primary: the pinned asset on the release for the current app version.
    Fallback: the newest release that actually carries a matching asset (so a
    fresh checkout whose version has no firmware yet still flashes the latest
    published image). Returns (bytes, None) or (None, reason). Never raises.
    """
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        try:
            r = await client.get(cub_svc.release_download_url(GITHUB_REPO, version, profile))
            if r.status_code == 200 and r.content:
                return r.content, None
        except Exception:
            pass
        try:
            rr = await client.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/releases",
                headers={"Accept": "application/vnd.github+json"},
                params={"per_page": 30})
            if rr.status_code == 200:
                for rel in rr.json():
                    for asset in rel.get("assets", []) or []:
                        name = str(asset.get("name", ""))
                        if (name.startswith(f"bandit-cub-{profile}-")
                                and name.endswith(".factory.bin")):
                            dl = str(asset.get("browser_download_url", ""))
                            if not _is_github_asset_url(dl):
                                continue
                            ra = await client.get(dl)
                            if ra.status_code == 200 and ra.content:
                                return ra.content, None
        except Exception:
            pass
    return None, "no_release_asset"


async def _release_asset_available(profile: str, version: str) -> bool:
    """A cheap check for whether a release image can be fetched, without pulling
    the whole binary (a HEAD on the pinned asset). Best-effort: any trouble
    reads as 'not published', which the page renders honestly."""
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            r = await client.head(cub_svc.release_download_url(GITHUB_REPO, version, profile))
            return r.status_code in (200, 206)
    except Exception:
        return False


async def _firmware_bytes(profile: str) -> bytes | None:
    """A profile's factory image: a locally built override, the on-disk cache,
    or a fresh fetch from the public repo's release (cached for next time).
    None when nothing is published yet. Never raises."""
    try:
        local = cub_svc.local_override_path(settings.data_dir, profile)
        if local.exists():
            return local.read_bytes()
        cached = cub_svc.cached_firmware_path(settings.data_dir, profile, APP_VERSION)
        if cached.exists():
            return cached.read_bytes()
    except Exception:
        return None
    data, _reason = await _fetch_release_firmware(profile, APP_VERSION)
    if data is None:
        return None
    try:
        cached.parent.mkdir(parents=True, exist_ok=True)
        tmp = cached.with_name(cached.name + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, cached)
    except Exception:
        pass  # a read-only data dir just means the next call fetches again
    return data


@router.get("/firmware/manifest.json")
async def cub_firmware_manifest(profile: str = ""):
    """The firmware manifest for one profile: what the browser flasher reads to
    flash a new Cub, and what a Cub in the kitchen reads to update itself. The
    part path is same-origin-relative to this URL, so ESP Web Tools fetches it
    back through the .bin proxy below.

    The ota block needs the image itself, to hash it, so the first call for a
    version pulls the release asset and caches it. Nothing to hash means the
    manifest goes out without the block rather than failing: the flasher still
    works, and a Cub tries again at its next check."""
    if cub_svc.CUB_PROFILES.get(profile) is None:
        return JSONResponse({"detail": "Unknown Cub hardware profile."},
                            status_code=404)
    ota = cub_svc.firmware_ota_block(profile, await _firmware_bytes(profile))
    manifest = cub_svc.firmware_manifest(profile, APP_VERSION, ota=ota)
    return JSONResponse(manifest, headers={"Cache-Control": "no-store"})


@router.get("/firmware/{profile}.ota.bin")
async def cub_firmware_ota_bin(profile: str):
    """The app image a Cub flashes over the air: the factory image above with
    the bootloader and partition table trimmed off, which is the only part an
    update replaces. Declared before the factory route below because both
    match this path and the first one declared wins."""
    if profile not in cub_svc.CUB_PROFILES:
        return JSONResponse({"detail": "Unknown Cub hardware profile."},
                            status_code=404)
    app = cub_svc.ota_image_from_factory(await _firmware_bytes(profile))
    if app is None:
        return JSONResponse(
            {"detail": "Firmware for this board has not been published yet.",
             "profile": profile, "version": APP_VERSION},
            status_code=404)
    return Response(content=app, media_type="application/octet-stream")


@router.get("/firmware/status")
async def cub_firmware_status():
    """Per-profile flashing readiness for the page: whether an image exists to
    flash (a local override, a cached download, or a published release asset).
    Lets the page show a calm 'not published yet' note instead of a button that
    fails when no firmware is available."""
    out = {}
    for profile, meta in cub_svc.CUB_PROFILES.items():
        info = {
            "label": meta["label"],
            "board": meta["board"],
            "chip_family": meta["chip_family"],
            "asset": cub_svc.firmware_asset_name(profile, APP_VERSION),
            "esptool": cub_svc.esptool_command(profile, APP_VERSION),
        }
        local = cub_svc.local_override_path(settings.data_dir, profile)
        cached = cub_svc.cached_firmware_path(settings.data_dir, profile, APP_VERSION)
        if local.exists() or cached.exists():
            info["available"] = True
        else:
            info["available"] = await _release_asset_available(profile, APP_VERSION)
        out[profile] = info
    return {"version": APP_VERSION, "profiles": out}


@router.get("/firmware/{profile}.bin")
async def cub_firmware_bin(profile: str):
    """Stream a profile's factory image. A locally built override wins, then the
    on-disk cache, then a fresh fetch from the public repo's release (cached for
    next time). A calm 404 (with a JSON reason) when nothing is published yet."""
    if profile not in cub_svc.CUB_PROFILES:
        return JSONResponse({"detail": "Unknown Cub hardware profile."},
                            status_code=404)
    name = cub_svc.firmware_asset_name(profile, APP_VERSION)
    disp = {"Content-Disposition": f'attachment; filename="{name}"'}

    local = cub_svc.local_override_path(settings.data_dir, profile)
    if local.exists():
        return FileResponse(local, media_type="application/octet-stream",
                            filename=name)
    cached = cub_svc.cached_firmware_path(settings.data_dir, profile, APP_VERSION)
    if cached.exists():
        return FileResponse(cached, media_type="application/octet-stream",
                            filename=name)

    data, reason = await _fetch_release_firmware(profile, APP_VERSION)
    if data is None:
        return JSONResponse(
            {"detail": "Firmware for this board has not been published yet.",
             "profile": profile, "version": APP_VERSION, "reason": reason},
            status_code=404)
    # Cache atomically for offline repeat flashes; a read-only data dir just
    # means the next flash fetches again.
    try:
        cached.parent.mkdir(parents=True, exist_ok=True)
        tmp = cached.with_name(cached.name + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, cached)
    except Exception:
        pass
    return Response(content=data, media_type="application/octet-stream",
                    headers=disp)


# -- registry management (Devices pane) -----------------------------------------


class CubEditBody(BaseModel):
    name: str | None = None
    overrides: dict | None = None


@router.get("/devices")
def list_cub_devices():
    return {"devices": cub_svc.list_cubs()}


@router.post("/devices/{device_id}")
def edit_cub_device(device_id: str, body: CubEditBody):
    """Rename a Cub and/or replace its per-device overrides. Only the fields
    present in the request are applied, so a rename never touches overrides
    and vice versa."""
    provided = body.model_dump(exclude_unset=True)
    ok = True
    if "name" in provided:
        ok = cub_svc.rename_cub(device_id, provided["name"] or "") and ok
    if "overrides" in provided:
        ok = cub_svc.set_cub_overrides(device_id, provided["overrides"] or {}) and ok
    if not ok:
        return {"ok": False, "error": "Unknown device."}
    return {"ok": True}


@router.delete("/devices/{device_id}")
def delete_cub_device(device_id: str):
    if not cub_svc.forget_cub(device_id):
        return {"ok": False, "error": "Unknown device."}
    return {"ok": True}
