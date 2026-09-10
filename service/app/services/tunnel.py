"""Tunnel service: manages Cloudflare Tunnel and Pantry Raider Cloud connections.

All subprocess calls are wrapped in try/except: docker may not be available in
all environments (CI, dev without Docker, HA add-on, etc.).
"""
from __future__ import annotations

import re
import subprocess


_CONTAINER_NAME = "foodassistant-tunnel"
_CF_IMAGE = "cloudflare/cloudflared:latest"
_CLOUD_REGISTER_URL = "https://cloud.foodassistant.app/tunnel/register"


class TunnelService:
    """Wraps docker/cloudflared tunnel lifecycle operations."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, mode: str, token: str) -> dict:
        """Start a tunnel in the given mode.

        Args:
            mode:  "cloudflare" or "subscription"
            token: Cloudflare tunnel token or subscription API token

        Returns:
            {"ok": True, "url": "https://..."} or {"ok": False, "error": "..."}
        """
        if mode == "cloudflare":
            return self._start_cloudflare(token)
        if mode == "subscription":
            return self._start_subscription(token)
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
            return {"ok": False, "error": "docker not found: is Docker running?"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "docker run timed out"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _start_subscription(self, token: str) -> dict:
        """Register with Pantry Raider Cloud (stubbed: no live endpoint yet)."""
        if not token:
            return {"ok": False, "error": "Subscription token is required."}
        try:
            import httpx
            response = httpx.post(
                _CLOUD_REGISTER_URL,
                json={"token": token},
                timeout=10.0,
            )
            if response.status_code == 200:
                data = response.json()
                return {"ok": True, "url": data.get("url", "")}
            return {"ok": False, "error": f"HTTP {response.status_code}"}
        except Exception:
            # In dev/offline mode return a mock response so the UI is exercisable
            return {
                "ok": True,
                "url": "https://demo.foodassistant.app",
                "_mock": True,
            }
