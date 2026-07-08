"""Print backend and printer registry (FoodAssistant-yg41).

The app prints labels and recipe pages through CUPS, but it deliberately does
NOT bundle a native CUPS binding (pycups). Instead it shells out to the standard
lp / lpstat tools, so the container stays light and works the same whether CUPS
runs on the host, in a sibling container, or on another machine reached through
the CUPS_SERVER environment variable. If those tools are not present (no print
stack installed), every call degrades quietly: queue discovery returns an empty
list and a print attempt returns a structured error instead of raising into the
request path.

The parsing of lpstat output is kept in small pure functions so it is unit
tested without a real CUPS server.
"""
from __future__ import annotations

import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class PrintResult:
    """Outcome of a print attempt. ``ok`` is the only thing a caller must check;
    ``job_id`` is set on success (when lp reports one) and ``error`` carries a
    short human message on failure."""
    ok: bool
    job_id: str = ""
    error: str = ""


# -- Pure lpstat parsers ----------------------------------------------------


def _parse_lpstat_printers(text: str) -> list[dict]:
    """Parse `lpstat -p` (or a bare `lpstat -e` name list) into queue dicts.

    `lpstat -p` prints one or more lines per printer, the first being
    "printer <name> is idle.  enabled since ..." or "... is now printing ...".
    `lpstat -e` prints just the queue name per line. This handles both: it reads
    the "printer <name> is <state>" form when present, and otherwise treats a
    lone token as a queue name with an unknown state. Returns
    [{"name", "state"}], order preserved, de-duplicated by name."""
    queues: list[dict] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        name = ""
        state = "unknown"
        if parts[0] == "printer" and len(parts) >= 2:
            name = parts[1]
            if "is" in parts:
                idx = parts.index("is")
                if idx + 1 < len(parts):
                    state = parts[idx + 1].rstrip(".")
        elif len(parts) == 1:
            # A bare queue name, as from `lpstat -e`.
            name = parts[0]
        else:
            continue
        if name and name not in seen:
            seen.add(name)
            queues.append({"name": name, "state": state})
    return queues


def _parse_lpstat_default(text: str) -> str:
    """Parse `lpstat -d` into the default queue name, or "" when there is none.

    The output is "system default destination: <name>" or "no system default
    destination"."""
    for raw in (text or "").splitlines():
        line = raw.strip()
        low = line.lower()
        if low.startswith("no system default"):
            return ""
        marker = "system default destination:"
        if low.startswith(marker):
            return line[len(marker):].strip()
    return ""


def _merge_default(queues: list[dict], default_name: str) -> list[dict]:
    """Return ``queues`` with an ``is_default`` flag set from ``default_name``."""
    return [
        {**q, "is_default": (q["name"] == default_name)}
        for q in queues
    ]


# -- Backend ----------------------------------------------------------------


class PrintBackend(ABC):
    """A place labels and documents can be sent to print."""

    @abstractmethod
    def available(self) -> bool:
        """True when this backend can actually print (its tools are present)."""

    @abstractmethod
    def list_queues(self) -> list[dict]:
        """Discover print queues as [{name, state, is_default}]. Never raises."""

    @abstractmethod
    def print_bytes(self, queue: str, data: bytes, *, options: dict | None = None) -> PrintResult:
        """Send ``data`` (PNG/PDF/raw) to ``queue``. Never raises; returns a
        PrintResult carrying either a job id or an error."""


class CupsBackend(PrintBackend):
    """CUPS backend that shells out to lp / lpstat.

    No native dependency: a plain subprocess call to the standard command-line
    tools. Honors CUPS_SERVER in the environment the way lp/lpstat already do,
    so pointing at a remote or sibling-container CUPS needs no code change."""

    def __init__(self, timeout: float = 8.0):
        self._timeout = timeout

    def available(self) -> bool:
        return shutil.which("lpstat") is not None

    def _run(self, args: list[str], *, input_bytes: bytes | None = None) -> subprocess.CompletedProcess | None:
        """Run a command, returning the completed process or None if the binary
        is missing or the call fails to even start / times out."""
        exe = shutil.which(args[0])
        if exe is None:
            return None
        try:
            return subprocess.run(
                [exe, *args[1:]],
                input=input_bytes,
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    def list_queues(self) -> list[dict]:
        if shutil.which("lpstat") is None:
            return []
        printers = self._run(["lpstat", "-p"])
        text = printers.stdout.decode("utf-8", "replace") if printers else ""
        queues = _parse_lpstat_printers(text)
        if not queues:
            # Fall back to the bare name list if -p gave nothing usable.
            names = self._run(["lpstat", "-e"])
            if names:
                queues = _parse_lpstat_printers(names.stdout.decode("utf-8", "replace"))
        default = self._run(["lpstat", "-d"])
        default_name = _parse_lpstat_default(
            default.stdout.decode("utf-8", "replace") if default else ""
        )
        return _merge_default(queues, default_name)

    def print_bytes(self, queue: str, data: bytes, *, options: dict | None = None) -> PrintResult:
        if not queue:
            return PrintResult(ok=False, error="No printer queue selected.")
        if shutil.which("lp") is None:
            return PrintResult(ok=False, error="Printing tools are not installed.")
        args = ["lp", "-d", queue]
        for key, value in (options or {}).items():
            args += ["-o", f"{key}={value}" if value != "" else str(key)]
        proc = self._run(args, input_bytes=data)
        if proc is None:
            return PrintResult(ok=False, error="Could not run the print command.")
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip() or "Print command failed."
            return PrintResult(ok=False, error=err)
        out = proc.stdout.decode("utf-8", "replace").strip()
        return PrintResult(ok=True, job_id=_parse_lp_job_id(out))


def _parse_lp_job_id(text: str) -> str:
    """Pull the job id out of lp's "request id is <queue>-<n> (...)" line."""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if "request id is" in line:
            after = line.split("request id is", 1)[1].strip()
            return after.split()[0] if after else ""
    return ""


# -- Module-level convenience (single shared CUPS backend) ------------------

_backend: PrintBackend = CupsBackend()


def printing_available() -> bool:
    """True when the host has the print tools (lpstat) available."""
    return _backend.available()


def list_queues() -> list[dict]:
    """List CUPS print queues as [{name, state, is_default}].

    Returns [] (never raises) when CUPS / lpstat is not present, so the rest of
    the app is unaffected on installs without a printer."""
    try:
        return _backend.list_queues()
    except Exception:
        return []


def print_bytes(queue: str, data: bytes, *, options: dict | None = None) -> PrintResult:
    """Send bytes to a print queue. Never raises: returns a PrintResult with a
    structured error on any failure."""
    try:
        return _backend.print_bytes(queue, data, options=options)
    except Exception as exc:  # last-resort guard: the request path must survive
        return PrintResult(ok=False, error=f"Print failed: {exc}")
