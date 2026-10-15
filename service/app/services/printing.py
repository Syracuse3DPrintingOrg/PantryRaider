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

import os
import re
import shutil
import socket
import stat
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from ..plain_digits import is_plain_digits


@dataclass
class PrintResult:
    """Outcome of a print attempt. ``ok`` is the only thing a caller must check;
    ``job_id`` is set on success (when lp reports one) and ``error`` carries a
    short human message on failure. ``error_kind`` classifies a failure so a
    caller can react differently: "offline" means the printer looks powered
    off or unreachable (FoodAssistant-fu62, see classify_print_error), "" means
    unclassified. Left "" on success."""
    ok: bool
    job_id: str = ""
    error: str = ""
    error_kind: str = ""


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


# -- Self-import dedup (FoodAssistant-zq6b) ----------------------------------
# With fleet printer sharing on, cups-browsed rediscovers this device's OWN
# DNS-SD-advertised shared queues and re-creates them locally as
# "<name>_<hostname>" duplicates (seen live: Zebra_ZM400 plus
# Zebra_ZM400_foodassistant). Filtering inside cups-browsed proved unreliable
# (its BrowseFilter did not match the self-advertised host string), so the
# queue list is cleaned app-side instead: deterministic, backend-agnostic, and
# every consumer of list_queues (settings dropdowns, /printing/queues) sees
# the same clean list. Queues from OTHER fleet devices keep their host suffix
# because their base name is not a local queue here.


def device_hostname() -> str:
    """This device's hostname, for spotting self-imported queue duplicates.

    FOODASSISTANT_HOSTNAME overrides it (tests, or an install whose CUPS host
    is not the container's own hostname)."""
    override = (os.environ.get("FOODASSISTANT_HOSTNAME") or "").strip()
    if override:
        return override
    try:
        return socket.gethostname() or ""
    except OSError:
        return ""


def _self_import_suffixes(hostname: str) -> set[str]:
    """The lowercase queue-name suffixes that mark a self-imported duplicate.

    The device hostname (and its short first label, since cups-browsed may use
    either), folded to valid queue-name characters the way CUPS does, plus the
    "series" tail some driverless self-imports carry. Pure."""
    suffixes = {"series"}
    host = (hostname or "").strip()
    for candidate in (host, host.split(".")[0]):
        cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", candidate).strip("_").lower()
        if cleaned:
            suffixes.add(cleaned)
    return suffixes


def dedupe_self_imports(queues: list[dict], hostname: str,
                        protected: set[str] | frozenset = frozenset()) -> list[dict]:
    """Hide cups-browsed self-import duplicates from a queue list. Pure.

    A queue named "<base>_<suffix>" is hidden only when ALL of these hold:
    the suffix is this device's hostname (or its short form, or the driverless
    "series" tail), the base queue itself is also in the list (so a genuinely
    remote "<name>_<otherhost>" import with no local base is always kept), and
    the queue is not one of the ``protected`` names (a queue someone already
    picked as their label/document/fleet printer must never vanish from the
    list, or the settings page would show a broken choice)."""
    names = {q.get("name", "") for q in queues}
    suffixes = _self_import_suffixes(hostname)
    kept: list[dict] = []
    for q in queues:
        name = q.get("name", "")
        if name in protected:
            kept.append(q)
            continue
        hidden = False
        low = name.lower()
        for suffix in suffixes:
            tail = "_" + suffix
            if low.endswith(tail) and len(name) > len(tail):
                base = name[:-len(tail)]
                if base in names:
                    hidden = True
                    break
        if not hidden:
            kept.append(q)
    return kept


def _configured_queue_names() -> set[str]:
    """Queue names the user (or the fleet default) has picked, which the dedup
    must never hide. Best-effort: settings problems just protect nothing."""
    try:
        from ..config import settings
        picked = {
            settings.label_printer_queue, settings.document_printer_queue,
            settings.fleet_label_printer_queue,
            settings.fleet_document_printer_queue,
        }
        return {(name or "").strip() for name in picked} - {""}
    except Exception:
        return set()


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


