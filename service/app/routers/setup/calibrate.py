"""Touch calibration and the kiosk navigate-to-dashboard handoff.

These endpoints let the web UI stream raw touch events and apply a computed
calibration matrix. Only meaningful on Pi Remote (venv service) where the
process can access /dev/input/event* directly (service user in input group).
"""

import asyncio
import fcntl
import json
import os
import re
import select
import struct
import threading
from pathlib import Path
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from ...config import settings
from ...hardware import is_raspberry_pi
from ...services.bridge import bridge_client
from ...templating import templates
from ._common import router, _HOST_BRIDGE, _KIOSK_NAV_FLAG, _write_kiosk_nav_flag
from .page import TestProviderPayload
from .connections import test_provider


# Touch calibration
# ------------------
# These endpoints let the web UI stream raw touch events and apply a computed
# calibration matrix. Only meaningful on Pi Remote (venv service) where the
# process can access /dev/input/event* directly (service user in input group).

# Substrings of common touch-controller device names that do NOT contain the
# word "touch" (capacitive and resistive panels alike), so name-matching catches
# them too. The capability check below is the real backstop for anything unnamed.
_TOUCH_CONTROLLER_HINTS = (
    "touch", "ads7846", "goodix", "ft5x", "ft6", "edt-ft5", "edt_ft5", "ektf",
    "ilitek", "ili210", "ili251", "silead", "eeti", "egalax", "hynitron",
    "st1633", "gslx680", "stmpe", "raspberrypi-ts", "waveshare", "hosyond",
    "chipone", "icn85", "cst", "zforce",
)


def _block_handler(block: str) -> str | None:
    m = re.search(r"Handlers=.*?(event\d+)", block)
    return "/dev/input/" + m.group(1) if m else None


def _looks_like_touchscreen(block: str) -> bool:
    """True when a /proc/bus/input/devices block is a touchscreen.

    Two signals, either is enough: the device name matches a known controller
    (covers panels that do not say "touch"), or the kernel flags it as a direct
    absolute pointer, i.e. INPUT_PROP_DIRECT (PROP bit 1) plus absolute axes.
    The capability check is what catches an oddly named or generic panel."""
    low = block.lower()
    if any(hint in low for hint in _TOUCH_CONTROLLER_HINTS):
        return True
    prop = re.search(r"^B: PROP=([0-9a-fA-F]+)", block, re.M)
    has_abs = re.search(r"^B: ABS=[0-9a-fA-F]", block, re.M) is not None
    # INPUT_PROP_DIRECT (bit 1) marks a screen-mapped device (a touchscreen),
    # versus an indirect one like a touchpad; require absolute axes too.
    return bool(prop) and bool(int(prop.group(1), 16) & 0x2) and has_abs


def _find_touch_device() -> str | None:
    """Return the first /dev/input/eventN that looks like a touchscreen."""
    try:
        blocks = open("/proc/bus/input/devices").read().split("\n\n")
    except OSError:
        return None
    for b in blocks:
        if _looks_like_touchscreen(b):
            h = _block_handler(b)
            if h:
                return h
    return None


# Linux input-event constants (linux/input-event-codes.h).
_EV_KEY = 0x01
_EV_ABS = 0x03
_ABS_X = 0x00
_ABS_Y = 0x01
_BTN_TOUCH = 0x14A
# struct input_event: a timeval (two longs) then type/code (u16) and value
# (s32). Native sizing matches the running kernel's word size (8-byte longs on
# a 64-bit kernel, 4-byte on 32-bit), so this adapts without per-arch branching.
_INPUT_EVENT_FORMAT = "llHHi"
_INPUT_EVENT_SIZE = struct.calcsize(_INPUT_EVENT_FORMAT)
# struct input_absinfo: six s32 (value, min, max, fuzz, flat, resolution).
_ABSINFO_FORMAT = "6i"
_ABSINFO_SIZE = struct.calcsize(_ABSINFO_FORMAT)


def _eviocgabs(axis: int) -> int:
    """ioctl request number for EVIOCGABS(axis): _IOR('E', 0x40+axis, absinfo)."""
    return (2 << 30) | (_ABSINFO_SIZE << 16) | (ord("E") << 8) | (0x40 + axis)


