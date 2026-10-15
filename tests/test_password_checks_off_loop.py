"""Password and recovery-code checks never run on the event loop.

A scrypt check takes about 120 ms on a desktop and several times that on a
Raspberry Pi. Run inside an ``async def`` route it freezes every other request
for that long, kiosk polls included, and a stream of wrong passwords would
freeze the app for as long as it lasted. Async routes hand these to a thread
(``asyncio.to_thread`` or ``run_in_threadpool``); sync routes already run in
one. This walks every router and fails on a direct call from an async route.
"""
from __future__ import annotations

import ast
import asyncio
import time
from pathlib import Path

ROUTERS = Path(__file__).resolve().parents[1] / "service" / "app" / "routers"

# Calls that run scrypt (directly or through one of these helpers).
SLOW = {
    "verify_secret", "hash_secret", "hash_recovery_codes", "consume_recovery_code",
    "_local_2fa_credential_ok", "_consume_local_recovery",
    "_require_current_password", "_require_backup_password",
}


def _name(func) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _direct_slow_calls(fn: ast.AsyncFunctionDef):
    found = []

    def visit(node):
        for child in ast.iter_child_nodes(node):
            # A nested sync def is a helper that runs wherever it is called.
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Call) and _name(child.func) in SLOW:
                found.append((_name(child.func), child.lineno))
            visit(child)

    visit(fn)
    return found


def test_no_async_route_runs_a_password_check_on_the_loop():
    offenders = []
    for path in sorted(ROUTERS.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                for name, line in _direct_slow_calls(node):
                    offenders.append(f"{path.relative_to(ROUTERS)}:{line} {node.name} calls {name}")
    assert not offenders, "\n".join(offenders)


def test_scanner_sees_a_direct_call():
    tree = ast.parse(
        "async def login(p, s):\n"
        "    if verify_secret(p, s):\n"
        "        return 1\n"
        "    return await asyncio.to_thread(verify_secret, p, s)\n")
    fn = tree.body[0]
    assert _direct_slow_calls(fn) == [("verify_secret", 2)]


def test_a_login_check_leaves_the_loop_free():
    """The real check, run the way the routes now run it, lets other tasks go on."""
    from app.passwords import hash_secret, verify_secret
    stored = hash_secret("correct horse")

    async def flow():
        ticks = []

        async def ticker():
            for _ in range(50):
                ticks.append(time.perf_counter())
                await asyncio.sleep(0.002)

        t = asyncio.create_task(ticker())
        ok = await asyncio.to_thread(verify_secret, "wrong", stored)
        await t
        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        return ok, max(gaps)

    ok, worst_gap = asyncio.run(flow())
    assert ok is False
    assert worst_gap < 0.08
