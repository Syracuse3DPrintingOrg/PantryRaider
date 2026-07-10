"""Admin REST for the satellite remotes list (main server only).

These routes run on the MAIN server and back the "Satellite devices" settings
pane. They sit behind the app's normal require_auth middleware (session cookie
or X-API-Key), so they need no extra auth check of their own. They never appear
on a satellite, which owns no remotes of its own.
"""
from __future__ import annotations

from fastapi import APIRouter, Body
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..config import settings
from ..services import devices, lan_scan

router = APIRouter(prefix="/api/devices", tags=["devices"])


class ScanBody(BaseModel):
    cidr: str = ""
    ports: list[int] | None = None


class CommandBody(BaseModel):
    command: str = ""


class LabelBody(BaseModel):
    label: str = ""


def _good_cidr(c: str | None) -> bool:
    """A usable scan range: present and not a Docker bridge network."""
    return bool(c) and not lan_scan.looks_dockerish(c)


# Kept for tests/back-compat: the Grocy/Mealie URL derivation now lives in
# lan_scan so the camera scan shares it.
def _lan_cidr_from_config_urls() -> str | None:
    return lan_scan.lan_cidr_from_config_urls()


def _resolve_scan_cidr(explicit: str) -> str | None:
    """Pick the network to scan, including a checked-in satellite's subnet as an
    extra candidate. Shared resolution (remembered range, LAN interface, backend
    URL host) lives in lan_scan.resolve_lan_cidr so the camera scan matches."""
    return lan_scan.resolve_lan_cidr(
        explicit, candidates=[devices.lan_cidr_from_known_devices()])


@router.get("")
def list_remotes():
    return {"devices": devices.list_devices()}


@router.post("/scan")
async def scan_lan(body: ScanBody = Body(default=ScanBody())):
    """Sweep the LAN for Pantry Raider instances and fold them into the list.

    Uses the requested CIDR, or this server's own /24 when none is given. The
    scan blocks (sockets), so it runs in a threadpool.
    """
    cidr = _resolve_scan_cidr(body.cidr or "")
    if not cidr:
        return {"ok": False, "needs_cidr": True, "error": (
            "This server runs in a Docker network, so it cannot detect your LAN "
            "on its own, and it found no LAN reference (no Grocy/Mealie LAN URL "
            "and no satellite checked in). Enter your LAN range (for example "
            "192.168.1.0/24) and scan again. Satellites also appear here on their "
            "own once they sync, so you usually do not need to scan at all.")}
    # Remember a good (non-Docker) range so future blank scans reuse it without
    # the user retyping it.
    if _good_cidr(cidr) and cidr != settings.lan_scan_cidr:
        try:
            settings.save({"lan_scan_cidr": cidr})
        except Exception:
            pass

    results = await run_in_threadpool(lan_scan.scan_for_instances, cidr, body.ports)
    # A malformed/too-large CIDR comes back as a single error dict.
    if len(results) == 1 and "error" in results[0]:
        return {"ok": False, "error": results[0]["error"], "cidr": cidr}

    # Drop this very server (it answers its own probe through a Docker gateway)
    # and any hit on a Docker address, so the list is never polluted with the
    # container network rather than real devices.
    self_id = settings.device_id
    kept = [r for r in results
            if not (self_id and r.get("device_id") == self_id)
            and not lan_scan.looks_dockerish((r.get("ip") or "") + "/32")]
    for r in kept:
        devices.record_scan_result(
            r.get("ip"),
            version=r.get("version"),
            deployment_mode=r.get("mode"),
        )
    return {"ok": True, "found": kept, "cidr": cidr,
            "dockerish": lan_scan.looks_dockerish(cidr)}


@router.post("/{device_id}/command")
def queue_device_command(device_id: str, body: CommandBody):
    if body.command not in devices.KNOWN_COMMANDS:
        return JSONResponse({"ok": False, "error": f"unknown command: {body.command}"}, status_code=400)
    ok = devices.queue_command(device_id, body.command)
    return {"ok": ok}


@router.post("/{device_id}/label")
def label_device(device_id: str, body: LabelBody):
    return {"ok": devices.set_label(device_id, body.label)}


@router.delete("/{device_id}")
def forget(device_id: str):
    return {"ok": devices.forget_device(device_id)}
