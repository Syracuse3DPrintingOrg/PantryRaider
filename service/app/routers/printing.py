"""Label and document printing endpoints (FoodAssistant-fb8x).

Device-local printing: food and spice labels to a small label printer, and
full-page recipe printouts to a regular document printer, both through CUPS.
This router is the thin request layer over two pure foundations already in the
tree: services/label_render.py (composes a label image) and services/printing.py
(the CUPS backend). It adds nothing to the rendering or the print path; it reads
what to print (an inventory item, a batch, some text, a recipe), builds the
right spec, and hands the bytes to the backend.

Printing is off by default and gated on settings.printing_enabled. Every
endpoint answers cleanly when printing is disabled or no print stack is
installed: a clear, user-facing message and a 4xx or a {ok: false} body, never a
500. There is no satellite forwarding here on purpose, printing happens on the
device the user is standing at. Each device prints to its own chosen queue when
it has one, otherwise to the fleet default the main server publishes and a
satellite inherits (resolve_effective_queue); LAN sharing plus cups-browsed make
a printer attached to any device reachable from all of them.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from ..config import settings
from ..services import bluetooth_print_setup, label_render, print_document, printing
from ..services.grocy import GrocyClient, GrocyError

router = APIRouter(prefix="/printing", tags=["printing"])


# -- Gate helpers -----------------------------------------------------------


def _printing_off_detail() -> Optional[str]:
    """A user-facing reason printing cannot run right now, or None when it can.

    Two gates: the master toggle must be on, and the device must actually have a
    print stack. Kept as one helper so every endpoint refuses the same way."""
    if not settings.printing_enabled:
        return ("Printing is turned off. Turn it on under Settings, Printing to "
                "print labels and recipes.")
    if not printing.printing_available():
        return ("No printer is set up on this device yet. Printing needs to be "
                "enabled on the device before labels can be sent.")
    return None


def _no_stack_detail() -> Optional[str]:
    """A reason discovery or adding a printer cannot run, or None.

    Discovery and add do NOT depend on the master toggle (you set printers up
    before turning printing on, exactly like the queue list). They only need a
    print system on the device, so this gates on availability alone."""
    if not printing.printing_available():
        return ("No printer service is set up on this device yet. Set printing "
                "up on the device first, then find or add a printer.")
    return None


def _effective_label_queue() -> str:
    """The label queue this device prints to (FoodAssistant-7u7z): its own local
    choice when set, otherwise the fleet default published by the main server and
    inherited by a satellite. See services.printing.resolve_effective_queue."""
    return printing.resolve_effective_queue(
        settings.label_printer_queue, settings.fleet_label_printer_queue)


def _effective_document_queue() -> str:
    """The document queue this device prints to: local choice, else the inherited
    fleet default."""
    return printing.resolve_effective_queue(
        settings.document_printer_queue, settings.fleet_document_printer_queue)


def _label_media_opts(width_in: float, height_in: float) -> dict:
    """Print options that pin the media to the label stock size (FoodAssistant-pbr9).

    Without a media size, CUPS rasterizes the label onto the queue's default
    media, which on a Supvan-style label printer produces a blank page (the
    content lands outside the wrong-sized page). Passing the exact stock size as
    a CUPS custom media keeps the whole label on the page. Sizes are the app's
    label dimensions in millimetres."""
    try:
        w = round(float(width_in or 0) * 25.4, 1)
        h = round(float(height_in or 0) * 25.4, 1)
    except (TypeError, ValueError):
        return {}
    if w <= 0 or h <= 0:
        return {}
    # Whole numbers read cleaner (40x30mm, not 40.0x30.0mm) and match a
    # driverless printer's advertised media exactly.
    ws = str(int(w)) if w == int(w) else str(w)
    hs = str(int(h)) if h == int(h) else str(h)
    return {"media": f"Custom.{ws}x{hs}mm"}


def _label_queue_detail() -> Optional[str]:
    """A reason a label cannot be printed (no queue chosen), or None."""
    off = _printing_off_detail()
    if off:
        return off
    if not _effective_label_queue():
        return "No label printer is chosen yet. Pick one under Settings, Printing."
    return None


def _document_queue_detail() -> Optional[str]:
    off = _printing_off_detail()
    if off:
        return off
    if not _effective_document_queue():
        return ("No document printer is chosen yet. Pick one under Settings, "
                "Printing.")
    return None


def _result_json(result: printing.PrintResult) -> dict:
    """A stable JSON body for a print attempt."""
    return {"ok": result.ok, "job_id": result.job_id, "error": result.error}


# -- Request models ---------------------------------------------------------


class LabelIn(BaseModel):
    """Print one food label. Either identify an inventory item by product_id
    (its name, added date, and best-by date are pulled from Grocy) or pass the
    fields directly. Explicit fields always win over the pulled ones, so a caller
    can tweak a single line. Size fields fall back to the configured label stock.
    """
    product_id: Optional[int] = None
    name: str = ""
    added: str = ""
    best_by: str = ""
    best_by_source: str = ""
    extra: str = ""
    width_in: Optional[float] = None
    height_in: Optional[float] = None
    dpi: Optional[int] = None
    show_logo: Optional[bool] = None


# A batch bigger than this needs an explicit confirm (FoodAssistant-np6o):
# printing a handful of labels is routine, printing dozens is easy to trigger
# by accident (a stray click, a huge import) and worth a second look first.
_BATCH_CONFIRM_THRESHOLD = 5


class BatchIn(BaseModel):
    product_ids: list[int] = []
    # A batch over BATCH_CONFIRM_THRESHOLD labels needs this set (FoodAssistant-
    # np6o): the client asks the user first and re-sends with confirmed: true.
    # Server-side so any caller gets the same protection, not just the app's
    # own UI.
    confirmed: bool = False


class DecorativeIn(BaseModel):
    text: str = ""
    width_in: Optional[float] = None
    height_in: Optional[float] = None
    dpi: Optional[int] = None
    bold: bool = True
    # Simple layout touches beyond plain centered text (FoodAssistant-nxr8):
    # an optional icon above the text (a key into label_render.ICON_GLYPHS)
    # and an optional hairline frame. Both off by default.
    icon: str = ""
    outline: bool = False


class AddPrinterIn(BaseModel):
    """Add a CUPS queue. ``name`` is the friendly queue name (letters, digits,
    dashes, underscores). ``connection`` is the device URI the UI builds (an
    ipp:// / ipps:// / dnssd:// address for a driverless printer, or
    socket://host:port for a raw network printer). ``model`` is the driver:
    "everywhere" for a driverless add, "raw" (or a driver name) for a socket
    add."""
    name: str = ""
    connection: str = ""
    model: str = "everywhere"


class RemovePrinterIn(BaseModel):
    name: str = ""


class DocumentIn(BaseModel):
    """Print a full page. Give a Mealie recipe slug, or raw html/text. A title
    is used for the text/html case (a recipe brings its own)."""
    recipe_slug: str = ""
    title: str = ""
    html: str = ""
    text: str = ""


# -- Spec building ----------------------------------------------------------


def _short_date(value: str) -> str:
    """Trim a Grocy timestamp to its date part for a compact label line."""
    return (value or "").split(" ")[0].split("T")[0].strip()


def _normalize_source(source: str) -> str:
    """Only the known best-by sources reach the renderer; anything else is
    treated as user-entered (no badge), so a stray value never prints garbage."""
    return source if source in ("manual", "default", "llm") else "manual"


async def _item_for_id(product_id: int) -> Optional[dict]:
    """The Grocy stock entry for a product id, or None. Raises GrocyError only on
    a backend failure (handled by the caller)."""
    entries = await GrocyClient().get_full_stock()
    for e in entries:
        if int(e.get("product_id") or 0) == int(product_id):
            return e
    return None


def _spec_from_fields(body: LabelIn, item: Optional[dict]) -> label_render.LabelSpec:
    """Build a LabelSpec, preferring explicit body fields over the pulled item.

    Best-by SOURCE (FoodAssistant-cidz): Grocy stores a best-by date but not
    where it came from. When the caller passes best_by_source explicitly, that
    always wins (a caller may stamp "est."/"AI" on the fly). Otherwise, for an
    item pulled from Grocy, services/best_by_provenance.py is consulted: it
    only answers when its recorded date still matches the item's CURRENT
    best-by, so a date the user has since edited directly in Grocy falls back
    to "manual" (no badge), the least-misleading choice. A label built from
    body fields alone (no Grocy item) also defaults to "manual"."""
    item = item or {}
    name = body.name.strip() or str(item.get("name") or "").strip()
    added = body.added.strip() or _short_date(item.get("added_date") or "")
    best_by = body.best_by.strip() or _short_date(item.get("best_before_date") or "")
    explicit_source = body.best_by_source.strip()
    if explicit_source:
        source = _normalize_source(explicit_source)
    elif item and best_by:
        from ..services import best_by_provenance
        source = best_by_provenance.lookup(item.get("product_id"), name, best_by)
    else:
        source = "manual"
    return label_render.LabelSpec(
        name=name,
        added=added,
        best_by=best_by,
        best_by_source=source,
        extra=body.extra.strip(),
        width_in=body.width_in or settings.label_width_in,
        height_in=body.height_in or settings.label_height_in,
        dpi=body.dpi or settings.label_dpi,
        show_logo=settings.label_show_logo if body.show_logo is None else body.show_logo,
    )


# -- Custom layout rendering ------------------------------------------------


def _values_from_spec(spec: label_render.LabelSpec) -> dict:
    """The values dict the layout engine consumes, built from a LabelSpec.

    The spec has no quantity/location of its own (those ride in ``extra``), so
    they default blank; a layout that binds them simply draws nothing."""
    return {
        "name": spec.name,
        "added": spec.added,
        "best_by": spec.best_by,
        "best_by_source": spec.best_by_source,
        "extra": spec.extra,
        "quantity": "",
        "location": "",
        "show_logo": spec.show_logo,
    }


def _saved_layout() -> Optional[label_render.LabelLayout]:
    """The user's saved custom label layout, or None when none is set or the
    stored JSON is unusable. Defensive: a malformed blob never breaks a print,
    it just falls back to the default renderer."""
    raw = (settings.label_layout_json or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    layout = label_render.LabelLayout.from_dict(data)
    if not layout.elements:
        return None
    return layout


def _render_label_image(spec: label_render.LabelSpec):
    """Render a food label to a PIL image, honouring a saved custom layout.

    When a custom layout is saved it drives the design, but the stock size still
    follows the spec (the configured label size), so a layout stays correct if
    the stock changes. With no custom layout, the shipped default renderer is
    used unchanged, so an untouched install prints exactly as before."""
    layout = _saved_layout()
    if layout is None:
        return label_render.render_label(spec)
    layout.width_in = spec.width_in
    layout.height_in = spec.height_in
    layout.dpi = spec.dpi
    layout.margin_in = spec.margin_in
    return label_render.render_layout(layout, _values_from_spec(spec))


# -- Endpoints --------------------------------------------------------------


_HOST_BRIDGE = "http://127.0.0.1:9299"


@router.post("/install")
async def install_print_stack():
    """Turn a device that has no print stack into one that can print.

    On a Pi (Pi Hosted or Pi Remote) this asks the on-device helper to install
    CUPS, Bluetooth, and a generic driver set, then point the app at the local
    print server. The install is safe to run more than once and does not touch
    anything until you ask for it here.

    On a plain server the app cannot install system packages for you, so this
    returns clear steps to bring up the printing service instead. Either way the
    response is user-facing: {ok, message, log?}."""
    if printing.printing_available():
        return {"ok": True, "message": "Printing is already set up on this device."}

    if settings.is_pi_appliance():
        from ..services.bridge import bridge_client
        try:
            # Installing packages can take a couple of minutes on a Pi; give the
            # bridge call room so a slow apt run is not cut off.
            async with bridge_client(timeout=610.0) as client:
                r = await client.post(f"{_HOST_BRIDGE}/print-setup")
            body = r.json()
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"ok": False, "message":
                 "Could not reach the device helper to set up printing. Make "
                 f"sure the device is fully started, then try again. ({e})"},
                status_code=502,
            )
        if r.status_code == 200 and body.get("ok"):
            return {"ok": True, "message":
                    "Printing is set up. Pick your label and document printers "
                    "below, then turn printing on.", "log": body.get("log", "")}
        return JSONResponse(
            {"ok": False, "message": body.get(
                "error", "The device could not finish setting up printing."),
             "log": body.get("log", "")},
            status_code=502,
        )

    # Server (Docker) mode: the app runs in a container and cannot install a
    # print server on the host for you. Point the user at the two ways to add
    # one. User-forward copy, no builder-side phrasing.
    return {"ok": False, "message":
            "To print on this server, first start the built-in print service, "
            "then add your printers right here in the app. Start the service by "
            "running this one command on the server, in the folder with your "
            "docker-compose.yml:\n"
            "  CUPS_SERVER=cups:631 docker compose --profile with-printing up -d\n"
            "Then use Find printers or Add by address in the Add a printer panel "
            "to add each one. Already running CUPS elsewhere? Set CUPS_SERVER to "
            "its address in your .env and restart the app instead."}


_BLUETOOTH_UNSUPPORTED_MESSAGE = (
    "Bluetooth label printer setup runs on a Pantry Raider appliance today. "
    "On this server, connect a label printer over the network instead, "
    "using Find printers or Add by address above.")


async def _run_bluetooth_print_setup() -> None:
    """Background task: ask the bridge to install the Supvan Bluetooth bridge
    and record the outcome (FoodAssistant-h2j6).

    Runs after the setup endpoint has already answered, so the caller does
    not wait through the on-device build. The bridge call itself may block for
    a long time on first run (compiling the Supvan printer-app), hence the
    generous timeout here to match the bridge's own.
    """
    from ..services.bridge import bridge_client
    try:
        async with bridge_client(timeout=1210.0) as client:
            r = await client.post(f"{_HOST_BRIDGE}/print-setup", json={"bluetooth": True})
        body = r.json()
    except Exception as e:  # noqa: BLE001
        bluetooth_print_setup.mark_result(
            False, f"Could not reach the device helper: {e}")
        return
    if r.status_code == 200 and body.get("ok"):
        bluetooth_print_setup.mark_result(True, body.get("log", ""))
    else:
        bluetooth_print_setup.mark_result(
            False, body.get("error", "") or body.get("log", "") or
            "The device could not finish setting up the Bluetooth printer.")


@router.post("/bluetooth/setup")
async def bluetooth_print_start():
    """Start setting up a Supvan T50M-family Bluetooth label printer.

    Installs bluez plus the Supvan CUPS printer-app on a Pi appliance, so a
    paired T50M shows up afterward as a normal network printer for Find
    printers. The first run compiles the bridge from source on the device,
    which can take several minutes; this kicks that off in the background and
    returns at once. Poll GET /printing/bluetooth/status for progress.
    Server mode has no on-device helper to run this against, so it answers
    with guidance instead of starting anything."""
    if not settings.is_pi_appliance():
        return {"ok": False, "message": _BLUETOOTH_UNSUPPORTED_MESSAGE}
    if bluetooth_print_setup.is_installing():
        return {"ok": True, "message":
                "Bluetooth printer setup is already running on this device."}
    bluetooth_print_setup.mark_installing()
    asyncio.create_task(_run_bluetooth_print_setup())
    return {"ok": True, "message":
            "Setting up the Bluetooth label printer. The first setup can "
            "take several minutes while it prepares the printer software."}


@router.get("/bluetooth/status")
async def bluetooth_print_status():
    """Bluetooth label printer setup status: not_set_up / installing / ready
    / failed, plus a log tail on failure. Pi appliance only; the Printing pane
    polls this while setup runs, and afterward to confirm the Supvan bridge
    service is actually alive (a helper run that reports success does not by
    itself guarantee the printer service came up)."""
    if not settings.is_pi_appliance():
        return {"status": "unsupported", "message": _BLUETOOTH_UNSUPPORTED_MESSAGE}
    state = bluetooth_print_setup.current()
    live_active: Optional[bool] = None
    try:
        from ..services.bridge import bridge_client
        async with bridge_client(timeout=8.0) as client:
            r = await client.get(f"{_HOST_BRIDGE}/print-setup/status")
        if r.status_code == 200:
            live_active = bool(r.json().get("supvan_active"))
    except Exception:  # noqa: BLE001
        live_active = None
    return bluetooth_print_setup.resolve_status(state, live_active)


@router.get("/queues")
def queues():
    """CUPS print queues on this device, for the settings pane's dropdowns.

    Never raises: an empty list means no print stack (or no queues). The master
    toggle does not gate discovery, so the settings pane can list queues while
    the feature is still being turned on."""
    return {"available": printing.printing_available(), "queues": printing.list_queues()}


@router.get("/label-media")
def label_media(queue: Optional[str] = None):
    """Media sizes the label queue advertises, in mm, for the Label size panel's
    "Use a size my printer supports" picker (FoodAssistant-u55y).

    Defaults to the effective label queue (this device's choice, else the
    fleet default) when ``queue`` is not given. Never raises: an unset or
    unreachable queue simply returns an empty list, so the picker can show
    "no sizes found" instead of an error."""
    q = (queue or "").strip() or _effective_label_queue()
    return {"queue": q, "sizes": printing.label_media(q) if q else []}


async def _bridge_json(path: str, payload: Optional[dict] = None, *, timeout: float = 60.0):
    """POST to a host-bridge endpoint with the shared token, returning
    (status_code, body_dict). Raises on a transport failure so the caller can
    answer with a clean, user-facing message. Mirrors the install path."""
    from ..services.bridge import bridge_client
    async with bridge_client(timeout=timeout) as client:
        r = await client.post(f"{_HOST_BRIDGE}{path}", json=payload or {})
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = {}
    return r.status_code, body


@router.get("/discover")
async def discover_printers():
    """Find network printers this device could add.

    On a Pi appliance the privileged discovery runs through the on-device helper
    (lpinfo needs to reach the host CUPS as root); on a plain server it runs
    against the local print service. Returns {ok, printers:[{name, uri, kind,
    driver}], message?}. Never 500s: a device with no print service yet gets a
    clean, user-facing note and an empty list."""
    detail = _no_stack_detail()
    if detail:
        return {"ok": False, "printers": [], "message": detail}
    if settings.is_pi_appliance():
        try:
            status, body = await _bridge_json("/printer-discover", timeout=60.0)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"ok": False, "printers": [], "message":
                 "Could not reach the device helper to look for printers. Make "
                 f"sure the device is fully started, then try again. ({e})"},
                status_code=502)
        if status == 200 and body.get("ok"):
            return {"ok": True, "printers": await _with_satellite_printers(
                body.get("printers", []))}
        return JSONResponse(
            {"ok": False, "printers": [], "message": body.get(
                "error", "The device could not look for printers just now.")},
            status_code=502)
    return {"ok": True, "printers": await _with_satellite_printers(
        printing.discover_printers())}


async def _with_satellite_printers(local: list[dict]) -> list[dict]:
    """Append printers hosted on this server's satellites (FoodAssistant-h1ms).

    Only the main server does this: a satellite has no satellites of its own,
    and it forwards its printer views upstream anyway. Best-effort, so a slow
    or offline satellite never blocks local discovery."""
    if settings.is_satellite():
        return local
    try:
        return local + await printing.satellite_printers()
    except Exception:
        return local


@router.post("/add")
async def add_printer(body: AddPrinterIn):
    """Add a printer as a CUPS queue so it shows up in the label and document
    lists. Pi appliance goes through the on-device helper (lpadmin needs root);
    a plain server adds it locally. Returns {ok, error?, message}; never 500s,
    and refuses an invalid name or address with a clean 400."""
    detail = _no_stack_detail()
    if detail:
        raise HTTPException(409, detail)
    if not printing.sanitize_queue_name(body.name):
        raise HTTPException(400, "Give the printer a name using letters, digits, "
                                 "dashes, or underscores, with no spaces.")
    if not printing.valid_connection(body.connection):
        raise HTTPException(400, "That printer address does not look right. Check "
                                 "the host or IP address and try again.")
    model = (body.model or "").strip() or "raw"
    if settings.is_pi_appliance():
        payload = {"name": body.name, "connection": body.connection, "model": model}
        try:
            status, resp = await _bridge_json("/printer-add", payload, timeout=120.0)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"ok": False, "message":
                 "Could not reach the device helper to add the printer. Make "
                 f"sure the device is fully started, then try again. ({e})"},
                status_code=502)
        if status == 200 and resp.get("ok"):
            return {"ok": True, "message": f"Added {body.name}. It is ready to pick below."}
        return JSONResponse(
            {"ok": False, "message": resp.get(
                "error", "The device could not add that printer.")},
            status_code=502)
    result = printing.add_printer(body.name, body.connection, model)
    if result.ok:
        return {"ok": True, "message": f"Added {body.name}. It is ready to pick below."}
    return JSONResponse({"ok": False, "message": result.error}, status_code=502)


@router.post("/remove")
async def remove_printer(body: RemovePrinterIn):
    """Remove a printer queue this device added. Pi goes through the helper; a
    server removes it locally. Never 500s."""
    detail = _no_stack_detail()
    if detail:
        raise HTTPException(409, detail)
    if not printing.sanitize_queue_name(body.name):
        raise HTTPException(400, "That printer name is not valid.")
    if settings.is_pi_appliance():
        try:
            status, resp = await _bridge_json(
                "/printer-remove", {"name": body.name}, timeout=60.0)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"ok": False, "message":
                 f"Could not reach the device helper to remove the printer. ({e})"},
                status_code=502)
        if status == 200 and resp.get("ok"):
            return {"ok": True, "message": f"Removed {body.name}."}
        return JSONResponse(
            {"ok": False, "message": resp.get(
                "error", "The device could not remove that printer.")},
            status_code=502)
    result = printing.remove_printer(body.name)
    if result.ok:
        return {"ok": True, "message": f"Removed {body.name}."}
    return JSONResponse({"ok": False, "message": result.error}, status_code=502)


@router.post("/label")
async def print_label(body: LabelIn):
    """Render one food label and send it to the label printer."""
    detail = _label_queue_detail()
    if detail:
        raise HTTPException(409, detail)
    item = None
    if body.product_id is not None:
        try:
            item = await _item_for_id(body.product_id)
        except GrocyError as e:
            raise HTTPException(502, f"Could not read the item from Grocy: {e}")
        if item is None:
            raise HTTPException(404, "That item is no longer in stock.")
    spec = _spec_from_fields(body, item)
    if not spec.name:
        raise HTTPException(400, "A label needs at least a name.")
    png = label_render.render_to_png_bytes(_render_label_image(spec))
    result = printing.print_bytes(
        _effective_label_queue(), png,
        options=_label_media_opts(spec.width_in, spec.height_in))
    return JSONResponse(_result_json(result), status_code=200 if result.ok else 502)


@router.post("/label/preview")
async def preview_label(body: LabelIn):
    """The rendered label as a PNG, so the UI can show it without printing.

    Preview is allowed even when printing is off: it is just an image, and the
    settings pane shows a live preview while the feature is being set up."""
    item = None
    if body.product_id is not None:
        try:
            item = await _item_for_id(body.product_id)
        except GrocyError:
            item = None
    spec = _spec_from_fields(body, item)
    if not spec.name:
        spec.name = "Sample label"
    png = label_render.render_to_png_bytes(_render_label_image(spec))
    return Response(content=png, media_type="image/png")


# -- Layout designer (FoodAssistant-bwl1 / -or5e) ---------------------------


class LayoutPreviewIn(BaseModel):
    """A layout to preview. ``layout`` is a LabelLayout dict (as the designer
    edits it); ``values`` are optional sample field values (name, added,
    best_by, best_by_source, extra, quantity, location). Missing values fall
    back to a built-in sample so the preview always shows something."""
    layout: dict[str, Any] = {}
    values: dict[str, Any] = {}


class LayoutSaveIn(BaseModel):
    """Save a custom label layout. ``layout`` is a LabelLayout dict; an empty
    dict (or no elements) clears the custom layout and returns to the default
    renderer."""
    layout: dict[str, Any] = {}


_SAMPLE_VALUES = {
    "name": "Chicken Stock",
    "added": "2026-07-08",
    "best_by": "2026-07-22",
    "best_by_source": "default",
    "extra": "Fridge, 2 cups",
    "quantity": "2 cups",
    "location": "Fridge",
}


@router.get("/label/presets")
def label_presets():
    """Common label formats for the size chooser and the designer.

    Each entry carries its key, name, size, dpi, and a starting layout dict. A
    read-only list, safe to call any time (printing off or no printer): the
    designer UI populates its format dropdown from this and applies a preset's
    size and starting layout client-side."""
    return {"presets": label_render.presets_detail()}


@router.post("/label/layout/preview")
def preview_layout(body: LayoutPreviewIn):
    """Render an arbitrary layout to a PNG with sample data, for a live preview.

    Takes the layout the designer is editing plus optional sample values and
    returns the rendered label image, so the UI can show changes without saving
    or printing. Never 500s on a malformed layout: it is validated (unknown
    fields dropped, fractions clamped, bad elements skipped) and a completely
    unusable body returns a clean 400."""
    if not isinstance(body.layout, dict) or not body.layout:
        raise HTTPException(400, "Send a layout to preview.")
    try:
        layout = label_render.LabelLayout.from_dict(body.layout)
    except Exception:  # noqa: BLE001 - validation must never surface a 500
        raise HTTPException(400, "That layout could not be read. Check the "
                                 "layout and try again.")
    values = {**_SAMPLE_VALUES, **(body.values or {})}
    try:
        img = label_render.render_layout(layout, values)
        png = label_render.render_to_png_bytes(img)
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "That layout could not be rendered. Check the "
                                 "layout and try again.")
    return Response(content=png, media_type="image/png")


@router.post("/label/layout")
def save_layout(body: LayoutSaveIn):
    """Save (or clear) the custom label layout.

    Validates the layout, normalizes it through the layout engine (dropping
    unknown fields and malformed elements), and stores it as JSON in settings.
    An empty layout, or one with no valid elements, clears the custom design so
    the label path returns to the default renderer. Never 500s on a bad body: it
    returns a clean 400 and leaves the saved layout untouched."""
    if not isinstance(body.layout, dict):
        raise HTTPException(400, "Send a layout to save.")
    # An empty layout clears the custom design.
    if not body.layout or not body.layout.get("elements"):
        settings.save({"label_layout_json": ""})
        return {"ok": True, "cleared": True, "elements": 0}
    try:
        layout = label_render.LabelLayout.from_dict(body.layout)
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "That layout could not be read. Check the "
                                 "layout and try again.")
    if not layout.elements:
        # Nothing valid survived normalization: refuse rather than silently
        # saving a blank design that would render an empty label.
        raise HTTPException(400, "That layout has no usable fields on it.")
    settings.save({"label_layout_json": json.dumps(layout.to_dict())})
    return {"ok": True, "cleared": False, "elements": len(layout.elements)}


# -- Saved layout presets (FoodAssistant-rhqa) -------------------------------
#
# A small named library of label designs, separate from the single "current"
# design saved above. The designer can save the design it is working on under
# a name, then load or delete it later, so a user can keep a couple of label
# designs (say, a compact fridge label and a detailed pantry label) and
# switch between them without rebuilding from scratch.


class LayoutPresetSaveIn(BaseModel):
    """Save (or overwrite) a named layout preset. ``layout`` is a LabelLayout
    dict, same shape as LayoutSaveIn."""
    name: str = ""
    layout: dict[str, Any] = {}


class LayoutPresetDeleteIn(BaseModel):
    name: str = ""


def _saved_layout_presets() -> list[dict]:
    return label_render.layout_presets_from_json(settings.label_layout_presets or "")


@router.get("/label/layout/presets")
def list_layout_presets():
    """The user's saved named layout designs (name + layout, no size fields
    of their own; the designer applies the current stock size on load)."""
    return {"presets": _saved_layout_presets()}


@router.post("/label/layout/presets")
def save_layout_preset(body: LayoutPresetSaveIn):
    """Save the given layout under a name, replacing any preset with the same
    name. Never 500s on a bad body: a missing name or an unusable layout
    returns a clean 400 and leaves the saved presets untouched."""
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "Give this design a name.")
    if not isinstance(body.layout, dict) or not body.layout.get("elements"):
        raise HTTPException(400, "That layout has no usable fields on it.")
    layout = label_render.LabelLayout.from_dict(body.layout)
    if not layout.elements:
        raise HTTPException(400, "That layout has no usable fields on it.")
    presets = [p for p in _saved_layout_presets() if p["name"] != name.strip()[:60]]
    presets.append({"name": name, "layout": layout.to_dict()})
    presets = label_render.validate_layout_presets(presets)
    settings.save({"label_layout_presets": label_render.layout_presets_to_json(presets)})
    return {"ok": True, "presets": presets}


@router.post("/label/layout/presets/delete")
def delete_layout_preset(body: LayoutPresetDeleteIn):
    """Remove a saved preset by name. Removing one that does not exist is a
    no-op that still returns the (unchanged) current list."""
    name = (body.name or "").strip()
    presets = [p for p in _saved_layout_presets() if p["name"] != name]
    settings.save({"label_layout_presets": label_render.layout_presets_to_json(presets)})
    return {"ok": True, "presets": presets}


@router.post("/label/batch")
async def print_label_batch(body: BatchIn):
    """Print a label for each item in a batch (e.g. a just-imported stock run).

    Renders one multi-page PDF, one label per page, and prints it as a single
    job so the labels come off the printer together."""
    detail = _label_queue_detail()
    if detail:
        raise HTTPException(409, detail)
    ids = [int(i) for i in body.product_ids if i is not None]
    if not ids:
        raise HTTPException(400, "No items were selected to print.")
    if len(ids) > _BATCH_CONFIRM_THRESHOLD and not body.confirmed:
        # A big batch runs the label printer for a while and burns through
        # stock; ask before sending it (FoodAssistant-np6o). A structured
        # response (not a 4xx) so any caller, not just the app's own JS, can
        # show the count and re-send with confirmed: true.
        return {"ok": False, "needs_confirmation": True, "count": len(ids)}
    try:
        entries = await GrocyClient().get_full_stock()
    except GrocyError as e:
        raise HTTPException(502, f"Could not read stock from Grocy: {e}")
    by_id = {int(e.get("product_id") or 0): e for e in entries}
    specs = []
    missing = 0
    for pid in ids:
        item = by_id.get(pid)
        if not item:
            missing += 1
            continue
        specs.append(_spec_from_fields(LabelIn(product_id=pid), item))
    if not specs:
        raise HTTPException(404, "None of those items are in stock any more.")
    pdf = label_render.render_batch_pdf_bytes(specs)
    result = printing.print_bytes(_effective_label_queue(), pdf)
    body_out = {**_result_json(result), "printed": len(specs), "skipped": missing}
    return JSONResponse(body_out, status_code=200 if result.ok else 502)


@router.post("/decorative")
async def print_decorative(body: DecorativeIn):
    """Print a dateless decorative label (spice jars, canisters, bins)."""
    detail = _label_queue_detail()
    if detail:
        raise HTTPException(409, detail)
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Enter the text for the label.")
    w_in = body.width_in or settings.label_width_in
    h_in = body.height_in or settings.label_height_in
    img = label_render.render_decorative_label(
        text,
        width_in=w_in,
        height_in=h_in,
        dpi=body.dpi or settings.label_dpi,
        bold=body.bold,
        icon=body.icon,
        outline=body.outline,
    )
    png = label_render.render_to_png_bytes(img)
    result = printing.print_bytes(
        _effective_label_queue(), png, options=_label_media_opts(w_in, h_in))
    return JSONResponse(_result_json(result), status_code=200 if result.ok else 502)


@router.post("/decorative/preview")
async def preview_decorative(body: DecorativeIn):
    """A decorative label as a PNG for the live preview. Allowed while off."""
    text = body.text.strip() or "Sample"
    img = label_render.render_decorative_label(
        text,
        width_in=body.width_in or settings.label_width_in,
        height_in=body.height_in or settings.label_height_in,
        dpi=body.dpi or settings.label_dpi,
        bold=body.bold,
        icon=body.icon,
        outline=body.outline,
    )
    png = label_render.render_to_png_bytes(img)
    return Response(content=png, media_type="image/png")


@router.get("/decorative/icons")
def decorative_icons():
    """The curated icon/symbol keys the decorative label (and the field
    designer's icon element) can place, for the UI's icon picker. Read-only,
    safe to call any time."""
    return {"icons": [{"key": k, "glyph": v} for k, v in label_render.ICON_GLYPHS.items()]}


@router.post("/document")
async def print_document_route(body: DocumentIn):
    """Print a full-page document to the document printer.

    Three inputs, in order of preference: a Mealie recipe slug (pulled and
    formatted), raw html, or plain text. The formatting lives in the pure
    print_document helper so the shaping is testable without a printer."""
    detail = _document_queue_detail()
    if detail:
        raise HTTPException(409, detail)

    if body.recipe_slug.strip():
        from ..services import current_recipe
        try:
            raw = await _mealie_get_recipe(body.recipe_slug.strip())
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"Could not load that recipe: {e}")
        if not raw:
            raise HTTPException(404, "That recipe could not be found.")
        recipe = current_recipe.from_mealie_detail(raw, body.recipe_slug.strip())
        pdf = print_document.render_recipe_pdf_bytes(recipe)
    elif body.html.strip():
        text = print_document.html_to_text(body.html)
        blocks = print_document.text_to_blocks(text, title=body.title)
        pdf = print_document.render_document_pdf_bytes(blocks)
    elif body.text.strip():
        blocks = print_document.text_to_blocks(body.text, title=body.title)
        pdf = print_document.render_document_pdf_bytes(blocks)
    else:
        raise HTTPException(400, "Nothing to print: pass a recipe, some text, or HTML.")

    options = print_document.document_print_options(
        settings.document_page_size, settings.document_color_mode,
        settings.document_duplex)
    result = printing.print_bytes(_effective_document_queue(), pdf, options=options)
    return JSONResponse(_result_json(result), status_code=200 if result.ok else 502)


async def _mealie_get_recipe(slug: str) -> Optional[dict]:
    """Fetch a recipe detail by slug from the active recipe backend, or None.

    Native mode reads recipe_store (which answers the same Mealie-detail
    shape the document formatter consumes), so recipe printing works with no
    Mealie at all; the Mealie branch is unchanged for installs still running
    their library there.
    """
    from ..services import recipe_source
    if recipe_source.active_backend() == recipe_source.BACKEND_NATIVE:
        from ..database import SessionLocal
        from ..services import recipe_store
        db = SessionLocal()
        try:
            return recipe_store.detail(db, slug)
        finally:
            db.close()
    if not settings.mealie_api_key:
        return None
    from ..services.mealie import MealieClient
    return await MealieClient().get_recipe(slug)
