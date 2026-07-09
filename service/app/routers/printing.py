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

from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from ..config import settings
from ..services import label_render, print_document, printing
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


class BatchIn(BaseModel):
    product_ids: list[int] = []


class DecorativeIn(BaseModel):
    text: str = ""
    width_in: Optional[float] = None
    height_in: Optional[float] = None
    dpi: Optional[int] = None
    bold: bool = True


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

    Best-by SOURCE caveat: Grocy stores a best-by date but not where it came
    from (a typed date, a category-rule estimate, or an AI guess). At print time
    we cannot tell them apart, so an item-derived label defaults to "manual" (no
    badge), the least-misleading choice, and a caller may pass best_by_source
    explicitly to stamp "est." or "AI"."""
    item = item or {}
    name = body.name.strip() or str(item.get("name") or "").strip()
    added = body.added.strip() or _short_date(item.get("added_date") or "")
    best_by = body.best_by.strip() or _short_date(item.get("best_before_date") or "")
    source = _normalize_source(body.best_by_source.strip())
    return label_render.LabelSpec(
        name=name,
        added=added,
        best_by=best_by,
        best_by_source=source,
        extra=body.extra.strip(),
        width_in=body.width_in or settings.label_width_in,
        height_in=body.height_in or settings.label_height_in,
        dpi=body.dpi or settings.label_dpi,
    )


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
            "To print on this server, add a print service. The quickest way is "
            "to start the built-in printing service:\n"
            "  CUPS_SERVER=cups:631 docker compose --profile with-printing up -d\n"
            "then add your printer in the CUPS admin at http://<this-server>:6631. "
            "If you already run CUPS elsewhere, set CUPS_SERVER to its address in "
            "your .env instead and restart the app. Your printers then appear in "
            "the lists below."}


@router.get("/queues")
def queues():
    """CUPS print queues on this device, for the settings pane's dropdowns.

    Never raises: an empty list means no print stack (or no queues). The master
    toggle does not gate discovery, so the settings pane can list queues while
    the feature is still being turned on."""
    return {"available": printing.printing_available(), "queues": printing.list_queues()}


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
            return {"ok": True, "printers": body.get("printers", [])}
        return JSONResponse(
            {"ok": False, "printers": [], "message": body.get(
                "error", "The device could not look for printers just now.")},
            status_code=502)
    return {"ok": True, "printers": printing.discover_printers()}


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
    png = label_render.render_to_png_bytes(label_render.render_label(spec))
    result = printing.print_bytes(_effective_label_queue(), png)
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
    png = label_render.render_to_png_bytes(label_render.render_label(spec))
    return Response(content=png, media_type="image/png")


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
    img = label_render.render_decorative_label(
        text,
        width_in=body.width_in or settings.label_width_in,
        height_in=body.height_in or settings.label_height_in,
        dpi=body.dpi or settings.label_dpi,
        bold=body.bold,
    )
    png = label_render.render_to_png_bytes(img)
    result = printing.print_bytes(_effective_label_queue(), png)
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
    )
    png = label_render.render_to_png_bytes(img)
    return Response(content=png, media_type="image/png")


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

    result = printing.print_bytes(_effective_document_queue(), pdf)
    return JSONResponse(_result_json(result), status_code=200 if result.ok else 502)


async def _mealie_get_recipe(slug: str) -> Optional[dict]:
    """Fetch a Mealie recipe detail by slug, or None when Mealie is not set up."""
    if not settings.mealie_api_key:
        return None
    from ..services.mealie import MealieClient
    return await MealieClient().get_recipe(slug)
