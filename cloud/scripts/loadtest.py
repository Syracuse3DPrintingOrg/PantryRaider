#!/usr/bin/env python3
"""Load test for Forager, run locally against a stubbed Gemini.

Seeds a database (Postgres via --db, or SQLite) with accounts, paid plans and a
usage ledger of a realistic size, boots Forager under one uvicorn worker
(wrapped by loadtest_probe.py, which measures the event loop and counts SQL
per request), and drives:

- concurrent AI proxy calls (/v1/ai/analyze) from many paired kitchens, the
  upstream answering after a delay like a real model;
- the status read the app polls (/v1/instance/me);
- pairing: a pairing code minted and redeemed;
- share links created by kitchens and opened by the public (/r/{token} and
  its download).

    python cloud/scripts/loadtest.py --db postgresql+psycopg2://u:p@127.0.0.1:19850/db \
        --minutes 5 --ledger-rows 300000

Sign-up stays out of it: accounts are written straight to the database. No
request leaves the machine.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import subprocess
import tempfile
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CLOUD = Path(__file__).resolve().parents[1]


class Gemini(BaseHTTPRequestHandler):
    """generateContent, answered after a model-like delay."""

    delay = (0.3, 1.2)

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        time.sleep(random.uniform(*self.delay))
        body = json.dumps({
            "candidates": [{"content": {"parts": [{"text": json.dumps(
                {"name": "Black Beans", "category": "canned", "storage": "pantry",
                 "shelf_life_days": 700})}]}}],
            "usageMetadata": {"promptTokenCount": 420, "candidatesTokenCount": 80},
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def seed(db_url: str, accounts: int, ledger_rows: int) -> list[tuple[int, str]]:
    """Accounts with a premium plan and a session each; returns (id, session)."""
    os.environ["CLOUD_DATABASE_URL"] = db_url
    sys.path.insert(0, str(CLOUD))
    from app.config import PLAN_QUOTAS
    from app.database import Base, SessionLocal, get_engine, init_db
    from app.models import Account, AuthSession, Entitlement, UsageLedger
    from app.security import new_token, token_hash

    engine = get_engine()
    Base.metadata.drop_all(bind=engine)
    if engine.dialect.name == "postgresql":
        with engine.begin() as c:
            from sqlalchemy import text
            c.execute(text("DROP TABLE IF EXISTS alembic_version"))
    init_db()
    db = SessionLocal()
    now = datetime.now(timezone.utc)
    iso = now.isoformat(timespec="seconds")
    later = (now + timedelta(days=30)).isoformat(timespec="seconds")
    out = []
    for i in range(accounts):
        a = Account(email=f"cook{i}@example.com", password_hash="x", created_at=iso,
                    email_verified=1)
        db.add(a)
        db.flush()
        db.add(Entitlement(account_id=a.id, plan="premium", status="active",
                           monthly_token_quota=PLAN_QUOTAS["premium"] * 1000,
                           source="stripe", updated_at=iso))
        tok = new_token("prs")
        db.add(AuthSession(token_hash=token_hash(tok), account_id=a.id,
                           created_at=iso, expires_at=later))
        out.append((a.id, tok))
    db.commit()
    # A ledger the size a running service builds up: a year of calls spread
    # over every account, most of them in past months.
    batch = []
    for i in range(ledger_rows):
        ts = now - timedelta(seconds=random.randint(0, 365 * 86400))
        batch.append({"account_id": out[i % accounts][0], "instance_id": None,
                      "month_key": f"{ts.year:04d}-{ts.month:02d}", "tokens": 500,
                      "kind": "enrich", "created_at": ts.isoformat(timespec="seconds")})
        if len(batch) >= 20000:
            db.execute(UsageLedger.__table__.insert(), batch)
            db.commit()
            batch = []
    if batch:
        db.execute(UsageLedger.__table__.insert(), batch)
        db.commit()
    if engine.dialect.name == "postgresql":
        with engine.begin() as c:
            from sqlalchemy import text
            c.execute(text("ANALYZE"))
    db.close()
    return out


def pct(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


class Stats:
    def __init__(self):
        self.lat = defaultdict(list)
        self.err = defaultdict(int)

    async def timed(self, name, fn, ok=(200,)):
        t0 = time.perf_counter()
        try:
            r = await fn()
        except Exception as e:  # noqa: BLE001
            self.err[f"{name}: {type(e).__name__}"] += 1
            return None
        if r.status_code not in ok:
            self.err[f"{name}: HTTP {r.status_code}"] += 1
            return r
        self.lat[name].append((time.perf_counter() - t0) * 1000)
        return r


async def pair(c, stats, session):
    r = await stats.timed("POST /v1/pairing/code", lambda: c.post(
        "/v1/pairing/code", headers={"Authorization": f"Bearer {session}"}))
    if r is None or r.status_code != 200:
        return None
    code = r.json()["code"]
    r = await stats.timed("POST /v1/pairing/redeem", lambda: c.post(
        "/v1/pairing/redeem", json={"code": code, "name": "Kitchen"}))
    if r is None or r.status_code != 200:
        return None
    return r.json()["instance_token"]


async def kitchen(c, stats, stop, token, interval, shares):
    h = {"Authorization": f"Bearer {token}"}
    n = 0
    while not stop.is_set():
        n += 1
        await stats.timed("POST /v1/ai/analyze", lambda: c.post(
            "/v1/ai/analyze", headers=h,
            data={"kind": "enrich", "text": "Black beans, 15 oz can, Mockery brand"}))
        if n % 5 == 0:
            await stats.timed("GET /v1/instance/me", lambda: c.get("/v1/instance/me", headers=h))
        if n % 20 == 0:
            r = await stats.timed("POST /v1/recipes/shares", lambda: c.post(
                "/v1/recipes/shares", headers=h, json={
                    "title": f"Chili {n}", "description": "Hearty and quick.",
                    "ingredients": ["1 lb beef", "1 can beans"],
                    "steps": ["Brown the beef.", "Simmer with beans."],
                    "attribution": "Family recipe"}))
            if r is not None and r.status_code == 200:
                shares.append(r.json()["token"])
        await asyncio.sleep(interval * random.uniform(0.5, 1.5))


async def visitor(c, stats, stop, shares):
    while not stop.is_set():
        if shares:
            t = random.choice(shares)
            await stats.timed("GET /r/{token}", lambda: c.get(f"/r/{t}"))
            if random.random() < 0.3:
                await stats.timed("GET /r/{token}/download", lambda: c.get(f"/r/{t}/download"))
        await asyncio.sleep(random.uniform(0.05, 0.3))


async def pairer(c, stats, stop, sessions, tokens, interval):
    while not stop.is_set():
        _aid, sess = random.choice(sessions)
        tok = await pair(c, stats, sess)
        if tok:
            tokens.append(tok)
        await asyncio.sleep(interval)


async def drive(base, args, sessions):
    import httpx
    stats = Stats()
    stop = asyncio.Event()
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=200)
    async with httpx.AsyncClient(base_url=base, timeout=60, limits=limits) as c:
        tokens = []
        for i in range(args.kitchens):
            tok = await pair(c, stats, sessions[i % len(sessions)][1])
            if tok:
                tokens.append(tok)
        stats.lat.clear()
        await c.get("/__loadtest/stats", params={"reset": 1})
        shares: list[str] = []
        tasks = [asyncio.create_task(kitchen(c, stats, stop, t, args.ai_interval, shares))
                 for t in tokens]
        tasks += [asyncio.create_task(visitor(c, stats, stop, shares))
                  for _ in range(args.visitors)]
        tasks.append(asyncio.create_task(pairer(c, stats, stop, sessions, [], args.pair_interval)))
        samples = []
        t0 = time.time()
        while time.time() - t0 < args.minutes * 60:
            await asyncio.sleep(min(args.sample_every, args.minutes * 60 - (time.time() - t0)))
            try:
                s = (await c.get("/__loadtest/stats", params={"reset": 1},
                                 timeout=300)).json()
            except Exception as e:  # noqa: BLE001
                print("stats request failed:", type(e).__name__, flush=True)
                continue
            done = sum(len(v) for v in stats.lat.values())
            samples.append({"t_min": round((time.time() - t0) / 60, 1), "proc": s["proc"],
                            "lag": s["lag_ms"], "requests": done})
            lag = s["lag_ms"]
            print(f"[{samples[-1]['t_min']:4.1f}m] rss={s['proc'].get('VmRSS')} fds={s['proc'].get('fds')}"
                  f" lag p50/p99/max={lag['p50']:.1f}/{lag['p99']:.1f}/{lag['max']:.1f}ms"
                  f" reqs={done} errs={sum(stats.err.values())}", flush=True)
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        final = (await c.get("/__loadtest/stats", timeout=300)).json()
    elapsed = args.minutes * 60
    lat = {k: {"n": len(v), "rps": round(len(v) / elapsed, 1), "p50": round(pct(v, .5), 1),
               "p95": round(pct(v, .95), 1), "p99": round(pct(v, .99), 1),
               "max": round(max(v), 1)} for k, v in sorted(stats.lat.items())}
    return {"latency": lat, "errors": dict(stats.err), "samples": samples,
            "stalls": final["stalls"], "sql": final["sql"], "proc": final["proc"]}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default="sqlite:///" + os.path.join(
        tempfile.gettempdir(), "forager-loadtest.sqlite"))
    ap.add_argument("--minutes", type=float, default=5)
    ap.add_argument("--accounts", type=int, default=50)
    ap.add_argument("--kitchens", type=int, default=60)
    ap.add_argument("--visitors", type=int, default=8)
    ap.add_argument("--ledger-rows", type=int, default=200_000)
    ap.add_argument("--ai-interval", type=float, default=1.0)
    ap.add_argument("--pair-interval", type=float, default=2.0)
    ap.add_argument("--sample-every", type=float, default=30)
    ap.add_argument("--port", type=int, default=19860)
    ap.add_argument("--gemini-port", type=int, default=19861)
    ap.add_argument("--report", default="forager-loadtest.json")
    args = ap.parse_args()

    t = time.time()
    sessions = seed(args.db, args.accounts, args.ledger_rows)
    print(f"seeded {args.accounts} accounts, {args.ledger_rows} ledger rows "
          f"in {time.time() - t:.0f}s", flush=True)
    gem = ThreadingHTTPServer(("127.0.0.1", args.gemini_port), Gemini)
    threading.Thread(target=gem.serve_forever, daemon=True).start()
    env = {**os.environ,
           "CLOUD_DATABASE_URL": args.db,
           "CLOUD_AI_FORWARDER": "gemini", "CLOUD_GEMINI_API_KEY": "stub",
           "LOADTEST_GEMINI_BASE": f"http://127.0.0.1:{args.gemini_port}/v1beta",
           "CLOUD_PROXY_RATE_PER_MINUTE": "0", "CLOUD_PAIRING_REDEEM_RATE_PER_MINUTE": "0",
           "CLOUD_SHARE_CREATE_RATE_PER_MINUTE": "0", "CLOUD_SIGNUP_RATE_PER_MINUTE": "0",
           "CLOUD_RETENTION_SWEEP_HOURS": "0", "CLOUD_TUNNEL_SWEEP_MINUTES": "0",
           "CLOUD_COOKIE_SECURE": "0",
           "CLOUD_TOTP_SECRET_KEY": "d7unhpVwhELezF83mmRqyIw_D7mrpagqElJ4SNZ6ZUg=",
           "PYTHONPATH": str(CLOUD / "scripts")}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "loadtest_probe:app", "--host", "127.0.0.1",
         "--port", str(args.port), "--workers", "1", "--log-level", "warning"],
        cwd=CLOUD, env=env)
    base = f"http://127.0.0.1:{args.port}"
    try:
        import httpx
        for _ in range(120):
            try:
                httpx.get(base + "/health", timeout=2)
                break
            except Exception:
                time.sleep(0.5)
        report = asyncio.run(drive(base, args, sessions))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        gem.shutdown()
    Path(args.report).write_text(json.dumps(report, indent=1))
    print(json.dumps({"latency": report["latency"], "errors": report["errors"],
                      "stalls": report["stalls"][:10],
                      "sql": {k: {kk: v[kk] for kk in ("requests", "mean", "max")}
                              for k, v in report["sql"].items()}}, indent=1))


if __name__ == "__main__":
    main()
