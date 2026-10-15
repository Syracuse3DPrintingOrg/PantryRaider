"""
Standalone FastAPI app that serves the Pantry Raider templates with realistic
mock data, for screenshot generation. Bypasses Grocy/Mealie entirely.
Run with: uvicorn screenshot_server:app --port 8999
"""
import json
from datetime import date, timedelta
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from pathlib import Path

# Minimal settings shim before importing templates
import sys; sys.path.insert(0, str(Path(__file__).parent))

from app.templating import templates
from app.navigation import all_tabs
from app.storage_categories import all_categories, OTHER

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key="screenshot-key")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "app/static"), name="static")

TODAY = date.today()

def _days(n): return (TODAY + timedelta(days=n)).isoformat()

MOCK_STOCK = {
    "refrigerated": [
        {"product_id": 1, "name": "Chicken Breast", "amount": 2, "unit": "kg",
         "best_before_date": _days(3), "days_remaining": 3, "urgency": "3d",
         "location_name": "Refrigerator", "storage_bucket": "refrigerated", "category": "Poultry", "added_date": _days(-2)},
        {"product_id": 2, "name": "Whole Milk", "amount": 1, "unit": "gal",
         "best_before_date": _days(5), "days_remaining": 5, "urgency": "7d",
         "location_name": "Refrigerator", "storage_bucket": "refrigerated", "category": "Dairy", "added_date": _days(-1)},
        {"product_id": 3, "name": "Sharp Cheddar", "amount": 0.5, "unit": "lb",
         "best_before_date": _days(14), "days_remaining": 14, "urgency": "ok",
         "location_name": "Refrigerator", "storage_bucket": "refrigerated", "category": "Dairy", "added_date": _days(-3)},
        {"product_id": 4, "name": "Baby Spinach", "amount": 1, "unit": "bag",
         "best_before_date": _days(2), "days_remaining": 2, "urgency": "3d",
         "location_name": "Refrigerator", "storage_bucket": "refrigerated", "category": "Produce", "added_date": _days(-1)},
        {"product_id": 5, "name": "Greek Yogurt", "amount": 3, "unit": "cup",
         "best_before_date": _days(7), "days_remaining": 7, "urgency": "7d",
         "location_name": "Refrigerator", "storage_bucket": "refrigerated", "category": "Dairy", "added_date": _days(-4)},
        {"product_id": 6, "name": "Eggs", "amount": 10, "unit": "ea",
         "best_before_date": _days(18), "days_remaining": 18, "urgency": "ok",
         "location_name": "Refrigerator", "storage_bucket": "refrigerated", "category": "Dairy", "added_date": _days(-5)},
    ],
    "frozen": [
        {"product_id": 7, "name": "Ground Beef (80/20)", "amount": 1.5, "unit": "lb",
         "best_before_date": _days(90), "days_remaining": 90, "urgency": "ok",
         "location_name": "Freezer", "storage_bucket": "frozen", "category": "Meat", "added_date": _days(-10)},
        {"product_id": 8, "name": "Frozen Corn", "amount": 2, "unit": "bag",
         "best_before_date": _days(180), "days_remaining": 180, "urgency": "ok",
         "location_name": "Freezer", "storage_bucket": "frozen", "category": "Produce", "added_date": _days(-20)},
        {"product_id": 9, "name": "Shrimp", "amount": 1, "unit": "lb",
         "best_before_date": _days(60), "days_remaining": 60, "urgency": "ok",
         "location_name": "Freezer", "storage_bucket": "frozen", "category": "Seafood", "added_date": _days(-7)},
    ],
    "room_temp": [
        {"product_id": 10, "name": "Bananas", "amount": 5, "unit": "ea",
         "best_before_date": _days(4), "days_remaining": 4, "urgency": "7d",
         "location_name": "Counter / Room Temp", "storage_bucket": "room_temp", "category": "Produce", "added_date": _days(-2)},
        {"product_id": 11, "name": "Avocados", "amount": 3, "unit": "ea",
         "best_before_date": _days(2), "days_remaining": 2, "urgency": "3d",
         "location_name": "Counter / Room Temp", "storage_bucket": "room_temp", "category": "Produce", "added_date": _days(-1)},
    ],
    "pantry": [
        {"product_id": 12, "name": "Jasmine Rice", "amount": 5, "unit": "lb",
         "best_before_date": _days(365), "days_remaining": 365, "urgency": "ok",
         "location_name": "Pantry / Dry Storage", "storage_bucket": "pantry", "category": "Grains", "added_date": _days(-30)},
        {"product_id": 13, "name": "Olive Oil", "amount": 1, "unit": "bottle",
         "best_before_date": _days(200), "days_remaining": 200, "urgency": "ok",
         "location_name": "Pantry / Dry Storage", "storage_bucket": "pantry", "category": "Condiments", "added_date": _days(-14)},
        {"product_id": 14, "name": "Canned Tomatoes", "amount": 4, "unit": "can",
         "best_before_date": _days(500), "days_remaining": 500, "urgency": "ok",
         "location_name": "Pantry / Dry Storage", "storage_bucket": "pantry", "category": "Canned", "added_date": _days(-7)},
        {"product_id": 15, "name": "Black Beans", "amount": 3, "unit": "can",
         "best_before_date": _days(600), "days_remaining": 600, "urgency": "ok",
         "location_name": "Pantry / Dry Storage", "storage_bucket": "pantry", "category": "Canned", "added_date": _days(-7)},
        {"product_id": 16, "name": "Pasta (Penne)", "amount": 2, "unit": "box",
         "best_before_date": _days(400), "days_remaining": 400, "urgency": "ok",
         "location_name": "Pantry / Dry Storage", "storage_bucket": "pantry", "category": "Grains", "added_date": _days(-20)},
    ],
    "other": [],
}

