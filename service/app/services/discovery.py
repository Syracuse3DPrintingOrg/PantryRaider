"""mDNS advertisement for the Home Assistant integration (FoodAssistant-ju93
follow-up).

Advertises this install as a ``_pantry-raider._tcp.local.`` mDNS service so a
Home Assistant instance on the LAN can discover it (base URL plus a little
identity) instead of the owner typing an IP into the integration's config
flow by hand.

Honest limitation: a Docker BRIDGE-networked server container only sees its
own container network, so mDNS packets never reach the LAN and this
advertisement is invisible to HA there. That's an accepted gap, not a bug to
chase: the two audiences that matter most, a host-networked pi_hosted
appliance and a bare pi_remote venv install, both share the host's real
network interfaces and advertise correctly. A bridge-networked server still
has the manual base_url entry in the integration's config flow.
"""
from __future__ import annotations

import logging
import os
import socket
import sys

logger = logging.getLogger(__name__)

SERVICE_TYPE = "_pantry-raider._tcp.local."


def _argv_port(argv) -> int | None:
    """The --port uvicorn was started with, read from its command line."""
    args = list(argv or [])
    for i, arg in enumerate(args):
        value = None
        if arg == "--port" and i + 1 < len(args):
            value = args[i + 1]
        elif arg.startswith("--port="):
            value = arg.split("=", 1)[1]
        if value is not None:
            try:
                return int(value)
            except ValueError:
                return None
    return None


def resolve_port(mode: str, argv=None) -> int:
    """The app's real listen port for this deployment mode.

    The announcement pairs this port with this process's own interface
    address, so it must be the port the process itself listens on, not a
    port Docker publishes on the host: an install in a container is reached
    at its container address on the container port (by Home Assistant on the
    same Docker network, or on a macvlan network where the container has a
    LAN address of its own).

      * ``PORT`` or ``UVICORN_PORT``, if set, always wins.
      * Then the ``--port`` uvicorn was started with (every launch path
        passes one).
      * Otherwise: a pi_remote venv install binds 80 directly; every other
        mode guesses 9284.

    A wrong guess only means the mDNS entry advertises an unreachable port;
    it never affects the rest of the app.
    """
    for var in ("PORT", "UVICORN_PORT"):
        v = os.environ.get(var)
        if v:
            try:
                return int(v)
            except ValueError:
                pass
    port = _argv_port(sys.argv if argv is None else argv)
    if port:
        return port
    return 80 if mode == "pi_remote" else 9284


def build_service_info(hostname: str, mode: str, version: str, device_id: str,
                        port: int | None = None) -> dict:
    """Pure builder for the mDNS service-info fields (name, port, TXT
    properties), kept separate from the real zeroconf.ServiceInfo
    construction so the shape can be unit tested without touching a socket."""
    port = port if port is not None else resolve_port(mode)
    safe_host = (hostname or "pantryraider").strip() or "pantryraider"
    return {
        "type_": SERVICE_TYPE,
        "name": f"Pantry Raider {safe_host}.{SERVICE_TYPE}",
        "port": port,
        # The mDNS server (host) record. Lives HERE, in the unit-tested
        # builder, because reading it from the wrong place in start() once
        # threw a KeyError that the fail-soft handler swallowed, so the
        # advertisement silently never registered (found live on the Bandit).
        "server": f"{safe_host}.local.",
        "properties": {
            "device_id": device_id or "",
            "mode": mode or "server",
            "version": version or "",
            "hostname": safe_host,
        },
    }


def _local_ipv4() -> str:
    """Best-effort local LAN IPv4 address via the UDP-connect trick (no
    packet actually leaves the host). Falls back to loopback, which still
    lets registration succeed even though the service would not be reachable
    from another host in that case."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# Module-level handles so start()/stop() can be called once each from the app
# lifespan without the caller having to thread an object through app.state.
_azc = None
_info = None


async def start(hostname: str, mode: str, version: str, device_id: str) -> None:
    """Register the mDNS service. Every failure mode here (the zeroconf
    package not installed, no usable network interface, a bridge-networked
    container that cannot reach the LAN) is caught and logged, never raised,
    so a broken mDNS environment can never keep the app from starting."""
    global _azc, _info
    try:
        from zeroconf import ServiceInfo
        from zeroconf.asyncio import AsyncZeroconf
    except Exception:
        logger.info("discovery: zeroconf not installed, skipping mDNS advertisement")
        return
    try:
        spec = build_service_info(hostname, mode, version, device_id)
        info = ServiceInfo(
            spec["type_"],
            spec["name"],
            addresses=[socket.inet_aton(_local_ipv4())],
            port=spec["port"],
            properties=spec["properties"],
            server=spec["server"],
        )
        azc = AsyncZeroconf()
        await azc.async_register_service(info)
        _azc, _info = azc, info
    except Exception:
        logger.info("discovery: mDNS advertisement failed to start (non-fatal)",
                    exc_info=True)


async def stop() -> None:
    """Unregister and close, if start() actually managed to register."""
    global _azc, _info
    if _azc is None:
        return
    azc, info = _azc, _info
    _azc, _info = None, None
    try:
        if info is not None:
            await azc.async_unregister_service(info)
        await azc.async_close()
    except Exception:
        pass
