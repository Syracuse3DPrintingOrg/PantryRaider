"""In-container WireGuard backend for Forager remote access on a server.

A Pi appliance owns its WireGuard endpoint on the host through the host bridge
(only root there can create the interface). A plain server (docker-compose) has
no bridge: the app runs in a container, so WireGuard runs INSIDE that container
instead. This module is the server-side equivalent of the bridge's /tunnel/*
handlers: keygen, config render, up, down, and status, kept as pure helpers
plus thin subprocess wrappers so the tricky parts unit-test without wg present.

The container needs NET_ADMIN and /dev/net/tun (granted by the shipped compose)
for wg-quick to create the interface; wg_available() reports whether this host
can host a tunnel at all. The private key is written to a 0600 file under
data_dir and is never logged or returned to the cloud (only the public half
leaves the device).
"""
from __future__ import annotations

import os
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

def render_config(private_key: str, address: str, server_public_key: str,
                  endpoint: str, allowed_ips: str, keepalive=25) -> str:
    """Render the wg-quick config text for the Forager interface. Pure.

    AllowedIPs is the server's /32 only (the cloud passes it), so this is a
    hub route to reach the kitchen through Forager, never a full tunnel that
    would capture all of the container's traffic; a 0.0.0.0/0 catch-all is
    never emitted. No DNS line is written: the container has no resolvconf, so
    a DNS directive would make wg-quick fail. Kept pure and off the log path so
    the private key stays put.
    """
    addr = str(address or "").strip()
    if addr and "/" not in addr:
        addr = addr + "/32"
    try:
        keep = int(keepalive)
    except (TypeError, ValueError):
        keep = 25
    lines = [
        "[Interface]",
        f"PrivateKey = {(private_key or '').strip()}",
        f"Address = {addr}",
        "",
        "[Peer]",
        f"PublicKey = {(server_public_key or '').strip()}",
        f"Endpoint = {(endpoint or '').strip()}",
        f"AllowedIPs = {(allowed_ips or '').strip()}",
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
    /dev/net/tun present (the compose grants the device plus NET_ADMIN). The
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