# -- Label media discovery (FoodAssistant-u55y) ------------------------------
# A label queue advertises the physical media sizes it supports through the
# CUPS "PageSize" (driver queues) or "media" (driverless/IPP queues) option,
# visible via `lpoptions -p <queue> -l`. Names come in two shapes seen in the
# wild: a driver name like "40x30mm.Borderless" (dimensions, then an optional
# dot-suffixed descriptor), and a PWG self-describing name like
# "om_40x30mm_40x30mm" or "na_letter_8.5x11in" (class prefix, optional
# descriptive middle segment, exact dimensions last). Both carry the true
# printable size, so pulling them into the Settings UI saves a user from
# guessing or measuring their label stock by hand.

_MEDIA_DIMS_RE = re.compile(r"(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)(mm|in)", re.IGNORECASE)
_MEDIA_POINTS_RE = re.compile(r"w(\d+(?:\.\d+)?)h(\d+(?:\.\d+)?)", re.IGNORECASE)


def _parse_media_token(token: str) -> "dict | None":
    """Parse one PageSize/media value into {w_mm, h_mm, label, is_default}.

    Returns None for a token that carries no concrete dimensions (a plain
    named size like "Letter" with no numbers, or a "Custom.WIDTHxHEIGHTin"
    placeholder that means "any size", not one the printer actually stocks).
    Pure."""
    raw = (token or "").strip()
    if not raw:
        return None
    is_default = raw.startswith("*")
    if is_default:
        raw = raw[1:]
    if not raw:
        return None
    low = raw.lower()
    if low.startswith("custom.") or low == "custom":
        return None

    descriptor = ""
    dims_source = raw
    if ("." in raw and not low.startswith("om_") and not low.startswith("oe_")
            and not _MEDIA_POINTS_RE.fullmatch(raw)):
        # Driver form: "40x30mm.Borderless" -> dims, then a dot-suffixed
        # human descriptor. Only split on the FIRST dot so a descriptor that
        # itself contains a dot (rare) stays intact.
        main, _, desc = raw.partition(".")
        dims_source = main
        descriptor = desc

    match = None
    if "_" in raw:
        # PWG self-describing name: class_name_dims, dims exact and last.
        matches = list(_MEDIA_DIMS_RE.finditer(raw))
        if matches:
            match = matches[-1]
            parts = raw.split("_")
            # Drop the leading class keyword and the trailing dims segment;
            # anything left over (and not itself dims-shaped) is a
            # human-readable descriptor, e.g. "na_letter_8.5x11in" -> "letter".
            middle = parts[1:-1] if len(parts) > 2 else []
            words = [p for p in middle if not _MEDIA_DIMS_RE.fullmatch(p)]
            descriptor = " ".join(w.replace("-", " ") for w in words).strip()
    if match is None:
        match = _MEDIA_DIMS_RE.search(dims_source)
    if match is None:
        # Adobe PPD point names ("w144h72", 1/72 in each), which the Zebra ZPL
        # driver and most label PPDs use (FoodAssistant-j8grf).
        pts = _MEDIA_POINTS_RE.fullmatch(dims_source)
        if pts is None:
            return None
        try:
            w = float(pts.group(1)) * 25.4 / 72
            h = float(pts.group(2)) * 25.4 / 72
        except ValueError:
            return None
    else:
        try:
            w = float(match.group(1))
            h = float(match.group(2))
        except ValueError:
            return None
        if match.group(3).lower() == "in":
            w *= 25.4
            h *= 25.4
    w = round(w, 1)
    h = round(h, 1)
    if w <= 0 or h <= 0:
        return None

    def _fmt(n: float) -> str:
        return str(int(n)) if n == int(n) else f"{n:g}"

    label = f"{_fmt(w)} x {_fmt(h)} mm"
    descriptor = descriptor.replace("_", " ").replace("-", " ").strip()
    if descriptor:
        label += f" ({descriptor.title()})"

    return {"w_mm": w, "h_mm": h, "label": label, "is_default": is_default}


