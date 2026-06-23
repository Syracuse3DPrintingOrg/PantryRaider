"""Server-side LAN scan to discover FoodAssistant instances.

A satellite normally registers itself by dialing out (see services/devices.py),
but a freshly imaged device, or a second server, may not have done so yet. This
gives the admin a one-shot active scan: probe a CIDR for open ports, then ask
each open host for /health and keep the ones that fingerprint as FoodAssistant.

Stdlib TCP connect for the cheap liveness check, httpx for the fingerprint. The
per-host probe is factored out so tests can monkeypatch it without any network.
"""
from __future__ import annotations

import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor

import httpx

# Default ports an instance answers on: the app's own port, plus 80 for setups
# behind a reverse proxy.
DEFAULT_PORTS = [9284, 80]

# Refuse anything larger than a /22 (1024 hosts). A bigger sweep would be slow
# and is almost never what a home user wants.
MAX_HOSTS = 1024


def _probe_host(ip: str, ports: list[int], timeout: float) -> dict | None:
    """Probe one host: TCP connect each port, fingerprint the first that opens.

    Returns a result dict when the host answers /health as FoodAssistant, else
    None. Swallows every per-host error so one dead host never aborts the sweep.
    """
    for port in ports:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                if sock.connect_ex((ip, port)) != 0:
                    continue
        except OSError:
            continue
        try:
            resp = httpx.get(f"http://{ip}:{port}/health", timeout=timeout)
            data = resp.json()
        except Exception:
            continue
        if isinstance(data, dict) and data.get("app") == "foodassistant":
            return {
                "ip": ip,
                "port": port,
                "version": data.get("version"),
                "mode": data.get("mode"),
                "status": data.get("status"),
            }
    return None


def scan_for_instances(cidr: str, ports: list[int] | None = None,
                       timeout: float = 0.4, concurrency: int = 64,
                       exclude: set[str] | None = None) -> list[dict]:
    """Scan a CIDR for FoodAssistant instances.

    Returns a list of result dicts (one per instance found). On a malformed or
    too-large CIDR returns a single-element list carrying an "error" key, so the
    caller can surface it without a separate exception path.

    The host running the scan answers its own /health fingerprint, so by default
    we drop this machine's own address from the sweep; callers can override the
    excluded set (pass an empty set to scan everything, including self).
    """
    ports = ports or list(DEFAULT_PORTS)
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError as exc:
        return [{"error": f"invalid network: {exc}"}]
    if net.num_addresses > MAX_HOSTS:
        return [{"error": f"network too large (max {MAX_HOSTS} hosts); use a /22 or smaller"}]

    if exclude is None:
        exclude = _local_ips()
    hosts = [str(h) for h in net.hosts() if str(h) not in exclude]
    found: list[dict] = []

    def _safe_probe(ip: str) -> dict | None:
        try:
            return _probe_host(ip, ports, timeout)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for result in pool.map(_safe_probe, hosts):
            if result:
                found.append(result)
    return found


def _outbound_ip() -> str | None:
    """This host's outbound interface address, or None if it cannot be found.

    Opening a UDP socket toward a public address does not send anything but lets
    the OS pick the outbound interface, whose address we read back.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return None


def _local_ips() -> set[str]:
    """Addresses that resolve to this host, so a scan can skip itself.

    Covers loopback plus the primary outbound interface. Resolving the hostname
    catches additional bound addresses where the platform supports it.
    """
    ips = {"127.0.0.1"}
    out = _outbound_ip()
    if out:
        ips.add(out)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return ips


def default_cidr() -> str | None:
    """Best-effort guess of this server's own /24, or None if it cannot be found."""
    ip = _outbound_ip()
    if not ip:
        return None
    try:
        net = ipaddress.ip_network(f"{ip}/24", strict=False)
    except ValueError:
        return None
    return str(net)
