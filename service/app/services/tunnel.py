"""Tunnel service: manages Cloudflare Tunnel and Pantry Raider Cloud connections.

All subprocess calls are wrapped in try/except: docker may not be available in
all environments (CI, dev without Docker, HA add-on, etc.).
"""
from __future__ import annotations

import re
import os
import shutil
import subprocess


_CONTAINER_NAME = "foodassistant-tunnel"
_CF_IMAGE = "cloudflare/cloudflared:latest"


def cloudflare_available(which=shutil.which, exists=os.path.exists) -> bool:
    """Whether THIS install can bring a cloudflared container up itself.

    Every call in this service shells out to the docker CLI, which means it
    needs both the CLI on PATH and a Docker socket it is allowed to talk to.
    The published app image ships neither, and no compose file mounts the
    socket (mounting it would hand the app root on the host, which is the
    wrong trade for a self-hosted kitchen). So on a plain Compose install this
    is False, and the honest answer is to say so rather than to offer a button
    that fails.

    A Pi appliance is a different story: there the tunnel is brought up by the
    host bridge, a root helper OUTSIDE the container, so that path never comes
    through here at all.

    The probes are injected so this is testable without Docker present.
    """
    if not which("docker"):
        return False
    return exists("/var/run/docker.sock")


class TunnelService:
    """Wraps docker/cloudflared tunnel lifecycle operations."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, mode: str, token: str) -> dict:
        """Start a tunnel in the given mode.

        Args:
            mode:  "cloudflare" (the only mode this service runs)
            token: Cloudflare tunnel token

        Returns:
            {"ok": True, "url": "https://..."} or {"ok": False, "error": "..."}
        """
        if mode == "cloudflare":
            if not cloudflare_available():
                return {"ok": False, "error": (
                    "This install cannot run the Cloudflare helper for you. "
                    "Run cloudflared alongside Pantry Raider yourself and "
                    "point it at this address; the Remote access page in the "
                    "docs has the steps.")}
            return self._start_cloudflare(token)
        if mode in ("forager", "subscription"):
            # Forager remote access does not run a container here, and the old
            # subscription registration had no service behind it: it answered
            # every failure with a fake success and a demo address.
            return {"ok": False, "error": (
                "Forager remote access is set up on the Forager page, "
                "not here.")}
        return {"ok": False, "error": f"Unknown tunnel mode: {mode!r}"}

    def stop(self) -> dict:
        """Stop the running tunnel container.

        Returns:
            {"ok": True} or {"ok": False, "error": "..."}
        """
        try:
            result = subprocess.run(
                ["docker", "stop", _CONTAINER_NAME],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                return {"ok": True}
            return {"ok": False, "error": result.stderr.strip() or result.stdout.strip()}
        except FileNotFoundError:
            return {"ok": False, "error": "docker not found"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "docker stop timed out"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def status(self) -> dict:
        """Check whether the tunnel container is running.

        Returns:
            {"running": bool, "url": str}
        """
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Running}}", _CONTAINER_NAME],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return {"running": False, "url": ""}
            running = result.stdout.strip().lower() == "true"
            url = self.get_url_from_cloudflare_logs() if running else ""
            return {"running": running, "url": url}
        except FileNotFoundError:
            return {"running": False, "url": ""}
        except subprocess.TimeoutExpired:
            return {"running": False, "url": ""}
        except Exception:
            return {"running": False, "url": ""}

    def get_url_from_cloudflare_logs(self) -> str:
        """Parse cloudflared container logs for the assigned public URL.

        Looks for trycloudflare.com or cloudflareaccess.com URLs.

        Returns:
            The public URL string, or "" if not found.
        """
        try:
            result = subprocess.run(
                ["docker", "logs", _CONTAINER_NAME],
                capture_output=True,
                text=True,
                timeout=10,
            )
            # cloudflared writes to stderr; combine both streams
            output = result.stdout + result.stderr
            # Match https://xxx.trycloudflare.com or https://xxx.cloudflareaccess.com
            pattern = r"https://[a-zA-Z0-9\-]+\.(?:trycloudflare|cloudflareaccess)\.com"
            match = re.search(pattern, output)
            return match.group(0) if match else ""
        except FileNotFoundError:
            return ""
        except subprocess.TimeoutExpired:
            return ""
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _start_cloudflare(self, token: str) -> dict:
        """Launch a cloudflared container using the given tunnel token."""
        if not token:
            return {"ok": False, "error": "Cloudflare tunnel token is required."}
        try:
            # Stop any existing container first (ignore errors)
            subprocess.run(
                ["docker", "stop", _CONTAINER_NAME],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            pass

        try:
            result = subprocess.run(
                [
                    "docker", "run", "-d", "--rm",
                    "--name", _CONTAINER_NAME,
                    _CF_IMAGE,
                    "tunnel", "--no-autoupdate", "run", "--token", token,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                return {"ok": True, "url": ""}
            return {"ok": False, "error": result.stderr.strip() or result.stdout.strip()}
        except FileNotFoundError:
            return {"ok": False, "error": (
                "The Docker command is not available inside Pantry Raider, so "
                "it cannot start the Cloudflare helper for you. Run "
                "cloudflared yourself and point it at this address.")}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "docker run timed out"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