def _parse_lpoptions_media(text: str) -> list[dict]:
    """Parse `lpoptions -p <queue> -l` output into supported label sizes.

    Scans every option line for one whose name is "PageSize" or "media"
    (case-insensitive; driver queues use the former, driverless/IPP queues
    the latter), of the form "Name/Label: value1 *value2 value3 ...". Each
    value is parsed by `_parse_media_token`; anything without concrete
    dimensions (a bare "Letter", a "Custom.WIDTHxHEIGHTin" placeholder) is
    skipped. Returns a de-duplicated list of {w_mm, h_mm, label, is_default},
    order preserved. Pure, so it is unit-tested against captured samples."""
    out: list[dict] = []
    seen: set[tuple[float, float]] = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or ":" not in line:
            continue
        key, _, rest = line.partition(":")
        name = key.split("/", 1)[0].strip().lower()
        if name not in ("pagesize", "media"):
            continue
        for token in rest.split():
            parsed = _parse_media_token(token)
            if not parsed:
                continue
            dims = (parsed["w_mm"], parsed["h_mm"])
            if dims in seen:
                continue
            seen.add(dims)
            out.append(parsed)
    return out


def mm_to_inches(mm: float) -> float:
    """Millimetres to inches, rounded to two decimals (the settings fields'
    precision). Pure."""
    return round(float(mm) / 25.4, 2)


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


# -- Powered-off detection (FoodAssistant-fu62) ------------------------------
# A Bluetooth label printer (the Supvan bridge) shuts itself off after a while
# of inactivity to save its battery. When it is asleep, the bridge's RFCOMM
# connect attempt fails with a distinctive phrasing ("Host is down", a
# connect timeout, "no route to host") instead of anything print-content
# related. Recognizing that signature lets a print endpoint answer with a
# clear "turn your printer on" prompt instead of a generic, confusing print
# failure. Best-effort: it only sees whatever text lp/lpstat hand back, so an
# unrecognized phrasing just falls through to "other" (a generic error), never
# a crash.

_OFFLINE_ERROR_SIGNATURES = (
    "host is down",
    "no route to host",
    "connection timed out",
    "operation timed out",
    "connect timed out",
    "unable to contact printer",
    "printer not connected",
    "printer is not connected",
    "printer is offline",
    "could not connect to printer",
    "connection refused",
    "device or resource busy",
    "no such device",
    "network host is down",
)


def classify_print_error(text: str) -> str:
    """Classify a print failure as "offline" (the printer looks powered off or
    unreachable) or "other" (any other failure). Pure: case-insensitively
    matches ``text`` (an lp/lpstat stderr message, or a job's
    printer-state-message) against known Bluetooth/RFCOMM and CUPS
    backend-connect failure phrasings. Blank text is "other", never "offline",
    so an empty/unknown failure keeps the generic message rather than
    guessing."""
    low = (text or "").strip().lower()
    if not low:
        return "other"
    for signature in _OFFLINE_ERROR_SIGNATURES:
        if signature in low:
            return "offline"
    return "other"


# -- Print service: socket, classification and notice (0.21) -----------------
# The print service that ships with the compose files (the with-printing
# profile) shares its local socket with the app through a volume, and newer
# compose files point CUPS_SERVER at that socket. An older compose file still
# says CUPS_SERVER=cups:631. When the shared socket is there, the app talks to
# the sidecar through it instead, so printing keeps working either way and the
# admin pages are never needed over the network. Only the two sidecar names
# are ever remapped: a print server anywhere else, or one set up by hand under
# another name, is left exactly as configured.
#
# The same check tells whether the owner's print service still lets other
# programs read its configuration without a password (an older compose file),
# and raises one inbox item until it is updated.

SIDECAR_HOSTS = ("cups", "foodassistant-cups")
_DEFAULT_CUPS_SOCKET = "/run/cups/cups.sock"

PRINT_SERVICE_KIND = "print_service_update"
PRINT_SERVICE_DEDUPE_KEY = "print_service_update"
PRINT_SERVICE_TITLE = "Update your print service"
PRINT_SERVICE_BODY = (
    "Other programs on this server can change your printers without asking. "
    "Settings, Printing shows how to fix it. Your printers are kept.")
PRINT_SERVICE_DOCS_URL = (
    "https://github.com/Syracuse3DPrintingOrg/PantryRaider/blob/main/docs/printing.md")

