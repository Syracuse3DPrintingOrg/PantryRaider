"""Keep blocking work off the event loop (FoodAssistant-7jnvb).

Every deployment runs one uvicorn worker, so an ``async def`` that blocks
stalls every other request on the box, kiosk polls included. FastAPI runs a
plain ``def`` route in its threadpool, and an ``async def`` hands blocking work
to a worker with ``run_in_threadpool`` (or ``asyncio.to_thread``).

This scan reads the source with ``ast`` (nothing is imported or run) and fails
on two patterns inside an ``async def`` under service/app:

1. A direct call to ``subprocess.run`` / ``Popen`` / ``check_output`` /
   ``check_call`` / ``call``, ``os.system`` or ``time.sleep``.
2. A SQLAlchemy session parameter (``db: Session = Depends(get_db)``, or any
   parameter annotated ``Session``) used on the loop. The session may only be
   handed to a worker (an argument of ``run_in_threadpool`` / ``to_thread``),
   used inside a nested ``def`` or ``lambda`` (which is what gets handed over),
   or passed to an awaited call (whose own ``async def`` this same scan
   checks).

Code nested in a ``def`` or ``lambda`` inside the ``async def`` is not counted,
because that is the shape of work being sent to a thread.
"""
from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "service" / "app"

BLOCKING_CALLS = {
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.check_output",
    "subprocess.check_call",
    "subprocess.call",
    "os.system",
    "time.sleep",
}

OFFLOAD_CALLS = {
    "run_in_threadpool",
    "to_thread",
    "run_sync",
}

# "relative/path.py::function" -> why it may stay as it is.
ALLOWLIST: dict[str, str] = {
    "routers/kiosk_status.py::_gather_satellite":
        "gathers the pending and action counts, which forward upstream on a "
        "satellite before they would ever touch the session",
}


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _walk_same_scope(node: ast.AST):
    """Yield (child, parent) pairs, not descending into nested defs/lambdas."""
    stack = [(child, node) for child in ast.iter_child_nodes(node)]
    while stack:
        child, parent = stack.pop()
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.Lambda, ast.ClassDef)):
            continue
        yield child, parent
        stack.extend((grand, child) for grand in ast.iter_child_nodes(child))


def _session_params(fn: ast.AsyncFunctionDef) -> set[str]:
    args = fn.args
    params = args.posonlyargs + args.args + args.kwonlyargs
    names = set()
    defaults = dict(zip(
        [a.arg for a in (args.posonlyargs + args.args)][-len(args.defaults):]
        if args.defaults else [],
        args.defaults,
    ))
    for a, d in zip(args.kwonlyargs, args.kw_defaults):
        if d is not None:
            defaults[a.arg] = d
    for a in params:
        ann = ast.unparse(a.annotation) if a.annotation is not None else ""
        default = defaults.get(a.arg)
        if ann.split(".")[-1] == "Session" or (
                default is not None and "get_db" in ast.unparse(default)):
            names.add(a.arg)
    return names


def _call_name(call: ast.Call) -> str:
    name = _dotted(call.func) or ""
    return name.rsplit(".", 1)[-1]


def _scan_function(fn: ast.AsyncFunctionDef) -> list[str]:
    """Problems inside one async def, as "line: what" strings."""
    sessions = _session_params(fn)
    pairs = list(_walk_same_scope(fn))
    awaited = {id(n.value) for n, _ in pairs
               if isinstance(n, ast.Await) and isinstance(n.value, ast.Call)}
    parent_of = {id(n): p for n, p in pairs}
    problems = []
    for node, parent in pairs:
        if isinstance(node, ast.Call) and _dotted(node.func) in BLOCKING_CALLS:
            problems.append(f"{node.lineno}: calls {_dotted(node.func)}")
        if (isinstance(node, ast.Name) and node.id in sessions
                and isinstance(node.ctx, ast.Load)):
            # A bound method of the session (run_in_threadpool(db.commit)) is
            # handed over the same way as the session itself.
            arg = parent if isinstance(parent, ast.Attribute) else node
            call = parent_of.get(id(arg))
            if isinstance(call, ast.keyword):
                call = parent_of.get(id(call))
            handed_off = (
                isinstance(call, ast.Call) and arg is not call.func
                and (_call_name(call) in OFFLOAD_CALLS or id(call) in awaited))
            if not handed_off:
                problems.append(
                    f"{node.lineno}: uses session {node.id!r} on the event loop")
    return problems


def _offenders() -> list[str]:
    found: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            key = f"{rel}::{fn.name}"
            if key in ALLOWLIST:
                continue
            found.extend(f"{key}:{p}" for p in _scan_function(fn))
    return found


def test_no_blocking_work_on_the_event_loop():
    offenders = _offenders()
    assert not offenders, (
        "Blocking work inside an async def runs on the only event loop. Make "
        "the route a plain def, or hand the blocking part to "
        "run_in_threadpool:\n  " + "\n  ".join(offenders))


def test_allowlist_entries_still_exist():
    """A stale allowlist entry would quietly excuse a future regression."""
    names = set()
    for path in APP.rglob("*.py"):
        rel = path.relative_to(APP).as_posix()
        for fn in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(fn, ast.AsyncFunctionDef):
                names.add(f"{rel}::{fn.name}")
    assert not (set(ALLOWLIST) - names), sorted(set(ALLOWLIST) - names)


def test_scan_catches_the_patterns():
    """The scanner itself flags each pattern and accepts the offloaded shape."""
    src = '''
async def bad_sleep():
    time.sleep(1)

async def bad_db(db: Session = Depends(get_db)):
    return db.query(X).all()

async def bad_commit(db: Session = Depends(get_db)):
    db.commit()

async def good_db(db: Session = Depends(get_db)):
    rows = await run_in_threadpool(lambda: db.query(X).all())
    other = await run_in_threadpool(load, db)
    await run_in_threadpool(db.rollback)
    return await helper(db=db)

async def good_nested():
    def work():
        subprocess.run(["lp"])
    await asyncio.to_thread(work)
'''
    tree = ast.parse(src)
    bad = {fn.name for fn in tree.body if _scan_function(fn)}
    assert bad == {"bad_sleep", "bad_db", "bad_commit"}