def _abs_axis(fd: int, axis: int, default_max: int = 4095) -> tuple[int | None, int, int]:
    """Return (value, min, max) for an absolute axis via EVIOCGABS.

    value is the axis position the device last reported (None when the ioctl
    fails); min/max fall back to a sane default range.
    """
    try:
        buf = bytearray(_ABSINFO_SIZE)
        fcntl.ioctl(fd, _eviocgabs(axis), buf, True)
        value, minimum, maximum, _fuzz, _flat, _res = struct.unpack(_ABSINFO_FORMAT, bytes(buf))
        if maximum > minimum:
            return value, minimum, maximum
    except OSError:
        pass
    return None, 0, default_max


def _fold_touch_events(data: bytes, x: int | None, y: int | None):
    """Fold a raw evdev read buffer into completed taps.

    Returns (taps, x, y): one (x, y) tuple per BTN_TOUCH release seen in the
    buffer, plus the updated last-known axis values to carry into the next
    read. The position is deliberately carried across taps rather than reset:
    the kernel input core suppresses ABS events whose value did not change, so
    a tap in line with the previous contact (same raw X or Y) arrives with no
    fresh coordinate for that axis. Resetting to None after each tap dropped
    exactly those releases (FoodAssistant-9ext: tap on a crosshair silently
    not registered).
    """
    taps: list[tuple[int, int]] = []
    for off in range(0, len(data) - _INPUT_EVENT_SIZE + 1, _INPUT_EVENT_SIZE):
        _s, _u, etype, code, value = struct.unpack(
            _INPUT_EVENT_FORMAT, data[off:off + _INPUT_EVENT_SIZE])
        if etype == _EV_ABS and code == _ABS_X:
            x = value
        elif etype == _EV_ABS and code == _ABS_Y:
            y = value
        elif etype == _EV_KEY and code == _BTN_TOUCH and value == 0 \
                and x is not None and y is not None:
            taps.append((x, y))
    return taps, x, y


async def _evtest_sse(device: str):
    """Stream touch axis ranges and taps from a kernel input device as SSE.

    Reads the input device directly in Python rather than shelling out to
    evtest, so it needs no extra binary in the image (the appliance container
    mounts /dev/input). A background thread does the blocking reads and hands
    completed taps to the async generator over a queue. Same shape as before: a
    ranges event first, then a tap event on each BTN_TOUCH release."""
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as e:
        yield "data: " + json.dumps({"type": "error", "msg": f"cannot open {device}: {e}"}) + "\n\n"
        return

    x0, x_min, x_max = _abs_axis(fd, _ABS_X)
    y0, y_min, y_max = _abs_axis(fd, _ABS_Y)

    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    stop = threading.Event()

    def _deliver(item) -> None:
        # The stream can end, and its event loop close, while this thread is
        # between reads (a client that went away, or app shutdown). Handing an
        # item to a closed loop raises "Event loop is closed", so drop it and
        # tell the thread to stop instead (FoodAssistant-dunku).
        if loop.is_closed():
            stop.set()
            return
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:
            stop.set()

    def _read_loop():
        # Seed the position from the axis state at open (EVIOCGABS reports the
        # last value the device sent). The kernel suppresses ABS events whose
        # value did not change, so the first tap can arrive as a bare
        # BTN_TOUCH press/release with no fresh coordinates; without the seed
        # that first tap was silently dropped (FoodAssistant-9ext).
        x, y = x0, y0
        try:
            while not stop.is_set() and not loop.is_closed():
                # select with a timeout so the thread checks `stop` and exits
                # promptly when the client disconnects, instead of blocking on a
                # read until the next physical touch.
                try:
                    r, _, _ = select.select([fd], [], [], 0.5)
                except (OSError, ValueError):
                    break
                if not r:
                    continue
                try:
                    data = os.read(fd, _INPUT_EVENT_SIZE * 64)
                except OSError:
                    break
                taps, x, y = _fold_touch_events(data, x, y)
                for tap in taps:
                    _deliver(tap)
        finally:
            # The thread owns the descriptor, so it is never closed under a
            # select still waiting on it.
            try:
                os.close(fd)
            except OSError:
                pass
            _deliver(None)

    t = threading.Thread(target=_read_loop, daemon=True)
    t.start()
    try:
        yield "data: " + json.dumps({
            "type": "ranges", "x_min": x_min, "x_max": x_max,
            "y_min": y_min, "y_max": y_max,
        }) + "\n\n"
        while True:
            item = await queue.get()
            if item is None:
                break
            tx, ty = item
            yield "data: " + json.dumps({"type": "tap", "x": tx, "y": ty}) + "\n\n"
    finally:
        stop.set()


