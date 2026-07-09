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

import re
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


def resolve_effective_queue(local: str, inherited: str) -> str:
    """The queue a device should actually print to (FoodAssistant-7u7z).

    A device's own local queue always wins when set, so a device that has picked
    its own printer keeps it. When it has not chosen one, the fleet default the
    main server published (pulled by a satellite into ``inherited``) is used.
    Empty means no queue is available. Pure so it is unit-tested and shared by
    every print path."""
    local = (local or "").strip()
    if local:
        return local
    return (inherited or "").strip()


# -- Discovery + add: pure helpers (FoodAssistant-r9a4) ----------------------
# Adding a printer shells lpadmin, whose queue name and device URI both flow
# into a privileged command. Every string is validated by a pure helper here
# and the command is always built as an argv list (never a shell string), so a
# fat-fingered or hostile name cannot inject anything. The same validation runs
# again on the host-bridge side (defense in depth) before lpadmin is invoked as
# root.

# CUPS queue names allow letters, digits, underscore, and dash. No spaces, no
# slashes, no shell metacharacters. This is deliberately stricter than CUPS
# itself (which also forbids '/', '#', and ' ') so a name is always safe as an
# argv token and as part of a device-URI-free lpadmin call.
_QUEUE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# A device/connection URI: scheme://... plus the characters real CUPS backends
# emit (percent-encoded DNS-SD names, IPv6 brackets, ports, paths). Everything
# a shell could act on (spaces, ; & | $ ` ' " < > ( ) { } * ?, backslash) is
# excluded, so the string is safe as a single argv token.
_CONNECTION_RE = re.compile(r"^[A-Za-z0-9._:/%\[\]@~+-]+$")


def sanitize_queue_name(name: str) -> str:
    """Return ``name`` when it is a valid CUPS queue name, else "".

    Pure and strict: a valid name is one or more of letters, digits, underscore,
    or dash, with surrounding whitespace trimmed. Anything with a space, slash,
    or shell metacharacter is rejected (returns "") rather than silently
    rewritten, so the caller fails loudly instead of creating a surprising
    queue. Used on both the app side and the bridge side before lpadmin runs."""
    n = (name or "").strip()
    return n if n and _QUEUE_NAME_RE.match(n) else ""


def valid_connection(connection: str) -> bool:
    """Whether ``connection`` is a safe device URI to hand to lpadmin -v.

    Pure: requires a scheme (``scheme://``) and only URI-safe characters, so no
    shell metacharacter or whitespace can ride along. Rejects a bare host or an
    empty string."""
    c = (connection or "").strip()
    if "://" not in c:
        return False
    return bool(_CONNECTION_RE.match(c))


# Bundled driver PPDs that foodassistant-print-setup installs on the device. The
# UI sends a stable keyword (never a path); the value is a fixed,
# server-controlled path, so no user input ever reaches lpadmin -P. Used for
# printers modern CUPS has no bundled or driverless support for, like a Zebra
# ZPL label printer, which rasterizes through the cups-filters rastertolabel
# filter (FoodAssistant-zqh0).
PPD_DRIVERS = {
    "zebra-zpl": "/usr/share/ppd/foodassistant/zebra-zpl.ppd",
}


def add_printer_args(name: str, connection: str, model: str = "everywhere") -> list[str]:
    """Build the lpadmin argv that adds a queue. Pure; raises ValueError on any
    invalid input so a bad name or URI never reaches the command.

    A driverless (IPP Everywhere) printer uses model ``everywhere``; a raw
    socket printer uses a caller-provided driver/model or ``raw``; a known
    bundled driver keyword (see PPD_DRIVERS, e.g. ``zebra-zpl``) adds with a
    fixed PPD file. The result is always a list (argv), never a shell string."""
    q = sanitize_queue_name(name)
    if not q:
        raise ValueError("Invalid printer name. Use letters, digits, dashes, or "
                         "underscores, with no spaces.")
    conn = (connection or "").strip()
    if not valid_connection(conn):
        raise ValueError("Invalid printer connection address.")
    m = (model or "").strip() or "raw"
    ppd = PPD_DRIVERS.get(m)
    if ppd:
        return ["lpadmin", "-p", q, "-E", "-v", conn, "-P", ppd]
    return ["lpadmin", "-p", q, "-E", "-v", conn, "-m", m]


def _classify_device(uri: str, make_model: str = "", info: str = "") -> tuple[str, str]:
    """Classify a discovered device URI into (kind, default_driver).

    Driverless (IPP Everywhere) schemes get kind "driverless" and model
    "everywhere"; a raw JetDirect socket gets kind "socket" and model "raw";
    anything else is "other" with a raw default. Pure."""
    scheme = uri.split("://", 1)[0].lower() if "://" in uri else uri.lower()
    if scheme in ("ipp", "ipps", "dnssd", "http", "https"):
        return ("driverless", "everywhere")
    if scheme == "socket":
        return ("socket", "raw")
    return ("other", "raw")


def _suggest_name(info: str, uri: str) -> str:
    """A safe suggested queue name from a device's info string (or its URI).

    Non-name characters collapse to underscores and the result is trimmed and
    capped, so it is always a valid CUPS queue name. Pure."""
    base = (info or uri or "printer").strip()
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", base).strip("_")
    return (cleaned[:63] or "printer")


