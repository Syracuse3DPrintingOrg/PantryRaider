#!/usr/bin/env python3
"""Populate a Pantry Raider instance with a curated demo pantry.

The goal is a stocked fridge and pantry that is rich enough for the
"What Can I Cook" page to return real suggestions, both from the free web
source (TheMealDB, which works out of the box) and from a Mealie recipe
library if the demo has one. The items are common recipe staples chosen so
that ordinary weeknight recipes match against them.

It talks to the running app's inventory import endpoint, which already
applies the shelf-life defaults, so each item only needs a name, a category,
and how it is stored. A spread of purchase dates gives the expiring views
something to show too.

Auth: requests from the machine the app runs on (127.0.0.1) are exempt from
login, so the simplest use is to run this on the device itself against the
default localhost URL. For a remote instance, pass --url and an --api-key
that matches the instance's API key.

Note: turn the demo's read-only "demo mode" OFF while seeding. Demo mode
blocks every write, so an import while it is on comes back refused. Seed
first, then switch demo mode on to freeze the instance.

Examples:
    # On the device over SSH (no auth needed on loopback):
    python3 scripts/seed-demo-inventory.py

    # Against a remote instance:
    python3 scripts/seed-demo-inventory.py \
        --url https://demo.example.com --api-key "$PANTRY_API_KEY"

    # See exactly what would be sent without touching anything:
    python3 scripts/seed-demo-inventory.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta

DEFAULT_URL = "http://127.0.0.1:9284"

# Curated staples. Each row: name, quantity, unit, category, storage_type,
# days_ago (when it was bought, so best-by lands on a realistic spread).
# Categories and storage types must match the app's enums (models/food.py):
#   category:     Poultry Meat Seafood Dairy Produce Grains Condiments
#                 Beverages Snacks Frozen Canned Other
#   storage_type: refrigerated frozen room_temp dry
_ITEMS: list[tuple[str, float, str, str, str, int]] = [
    # Proteins
    ("Chicken Breast",       2,   "lb",   "Poultry",    "refrigerated", 1),
    ("Ground Beef",          1,   "lb",   "Meat",       "refrigerated", 2),
    ("Bacon",                1,   "pack", "Meat",       "refrigerated", 4),
    ("Salmon Fillet",        2,   "item", "Seafood",    "frozen",       10),
    ("Large Eggs",           12,  "item", "Dairy",      "refrigerated", 5),
    # Dairy
    ("Whole Milk",           1,   "item", "Dairy",      "refrigerated", 3),
    ("Butter",               1,   "item", "Dairy",      "refrigerated", 7),
    ("Cheddar Cheese",       1,   "item", "Dairy",      "refrigerated", 6),
    ("Parmesan Cheese",      1,   "item", "Dairy",      "refrigerated", 9),
    ("Greek Yogurt",         1,   "item", "Dairy",      "refrigerated", 2),
    ("Heavy Cream",          1,   "item", "Dairy",      "refrigerated", 4),
    # Produce
    ("Yellow Onion",         3,   "item", "Produce",    "room_temp",    5),
    ("Garlic",               1,   "item", "Produce",    "room_temp",    8),
    ("Roma Tomato",          5,   "item", "Produce",    "room_temp",    2),
    ("Russet Potato",        5,   "item", "Produce",    "room_temp",    9),
    ("Carrot",               6,   "item", "Produce",    "refrigerated", 6),
    ("Bell Pepper",          3,   "item", "Produce",    "refrigerated", 3),
    ("Baby Spinach",         1,   "item", "Produce",    "refrigerated", 1),
    ("Mushrooms",            1,   "item", "Produce",    "refrigerated", 2),
    ("Lemon",                3,   "item", "Produce",    "refrigerated", 7),
    ("Broccoli",             1,   "item", "Produce",    "refrigerated", 3),
    ("Celery",               1,   "item", "Produce",    "refrigerated", 5),
    ("Green Onion",          1,   "item", "Produce",    "refrigerated", 2),
    # Grains and baking
    ("White Rice",           2,   "lb",   "Grains",     "dry",          30),
    ("Spaghetti",            1,   "item", "Grains",     "dry",          40),
    ("All-Purpose Flour",    1,   "item", "Grains",     "dry",          25),
    ("Bread",                1,   "item", "Grains",     "room_temp",    2),
    ("Rolled Oats",          1,   "item", "Grains",     "dry",          35),
    # Canned and pantry
    ("Canned Diced Tomatoes",2,   "item", "Canned",     "dry",          20),
    ("Tomato Paste",         1,   "item", "Canned",     "dry",          20),
    ("Black Beans",          2,   "item", "Canned",     "dry",          22),
    ("Chicken Stock",        2,   "item", "Canned",     "dry",          18),
    # Condiments and staples
    ("Olive Oil",            1,   "item", "Condiments", "dry",          45),
    ("Soy Sauce",            1,   "item", "Condiments", "dry",          50),
    ("Honey",                1,   "item", "Condiments", "dry",          60),
    ("Sugar",                1,   "item", "Condiments", "dry",          40),
]


def build_items(today: date) -> list[dict]:
    """Turn the curated table into FoodItem payloads with spread purchase dates."""
    items = []
    for name, qty, unit, category, storage, days_ago in _ITEMS:
        items.append({
            "name": name,
            "quantity": float(qty),
            "unit": unit,
            "category": category,
            "storage_type": storage,
            "purchased_on": (today - timedelta(days=days_ago)).isoformat(),
        })
    return items


def _request(url: str, payload: dict, api_key: str, timeout: float) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    if api_key:
        req.add_header("X-API-Key", api_key)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Seed a Pantry Raider demo with sample inventory.")
    p.add_argument("--url", default=DEFAULT_URL,
                   help=f"Base URL of the instance (default {DEFAULT_URL}).")
    p.add_argument("--api-key", default="",
                   help="X-API-Key for a remote instance. Not needed on localhost.")
    p.add_argument("--timeout", type=float, default=60.0, help="Per-request timeout, seconds.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the items that would be sent and exit without posting.")
    args = p.parse_args(argv)

    base = args.url.rstrip("/")
    items = build_items(date.today())

    if args.dry_run:
        print(f"Would send {len(items)} items to {base}/inventory/import:")
        for it in items:
            print(f"  - {it['name']:24s} {it['category']:11s} {it['storage_type']}")
        return 0

    print(f"Seeding {len(items)} items into {base} ...")
    try:
        result = _request(f"{base}/inventory/import", {"items": items},
                          args.api_key, args.timeout)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"Request failed: HTTP {e.code}", file=sys.stderr)
        print(f"  {body}", file=sys.stderr)
        if e.code == 403 and "demo" in body.lower():
            print("  Looks like demo mode is on. Turn it off, seed, then turn it "
                  "back on.", file=sys.stderr)
        elif e.code == 401:
            print("  Unauthorized. Run this on the device (localhost is exempt) "
                  "or pass --api-key.", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"Could not reach {base}: {e.reason}", file=sys.stderr)
        print("  Is the app running, and is the URL right?", file=sys.stderr)
        return 1

    imported = result.get("imported", 0)
    errors = [r for r in result.get("results", []) if r.get("status") == "error"]
    print(f"Imported {imported} of {len(items)} items.")
    if errors:
        print(f"{len(errors)} item(s) failed:", file=sys.stderr)
        for r in errors[:10]:
            name = items[r["index"]]["name"] if r.get("index") is not None else "?"
            print(f"  - {name}: {r.get('error')}", file=sys.stderr)
        print("  A common cause is Grocy not being set up on this instance yet.",
              file=sys.stderr)
        return 1

    print("Done. Open \"What Can I Cook\" to see recipe suggestions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