MOCK_RECIPES = [
    {"name": "Chicken Stir Fry", "slug": "chicken-stir-fry", "id": "1",
     "recipeIngredient": [{"note": "chicken breast"}, {"note": "soy sauce"}, {"note": "broccoli"}],
     "image": None, "tier": "ready",
     "matched": ["chicken breast"], "missing": [], "expiring": ["chicken breast"],
     "score": 0.95},
    {"name": "Avocado Egg Toast", "slug": "avocado-egg-toast", "id": "2",
     "recipeIngredient": [{"note": "eggs"}, {"note": "avocado"}, {"note": "bread"}],
     "image": None, "tier": "ready",
     "matched": ["eggs", "avocados"], "missing": ["bread"], "expiring": ["avocados"],
     "score": 0.85},
    {"name": "Shrimp Pasta", "slug": "shrimp-pasta", "id": "3",
     "recipeIngredient": [{"note": "shrimp"}, {"note": "pasta"}, {"note": "garlic"}, {"note": "olive oil"}],
     "image": None, "tier": "ready",
     "matched": ["shrimp", "pasta", "olive oil"], "missing": [], "expiring": [],
     "score": 0.90},
    {"name": "Rice Bowl with Beans", "slug": "rice-bowl", "id": "4",
     "recipeIngredient": [{"note": "jasmine rice"}, {"note": "black beans"}, {"note": "salsa"}, {"note": "cheese"}],
     "image": None, "tier": "staples",
     "matched": ["jasmine rice", "black beans", "sharp cheddar"], "missing": ["salsa"], "expiring": [],
     "score": 0.70},
    {"name": "Beef Tacos", "slug": "beef-tacos", "id": "5",
     "recipeIngredient": [{"note": "ground beef"}, {"note": "taco shells"}, {"note": "cheese"}, {"note": "salsa"}],
     "image": None, "tier": "staples",
     "matched": ["ground beef", "sharp cheddar"], "missing": ["taco shells", "salsa"], "expiring": [],
     "score": 0.65},
]

MOCK_MEALPLAN = [
    {"date": _days(0), "recipe_name": "Chicken Stir Fry", "recipe_slug": "chicken-stir-fry", "servings": 4},
    {"date": _days(1), "recipe_name": "Avocado Egg Toast", "recipe_slug": "avocado-egg-toast", "servings": 2},
    {"date": _days(2), "recipe_name": "Shrimp Pasta", "recipe_slug": "shrimp-pasta", "servings": 4},
    {"date": _days(4), "recipe_name": "Rice Bowl with Beans", "recipe_slug": "rice-bowl", "servings": 3},
    {"date": _days(5), "recipe_name": "Beef Tacos", "recipe_slug": "beef-tacos", "servings": 4},
]

