"""What counts as this home's own network.

The access rules (services/access.py) treat a request from the home network
differently from one that came from the internet. The fixed private ranges
cover most homes; this module adds the networks this device is directly
attached to (read from /proc, so a home whose LAN uses global IPv6 addresses
still counts, while a server with a public address of its own keeps only its
private networks), the owner's extra ranges from the lan_cidrs setting, and
the extra host names from home_host_names.

Everything here is standard library only, cached briefly, and never raises:
a missing /proc (a test box, a non-Linux host) just means "no extra
networks".
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import threading
import time

_CACHE_SECONDS = 60.0

# Interfaces that are never "home": VPN and tunnel devices reach other
# networks, so being attached to one says nothing about where a peer is.
_SKIP_IFACE_PREFIXES = ("lo", "wg", "tun", "tap", "tailscale", "zt", "utun")

# The address foodassistant-ap-watchdog gives wlan0 while the recovery hotspot
# is up, and the network phones join through it.
RECOVERY_HOTSPOT_NET = ipaddress.ip_network("192.168.99.0/24")
_RECOVERY_HOTSPOT_ADDR = "192.168.99.1"

_MAX_CIDRS = 32
_MAX_HOST_NAMES = 16
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

_lock = threading.Lock()
_cache: dict = {}


def _cached(key: str, compute):
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < _CACHE_SECONDS:
            return hit[1]
    try:
        value = compute()
    except Exception:
        value = None
    with _lock:
        _cache[key] = (now, value)
    return value


def clear_cache() -> None:
    """Forget every cached answer (tests, and after a network change)."""
    with _lock:
        _cache.clear()


def _skip_iface(name: str) -> bool:
    return any(name == p or name.startswith(p) for p in _SKIP_IFACE_PREFIXES)


def _hex_le_ipv4(value: str) -> ipaddress.IPv4Address:
    """An IPv4 address written as little-endian hex, the way /proc/net/route does."""
    raw = int(value, 16).to_bytes(4, "little")
    return ipaddress.IPv4Address(raw)


def _read_onlink(proc_root: str) -> list:
    nets: list = []
    # IPv4: directly attached routes (no gateway, a real mask, route up).
    try:
        with open(os.path.join(proc_root, "net", "route")) as fh:
            lines = fh.read().splitlines()[1:]
    except OSError:
        lines = []
    for line in lines:
        parts = line.split()
        if len(parts) < 8:
            continue
        iface, dest, gateway, flags, mask = parts[0], parts[1], parts[2], parts[3], parts[7]
        try:
            if _skip_iface(iface) or gateway != "00000000" or mask == "00000000":
                continue
            if not int(flags, 16) & 0x1:
                continue
            net = ipaddress.IPv4Network(
                f"{_hex_le_ipv4(dest)}/{_hex_le_ipv4(mask)}", strict=False)
        except (ValueError, TypeError):
            continue
        if net.prefixlen < 8:
            continue
        if net not in nets:
            nets.append(net)
    # IPv6: global-scope addresses of this device and their prefixes.
    try:
        with open(os.path.join(proc_root, "net", "if_inet6")) as fh:
            lines6 = fh.read().splitlines()
    except OSError:
        lines6 = []
    for line in lines6:
        parts = line.split()
        if len(parts) < 6:
            continue
        addr_hex, plen_hex, scope_hex, iface = parts[0], parts[2], parts[3], parts[5]
        try:
            if _skip_iface(iface) or int(scope_hex, 16) != 0:
                continue
            plen = int(plen_hex, 16)
            if plen < 32 or len(addr_hex) != 32:
                continue
            addr = ipaddress.IPv6Address(bytes.fromhex(addr_hex))
            net = ipaddress.IPv6Network(f"{addr}/{plen}", strict=False)
        except (ValueError, TypeError):
            continue
        if net not in nets:
            nets.append(net)
    # A device with a public IPv4 network of its own (a rented server, or one
    # plugged straight into the modem) is attached to its provider's network,
    # where the neighbours are strangers; there only the private ranges count.
    # Behind a home router every IPv4 network is private, and the home's
    # global IPv6 network still counts.
    if any(net.version == 4 and not _private_range(net) for net in nets):
        nets = [net for net in nets if _private_range(net)]
    return nets


# Ranges a home network uses, for telling a home from a server with a public
# address (100.64.0.0/10 included: carrier-grade NAT and Tailscale addresses
# are not public ones).
_PRIVATE_RANGES = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10",
    "fc00::/7", "fe80::/10",
))


def _private_range(net) -> bool:
    return any(net.version == r.version and net.subnet_of(r) for r in _PRIVATE_RANGES)


def onlink_networks(proc_root: str = "/proc") -> list:
    """Networks this device is directly attached to, VPN interfaces skipped.

    IPv4 networks shorter than /8 and IPv6 shorter than /32 are left out (no
    home is that big). On a device with a public IPv4 network of its own only
    the private ranges are kept. Cached for a minute; any error gives []."""
    value = _cached(f"onlink:{proc_root}", lambda: _read_onlink(proc_root))
    return list(value or [])


def _read_gateways(proc_root: str) -> set:
    out: set = set()
    try:
        with open(os.path.join(proc_root, "net", "route")) as fh:
            lines = fh.read().splitlines()[1:]
    except OSError:
        return out
    for line in lines:
        parts = line.split()
        if len(parts) < 8:
            continue
        try:
            if parts[1] != "00000000" or parts[7] != "00000000":
                continue
            if parts[2] == "00000000":
                continue
            out.add(str(_hex_le_ipv4(parts[2])))
        except (ValueError, TypeError):
            continue
    return out


def default_gateways(proc_root: str = "/proc") -> set:
    """IPv4 default gateways of this device (cached for a minute, errors give set())."""
    value = _cached(f"gw:{proc_root}", lambda: _read_gateways(proc_root))
    return set(value or ())


_DOCKER_NET = ipaddress.ip_network("172.16.0.0/12")


def in_docker_bridge(proc_root: str = "/proc", dockerenv: str = "/.dockerenv") -> bool:
    """True inside a Docker container on a bridge network (172.16.0.0/12 gateway)."""
    try:
        if not os.path.exists(dockerenv):
            return False
        for gw in default_gateways(proc_root):
            try:
                if ipaddress.ip_address(gw) in _DOCKER_NET:
                    return True
            except ValueError:
                continue
    except Exception:
        return False
    return False


def _split(text) -> list[str]:
    if not isinstance(text, str):
        return []
    return [p for p in re.split(r"[\s,]+", text) if p]


def parse_cidrs(text) -> tuple[list, list[str]]:
    """Parse a lenient list of networks: (networks, rejected entries).

    Commas and whitespace separate entries. IPv4 networks shorter than /8,
    IPv6 shorter than /16, anything unparsable and anything past the 32nd
    entry are rejected."""
    nets: list = []
    rejected: list[str] = []
    for entry in _split(text):
        if len(nets) >= _MAX_CIDRS:
            rejected.append(entry)
            continue
        try:
            net = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            rejected.append(entry)
            continue
        if (net.version == 4 and net.prefixlen < 8) or (net.version == 6 and net.prefixlen < 16):
            rejected.append(entry)
            continue
        if net not in nets:
            nets.append(net)
    return nets, rejected


def valid_host_name(name: str) -> bool:
    """A host name of dot-separated labels (1 to 63 of a-z, 0-9, inner hyphens)."""
    if not name or len(name) > 253:
        return False
    return all(_LABEL_RE.match(label) for label in name.split("."))


def parse_host_names(text) -> tuple[list[str], list[str]]:
    """Parse a lenient list of host names: (names, rejected entries).

    Lowercased, one trailing dot stripped, at most 16 names."""
    names: list[str] = []
    rejected: list[str] = []
    for entry in _split(text):
        name = entry.lower()
        if name.endswith("."):
            name = name[:-1]
        if len(names) >= _MAX_HOST_NAMES or not valid_host_name(name):
            rejected.append(entry)
            continue
        if name not in names:
            names.append(name)
    return names, rejected


def _hotspot_bound() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((_RECOVERY_HOTSPOT_ADDR, 0))
    return True


def recovery_hotspot_up() -> bool:
    """True while this device holds 192.168.99.1, the recovery hotspot's address.

    On a household LAN that happens to use 192.168.99.0/24 the router holds
    .1, so the bind fails and this stays False. Cached for a minute; any
    error gives False."""
    return bool(_cached("hotspot", _hotspot_bound))