# The kiosk on the Pi watches for this flag and, when present, navigates its own
# display to the fullscreen calibration page. That is how a "Calibrate" click in
# a remote browser starts the tap sequence on the Pi's physical touchscreen
# (where the person actually is) instead of on the remote browser.
_CAL_FLAG = Path(settings.data_dir) / "calibrate_touch.flag"


@router.post("/calibrate/touch/request")
async def calibrate_touch_request():
    """Signal the kiosk to launch the fullscreen calibration page on its display."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    if not _find_touch_device():
        return JSONResponse({"ok": False, "error": "No touch device detected on this Pi."})
    try:
        _CAL_FLAG.parent.mkdir(parents=True, exist_ok=True)
        _CAL_FLAG.write_text("1")
        # Drop any leftover cancel/done from a previous run so they do not abort
        # or prematurely clear this one.
        _CAL_CANCEL_FLAG.unlink(missing_ok=True)
        _CAL_DONE_FLAG.unlink(missing_ok=True)
    except OSError as e:
        return JSONResponse({"ok": False, "error": str(e)})
    return {"ok": True, "message": "Calibration started on the Pi touchscreen."}


@router.get("/calibrate/touch/pending")
async def calibrate_touch_pending():
    """Polled by the kiosk page; true once a remote browser asks to calibrate."""
    return {"pending": _CAL_FLAG.exists()}


# Cancelling calibration belongs on the REMOTE browser, not the Pi touchscreen:
# the panel is uncalibrated during the test, so a Cancel button on it is hard to
# hit. The remote UI sets this flag; the fullscreen calibration page polls it and
# returns to the dashboard. One-shot, cleared on read, same pattern as above.
_CAL_CANCEL_FLAG = Path(settings.data_dir) / "calibrate_cancel.flag"


@router.post("/calibrate/touch/cancel")
async def calibrate_touch_cancel():
    """From the remote UI: ask the Pi calibration page to stop and go back."""
    try:
        _CAL_CANCEL_FLAG.parent.mkdir(parents=True, exist_ok=True)
        _CAL_CANCEL_FLAG.write_text("1")
        # If the kiosk never reached the calibration page, also clear the start
        # flag so a queued request does not fire later.
        _CAL_FLAG.unlink(missing_ok=True)
    except OSError as e:
        return JSONResponse({"ok": False, "error": str(e)})
    return {"ok": True}


@router.get("/calibrate/touch/cancel/pending")
async def calibrate_touch_cancel_pending():
    """Polled by the Pi calibration page; true once the remote asks to cancel."""
    pending = _CAL_CANCEL_FLAG.exists()
    if pending:
        try:
            _CAL_CANCEL_FLAG.unlink()
        except OSError:
            pass
    return {"pending": pending}


@router.post("/calibrate/touch/reset")
async def calibrate_touch_reset():
    """Remove the calibration matrix (revert to the panel default), via the
    host bridge. Used to recover from a calibration that came out wrong."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    try:
        async with bridge_client(timeout=40.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/touch/calibrate/reset")
        # An old host bridge predates this route and answers 404
        # {"error": "not found"}; say something actionable instead.
        if r.status_code == 404:
            return JSONResponse({"ok": False, "error":
                "The device helper software is out of date. Press Update "
                "under Backup & Updates, then try again."})
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


# Kiosk navigate-to-dashboard (the flag itself lives in _common.py, since
# save_setup raises it too).
@router.post("/kiosk/navigate/request")
async def kiosk_navigate_request():
    """Signal the kiosk to leave the wizard and load the dashboard.

    The save that completes first-time setup writes the flag itself (see
    save_setup); this endpoint remains for a wizard re-run by a signed-in
    admin. Best effort: if the flag cannot be written we report it but never
    block finishing setup.
    """
    err = _write_kiosk_nav_flag()
    if err:
        return JSONResponse({"ok": False, "error": err})
    return {"ok": True}


@router.get("/kiosk/navigate/pending")
async def kiosk_navigate_pending():
    """Polled by the kiosk page; true once setup has finished. One-shot.

    Clears the flag as it reports it so the kiosk navigates exactly once and the
    poll does not loop the display back to the dashboard on every tick.
    """
    pending = _KIOSK_NAV_FLAG.exists()
    if pending:
        try:
            _KIOSK_NAV_FLAG.unlink()
        except OSError:
            pass
    return {"pending": pending}


@router.get("/calibrate/touch/page", response_class=HTMLResponse)
async def calibrate_touch_page(request: Request):
    """Fullscreen calibration page the kiosk navigates to. Clears the flag.

    The active output rotation is passed in so the page can compensate for it:
    wlroots applies the output transform to touch input as well as the display,
    so the calibration matrix (applied before that transform) must be fit in the
    pre-transform space.
    """
    try:
        _CAL_FLAG.unlink()
    except OSError:
        pass
    rotation = 0
    try:
        async with bridge_client(timeout=4.0) as c:
            data = (await c.get(f"{_HOST_BRIDGE}/display/rotation")).json()
        if data.get("ok"):
            rotation = int(data.get("rotation", 0) or 0)
    except Exception:
        rotation = 0
    return templates.TemplateResponse(
        request, "calibrate.html", {"rotation": rotation})


@router.get("/calibrate/touch/events")
async def calibrate_touch_events():
    """SSE stream of raw ABS_X/ABS_Y touch events from the kernel input layer.

    First event: {"type": "ranges", "x_min": 0, "x_max": 4095, ...}
    Subsequent: {"type": "tap", "x": int, "y": int} on each BTN_TOUCH release.
    """
    if not is_raspberry_pi():
        return JSONResponse({"error": "Not available on this platform."}, status_code=400)
    device = _find_touch_device()
    if not device:
        return JSONResponse({"error": "No touch device found."}, status_code=400)
    return StreamingResponse(
        _evtest_sse(device),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


class TouchMatrixPayload(BaseModel):
    matrix: str = ""


@router.post("/calibrate/touch/apply")
async def calibrate_touch_apply(payload: TouchMatrixPayload):
    """Write a LIBINPUT_CALIBRATION_MATRIX via the host bridge.

    The app service runs with privileges capped to CAP_NET_BIND_SERVICE, so it
    cannot write to /etc/udev/rules.d or run udevadm itself (and sudo fails
    under that cap bound). The host bridge runs as root and applies the matrix.
    """
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    parts = payload.matrix.strip().split()
    if len(parts) != 6:
        return JSONResponse({"ok": False, "error": "Matrix must be exactly 6 floats."})
    try:
        [float(p) for p in parts]
    except ValueError:
        return JSONResponse({"ok": False, "error": "Non-numeric value in matrix."})
    matrix_str = " ".join(parts)
    try:
        async with bridge_client(timeout=20.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/touch/calibrate",
                             json={"matrix": matrix_str})
        data = r.json()
        if data.get("ok"):
            # Signal the remote browser that calibration finished, so it can
            # clear the Cancel control (the kiosk restarts here and cannot report
            # back itself). One-shot flag, read+cleared by the done poll below.
            try:
                _CAL_DONE_FLAG.write_text("1")
                _CAL_CANCEL_FLAG.unlink(missing_ok=True)
            except OSError:
                pass
            return {"ok": True, "message": data.get("message", ""),
                    "kiosk_restarted": data.get("kiosk_restarted", False)}
        # A stale bridge (updated app, old bridge) delegated to a separate
        # calibrate helper and answered "calibrate helper not installed"
        # (FoodAssistant-jppi). The current bridge writes the rule itself and
        # the updater refreshes the bridge, so the fix is one Update away.
        err = data.get("error", f"HTTP {r.status_code}")
        if "not installed" in err.lower():
            err = ("The device helper software is out of date. Press Update "
                   "under Backup & Updates, then calibrate again.")
        return JSONResponse({"ok": False, "error": err})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


_CAL_DONE_FLAG = Path(settings.data_dir) / "calibrate_done.flag"


@router.get("/calibrate/touch/done/pending")
async def calibrate_touch_done_pending():
    """Polled by the remote UI; true once a calibration has been applied. The
    flag is one-shot so the Cancel control clears exactly once."""
    pending = _CAL_DONE_FLAG.exists()
    if pending:
        try:
            _CAL_DONE_FLAG.unlink()
        except OSError:
            pass
    return {"pending": pending}


# Backwards-compatible alias for the old endpoint name
@router.post("/test/vision")
async def test_vision_legacy(payload: dict):
    provider = payload.get("vision_provider") or payload.get("provider", "")
    key_field = f"{provider}_api_key"
    return await test_provider(TestProviderPayload(
        provider=provider,
        api_key=payload.get(key_field, payload.get("api_key", "")),
        model=payload.get(f"{provider}_model", payload.get("model", "")),
        base_url=payload.get("ollama_base_url", payload.get("base_url", "")),
    ))