MOCK_SHOPPING = [
    {"id": "1", "note": "Bread", "checked": False, "position": 1},
    {"id": "2", "note": "Taco Shells", "checked": False, "position": 2},
    {"id": "3", "note": "Salsa", "checked": False, "position": 3},
    {"id": "4", "note": "Broccoli", "checked": True, "position": 4},
    {"id": "5", "note": "Garlic", "checked": True, "position": 5},
]

MOCK_EXPIRING = [
    {"product_id": 4, "amount": 1, "best_before_date": _days(2),
     "product": {"name": "Baby Spinach", "qu_unit_stock": {"name": "bag"}, "location": {"name": "Refrigerator"}},
     "days_remaining": 2, "urgency": "3d"},
    {"product_id": 11, "amount": 3, "best_before_date": _days(2),
     "product": {"name": "Avocados", "qu_unit_stock": {"name": "ea"}, "location": {"name": "Counter / Room Temp"}},
     "days_remaining": 2, "urgency": "3d"},
    {"product_id": 1, "amount": 2, "best_before_date": _days(3),
     "product": {"name": "Chicken Breast", "qu_unit_stock": {"name": "kg"}, "location": {"name": "Refrigerator"}},
     "days_remaining": 3, "urgency": "3d"},
    {"product_id": 10, "amount": 5, "best_before_date": _days(4),
     "product": {"name": "Bananas", "qu_unit_stock": {"name": "ea"}, "location": {"name": "Counter / Room Temp"}},
     "days_remaining": 4, "urgency": "7d"},
    {"product_id": 2, "amount": 1, "best_before_date": _days(5),
     "product": {"name": "Whole Milk", "qu_unit_stock": {"name": "gal"}, "location": {"name": "Refrigerator"}},
     "days_remaining": 5, "urgency": "7d"},
    {"product_id": 5, "amount": 3, "best_before_date": _days(7),
     "product": {"name": "Greek Yogurt", "qu_unit_stock": {"name": "cup"}, "location": {"name": "Refrigerator"}},
     "days_remaining": 7, "urgency": "7d"}
]

MOCK_DEFAULTS = [
    {"id": 1, "category": "Poultry", "storage_type": "refrigerated", "shelf_life_days": 3, "notes": ""},
    {"id": 2, "category": "Meat", "storage_type": "refrigerated", "shelf_life_days": 4, "notes": ""},
    {"id": 3, "category": "Seafood", "storage_type": "refrigerated", "shelf_life_days": 2, "notes": ""},
    {"id": 4, "category": "Dairy", "storage_type": "refrigerated", "shelf_life_days": 14, "notes": ""},
    {"id": 5, "category": "Produce", "storage_type": "room_temp", "shelf_life_days": 7, "notes": ""},
    {"id": 6, "category": "Grains", "storage_type": "dry", "shelf_life_days": 365, "notes": ""},
    {"id": 7, "category": "Canned", "storage_type": "dry", "shelf_life_days": 730, "notes": ""},
    {"id": 8, "category": "Frozen", "storage_type": "frozen", "shelf_life_days": 180, "notes": ""},
    {"id": 9, "category": "Condiments", "storage_type": "dry", "shelf_life_days": 365, "notes": ""},
    {"id": 10, "category": "Beverages", "storage_type": "room_temp", "shelf_life_days": 30, "notes": ""},
]


def base_ctx(request, active):
    from app.config import settings
    return {
        "request": request,
        "active": active,
        "tabs": all_tabs(),
        "ingress_path": "",
        "s": settings,
    }


@app.get("/ui/", response_class=HTMLResponse)
@app.get("/ui/inventory", response_class=HTMLResponse)
async def inventory(request: Request):
    categories = all_categories()
    return templates.TemplateResponse(request, "inventory.html", {
        **base_ctx(request, "inventory"),
        "message": None,
        "message_type": "success",
        "categories": categories,
        "panels": categories + [{**OTHER, "custom": False}],
    })


@app.get("/inventory/dashboard")
async def dashboard():
    return JSONResponse(MOCK_STOCK)


