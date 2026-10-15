"""Server-side LAN scan to discover Pantry Raider instances.

A satellite normally registers itself by dialing out (see services/devices.py),
but a freshly imaged device, or a second server, may not have done so yet. This
gives the admin a one-shot active scan: probe a CIDR for open ports, then ask
each open host for /health and keep the ones that fingerprint as Pantry Raider.

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

    Returns a result dict when the host answers /health as Pantry Raider, else
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
                "device_id": data.get("device_id"),
            }
    return None


def scan_for_instances(cidr: str, ports: list[int] | None = None,
                       timeout: float = 0.4, concurrency: int = 64,
                       exclude: set[str] | None = None) -> list[dict]:
    """Scan a CIDR for Pantry Raider instances.

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


def _rank_ip(ip: str) -> int:
    """Lower rank = more likely to be a real home/office LAN. Docker's default
    bridge lives in 172.16/12, so that range is ranked last so a containerized
    server prefers a real LAN interface when it has one (Pantry Raider)."""
    if ip.startswith("192.168."):
        return 0
    if ip.startswith("10."):
        return 1
    if ip.startswith("172."):
        return 3  # Docker bridge range: least likely the user's LAN
    return 2


def looks_dockerish(cidr: str) -> bool:
    """True when a CIDR is in Docker's default bridge range (172.16/12), so the
    UI can hint that the user should enter their real LAN range instead."""
    try:
        net = ipaddress.ip_network(cidr, strict=False)
        return net.subnet_of(ipaddress.ip_network("172.16.0.0/12"))
    except (ValueError, TypeError):
        return False


# The private-use blocks a LAN scan is allowed to sweep. Anything outside these
# is a public (or otherwise off-limits) target: the scan and probe exist to find
# devices on the user's own network, never to become an internet port scanner
# (FoodAssistant-tfrm).
_PRIVATE_SCAN_BLOCKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def is_private_cidr(cidr: str) -> bool:
    """True only when every address in ``cidr`` sits in an RFC 1918 LAN block.

    A scan target must be fully inside 10/8, 172.16/12, or 192.168/16. Loopback,
    link-local, carrier-grade NAT, and every public range are refused, so an
    admin (or a misled admin session) cannot turn the LAN scanner into an
    arbitrary internal or internet port scanner. Pure.
    """
    try:
        net = ipaddress.ip_network((cidr or "").strip(), strict=False)
    except (ValueError, TypeError):
        return False
    return any(net.subnet_of(block) for block in _PRIVATE_SCAN_BLOCKS)


def default_cidr() -> str | None:
    """Best-effort guess of this host's own LAN /24, or None if none is found.

    Prefers a real LAN interface (192.168/10) over a Docker bridge address, so a
    containerized server does not default to scanning its 172.x Docker network.
    A bridge-only container still only sees its Docker IP; the caller can pass an
    explicit CIDR to scan the LAN through the host in that case.
    """
    cands = {ip for ip in _local_ips() if not ip.startswith("127.")}
    if not cands:
        return None
    ip = sorted(cands, key=lambda x: (_rank_ip(x), x))[0]
    try:
        return str(ipaddress.ip_network(f"{ip}/24", strict=False))
    except ValueError:
        return None


def lan_cidr_from_config_urls() -> str | None:
    """Derive a LAN /24 from a configured backend URL (Grocy, Mealie). The user
    points those at a real LAN IP and the container reaches them there, so they
    are a reliable LAN reference even on a bridge container that cannot see its
    own LAN address. Shared by the device scan and the camera scan."""
    from ..config import settings
    from urllib.parse import urlparse
    for url in (settings.grocy_base_url, settings.mealie_base_url,
                settings.grocy_public_url, settings.mealie_public_url):
        host = urlparse(url or "").hostname or ""
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            continue  # a hostname, not a literal IP
        if ip.is_loopback or not ip.is_private:
            continue
        net = str(ipaddress.ip_network(f"{host}/24", strict=False))
        if not looks_dockerish(net):
            return net
    return None


def resolve_lan_cidr(explicit: str = "", candidates=None) -> str | None:
    """Pick the network to scan, shared by the device and camera scans: the
    user's explicit range, else a remembered one, any caller-supplied candidates
    (e.g. a checked-in satellite's subnet), a real LAN interface, and finally a
    configured backend URL host. Docker subnets are skipped so a bridge container
    never defaults to scanning its own network. Returns None when nothing fits.
    """
    explicit = (explicit or "").strip()
    if explicit:
        # Only a private-LAN range is ever scanned. A public (or otherwise
        # off-limits) explicit CIDR is refused rather than swept, so this never
        # becomes an internet port scanner (FoodAssistant-tfrm).
        return explicit if is_private_cidr(explicit) else None
    from ..config import settings
    seq = [settings.lan_scan_cidr]
    if candidates:
        seq += list(candidates)
    seq += [default_cidr(), lan_cidr_from_config_urls()]
    for c in seq:
        # Auto-detected candidates must also be private (a config URL could point
        # anywhere); Docker bridge ranges are skipped as before.
        if c and is_private_cidr(c) and not looks_dockerish(c):
            return c
    return None