# How long a print service check stays fresh, and how often GET /printing/queues
# may start a new one.
PRINT_SERVICE_REFRESH_SECONDS = 600.0
# A check that could not reach the print service at all (it is still starting
# after a reboot, or it is stopped) proves nothing, so it is tried again sooner.
PRINT_SERVICE_RETRY_SECONDS = 60.0
_PROBE_TIMEOUT = 2.0

# The cached check. "value" is None until the first refresh finishes. "at" and
# "claimed" use _clock (monotonic), never the wall clock. "inconclusive" is
# True when the last check could not reach the print service.
_clock = time.monotonic
_print_service: dict = {"value": None, "at": None, "claimed": None,
                        "inconclusive": False}


def cups_socket_path() -> str:
    """The shared print service socket, read from PR_CUPS_SOCKET at call time
    (default /run/cups/cups.sock)."""
    return (os.environ.get("PR_CUPS_SOCKET") or "").strip() or _DEFAULT_CUPS_SOCKET


def _is_socket(path: str) -> bool:
    """True when ``path`` exists and is a Unix socket. Never raises."""
    if not path:
        return False
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except (OSError, ValueError):
        return False


def _split_server(value: str) -> tuple[str, str]:
    """Split a CUPS_SERVER value into (host, port). Pure.

    A value starting with "/" is a socket path and has no host (""). The host
    is lowercased; the port is "" when absent or not a number. An IPv6 literal
    in brackets keeps its brackets."""
    v = (value or "").strip()
    if not v or v.startswith("/"):
        return "", ""
    # CUPS_SERVER may end in an option such as "/version=1.1"; it is not part
    # of the host or the port.
    v = v.split("/", 1)[0]
    if v.startswith("["):
        end = v.find("]")
        if end == -1:
            return v.lower(), ""
        host, rest = v[:end + 1], v[end + 1:]
        port = rest[1:] if rest.startswith(":") else ""
    elif v.count(":") == 1:
        host, _, port = v.partition(":")
    else:
        host, port = v, ""
    return host.strip().lower(), (port if is_plain_digits(port) else "")


def effective_cups_server(value: str, socket_exists: bool) -> str:
    """The CUPS_SERVER the print tools should use. Pure.

    A sidecar host (cups or foodassistant-cups, with or without a port) is
    swapped for the shared socket when that socket exists. Anything else,
    including an empty value (the CUPS client already prefers the default
    socket) and any other host, comes back unchanged."""
    host, _port = _split_server(value)
    if host in SIDECAR_HOSTS and socket_exists:
        return cups_socket_path()
    return value


def _cups_env() -> dict | None:
    """The environment for a CUPS tool, or None to inherit the process
    environment exactly as it is.

    Only a sidecar CUPS_SERVER with the shared socket present changes
    anything, and then only CUPS_SERVER."""
    value = os.environ.get("CUPS_SERVER", "")
    host, _port = _split_server(value)
    if host not in SIDECAR_HOSTS:
        return None
    effective = effective_cups_server(value, _is_socket(cups_socket_path()))
    if effective == value:
        return None
    env = dict(os.environ)
    env["CUPS_SERVER"] = effective
    return env


def classify_print_service(value: str, socket_exists: bool,
                           admin_conf_open: bool) -> str:
    """Classify how this app reaches its print service. Pure.

    current:  the shared socket is there and CUPS_SERVER is empty, a path,
              or a sidecar host (printing goes through the socket).
    none:     CUPS_SERVER is empty, or a path, and there is no socket.
    legacy:   a sidecar host, no socket, and the service hands out its
              configuration to any program without a password.
    external: everything else (a print server elsewhere, localhost on a Pi,
              a hand-built container that keeps its configuration private).
    For a path value, ``socket_exists`` means that path is a socket."""
    v = (value or "").strip()
    host, _port = _split_server(v)
    is_path = v.startswith("/")
    if socket_exists and (not v or is_path or host in SIDECAR_HOSTS):
        return "current"
    if not v or is_path:
        return "none"
    if host in SIDECAR_HOSTS and admin_conf_open:
        return "legacy"
    return "external"