@app.get("/ui/expiring", response_class=HTMLResponse)
async def expiring(request: Request):
    return templates.TemplateResponse(request, "expiring.html", {
        **base_ctx(request, "expiring"),
        "items": MOCK_EXPIRING,
        "mealie_configured": False,
        "mealie_url": "",
        "mealie_shopping": [],
    })


@app.get("/ui/add", response_class=HTMLResponse)
async def add(request: Request):
    return templates.TemplateResponse(request, "add.html", {
        **base_ctx(request, "add"),
        "vision_provider": "gemini",
    })


@app.get("/ui/cook", response_class=HTMLResponse)
async def cook(request: Request):
    return templates.TemplateResponse(request, "cook.html", {
        **base_ctx(request, "cook"),
        "mealie_configured": True,
        "mealie_url": "http://localhost:9285",
        "recipe_source": "themealdb",
        "tiers": {
            "ready": MOCK_RECIPES[:3],
            "staples": MOCK_RECIPES[3:],
            "shopping": [],
        },
        "stock_count": 16,
        "ai_available": True,
    })


@app.get("/ui/recipes", response_class=HTMLResponse)
async def recipes(request: Request):
    return templates.TemplateResponse(request, "recipes.html", {
        **base_ctx(request, "recipes"),
        "mealie_configured": True,
        "mealie_url": "http://localhost:9285",
        "recipe_source": "themealdb",
    })


@app.get("/ui/mealplan", response_class=HTMLResponse)
async def mealplan(request: Request):
    return templates.TemplateResponse(request, "mealplan.html", {
        **base_ctx(request, "mealplan"),
        "mealie_configured": True,
        "mealie_url": "http://localhost:9285",
        "plan": MOCK_MEALPLAN,
        "week_start": TODAY.isoformat(),
    })


@app.get("/ui/shopping", response_class=HTMLResponse)
async def shopping(request: Request):
    return templates.TemplateResponse(request, "shopping.html", {
        **base_ctx(request, "shopping"),
        "mealie_configured": True,
        "mealie_url": "http://localhost:9285",
        "items": MOCK_SHOPPING,
        "list_name": "This week",
    })


@app.get("/ui/defaults", response_class=HTMLResponse)
async def defaults(request: Request):
    return templates.TemplateResponse(request, "defaults.html", {
        **base_ctx(request, "defaults"),
        "rows": MOCK_DEFAULTS,
    })


@app.get("/setup", response_class=HTMLResponse)
async def setup(request: Request):
    from app.config import settings, APP_VERSION
    from app.storage_categories import custom_categories
    return templates.TemplateResponse(request, "setup.html", {
        **base_ctx(request, "setup"),
        "s": settings,
        "configured": True,
        "has": {f: True for f in ["grocy_api_key", "gemini_api_key"]},
        "version": APP_VERSION,
        "custom_categories": custom_categories(),
    })


# Stubs so templates don't 404 on API calls
@app.get("/mealie/suggest")
async def mealie_suggest():
    def card(r, tier):
        return {"name": r["name"], "slug": r["slug"], "id": r["id"],
                "image": None, "matched": r.get("matched", []),
                "missing": r.get("missing", []), "expiring": r.get("expiring", []),
                "tier": tier}
    return JSONResponse({
        "tiers": {
            "ready":    [card(r, "ready")    for r in MOCK_RECIPES[:3]],
            "staples":  [card(r, "staples")  for r in MOCK_RECIPES[3:]],
            "shopping": [],
        },
        "external_tiers": {},
        "recipes_considered": 5,
        "external_considered": 20,
        "inventory_items": 16,
    })

@app.get("/mealie/mealplan")
async def mealie_mealplan():
    from datetime import date as d, timedelta
    today = d.today()
    days = {}
    for i in range(7):
        day = (today + timedelta(days=i)).isoformat()
        days[day] = []
    for i, entry in enumerate(MOCK_MEALPLAN):
        day = entry["date"]
        if day in days:
            days[day].append({
                "id": i + 1,
                "entry_type": "Recipe",
                "title": entry["recipe_name"],
                "recipe_slug": entry["recipe_slug"],
                "servings": entry["servings"],
            })
    return JSONResponse({"days": days})


@app.get("/pending/count")
async def pending_count(): return {"count": 2}
