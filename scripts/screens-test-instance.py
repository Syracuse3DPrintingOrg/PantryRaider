#!/usr/bin/env python3
"""Run a throwaway Pantry Raider for trying screens, timers and voice against.

Starts the mock Grocy/Mealie the browser tests use plus the real app from
this checkout, with its own data folder, a password, and a few recipes in the
library. Nothing here touches a real install.

    python scripts/screens-test-instance.py --port 19284 --data ~/pr-screens-test

On first run it writes three files into the data folder's parent, each mode
600: screens-test-password, screens-test-ha-key and screens-test-screen-key.
Stop it with Ctrl-C (or stop the unit it runs under).
"""
from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests" / "browser"))
sys.path.insert(0, str(REPO / "service"))

import _mockstack  # noqa: E402

RECIPES = [
    {"name": "Lasagna", "servings": 8,
     "ingredients": ["12 lasagna noodles", "1 lb ground beef", "24 oz marinara sauce",
                     "15 oz ricotta", "2 cups shredded mozzarella", "1/2 cup grated parmesan"],
     "steps": ["Heat the oven to 375 F and boil the noodles for 8 minutes.",
               "Brown the beef, then stir in the marinara and simmer for 10 minutes.",
               "Layer noodles, ricotta, sauce and mozzarella in a baking dish.",
               "Cover with foil and bake for 45 minutes.",
               "Uncover, add the parmesan and bake for 10 minutes. Rest for 15 minutes."]},
    {"name": "Lasagna Soup", "servings": 6,
     "ingredients": ["1 lb Italian sausage", "1 onion", "6 cups chicken broth",
                     "8 oz broken lasagna noodles", "1 cup ricotta"],
     "steps": ["Brown the sausage with the onion.",
               "Add the broth and noodles and simmer for 12 minutes.",
               "Serve with a spoonful of ricotta."]},
    {"name": "Weeknight Chicken Tacos", "servings": 4,
     "ingredients": ["1 lb chicken thighs", "8 tortillas", "1 lime", "1 cup salsa"],
     "steps": ["Cook the chicken for 6 minutes a side.", "Slice, squeeze the lime over it, and serve."]},
    {"name": "Fish Tacos", "servings": 4,
     "ingredients": ["1 lb cod", "8 tortillas", "2 cups shredded cabbage", "1 lime"],
     "steps": ["Pan fry the cod for 4 minutes a side.", "Serve in tortillas with the cabbage."]},
    {"name": "Banana Bread", "servings": 10,
     "ingredients": ["3 ripe bananas", "2 cups flour", "1 cup sugar", "1 egg", "1/2 cup butter"],
     "steps": ["Mash the bananas and mix everything.", "Bake at 350 F for 60 minutes."]},
    {"name": "Best-Ever Key Lime Pie", "servings": 8,
     "ingredients": ["1 1/2 cups graham cracker crumbs", "6 tbsp melted butter",
                     "14 oz sweetened condensed milk", "4 egg yolks", "1/2 cup key lime juice"],
     "steps": ["Heat the oven to 350 F.",
               "Press the crumbs and butter into a pie dish and bake the crust for 10 minutes.",
               "Whisk the condensed milk, yolks and lime juice until smooth.",
               "Pour the filling into the crust and bake for 15 minutes.",
               "Cool for 30 minutes, then chill for 3 hours before slicing."]},
    {"name": "Key Lime Pie Bars", "servings": 16,
     "ingredients": ["2 cups graham cracker crumbs", "1/2 cup butter", "2 cans condensed milk",
                     "3/4 cup key lime juice"],
     "steps": ["Press the crust into a lined pan and bake for 12 minutes.",
               "Stir the filling together, pour it over and bake for 20 minutes.",
               "Chill for 2 hours, then cut into bars."]},
]


def _secret_file(path: Path, value: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(value + "\n")


def seed(data_dir: Path) -> None:
    """Recipes, a password and two keys, written with the app's own code
    before it starts."""
    os.environ["DATA_DIR"] = str(data_dir)
    from app.config import settings
    from app.database import Base, SessionLocal, engine
    import app.models.db_models  # noqa: F401  (registers the tables)
    Base.metadata.create_all(bind=engine)
    from app.passwords import hash_secret
    from app.services import credentials, recipe_store
    db = SessionLocal()
    try:
        if recipe_store.count(db) == 0:
            for r in RECIPES:
                recipe_store.create_from_parsed(db, {
                    "name": r["name"], "servings": r["servings"],
                    "ingredients": r["ingredients"], "instructions": r["steps"]})
    finally:
        db.close()
    out = data_dir.parent
    pw_file = out / "screens-test-password"
    if not pw_file.exists():
        password = secrets.token_urlsafe(12)
        _secret_file(pw_file, password)
        settings.save({"auth_password": hash_secret(password), "auth_required": True})
        for scope, name in (("ha", "Home Assistant (test)"), ("screen", "Kitchen tablet (test)")):
            _rec, key = credentials.mint(scope, name, "manual", created_by="test instance")
            _secret_file(out / f"screens-test-{scope}-key", key)
        credentials.flush()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=19284)
    ap.add_argument("--mock-port", type=int, default=19285)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--data", default=str(Path.home() / "pr-screens-test" / "data"))
    args = ap.parse_args()
    data_dir = Path(args.data).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)

    mock = ThreadingHTTPServer(("127.0.0.1", args.mock_port), _mockstack.MockHandler)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    env = _mockstack.app_env(args.mock_port, str(data_dir), {
        "AUTH_REQUIRED": "true", "DEPLOYMENT_MODE": "server"})
    # The browser tests share a fixed session secret. This instance listens
    # on the network, so it makes and keeps its own instead.
    env.pop("SECRET_KEY", None)
    # Recipes come from the app's own library here, not the mock Mealie.
    env.pop("MEALIE_BASE_URL", None)
    env.pop("MEALIE_API_KEY", None)
    subprocess.run([sys.executable, "-c",
                    "import sys; sys.path.insert(0, 'scripts'); "
                    "import importlib.util as u; "
                    "s = u.spec_from_file_location('sti', 'scripts/screens-test-instance.py'); "
                    "m = u.module_from_spec(s); s.loader.exec_module(m); "
                    f"m.seed(m.Path({str(data_dir)!r}))"],
                   cwd=REPO, env=env, check=True)
    try:
        subprocess.run([sys.executable, "-m", "uvicorn", "app.main:app", "--host", args.host,
                        "--port", str(args.port), "--workers", "1", "--log-level", "warning",
                        "--timeout-graceful-shutdown", "2"],
                       cwd=REPO / "service", env=env)
    finally:
        mock.shutdown()


if __name__ == "__main__":
    main()
