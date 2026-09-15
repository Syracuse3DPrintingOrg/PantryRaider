"""Reusable mock Grocy + Mealie backend and app boot for browser tests.

Extracted from scripts/capture-screenshots.py (which now imports from here) so
the headless-browser suite in tests/browser/ and the screenshot tool boot the
exact same standalone app: a stdlib HTTP server playing Grocy and Mealie with
believable demo data, plus a real uvicorn subprocess running the FastAPI app
against it. No Docker and no network.

Demo expiry dates are generated relative to today so urgency badges always
look right.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

TODAY = date.today()


def _d(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# Demo data: a believable kitchen. Locations map onto the app's four storage
# buckets (storage_categories.BUILTIN_CATEGORIES), groups onto categories.
# ---------------------------------------------------------------------------

LOCATIONS = [
    {"id": 1, "name": "Refrigerator"},
    {"id": 2, "name": "Freezer"},
    {"id": 3, "name": "Counter / Room Temp"},
    {"id": 4, "name": "Pantry / Dry Storage"},
]
GROUPS = [
    {"id": 1, "name": "Poultry"}, {"id": 2, "name": "Dairy"},
    {"id": 3, "name": "Produce"}, {"id": 4, "name": "Meat"},
    {"id": 5, "name": "Seafood"}, {"id": 6, "name": "Grains"},
    {"id": 7, "name": "Canned"}, {"id": 8, "name": "Condiments"},
]

# name, qty, unit, location_id, group_id, days-until-best-by
_STOCK_ROWS = [
    ("Chicken Breast", 2, "lb", 1, 1, 3),
    ("Whole Milk", 1, "gal", 1, 2, 5),
    ("Sharp Cheddar", 0.5, "lb", 1, 2, 14),
    ("Baby Spinach", 1, "bag", 1, 3, 2),
    ("Greek Yogurt", 3, "cup", 1, 2, 7),
    ("Eggs", 10, "ea", 1, 2, 18),
    ("Ground Beef (80/20)", 1.5, "lb", 2, 4, 90),
    ("Frozen Corn", 2, "bag", 2, 3, 180),
    ("Shrimp", 1, "lb", 2, 5, 60),
    ("Bananas", 5, "ea", 3, 3, 4),
    ("Avocados", 3, "ea", 3, 3, 2),
    ("Jasmine Rice", 5, "lb", 4, 6, 365),
    ("Olive Oil", 1, "bottle", 4, 8, 200),
    ("Canned Tomatoes", 4, "can", 4, 7, 500),
    ("Black Beans", 3, "can", 4, 7, 600),
    ("Pasta (Penne)", 2, "box", 4, 6, 400),
]

STOCK = [
    {
        "product_id": i + 1,
        "amount": qty,
        "best_before_date": _d(days),
        "location_id": loc,
        "product": {
            "name": name, "location_id": loc, "product_group_id": grp,
            "qu_unit_stock": {"name": unit},
            "location": {"name": next(l["name"] for l in LOCATIONS if l["id"] == loc)},
        },
    }
    for i, (name, qty, unit, loc, grp, days) in enumerate(_STOCK_ROWS)
]
STOCK_LOG = [
    {"product_id": i + 1,
     "row_created_timestamp": (datetime.now() - timedelta(days=i % 9)).strftime("%Y-%m-%d %H:%M:%S")}
    for i in range(len(_STOCK_ROWS))
]

# Recipes whose ingredients token-match the stock above so the Cook page fills
# all three tiers (ready / with staples / worth shopping for).
RECIPES = [
    {"id": "r1", "slug": "cheesy-chicken-rice", "name": "Cheesy Chicken and Rice",
     "description": "One-pan chicken with jasmine rice, corn, and melted cheddar.",
     "image": None, "recipeIngredient": [
         {"note": "2 chicken breasts"}, {"note": "1 cup jasmine rice"},
         {"note": "1 cup frozen corn"}, {"note": "1 cup shredded sharp cheddar"}]},
    {"id": "r2", "slug": "banana-yogurt-smoothie", "name": "Banana Yogurt Smoothie",
     "description": "Quick breakfast blend of ripe bananas, greek yogurt, and milk.",
     "image": None, "recipeIngredient": [
         {"note": "2 bananas"}, {"note": "1 cup greek yogurt"},
         {"note": "1 cup whole milk"}]},
    {"id": "r3", "slug": "spinach-cheddar-omelette", "name": "Spinach Cheddar Omelette",
     "description": "Fluffy omelette with wilted spinach and sharp cheddar.",
     "image": None, "recipeIngredient": [
         {"note": "3 eggs"}, {"note": "1 cup baby spinach"},
         {"note": "sharp cheddar"}, {"note": "1 tbsp butter"}]},
    {"id": "r4", "slug": "beef-black-bean-chili", "name": "Beef and Black Bean Chili",
     "description": "Hearty chili with ground beef, black beans, and tomatoes.",
     "image": None, "recipeIngredient": [
         {"note": "1 lb ground beef"}, {"note": "1 can black beans"},
         {"note": "1 can canned tomatoes"}, {"note": "smoked paprika"}]},
    {"id": "r5", "slug": "creamy-chicken-penne", "name": "Creamy Chicken Penne",
     "description": "Penne tossed with seared chicken in a light cream sauce.",
     "image": None, "recipeIngredient": [
         {"note": "1 chicken breast"}, {"note": "2 cups penne pasta"},
         {"note": "1 cup heavy cream"}, {"note": "grated parmesan"}]},
    {"id": "r6", "slug": "shakshuka", "name": "Shakshuka",
     "description": "Eggs poached in spiced tomato sauce.",
     "image": None, "recipeIngredient": [
         {"note": "6 eggs"}, {"note": "1 can canned tomatoes"},
         {"note": "olive oil"}, {"note": "1 red bell pepper"}]},
]

_PLAN_ROWS = [
    (0, "breakfast", "banana-yogurt-smoothie"),
    (0, "dinner", "cheesy-chicken-rice"),
    (1, "breakfast", "spinach-cheddar-omelette"),
    (1, "dinner", "beef-black-bean-chili"),
    (2, "lunch", None),
    (2, "dinner", "creamy-chicken-penne"),
    (3, "breakfast", "shakshuka"),
    (4, "dinner", "beef-black-bean-chili"),
    (5, "dinner", "cheesy-chicken-rice"),
    (6, "breakfast", "banana-yogurt-smoothie"),
]
_RECIPES_BY_SLUG = {r["slug"]: r for r in RECIPES}
MEALPLAN = [
    {"id": i + 1, "date": _d(day), "entryType": etype,
     "title": "" if slug else "Leftovers",
     "recipe": ({"slug": slug, "name": _RECIPES_BY_SLUG[slug]["name"]} if slug else None)}
    for i, (day, etype, slug) in enumerate(_PLAN_ROWS)
]


# ---------------------------------------------------------------------------
# Mock Grocy + Mealie on one port
# ---------------------------------------------------------------------------

class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self._json({"ok": True})

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        m = re.match(r"^/api/recipes/([^/]+)$", path)
        if m:
            recipe = _RECIPES_BY_SLUG.get(m.group(1))
            return self._json(recipe if recipe else {"detail": "not found"},
                              200 if recipe else 404)
        routes = {
            # Grocy
            "/api/system/info": {"grocy_version": {"Version": "4.2.0"}, "grocy_version_string": "4.2.0"},
            "/api/stock": STOCK,
            "/api/objects/locations": LOCATIONS,
            "/api/objects/product_groups": GROUPS,
            "/api/objects/stock": STOCK_LOG,
            "/api/objects/quantity_units": [],
            "/api/objects/products": [],
            # Mealie
            "/api/users/self": {"username": "demo", "email": "demo@example.com"},
            "/api/recipes": {"items": [{"slug": r["slug"]} for r in RECIPES]},
            "/api/households/mealplans": {"items": MEALPLAN},
        }
        if path in routes:
            return self._json(routes[path])
        return self._json({"detail": f"mock: no route {path}"}, 404)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, timeout: float = 30.0) -> None:
    import httpx
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            httpx.get(url, timeout=2.0)
            return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f"{url} did not come up")


def app_env(mock_port: int, data_dir: str, extra: dict | None = None) -> dict:
    """The environment for a standalone app run against the mock backend."""
    env = {
        **os.environ,
        "AUTH_REQUIRED": "false",
        "SECRET_KEY": "browser-test-session",
        "DATA_DIR": data_dir,
        "GROCY_BASE_URL": f"http://127.0.0.1:{mock_port}",
        "GROCY_API_KEY": "demo",
        "MEALIE_BASE_URL": f"http://127.0.0.1:{mock_port}",
        "MEALIE_API_KEY": "demo",
        "RECIPE_SOURCE": "off",
        # Marks AI features as available in the UI; no AI call is ever made.
        "VISION_PROVIDER": "anthropic",
        "ANTHROPIC_API_KEY": "demo-placeholder",
    }
    env.update(extra or {})
    return env


@contextmanager
def boot_app(extra_env: dict | None = None):
    """Start the mock backend plus the real app; yield the app's base URL.

    The app runs as a uvicorn subprocess (like the container does) so its
    in-memory registries (timers, scanner mode, current recipe) behave exactly
    as deployed. Everything is torn down on exit.
    """
    mock_port, app_port = free_port(), free_port()
    mock = ThreadingHTTPServer(("127.0.0.1", mock_port), MockHandler)
    threading.Thread(target=mock.serve_forever, daemon=True).start()

    data_dir = tempfile.mkdtemp(prefix="pr-browser-")
    app = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(app_port), "--log-level", "warning"],
        cwd=REPO / "service", env=app_env(mock_port, data_dir, extra_env),
    )
    try:
        base = f"http://127.0.0.1:{app_port}"
        wait_http(f"{base}/health")
        yield base
    finally:
        app.terminate()
        app.wait(timeout=10)
        mock.shutdown()


def chromium_executable() -> str:
    """An explicitly pinned Chromium build, or "" for Playwright's default.

    CHROMIUM_EXECUTABLE wins; otherwise a prepared PLAYWRIGHT_BROWSERS_PATH
    directory containing a ``chromium`` binary/dir is used (the environment on
    some runners ships one). Empty string means launch() should use whatever
    ``playwright install chromium`` put in place.
    """
    exe = os.environ.get("CHROMIUM_EXECUTABLE", "")
    if exe:
        return exe
    candidate = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")) / "chromium"
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH") and candidate.exists():
        return str(candidate)
    return ""