def _parse_lpinfo_v(text: str) -> list[dict]:
    """Parse ``lpinfo -l -v`` output into discovered device candidates.

    Handles the long ``-l`` block form (a ``Device:`` line then indented
    ``key = value`` fields) and degrades to the short ``<class> <uri>`` form.
    Bare backend schemes (a lone ``socket`` / ``ipp`` with no ``://`` target)
    are skipped: they are the backend itself, not a reachable printer. Returns
    [{uri, info, make_model, kind, driver}], order preserved. Pure, so it is
    unit-tested against captured sample output."""
    records: list[dict] = []
    cur: dict | None = None
    for raw in (text or "").splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("Device:"):
            if cur is not None:
                records.append(cur)
            cur = {}
            stripped = stripped[len("Device:"):].strip()
            if not stripped:
                continue
        if cur is None:
            # Short form emitted without -l: "<class> <uri>".
            parts = stripped.split(None, 1)
            if len(parts) == 2 and "://" in parts[1]:
                records.append({"class": parts[0], "uri": parts[1].strip()})
            continue
        if "=" in stripped:
            key, val = stripped.split("=", 1)
            cur[key.strip()] = val.strip()
    if cur is not None:
        records.append(cur)

    out: list[dict] = []
    for r in records:
        uri = (r.get("uri") or "").strip()
        if "://" not in uri:
            continue  # a bare backend scheme, not a printer
        make_model = r.get("make-and-model") or ""
        info = r.get("info") or make_model or ""
        kind, driver = _classify_device(uri, make_model, info)
        out.append({"uri": uri, "info": info, "make_model": make_model,
                    "kind": kind, "driver": driver})
    return out


def _parse_lpstat_v(text: str) -> dict:
    """Parse ``lpstat -v`` ("device for NAME: uri") into {name: uri}.

    Used to exclude already-configured queues from discovery, matched by their
    device URI. Pure."""
    out: dict = {}
    marker = "device for "
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.lower().startswith(marker):
            rest = line[len(marker):]
            if ":" in rest:
                name, uri = rest.split(":", 1)
                out[name.strip()] = uri.strip()
    return out


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
        """True when a CUPS scheduler is actually reachable, not merely when the
        lpstat client exists. The client is baked into the app image, so a bare
        `which lpstat` is always true and would wrongly report the print stack as
        ready, hiding the Install prompt and showing an empty printer list with
        no explanation. `lpstat -r` reports whether a scheduler is running and
        reachable, locally or through CUPS_SERVER."""
        if shutil.which("lpstat") is None:
            return False
        proc = self._run(["lpstat", "-r"])
        if proc is None:
            return False
        out = (proc.stdout or b"").decode("utf-8", "replace").lower()
        return "scheduler is running" in out

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

    def discover_printers(self) -> list[dict]:
        """Discover addable network printers via lpinfo, excluding queues that
        are already configured (matched by device URI). Never raises."""
        if shutil.which("lpinfo") is None:
            return []
        proc = self._run(
            ["lpinfo", "--include-schemes", "dnssd,ipp,ipps,socket", "-l", "-v"])
        text = proc.stdout.decode("utf-8", "replace") if proc else ""
        candidates = _parse_lpinfo_v(text)
        vproc = self._run(["lpstat", "-v"])
        existing = set(_parse_lpstat_v(
            vproc.stdout.decode("utf-8", "replace") if vproc else "").values())
        result: list[dict] = []
        for c in candidates:
            if c["uri"] in existing:
                continue
            result.append({
                "name": _suggest_name(c["info"], c["uri"]),
                "uri": c["uri"],
                "kind": c["kind"],
                "driver": c["driver"],
                "info": c["info"],
            })
        return result

    def add_printer(self, name: str, connection: str, model: str = "everywhere") -> PrintResult:
        """Add a CUPS queue with lpadmin. Never raises: an invalid name or URI,
        or a missing lpadmin, returns a structured error."""
        try:
            args = add_printer_args(name, connection, model)
        except ValueError as exc:
            return PrintResult(ok=False, error=str(exc))
        if shutil.which("lpadmin") is None:
            return PrintResult(ok=False, error="Printing tools are not installed.")
        proc = self._run(args)
        if proc is None:
            return PrintResult(ok=False, error="Could not run the add-printer command.")
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip() or "Adding the printer failed."
            return PrintResult(ok=False, error=err)
        return PrintResult(ok=True)

    def remove_printer(self, name: str) -> PrintResult:
        """Remove a CUPS queue with lpadmin -x. Never raises."""
        q = sanitize_queue_name(name)
        if not q:
            return PrintResult(ok=False, error="Invalid printer name.")
        if shutil.which("lpadmin") is None:
            return PrintResult(ok=False, error="Printing tools are not installed.")
        proc = self._run(["lpadmin", "-x", q])
        if proc is None:
            return PrintResult(ok=False, error="Could not run the remove-printer command.")
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip() or "Removing the printer failed."
            return PrintResult(ok=False, error=err)
        return PrintResult(ok=True)

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


def discover_printers() -> list[dict]:
    """Discover addable network printers on this device's CUPS as
    [{name, uri, kind, driver, info}]. Returns [] (never raises) when lpinfo or
    the print stack is not present."""
    try:
        return _backend.discover_printers()
    except Exception:
        return []


def add_printer(name: str, connection: str, model: str = "everywhere") -> PrintResult:
    """Add a CUPS queue on this device. Never raises: a structured error on any
    failure, so the request path survives."""
    try:
        return _backend.add_printer(name, connection, model)
    except Exception as exc:
        return PrintResult(ok=False, error=f"Could not add the printer: {exc}")


def remove_printer(name: str) -> PrintResult:
    """Remove a CUPS queue on this device. Never raises."""
    try:
        return _backend.remove_printer(name)
    except Exception as exc:
        return PrintResult(ok=False, error=f"Could not remove the printer: {exc}")
