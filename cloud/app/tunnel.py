"""Pure allocation helpers for the WireGuard remote-access tunnel.

IP addresses and subdomains are handed out from here, kept free of the
database and network so the tricky parts (finding the next free host,
sanitizing an arbitrary hostname into a safe subdomain, breaking collisions)
unit-test on their own. The router in routers/tunnel.py supplies the
"existing" sets from the database and persists what these return.
"""
from __future__ import annotations

import base64
import binascii
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

# A WireGuard public key is 32 bytes in standard base64: 43 characters from
# the base64 alphabet plus one "=" of padding, 44 in all.
_WG_KEY_RE = re.compile(r"^[A-Za-z0-9+/]{43}=$")

# The ports a kitchen's app may sit behind on its end of the tunnel.
MIN_APP_PORT = 1
MAX_APP_PORT = 65535


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


def valid_wireguard_key(key: str) -> bool:
    """Whether ``key`` is shaped like a WireGuard public key: 44 characters of
    standard base64 ending in "=", decoding to exactly 32 bytes. Anything else
    is refused before it reaches the VPS, where it would end up in wg and in
    the agent's state file."""
    if not isinstance(key, str) or not _WG_KEY_RE.fullmatch(key):
        return False
    try:
        return len(base64.b64decode(key, validate=True)) == 32
    except (binascii.Error, ValueError):
        return False


def valid_app_port(port) -> bool:
    """Whether ``port`` is a usable TCP port number (1 to 65535)."""
    return (isinstance(port, int) and not isinstance(port, bool)
            and MIN_APP_PORT <= port <= MAX_APP_PORT)



# Names a kitchen may never claim. They read as Forager's own pages (a
# "login" or "billing" address on the Forager domain is a ready-made phishing
# page) or are names the service may need for itself later.
RESERVED_SUBDOMAINS = frozenset({
    "www", "login", "logout", "signin", "sign-in", "signup", "sign-up",
    "register", "auth", "oauth", "sso", "account", "accounts", "my",
    "billing", "pay", "payment", "payments", "checkout", "invoice",
    "invoices", "support", "help", "helpdesk", "contact", "admin",
    "administrator", "root", "staff", "moderator", "api", "app", "apps",
    "mail", "email", "smtp", "imap", "pop", "pop3", "mx", "webmail",
    "status", "forager", "pantryraider", "pantry-raider", "docs", "doc",
    "demo", "portal", "dashboard", "console", "secure", "security",
    "verify", "password", "reset", "static", "assets", "cdn", "media",
    "img", "images", "blog", "news", "shop", "store", "dev", "staging",
    "stage", "test", "beta", "preview", "ns", "ns1", "ns2", "dns", "vpn",
    "wg", "tunnel", "proxy", "gateway", "health", "internal", "official",
    "team", "legal", "privacy", "terms", "abuse", "postmaster",
    "hostmaster", "webmaster", "noreply", "no-reply",
})


def is_reserved_subdomain(label: str) -> bool:
    return (label or "").strip().lower() in RESERVED_SUBDOMAINS


def claimable_base(base: str) -> str:
    """A sanitized label made safe to hand out: a reserved name gets
    "-kitchen" added ("app" becomes "app-kitchen"), so a hostname that
    happens to match one still yields a readable address of its own."""
    if not is_reserved_subdomain(base):
        return base
    return sanitize_subdomain(f"{base[:MAX_SUBDOMAIN_LENGTH - 8]}-kitchen")
