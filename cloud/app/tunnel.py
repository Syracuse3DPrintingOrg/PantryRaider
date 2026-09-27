"""Pure allocation helpers for the WireGuard remote-access tunnel.

IP addresses and subdomains are handed out from here, kept free of the
database and network so the tricky parts (finding the next free host,
sanitizing an arbitrary hostname into a safe subdomain, breaking collisions)
unit-test on their own. The router in routers/tunnel.py supplies the
"existing" sets from the database and persists what these return.
"""
from __future__ import annotations

import ipaddress
import re

# The tunnel network. Every kitchen gets a /32 out of this /16. .0 is the
# network address and .1 is the VPS server tunnel IP (wg0), so host
# allocation starts at .2.
TUNNEL_CIDR = "10.99.0.0/16"
SERVER_TUNNEL_IP = "10.99.0.1"
# Host offsets that are never handed to a kitchen: the network address and
# the server itself.
_RESERVED_OFFSETS = (0, 1)

# WireGuard reads the whole /16 as reachable through wg0; a kitchen is pinned
# to a single address, so the app is told its peer is allowed only the
# server's /32.
SERVER_ALLOWED_IPS = f"{SERVER_TUNNEL_IP}/32"

# Subdomain length cap. DNS labels top out at 63 characters; a shorter cap
# keeps URLs readable and leaves room for a "-2" uniqueness suffix.
MAX_SUBDOMAIN_LENGTH = 32
_FALLBACK_SUBDOMAIN = "kitchen"


def allocate_ip(existing_ips, cidr: str = TUNNEL_CIDR) -> str:
    """The lowest free host address in ``cidr``, skipping .0 and .1.

    ``existing_ips`` is any iterable of already-assigned address strings.
    Raises RuntimeError if the pool is exhausted (a /16 holds ~65k kitchens,
    so this is a safety net, not an expected path).
    """
    net = ipaddress.ip_network(cidr, strict=False)
    taken = set()
    for ip in existing_ips:
        try:
            taken.add(ipaddress.ip_address(str(ip).strip()))
        except ValueError:
            continue
    reserved = {net.network_address + off for off in _RESERVED_OFFSETS}
    for host in net.hosts():
        if host in reserved or host in taken:
            continue
        return str(host)
    raise RuntimeError("Tunnel address pool exhausted")


def sanitize_subdomain(hint: str) -> str:
    """Turn an arbitrary hostname hint into a safe DNS subdomain label.

    Lowercases, keeps only ``[a-z0-9-]``, collapses runs of dashes, trims
    leading and trailing dashes, and caps the length. Falls back to
    "kitchen" when nothing usable survives (e.g. an empty or all-symbol
    hint), so the caller always gets a valid label to make unique.
    """
    low = (hint or "").strip().lower()
    # Anything that is not a-z, 0-9, or dash becomes a dash, so "Dan's Pi"
    # and "dan.pi" both collapse cleanly rather than dropping characters.
    cleaned = re.sub(r"[^a-z0-9-]+", "-", low)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    cleaned = cleaned[:MAX_SUBDOMAIN_LENGTH].strip("-")
    return cleaned or _FALLBACK_SUBDOMAIN


def ensure_unique_subdomain(base: str, existing) -> str:
    """A subdomain not already in ``existing``, suffixing -2, -3, ... on
    collision. ``base`` should already be sanitized. The suffix is trimmed to
    respect MAX_SUBDOMAIN_LENGTH so a long base plus "-12" never overflows.
    """
    taken = {str(s).strip().lower() for s in existing}
    if base not in taken:
        return base
    n = 2
    while True:
        suffix = f"-{n}"
        trimmed = base[:MAX_SUBDOMAIN_LENGTH - len(suffix)].strip("-")
        candidate = f"{trimmed or _FALLBACK_SUBDOMAIN}{suffix}"
        if candidate not in taken:
            return candidate
        n += 1
