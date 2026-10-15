"""In-process probe for cloud/scripts/loadtest.py: wraps Forager and measures it.

uvicorn runs ``loadtest_probe:app``. Every request passes through untouched;
the wrapper adds what a client cannot see:

- event-loop lag: a task sleeps 100 ms in a loop and records the overshoot;
- stall stacks: a watchdog thread samples the loop thread's stack whenever
  the loop has not ticked for LOADTEST_STALL_MS, so a blocking call is caught
  with the line that made it;
- SQL statements per request, by route, counted with a SQLAlchemy event
  listener (an N+1 shows up as a count that grows with the data);
- RSS and open file descriptors from /proc.

GET /__loadtest/stats returns it all as JSON. LOADTEST_GEMINI_BASE points the
Gemini forwarder at a local stub instead of Google.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import os
import sys
import threading
import time
import traceback
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import event  # noqa: E402

from app import forwarder  # noqa: E402
from app.database import get_engine  # noqa: E402
from app.main import app as _inner  # noqa: E402

if os.environ.get("LOADTEST_GEMINI_BASE"):
    forwarder._GEMINI_BASE = os.environ["LOADTEST_GEMINI_BASE"]

STALL_MS = float(os.environ.get("LOADTEST_STALL_MS", "25"))

_route: contextvars.ContextVar[list | None] = contextvars.ContextVar("route", default=None)
_sql_per_route: dict[str, list[int]] = defaultdict(list)
_sql_text: dict[str, Counter] = defaultdict(Counter)
_lags: list[float] = []
_stall_ms: Counter = Counter()
_beat = {"t": time.monotonic(), "tid": None}
_started = {"done": False}


@event.listens_for(get_engine(), "before_cursor_execute")
def _count(conn, cursor, statement, params, context, executemany):
    box = _route.get()
    if box is not None:
        box[1] += 1
        _sql_text[box[0]][" ".join(statement.split())[:140]] += 1


def _route_key(path: str) -> str:
    parts = path.strip("/").split("/")
    if parts and parts[0] == "r" and len(parts) >= 2:
        parts[1] = "{token}"
    return "/" + "/".join(parts)


def _proc() -> dict:
    out = {}
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith(("VmRSS", "VmHWM", "Threads")):
                k, v = line.split(":", 1)
                out[k] = v.strip()
        out["fds"] = len(os.listdir("/proc/self/fd"))
    except OSError:
        pass
    return out


async def _lag_task():
    while True:
        t0 = time.perf_counter()
        await asyncio.sleep(0.1)
        _lags.append((time.perf_counter() - t0 - 0.1) * 1000)


async def _beat_task():
    while True:
        _beat["t"] = time.monotonic()
        await asyncio.sleep(0.005)


def _frames(frames) -> str:
    keep = []
    for fs in frames:
        f = fs.filename
        if "/asyncio/" in f or "/uvicorn/" in f or "loadtest_probe" in f:
            continue
        keep.append(f"{f.split('/cloud/')[-1].split('site-packages/')[-1]}:{fs.lineno} {fs.name}")
    return " <- ".join(reversed(keep[-8:]))


def _watchdog():
    while True:
        time.sleep(0.01)
        if (time.monotonic() - _beat["t"]) * 1000 < STALL_MS:
            continue
        frame = sys._current_frames().get(_beat["tid"])
        if frame is not None:
            _stall_ms[_frames(traceback.extract_stack(frame))] += 10


def _start():
    if _started["done"]:
        return
    _started["done"] = True
    loop = asyncio.get_running_loop()
    _beat["tid"] = threading.get_ident()
    loop.create_task(_lag_task())
    loop.create_task(_beat_task())
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
    sql = {}
    for route, counts in _sql_per_route.items():
        sql[route] = {"requests": len(counts), "mean": round(sum(counts) / len(counts), 1),
                      "max": max(counts),
                      "top": [{"n": n, "sql": s} for s, n in _sql_text[route].most_common(6)]}
    return {
        "proc": _proc(),
        "lag_ms": {"n": len(lags), "p50": _pct(lags, .5), "p95": _pct(lags, .95),
                   "p99": _pct(lags, .99), "max": max(lags) if lags else 0.0},
        "stalls": [{"stack": k, "ms": v} for k, v in _stall_ms.most_common(20)],
        "sql": sql,
        "tasks": len(asyncio.all_tasks()),
    }


async def app(scope, receive, send):
    if scope["type"] != "http":
        return await _inner(scope, receive, send)
    _start()
    path = scope.get("path", "")
    if path == "/__loadtest/stats":
        body = json.dumps(_stats(b"reset=1" in scope.get("query_string", b""))).encode()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})
        return
    key = f"{scope['method']} {_route_key(path)}"
    box = [key, 0]
    tok = _route.set(box)
    try:
        await _inner(scope, receive, send)
    finally:
        _route.reset(tok)
        _sql_per_route[key].append(box[1])