async def _admin_conf_open(host: str, port: str) -> bool | None:
    """Whether the print service at host:port hands out its cupsd.conf to a
    request with no credentials (a 200). Any other answer is False. None when
    the request got no answer at all (refused, timed out, name not found).
    Never raises."""
    try:
        import httpx
        url = f"http://{host}:{port or '631'}/admin/conf/cupsd.conf"
        # trust_env=False: no proxy and no .netrc credentials, so the check
        # asks exactly the way an unknown program on the network would.
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT, follow_redirects=False,
                                     trust_env=False) as client:
            r = await client.get(url)
        return r.status_code == 200
    except Exception:
        return None


def _is_satellite() -> bool:
    try:
        from ..config import settings
        return bool(settings.is_satellite())
    except Exception:
        return False


async def refresh_print_service_state(*, max_age: float = PRINT_SERVICE_REFRESH_SECONDS) -> str:
    """Check the print service and cache the result. Never raises.

    A cached result younger than ``max_age`` seconds is returned as it is. A
    satellite prints through its own device and is always "external". The
    network check only runs for a sidecar host with no shared socket.

    A network check that gets no answer is inconclusive: the result is the
    last state this process knew ("external" when there is none), the inbox
    item is left as it is (see refresh_print_service_and_notice), and the
    check may run again after PRINT_SERVICE_RETRY_SECONDS."""
    now = _clock()
    cached, at = _print_service.get("value"), _print_service.get("at")
    if cached is not None and at is not None and now - at < max_age:
        return cached
    state = "external"
    inconclusive = False
    try:
        if not _is_satellite():
            value = (os.environ.get("CUPS_SERVER") or "").strip()
            host, port = _split_server(value)
            socket_path = value if value.startswith("/") else cups_socket_path()
            socket_exists = _is_socket(socket_path)
            admin_open: bool | None = False
            if host in SIDECAR_HOSTS and not socket_exists:
                admin_open = await _admin_conf_open(host, port)
            if admin_open is None:
                inconclusive = True
                state = cached or "external"
            else:
                state = classify_print_service(value, socket_exists, admin_open)
    except Exception:
        state = "external"
    done = _clock()
    _print_service["value"] = state
    _print_service["inconclusive"] = inconclusive
    if inconclusive:
        # Fresh for only PRINT_SERVICE_RETRY_SECONDS, for the cache and for
        # claim_print_service_refresh alike.
        _print_service["at"] = done - PRINT_SERVICE_REFRESH_SECONDS + PRINT_SERVICE_RETRY_SECONDS
        _print_service["claimed"] = None
    else:
        _print_service["at"] = done
    return state


def print_service_state() -> str:
    """The cached print service state, or "unknown" before the first check.
    No I/O, so a page can read it while it renders."""
    value = _print_service.get("value")
    return value if value else "unknown"


def claim_print_service_refresh() -> bool:
    """True at most once every ten minutes (and never on a satellite), for a
    caller that wants to start a background refresh. No I/O."""
    if _is_satellite():
        return False
    now = _clock()
    last = [t for t in (_print_service.get("at"), _print_service.get("claimed"))
            if t is not None]
    if last and now - max(last) < PRINT_SERVICE_REFRESH_SECONDS:
        return False
    _print_service["claimed"] = now
    return True


def print_service_fix_kind() -> str:
    """Which fix the Printing pane offers for an older print service. No I/O.

    compose: the app runs from the published docker-compose.prod.yml, which
             always gives the app WATCHTOWER_HTTP_API_TOKEN.
    source:  a copy built from source (its docker-compose.yml sets PR_DEV=1),
             fixed by updating the copy.
    manual:  anything else, such as a print server set up by hand."""
    if (os.environ.get("WATCHTOWER_HTTP_API_TOKEN") or "").strip():
        return "compose"
    if (os.environ.get("PR_DEV") or "").strip() == "1":
        return "source"
    return "manual"


