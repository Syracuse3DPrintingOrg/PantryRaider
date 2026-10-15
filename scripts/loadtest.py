#!/usr/bin/env python3
"""Load and soak test for the app, run locally against the mock Grocy/Mealie.

Boots the mock backend from tests/browser/_mockstack.py and the real app under
one uvicorn worker (wrapped by scripts/loadtest_probe.py, which measures the
event loop from inside), then drives a kitchen's worth of traffic:

- N kiosks, each holding an /events/poll long-poll and polling /kiosk/status
  every 2 s;
- bursts of page loads (/ui/start, /ui/inventory, /ui/pending, /ui/shopping,
  /ui/cook);
- timers created and cancelled, barcode scans posted to /pending/scan, and a
  settings save every so often;
- a few keyed clients (a Stream Deck, Home Assistant) presenting the API key,
  so the per-key use record is written as it would be on a real install.

Every minute it prints per-endpoint latency, errors, event-loop lag, RSS and
open file descriptors. At the end it writes a JSON report.

    python scripts/loadtest.py --minutes 20
    python scripts/loadtest.py --minutes 60 --kiosks 5 --tracemalloc   # soak

Nothing reaches the internet: outbound HTTPS is pointed at a closed local port.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests" / "browser"))
import _mockstack  # noqa: E402

API_KEY = "loadtest-key-0123456789abcdef"
PAGES = ["/ui/start", "/ui/inventory", "/ui/pending", "/ui/shopping", "/ui/cook"]
BROWSER_UA = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


class Mock(_mockstack.MockHandler):
    """The shared mock plus an Open Food Facts product answer."""

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/v3/product/"):
            code = path.rsplit("/", 1)[-1]
            return self._json({"status": "success", "product": {
                "code": code, "product_name": f"Test Beans {code[-3:]}",
                "brands": "Mockery", "categories_tags": ["en:canned-foods"]}})
        return super().do_GET()


class Stats:
    def __init__(self):
        self.lat = defaultdict(list)
        self.err = defaultdict(int)
        self.err_detail = defaultdict(int)
        self.window_lat = defaultdict(list)

    def add(self, name, ms):
        self.lat[name].append(ms)
        self.window_lat[name].append(ms)

    def fail(self, name, why):
        self.err[name] += 1
        self.err_detail[f"{name}: {why}"[:160]] += 1


def pct(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


async def timed(stats, name, coro_fn, ok=(200,)):
    t0 = time.perf_counter()
    try:
        r = await coro_fn()
    except Exception as e:  # noqa: BLE001
        stats.fail(name, type(e).__name__)
        return None
    ms = (time.perf_counter() - t0) * 1000
    if r.status_code not in ok:
        stats.fail(name, f"HTTP {r.status_code}")
        return r
    stats.add(name, ms)
    return r


async def kiosk(base, stats, stop, i):
    headers = {"user-agent": BROWSER_UA, "accept": "application/json"}
    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=40) as c:
        since = {"v": 0}

        async def longpoll():
            while not stop.is_set():
                t0 = time.perf_counter()
                try:
                    r = await c.get("/events/poll", params={"since": since["v"], "wait": 25})
                    if r.status_code == 200:
                        since["v"] = r.json().get("last_id", since["v"]) or since["v"]
                        stats.add("events/poll(long)", (time.perf_counter() - t0) * 1000)
                    else:
                        stats.fail("events/poll(long)", f"HTTP {r.status_code}")
                        await asyncio.sleep(1)
                except Exception as e:  # noqa: BLE001
                    if not stop.is_set():
                        stats.fail("events/poll(long)", type(e).__name__)
                        await asyncio.sleep(1)

        lp = asyncio.create_task(longpoll())
        await asyncio.sleep(random.random() * 2)
        while not stop.is_set():
            await timed(stats, "kiosk/status", lambda: c.get(
                "/kiosk/status", params={"since": since["v"], "kiosk": 1, "expiring": i % 2,
                                         "scanner": 1, "activity": 1}))
            await asyncio.sleep(2)
        lp.cancel()


async def pages(base, stats, stop, interval):
    headers = {"user-agent": BROWSER_UA, "accept": "text/html"}
    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=30) as c:
        while not stop.is_set():
            burst = random.sample(PAGES, k=random.randint(2, len(PAGES)))
            await asyncio.gather(*[timed(stats, p, lambda p=p: c.get(p)) for p in burst])
            await asyncio.sleep(interval * random.uniform(0.5, 1.5))


async def timers(base, stats, stop, interval):
    async with httpx.AsyncClient(base_url=base, headers={"user-agent": BROWSER_UA}, timeout=30) as c:
        while not stop.is_set():
            r = await timed(stats, "POST /timers", lambda: c.post(
                "/timers", json={"label": "pasta", "seconds": random.randint(30, 900)}))
            await asyncio.sleep(interval * random.uniform(0.3, 1.0))
            await timed(stats, "GET /timers", lambda: c.get("/timers"))
            if r is not None and r.status_code == 200:
                tid = ((r.json() or {}).get("timer") or {}).get("id")
                if tid is not None:
                    await timed(stats, "DELETE /timers/{id}", lambda: c.delete(f"/timers/{tid}"),
                                ok=(200, 404))
            await asyncio.sleep(interval * random.uniform(0.3, 1.0))


async def scans(base, stats, stop, interval):
    async with httpx.AsyncClient(base_url=base, headers={"user-agent": "HomeAssistant/2026.10"},
                                 timeout=30) as c:
        n = 0
        while not stop.is_set():
            n += 1
            code = f"0{random.randint(10**10, 10**11 - 1)}"
            await timed(stats, "POST /pending/scan", lambda: c.post(
                "/pending/scan", json={"barcode": code, "source": "scanner"}))
            await asyncio.sleep(interval * random.uniform(0.5, 1.5))


async def saves(base, stats, stop, interval):
    async with httpx.AsyncClient(base_url=base, headers={"user-agent": BROWSER_UA}, timeout=30) as c:
        n = 0
        while not stop.is_set():
            n += 1
            await timed(stats, "POST /setup/save", lambda: c.post(
                "/setup/save", json={"staple_items": f"salt, pepper, oil{n % 7}"}))
            await asyncio.sleep(interval * random.uniform(0.5, 1.5))


async def keyed(base, stats, stop, i, interval):
    """A Stream Deck or HA integration presenting the API key."""
    ua = ["foodassistant-streamdeck/0.21", "HomeAssistant/2026.10", "PantryRaiderHA/1.4"][i % 3]
    headers = {"user-agent": ua, "x-api-key": API_KEY, "x-device-id": f"dev-{i}"}
    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=30) as c:
        while not stop.is_set():
            await timed(stats, "GET /timers (key)", lambda: c.get("/timers"))
            await asyncio.sleep(interval * random.uniform(0.5, 1.5))


async def health_probe(base, stats, stop):
    """A cheap request every 250 ms: its latency is the loop's responsiveness
    as a client sees it."""
    async with httpx.AsyncClient(base_url=base, timeout=30) as c:
        while not stop.is_set():
            await timed(stats, "/events/count", lambda: c.get("/events/count"))
            await asyncio.sleep(0.25)


def summarize(stats, which="lat"):
    src = getattr(stats, which if which != "window" else "window_lat")
    rows = {}
    for name, xs in sorted(src.items()):
        rows[name] = {"n": len(xs), "p50": round(pct(xs, .5), 1), "p95": round(pct(xs, .95), 1),
                      "p99": round(pct(xs, .99), 1), "max": round(max(xs), 1) if xs else 0}
    return rows


async def monitor(base, stats, stop, samples, interval, tracemalloc_on):
    async with httpx.AsyncClient(base_url=base, timeout=300) as c:
        t_start = time.time()
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            try:
                s = (await c.get("/__loadtest/stats", params={"reset": 1})).json()
            except Exception as e:  # noqa: BLE001
                print("stats failed:", e, flush=True)
                continue
            win = summarize(stats, "window")
            stats.window_lat.clear()
            sample = {"t_min": round((time.time() - t_start) / 60, 1), "proc": s["proc"],
                      "lag_ms": s["lag_ms"], "tasks": s["tasks"], "threads": s["threads"],
                      "errors": dict(stats.err), "window": win}
            if tracemalloc_on:
                try:
                    tm = (await c.get("/__loadtest/tracemalloc")).json()
                    sample["traced_mb"] = tm.get("traced_mb")
                    sample["tm_top"] = tm.get("top", [])[:8]
                except Exception:
                    pass
            samples.append(sample)
            lag = s["lag_ms"]
            print(f"[{sample['t_min']:5.1f}m] rss={s['proc'].get('VmRSS')} fds={s['proc'].get('fds')}"
                  f" tasks={s['tasks']} thr={s['threads']} lag p50/p99/max="
                  f"{lag['p50']:.1f}/{lag['p99']:.1f}/{lag['max']:.1f}ms"
                  f" health p99={win.get('/events/count', {}).get('p99')}"
                  f" errs={sum(stats.err.values())}"
                  + (f" traced={sample.get('traced_mb', 0):.1f}MB" if tracemalloc_on else ""),
                  flush=True)


async def drive(base, args):
    stats = Stats()
    stop = asyncio.Event()
    samples = []
    # A tracemalloc snapshot of the whole heap takes a while.
    async with httpx.AsyncClient(base_url=base, timeout=300) as c:
        await c.get("/__loadtest/stats")
        if args.tracemalloc:
            await c.get("/__loadtest/tracemalloc")
    tasks = [asyncio.create_task(kiosk(base, stats, stop, i)) for i in range(args.kiosks)]
    tasks += [asyncio.create_task(pages(base, stats, stop, args.page_interval)),
              asyncio.create_task(pages(base, stats, stop, args.page_interval)),
              asyncio.create_task(timers(base, stats, stop, args.timer_interval)),
              asyncio.create_task(scans(base, stats, stop, args.scan_interval)),
              asyncio.create_task(saves(base, stats, stop, args.save_interval)),
              asyncio.create_task(health_probe(base, stats, stop))]
    tasks += [asyncio.create_task(keyed(base, stats, stop, i, args.key_interval))
              for i in range(args.keyed)]
    mon = asyncio.create_task(monitor(base, stats, stop, samples, args.sample_every,
                                      args.tracemalloc))
    await asyncio.sleep(args.minutes * 60)
    stop.set()
    await asyncio.sleep(0.5)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await mon
    async with httpx.AsyncClient(base_url=base, timeout=60) as c:
        final = (await c.get("/__loadtest/stats")).json()
    return {"args": vars(args), "latency": summarize(stats), "errors": dict(stats.err),
            "error_detail": dict(stats.err_detail), "samples": samples,
            "stalls": final["stalls"], "final_proc": final["proc"]}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--minutes", type=float, default=20)
    ap.add_argument("--kiosks", type=int, default=10)
    ap.add_argument("--keyed", type=int, default=3)
    ap.add_argument("--page-interval", type=float, default=4)
    ap.add_argument("--timer-interval", type=float, default=3)
    ap.add_argument("--scan-interval", type=float, default=4)
    ap.add_argument("--save-interval", type=float, default=30)
    ap.add_argument("--key-interval", type=float, default=1)
    ap.add_argument("--sample-every", type=float, default=60)
    ap.add_argument("--app-port", type=int, default=19800)
    ap.add_argument("--mock-port", type=int, default=19801)
    ap.add_argument("--tracemalloc", action="store_true")
    ap.add_argument("--data-dir", default="")
    ap.add_argument("--report", default="loadtest-report.json")
    args = ap.parse_args()

    mock = ThreadingHTTPServer(("127.0.0.1", args.mock_port), Mock)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    data_dir = args.data_dir or tempfile.mkdtemp(prefix="pr-loadtest-")
    mock_url = f"http://127.0.0.1:{args.mock_port}"
    env = _mockstack.app_env(args.mock_port, data_dir, {
        "API_KEY": API_KEY,
        "LOADTEST_OFF_URL": mock_url,
        "BARCODE_ENRICHMENT": "off",
        "START_PAGE_ENABLED": "true",
        "HA_EVENTS_ENABLED": "true",
        # Any request that would leave the machine fails fast instead.
        "HTTPS_PROXY": "http://127.0.0.1:19899", "HTTP_PROXY": "http://127.0.0.1:19899",
        "NO_PROXY": "127.0.0.1,localhost",
        "LOADTEST_TRACEMALLOC": "1" if args.tracemalloc else "0",
        "PYTHONPATH": str(REPO / "scripts"),
    })
    app = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "loadtest_probe:app", "--host", "127.0.0.1",
         "--port", str(args.app_port), "--workers", "1", "--log-level", "warning",
         "--timeout-graceful-shutdown", "2"],
        cwd=REPO / "service", env=env)
    base = f"http://127.0.0.1:{args.app_port}"
    try:
        # tracemalloc makes the start several times slower.
        _mockstack.wait_http(f"{base}/health", timeout=240 if args.tracemalloc else 60)
        print(f"app up at {base}, data in {data_dir}", flush=True)
        report = asyncio.run(drive(base, args))
    finally:
        app.terminate()
        try:
            app.wait(timeout=10)
        except subprocess.TimeoutExpired:
            app.kill()
        mock.shutdown()
    Path(args.report).write_text(json.dumps(report, indent=1))
    print(json.dumps({"latency": report["latency"], "errors": report["error_detail"],
                      "stalls": report["stalls"][:12]}, indent=1))


if __name__ == "__main__":
    main()
