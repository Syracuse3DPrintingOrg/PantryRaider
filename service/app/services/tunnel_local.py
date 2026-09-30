"""In-container WireGuard backend for Forager remote access on a server.

A Pi appliance owns its WireGuard endpoint on the host through the host bridge
(only root there can create the interface). A plain server (docker-compose) has
no bridge: the app runs in a container, so WireGuard runs INSIDE that container
instead. This module is the server-side equivalent of the bridge's /tunnel/*
handlers: keygen, config render, up, down, and status, kept as pure helpers
plus thin subprocess wrappers so the tricky parts unit-test without wg present.

The container needs NET_ADMIN and /dev/net/tun for wg-quick to create the
interface. The default compose deliberately does NOT grant those, so an
ordinary install runs without them and remote access reports itself
unavailable; they come from the docker-compose.forager.yml override, which a
server adds only when it actually wants remote access. wg_available() reports
whether this host can host a tunnel at all. The private key is written to a 0600 file under
data_dir and is never logged or returned to the cloud (only the public half
leaves the device).
"""
from __future__ import annotations

import ipaddress
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from ..config import settings

# The interface name matches the bridge's, so the two backends look identical
# to the cloud and to a support bundle. wg-quick derives the config path from
# the interface name, so the config always lives at /etc/wireguard/<iface>.conf.
INTERFACE = "fa-forager"
CONFIG_PATH = f"/etc/wireguard/{INTERFACE}.conf"


def _key_path() -> Path:
    """Where the device private key is kept (0600), under data_dir so it
    survives a container recreate on the mounted data volume."""
    return Path(settings.data_dir) / "wireguard" / f"{INTERFACE}.key"


# --- Pure helpers (unit-tested; no wg, no disk) ---------------------------

# A WireGuard key is 32 bytes of base64: 43 characters plus the '=' pad.
# Matched with fullmatch throughout, since a trailing '$' would still accept a
# value ending in a newline.
_KEY_RE = re.compile(r"[A-Za-z0-9+/]{43}=")
# host:port, where the host is a name or an address literal.
_ENDPOINT_RE = re.compile(r"[A-Za-z0-9.:_-]+:\d{1,5}")


def _reject(field: str) -> ValueError:
    """The error for a parameter that failed its check.

    Names the field but never the value: the same config carries the private
    key, and this message travels into logs and error paths.
    """
    return ValueError(f"invalid WireGuard {field}")


def _one_line(value, field: str) -> str:
    """One config value, stripped, with a line break treated as an attack.

    wg-quick's config is line based, so a newline inside any value would let
    whoever supplied it append directives of its own, and PostUp runs as root.
    Rejecting CR and LF outright closes that door before any per-field check.
    """
    text = str(value or "").strip()
    if "\n" in text or "\r" in text:
        raise _reject(field)
    return text


def _checked_key(value, field: str) -> str:
    """A base64 WireGuard key, or a rejection."""
    key = _one_line(value, field)
    if not _KEY_RE.fullmatch(key):
        raise _reject(field)
    return key


def _checked_address(value) -> str:
    """The interface address: an IP literal, prefix optional.

    A bare tunnel IP is normalized to a host address (/32, or /128 on v6).
    """
    addr = _one_line(value, "address")
    host, slash, prefix = addr.partition("/")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise _reject("address") from None
    width = 32 if ip.version == 4 else 128
    if not slash:
        return f"{addr}/{width}"
    if not prefix.isdigit() or int(prefix) > width:
        raise _reject("address")
    return addr


def _checked_allowed_ips(value) -> str:
    """The routed networks: a comma-separated list of CIDRs or IP literals."""
    parts = [p.strip() for p in _one_line(value, "allowed IPs").split(",")]
    parts = [p for p in parts if p]
    if not parts:
        raise _reject("allowed IPs")
    for part in parts:
        try:
            ipaddress.ip_network(part, strict=False)
        except ValueError:
            raise _reject("allowed IPs") from None
    return ", ".join(parts)


def _checked_endpoint(value) -> str:
    """The peer endpoint: host:port."""
    endpoint = _one_line(value, "endpoint")
    if not _ENDPOINT_RE.fullmatch(endpoint):
        raise _reject("endpoint")
    if not 1 <= int(endpoint.rsplit(":", 1)[1]) <= 65535:
        raise _reject("endpoint")
    return endpoint