def sync_print_service_notice(db, state: str) -> None:
    """Raise or clear the one "Update your print service" inbox item.

    "legacy" creates the item, or refreshes it without touching a snooze or
    an archive. Any other state archives an open or snoozed item and retires
    its dedupe key, so the notice clears itself once the service is updated
    and a later problem starts a fresh one."""
    from . import action_items
    from ..models.db_models import ActionItem
    if state == "legacy":
        action_items.create(
            db, PRINT_SERVICE_KIND, PRINT_SERVICE_TITLE,
            body=PRINT_SERVICE_BODY, dedupe_key=PRINT_SERVICE_DEDUPE_KEY,
            level="warning", payload={"url": PRINT_SERVICE_DOCS_URL})
        return
    rows = (
        db.query(ActionItem)
        .filter(ActionItem.kind == PRINT_SERVICE_KIND,
                ActionItem.dedupe_key.isnot(None))
        .all()
    )
    if not rows:
        return
    stamp = action_items._iso(action_items._now())
    for row in rows:
        if row.status in ("open", "snoozed"):
            row.status = "archived"
        row.dedupe_key = None
        row.updated_at = stamp
    db.commit()


async def refresh_print_service_and_notice() -> str:
    """Check the print service, then raise or clear its inbox item with a
    database session of its own. Never raises and never changes printing.

    A check that could not reach the print service leaves the item alone, so
    a print service that is still starting after a reboot never clears it."""
    try:
        state = await refresh_print_service_state()
    except Exception:
        return "unknown"
    if _print_service.get("inconclusive"):
        return state

    def _sync() -> None:
        from ..database import SessionLocal
        db = SessionLocal()
        try:
            sync_print_service_notice(db, state)
        finally:
            db.close()

    try:
        # A busy database never stalls the event loop while the notice syncs.
        import asyncio
        await asyncio.to_thread(_sync)
    except Exception:
        pass
    return state


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

    # A print job gets a longer timeout than other commands (FoodAssistant-
    # fu62): a sleeping Bluetooth printer's RFCOMM connect can take a while to
    # fail on its own, and a request needs a bound that gives it a real
    # chance while still guaranteeing the request eventually returns instead
    # of hanging indefinitely.
    _PRINT_TIMEOUT = 20.0
    # The is-the-print-stack-there probe gets a SHORT timeout of its own
    # (FoodAssistant-0hwez): it runs while the settings page is being built,
    # where 8 seconds of a wedged lpstat is a hung-looking page. A healthy
    # scheduler answers lpstat -r in milliseconds; anything slower reads as
    # unavailable and the pane shows its calm "needs enabling" note. Real
    # print jobs keep their own patient timeouts above.
    _AVAILABILITY_TIMEOUT = 1.5

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
        proc = self._run(["lpstat", "-r"], timeout=self._AVAILABILITY_TIMEOUT)
        if proc is None:
            return False
        out = (proc.stdout or b"").decode("utf-8", "replace").lower()
        return "scheduler is running" in out

    def _run(self, args: list[str], *, input_bytes: bytes | None = None,
             timeout: float | None = None) -> subprocess.CompletedProcess | None:
        """Run a command, returning the completed process or None if the binary
        is missing or the call fails to even start / times out. ``timeout``
        overrides the instance default for callers (like print_bytes) that
        need a different bound."""
        exe = shutil.which(args[0])
        if exe is None:
            return None
        try:
            return subprocess.run(
                [exe, *args[1:]],
                input=input_bytes,
                capture_output=True,
                timeout=timeout if timeout is not None else self._timeout,
                check=False,
                env=_cups_env(),
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

    def label_media(self, queue: str) -> list[dict]:
        """Media sizes ``queue`` advertises, as [{w_mm, h_mm, label, is_default}].

        Reads `lpoptions -p <queue> -l`. Never raises: an unknown queue,
        missing lpoptions, or a queue with no PageSize/media option returns
        []."""
        q = sanitize_queue_name(queue)
        if not q or shutil.which("lpoptions") is None:
            return []
        proc = self._run(["lpoptions", "-p", q, "-l"])
        if proc is None or proc.returncode != 0:
            return []
        return _parse_lpoptions_media(proc.stdout.decode("utf-8", "replace"))

    def print_bytes(self, queue: str, data: bytes, *, options: dict | None = None) -> PrintResult:
        if not queue:
            return PrintResult(ok=False, error="No printer queue selected.")
        # The command is built as an argv list (no shell), so a queue name cannot
        # inject a command, but reject anything that is not a plausible queue name
        # anyway: whitespace, control characters, or a leading dash (which could
        # be read as an lp option). Every queue this app adds already passes
        # sanitize_queue_name; this guards a queue that arrived some other way.
        if (queue != queue.strip() or queue.startswith("-")
                or any(c.isspace() or ord(c) < 0x20 for c in queue)):
            return PrintResult(ok=False, error="Invalid printer queue name.")
        exe = shutil.which("lp")
        if exe is None:
            return PrintResult(ok=False, error="Printing tools are not installed.")
        args = ["lp", "-d", queue]
        for key, value in (options or {}).items():
            args += ["-o", f"{key}={value}" if value != "" else str(key)]
        try:
            proc = subprocess.run(
                [exe, *args[1:]], input=data, capture_output=True,
                timeout=self._PRINT_TIMEOUT, check=False, env=_cups_env(),
            )
        except subprocess.TimeoutExpired:
            # A hung connect attempt (FoodAssistant-fu62): a powered-off
            # Bluetooth printer is the prime suspect, so classify it as
            # "offline" rather than a generic failure.
            return PrintResult(
                ok=False, error_kind="offline",
                error="The printer did not respond in time. It may be powered off.")
        except (OSError, subprocess.SubprocessError):
            return PrintResult(ok=False, error="Could not run the print command.")
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip() or "Print command failed."
            return PrintResult(ok=False, error=err, error_kind=classify_print_error(err))
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
    the app is unaffected on installs without a printer. Self-imported
    duplicates from cups-browsed fleet sharing are hidden (FoodAssistant-zq6b),
    so every consumer of this list sees the clean set."""
    try:
        queues = _backend.list_queues()
    except Exception:
        return []
    try:
        return dedupe_self_imports(queues, device_hostname(),
                                   _configured_queue_names())
    except Exception:
        return queues


def local_label_queue(device_choice: str = "") -> str:
    """The name of a label printer physically hosted on THIS device, or "".

    A device that has its own label printer (a Supvan Bluetooth printer the
    bridge auto-registered as a local CUPS queue, or an explicitly chosen local
    queue) prints to it directly instead of forwarding label jobs to the server
    (FoodAssistant-eml9 follow-up, Dan 2026-07-11: "whatever is most robust").
    The printer is right here, so bouncing the job up to the server and back
    over the network, with a hard dependency on the server being reachable to
    print a label, is fragile. An explicit device_choice that resolves to a real
    local queue wins; otherwise a Supvan queue is auto-detected by name. Cheap
    and best-effort; never raises."""
    try:
        names = [str(q.get("name", "")) for q in list_queues()]
    except Exception:
        return ""
    choice = (device_choice or "").strip()
    if choice and choice in names:
        return choice
    for n in names:
        if "supvan" in n.lower():
            return n
    return ""


def print_bytes(queue: str, data: bytes, *, options: dict | None = None) -> PrintResult:
    """Send bytes to a print queue. Never raises: returns a PrintResult with a
    structured error on any failure."""
    try:
        return _backend.print_bytes(queue, data, options=options)
    except Exception as exc:  # last-resort guard: the request path must survive
        return PrintResult(ok=False, error=f"Print failed: {exc}")


def label_media(queue: str) -> list[dict]:
    """Media sizes a queue advertises, as [{w_mm, h_mm, label, is_default}].

    Returns [] (never raises) when the queue is invalid, lpoptions is not
    present, or the queue advertises no concrete sizes."""
    try:
        return _backend.label_media(queue)
    except Exception:
        return []


def discover_printers() -> list[dict]:
    """Discover addable network printers on this device's CUPS as
    [{name, uri, kind, driver, info}]. Returns [] (never raises) when lpinfo or
    the print stack is not present."""
    try:
        return _backend.discover_printers()
    except Exception:
        return []


# A row from the Supvan bridge's index page:
#   <li><b>Supvan T50 Series T0148...</b> (<code>supvan_t50_series_t0148...</code>) ...
_SUPVAN_BRIDGE_ROW = re.compile(r"<li><b>(?P<name>.*?)</b>\s*\(<code>(?P<queue>[^<]+)</code>\)")

# A battery reading the bridge/status page may carry, e.g. "Battery: 45%",
# "battery 45 %", "Battery level: 45%". Many label printers report no battery
# at all, so this is best-effort and simply finds nothing when it is absent
# (FoodAssistant-oyt9).
_SUPVAN_BATTERY_RE = re.compile(r"batter(?:y|ie)[^0-9%]{0,20}?(\d{1,3})\s*%",
                                re.IGNORECASE)


def parse_supvan_battery(html: str) -> int | None:
    """The label-printer battery percentage from a Supvan bridge/status page, or
    None when the page reports none. Clamped to 0..100. Pure: text in, number or
    None out, so it tests without a live bridge and never invents a value."""
    m = _SUPVAN_BATTERY_RE.search(html or "")
    if not m:
        return None
    try:
        pct = int(m.group(1))
    except (TypeError, ValueError):
        return None
    if pct < 0 or pct > 100:
        return None
    return pct


def parse_supvan_bridge_page(html: str) -> list[tuple[str, str]]:
    """Parse a Supvan bridge index page into (printer name, queue id) pairs.

    The bridge (heeen ipp-printer-app) serves a small HTML list of its printer
    instances; each carries the human name and the IPP queue id. Pure so it
    tests without a live bridge."""
    import html as _html
    out: list[tuple[str, str]] = []
    for m in _SUPVAN_BRIDGE_ROW.finditer(html or ""):
        name = _html.unescape(m.group("name")).strip()
        queue = m.group("queue").strip()
        if name and queue:
            out.append((name, queue))
    return out


async def satellite_printers() -> list[dict]:
    """Printers hosted on satellites, offered to the main server by LAN address
    (FoodAssistant-h1ms).

    A satellite's Bluetooth label printer bridge advertises only over mDNS,
    which does not cross a Docker-hosted server's network, so the server never
    discovers it the normal way. The server already knows each satellite's LAN
    IP from its heartbeats, though, and can reach the bridge directly, so we
    probe each online satellite's bridge on port 8631 and offer its queues as
    ipp://<satellite-ip>:8631 printers the server can add and print to over the
    LAN. Best-effort and quiet: an unreachable or bridge-less satellite is
    simply skipped."""
    from . import devices
    try:
        rows = devices.list_devices()
    except Exception:
        return []
    import httpx
    out: list[dict] = []
    async with httpx.AsyncClient(timeout=4.0) as client:
        for dev in rows:
            ip = (dev.get("ip") or "").strip()
            # Only an online satellite hosts its own printers; skip loopback and
            # anything without a real LAN address.
            if (not ip or ip.startswith("127.") or not dev.get("online")
                    or dev.get("deployment_mode") != "pi_remote"):
                continue
            try:
                r = await client.get(f"http://{ip}:8631/")
                if r.status_code != 200:
                    continue
                pairs = parse_supvan_bridge_page(r.text)
                # Best-effort: some bridges expose a battery reading; surface it
                # when present, omit it entirely when not (FoodAssistant-oyt9).
                battery = parse_supvan_battery(r.text)
            except Exception:
                continue
            where = (dev.get("label") or dev.get("hostname") or ip).strip()
            # The queue name must be CUPS-safe (letters/digits/dash/underscore,
            # no spaces): the Add flow uses `name` verbatim as the queue name and
            # rejects anything else. The human string lives in `info`, which is
            # what the discovery card shows. The IP's last octet keeps names
            # distinct when two satellites share the default "foodassistant"
            # hostname.
            safe_where = re.sub(r"[^A-Za-z0-9]+", "_", where).strip("_") or "satellite"
            octet = ip.rsplit(".", 1)[-1]
            batt_suffix = f", battery {battery}%" if battery is not None else ""
            for name, queue in pairs:
                entry = {
                    "name": f"Supvan_{safe_where}_{octet}",
                    "uri": f"ipp://{ip}:8631/ipp/print/{queue}",
                    "kind": "driverless",
                    "driver": "everywhere",
                    "info": f"{name} on {where} (Bluetooth label printer{batt_suffix})",
                }
                if battery is not None:
                    entry["battery"] = battery
                out.append(entry)
    return out


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
