"""In-process probe for scripts/loadtest.py: wraps the real app and measures it.

uvicorn runs ``loadtest_probe:app`` instead of ``app.main:app``. The wrapper
passes every request through untouched and adds what an outside client cannot
see:

- event-loop lag: a task sleeps 100 ms in a loop and records how late it
  wakes; anything blocking the loop shows up as overshoot.
- stall stacks: a watchdog thread notices when the loop has not ticked for
  more than STALL_MS and samples the loop thread's Python stack, so a blocking
  call is caught with the line that made it.
- RSS and open file descriptors, read from /proc.
- tracemalloc snapshots on demand, compared against the first one.

GET /__loadtest/stats returns all of it as JSON; GET /__loadtest/tracemalloc
takes a snapshot and returns the top growth since the baseline.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import traceback
import tracemalloc
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))

if os.environ.get("LOADTEST_TRACEMALLOC") == "1":
    tracemalloc.start(8)

from app.main import app as _inner  # noqa: E402

if os.environ.get("LOADTEST_OFF_URL"):
    # Open Food Facts lookups go to the mock backend, never the internet.
    from app.services import barcode as _barcode
    _barcode.OFF_PRODUCT_URL = os.environ["LOADTEST_OFF_URL"] + "/api/v3/product/{code}"

STALL_MS = float(os.environ.get("LOADTEST_STALL_MS", "40"))

_lags: list[float] = []
_stalls: Counter = Counter()
_stall_ms: Counter = Counter()
_heartbeat = {"t": time.monotonic(), "thread": None}
_started = {"done": False}
_baseline: dict = {}


def _proc_status() -> dict:
    out = {}
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith(("VmRSS", "VmHWM", "Threads")):
                k, v = line.split(":", 1)
                out[k] = v.strip()
    except OSError:
        pass
    try:
        out["fds"] = len(os.listdir("/proc/self/fd"))
    except OSError:
        pass
    return out


async def _lag_task():
    while True:
        t0 = time.perf_counter()
        await asyncio.sleep(0.1)
        _lags.append((time.perf_counter() - t0 - 0.1) * 1000)


async def _heartbeat_task():
    while True:
        _heartbeat["t"] = time.monotonic()
        await asyncio.sleep(0.005)


def _app_frames(frames) -> str:
    """The innermost few frames, keeping app and library code readable."""
    keep = []
    for fs in frames:
        f = fs.filename
        if "/asyncio/" in f or "/uvicorn/" in f or "loadtest_probe" in f:
            continue
        short = f.split("/service/")[-1].split("site-packages/")[-1]
        keep.append(f"{short}:{fs.lineno} {fs.name}")
    return " <- ".join(reversed(keep[-8:]))


def _watchdog():
    tid = _heartbeat["thread"]
    last_key = None
    while True:
        time.sleep(0.01)
        late = (time.monotonic() - _heartbeat["t"]) * 1000
        if late < STALL_MS:
            last_key = None
            continue
        frame = sys._current_frames().get(tid)
        if frame is None:
            continue
        key = _app_frames(traceback.extract_stack(frame))
        if key != last_key:
            _stalls[key] += 1
        _stall_ms[key] += 10
        last_key = key


def _start():
    if _started["done"]:
        return
    _started["done"] = True
    loop = asyncio.get_running_loop()
    _heartbeat["thread"] = threading.get_ident()
    loop.create_task(_lag_task())
    loop.create_task(_heartbeat_task())
    threading.Thread(target=_watchdog, daemon=True).start()


def _pct(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


def _stats(reset: bool) -> dict:
    lags = list(_lags)
    if reset:
        _lags.clear()
    out = {
        "proc": _proc_status(),
        "lag_ms": {"n": len(lags), "p50": _pct(lags, .5), "p95": _pct(lags, .95),
                   "p99": _pct(lags, .99), "max": max(lags) if lags else 0.0},
        "stalls": [{"stack": k, "count": _stalls[k], "ms": _stall_ms[k]}
                   for k, _ in _stall_ms.most_common(25)],
        "tasks": len(asyncio.all_tasks()),
        "threads": threading.active_count(),
    }
    return out


def _tracemalloc_diff(limit: int = 25) -> dict:
    if not tracemalloc.is_tracing():
        return {"error": "start the app with LOADTEST_TRACEMALLOC=1"}
    snap = tracemalloc.take_snapshot().filter_traces((
        tracemalloc.Filter(False, tracemalloc.__file__),
        tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
    ))
    if "snap" not in _baseline:
        _baseline["snap"] = snap
        return {"baseline": True, "traced_mb": tracemalloc.get_traced_memory()[0] / 1e6}
    diff = snap.compare_to(_baseline["snap"], "traceback")
    rows = []
    for st in diff[:limit]:
        rows.append({"size_diff_kb": st.size_diff / 1024, "count_diff": st.count_diff,
                     "where": [f"{f.filename.split('site-packages/')[-1]}:{f.lineno}"
                               for f in st.traceback[-6:]]})
    return {"traced_mb": tracemalloc.get_traced_memory()[0] / 1e6, "top": rows}


async def app(scope, receive, send):
    if scope["type"] == "http":
        _start()
        path = scope.get("path", "")
        if path.startswith("/__loadtest/"):
            if path == "/__loadtest/stats":
                reset = b"reset=1" in scope.get("query_string", b"")
                body = _stats(reset)
            elif path == "/__loadtest/tracemalloc":
                body = await asyncio.to_thread(_tracemalloc_diff)
            elif path == "/__loadtest/gc":
                import gc
                body = {"collected": gc.collect()}
            else:
                body = {"error": "unknown"}
            data = json.dumps(body).encode()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": data})
            return
    await _inner(scope, receive, send)