def render_config(private_key: str, address: str, server_public_key: str,
                  endpoint: str, allowed_ips: str, keepalive=25) -> str:
    """Render the wg-quick config text for the Forager interface. Pure.

    Every value is checked against the shape wg-quick expects before it is
    written, and a bad one raises ValueError instead of reaching the file. The
    parameters arrive from the cloud over the wire, and a wg-quick config is
    line based, so an unchecked newline would let a value carry its own PostUp
    line, which wg-quick runs as root.

    AllowedIPs is the server's /32 only (the cloud passes it), so this is a
    hub route to reach the kitchen through Forager, never a full tunnel that
    would capture all of the container's traffic; a 0.0.0.0/0 catch-all is
    never emitted. No DNS line is written: the container has no resolvconf, so
    a DNS directive would make wg-quick fail. Kept pure and off the log path so
    the private key stays put.
    """
    priv = _checked_key(private_key, "private key")
    addr = _checked_address(address)
    pub = _checked_key(server_public_key, "peer key")
    peer_endpoint = _checked_endpoint(endpoint)
    routes = _checked_allowed_ips(allowed_ips)
    # The int coercion is the whole check here: anything that is not a number
    # falls back to the default instead of reaching the file.
    try:
        keep = int(keepalive)
    except (TypeError, ValueError):
        keep = 25
    lines = [
        "[Interface]",
        f"PrivateKey = {priv}",
        f"Address = {addr}",
        "",
        "[Peer]",
        f"PublicKey = {pub}",
        f"Endpoint = {peer_endpoint}",
        f"AllowedIPs = {routes}",
        f"PersistentKeepalive = {keep}",
        "",
    ]
    return "\n".join(lines)


def parse_handshakes(text: str) -> int:
    r"""Latest handshake epoch (int) from `wg show <iface> latest-handshakes`.

    That command prints one `<pubkey>\t<epoch-seconds>` line per peer; a peer
    that has never completed a handshake reports 0. Returns the largest epoch
    seen, or 0 when there is none. Pure, so the status parse unit-tests against
    stubbed command output.
    """
    best = 0
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            epoch = int(parts[-1])
        except ValueError:
            continue
        best = max(best, epoch)
    return best


def wg_available(which=shutil.which, exists=os.path.exists) -> bool:
    """Whether this server can host an in-container WireGuard tunnel.

    Needs both `wg` and `wg-quick` on PATH (the image installs them) AND
    /dev/net/tun present, which arrives with the docker-compose.forager.yml
    override (the device plus NET_ADMIN), not with the default compose. The
    probes are injected so this is testable without a real wg install or tun
    device. The router calls this to decide the local path vs an honest error.
    """
    if not (which("wg") and which("wg-quick")):
        return False
    return bool(exists("/dev/net/tun"))


# --- Side-effecting layer (subprocess, disk); not exercised in tests ------

def keygen() -> str:
    """Generate a WireGuard keypair; keep the private key on the device.

    Writes the private key to a 0600 file under data_dir and returns only the
    public key. The private key is never logged or returned.
    """
    priv = subprocess.run(["wg", "genkey"], capture_output=True, text=True,
                          timeout=10).stdout.strip()
    if not priv:
        raise RuntimeError("wg genkey produced no key")
    pub = subprocess.run(["wg", "pubkey"], input=priv, capture_output=True,
                         text=True, timeout=10).stdout.strip()
    if not pub:
        raise RuntimeError("wg pubkey produced no key")
    kp = _key_path()
    kp.parent.mkdir(parents=True, exist_ok=True)
    kp.write_text(priv + "\n")
    os.chmod(kp, 0o600)
    return pub


def up(address: str, server_public_key: str, endpoint: str, allowed_ips: str,
       keepalive=25, private_key: str = "") -> None:
    """Write the interface config and bring it up (idempotent).

    Uses the private key from the last keygen (or the one passed in). Tears
    down any existing interface first so a re-enable with fresh parameters
    converges instead of erroring. The config text holds the private key, so it
    is written to a 0600 file and never logged.
    """
    if not private_key:
        try:
            private_key = _key_path().read_text().strip()
        except OSError:
            private_key = ""
    if not private_key:
        raise RuntimeError("no WireGuard private key; run keygen first")
    config = render_config(private_key, address, server_public_key, endpoint,
                           allowed_ips, keepalive=keepalive)
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    # Idempotent: down any prior interface before re-applying (best effort).
    subprocess.run(["wg-quick", "down", INTERFACE], capture_output=True,
                   text=True, timeout=30)
    with open(CONFIG_PATH, "w") as f:
        f.write(config)
    os.chmod(CONFIG_PATH, 0o600)
    r = subprocess.run(["wg-quick", "up", INTERFACE], capture_output=True,
                       text=True, timeout=30)
    if r.returncode != 0:
        # Report only the command's own message; never the config (it holds the
        # key). wg-quick's stderr does not echo the key.
        raise RuntimeError((r.stderr or r.stdout or "wg-quick up failed").strip())


def down() -> None:
    """Take the interface down (best effort)."""
    subprocess.run(["wg-quick", "down", INTERFACE], capture_output=True,
                   text=True, timeout=30)


def status() -> dict:
    """Whether the interface is up and its latest handshake age (seconds)."""
    if not wg_available():
        return {"up": False, "last_handshake_seconds": None}
    try:
        r = subprocess.run(["wg", "show", INTERFACE, "latest-handshakes"],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return {"up": False, "last_handshake_seconds": None}
    if r.returncode != 0:
        return {"up": False, "last_handshake_seconds": None}
    epoch = parse_handshakes(r.stdout)
    seconds = int(time.time() - epoch) if epoch > 0 else None
    return {"up": True, "last_handshake_seconds": seconds}
