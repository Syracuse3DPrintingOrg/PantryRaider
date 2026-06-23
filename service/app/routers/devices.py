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

from ..services import devices, lan_scan

router = APIRouter(prefix="/api/devices", tags=["devices"])


class ScanBody(BaseModel):
    cidr: str = ""
    ports: list[int] | None = None


class CommandBody(BaseModel):
    command: str = ""


class LabelBody(BaseModel):
    label: str = ""


@router.get("")
def list_remotes():
    return {"devices": devices.list_devices()}


@router.post("/scan")
async def scan_lan(body: ScanBody = Body(default=ScanBody())):
    """Sweep the LAN for FoodAssistant instances and fold them into the list.

    Uses the requested CIDR, or this server's own /24 when none is given. The
    scan blocks (sockets), so it runs in a threadpool.
    """
    cidr = (body.cidr or "").strip() or lan_scan.default_cidr()
    if not cidr:
        return {"ok": False, "error": "Could not determine a network to scan; enter a CIDR like 192.168.1.0/24."}

    results = await run_in_threadpool(lan_scan.scan_for_instances, cidr, body.ports)
    # A malformed/too-large CIDR comes back as a single error dict.
    if len(results) == 1 and "error" in results[0]:
        return {"ok": False, "error": results[0]["error"], "cidr": cidr}

    for r in results:
        devices.record_scan_result(
            r.get("ip"),
            version=r.get("version"),
            deployment_mode=r.get("mode"),
        )
    return {"ok": True, "found": results, "cidr": cidr}


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
