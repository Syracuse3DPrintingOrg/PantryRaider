import secrets
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from typing import Optional

from ..config import settings
from ..database import get_db
from ..ingress import ingress_redirect
from ..models.db_models import ExpiryDefault
from ..services.grocy import GrocyClient
from ..storage_categories import all_categories, OTHER
from ..templating import templates

router = APIRouter(prefix="/ui", tags=["ui"])


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if not settings.auth_password or request.session.get("authed"):
        return ingress_redirect(request, "/ui/")
    if request.session.get("totp_pending"):
        return templates.TemplateResponse(request, "login.html",
            {"request": request, "error": None, "step": "totp"})
    return templates.TemplateResponse(request, "login.html",
        {"request": request, "error": None, "step": "password"})


@router.post("/login")
def login(request: Request, password: str = Form(None), totp_code: str = Form(None)):
    # Step 2: TOTP verification (password already accepted in this session)
    if request.session.get("totp_pending"):
        import pyotp
        totp = pyotp.TOTP(settings.totp_secret)
        if totp_code and totp.verify(totp_code.strip(), valid_window=1):
            request.session.pop("totp_pending", None)
            request.session["authed"] = True
            return ingress_redirect(request, "/ui/")
        return templates.TemplateResponse(request, "login.html",
            {"request": request, "error": "Invalid code: try again.", "step": "totp"},
            status_code=401)

    # Step 1: password check
    if not (settings.auth_password and password and
            secrets.compare_digest(password, settings.auth_password)):
        return templates.TemplateResponse(request, "login.html",
            {"request": request, "error": "Incorrect password.", "step": "password"},
            status_code=401)

    if settings.totp_secret:
        request.session["totp_pending"] = True
        return templates.TemplateResponse(request, "login.html",
            {"request": request, "error": None, "step": "totp"})

    request.session["authed"] = True
    return ingress_redirect(request, "/ui/")


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return ingress_redirect(request, "/ui/login")


@router.get("/", response_class=HTMLResponse)
@router.get("/inventory", response_class=HTMLResponse)
async def inventory_page(request: Request):
    categories = all_categories()
    return templates.TemplateResponse(request, "inventory.html", {
        "request": request,
        "active": "inventory",
        "message": request.query_params.get("msg"),
        "message_type": request.query_params.get("msg_type", "success"),
        # Movable categories (built-in + custom) and the always-on "other" panel
        "categories": categories,
        "panels": categories + [{**OTHER, "custom": False}],
        "grocy_url": settings.grocy_link_url(),
    })


@router.get("/add", response_class=HTMLResponse)
async def add_page(request: Request):
    return templates.TemplateResponse(request, "add.html", {
        "request": request,
        "active": "add",
    })


@router.get("/pending", response_class=HTMLResponse)
async def pending_page(request: Request):
    return templates.TemplateResponse(request, "pending.html", {
        "request": request,
        "active": "pending",
    })


@router.get("/recipes", response_class=HTMLResponse)
async def recipes_page(request: Request):
    return templates.TemplateResponse(request, "recipes.html", {
        "request": request,
        "active": "recipes",
        "mealie_configured": settings.mealie_configured(),
        "mealie_url": settings.mealie_link_url(),
    })


@router.get("/cook", response_class=HTMLResponse)
async def cook_page(request: Request):
    return templates.TemplateResponse(request, "cook.html", {
        "request": request,
        "active": "cook",
        "mealie_configured": settings.mealie_configured(),
        "mealie_url": settings.mealie_link_url(),
    })


@router.get("/mealplan", response_class=HTMLResponse)
async def mealplan_page(request: Request):
    return templates.TemplateResponse(request, "mealplan.html", {
        "request": request,
        "active": "mealplan",
        "mealie_configured": settings.mealie_configured(),
        "mealie_url": settings.mealie_link_url(),
    })


@router.get("/shopping", response_class=HTMLResponse)
async def shopping_page(request: Request):
    return templates.TemplateResponse(request, "shopping.html", {
        "request": request,
        "active": "shopping",
        "mealie_configured": settings.mealie_configured(),
        "mealie_url": settings.mealie_link_url(),
    })


@router.get("/expiring", response_class=HTMLResponse)
async def expiring_page(request: Request, days: int = 7):
    grocy = GrocyClient()
    try:
        items = await grocy.get_expiring(days)
    except Exception:
        items = []
    return templates.TemplateResponse(request, "expiring.html", {
        "request": request,
        "items": items,
        "days": days,
        "active": "expiring",
        "message": request.query_params.get("msg"),
        "message_type": request.query_params.get("msg_type", "success"),
    })


@router.post("/consume/{product_id}")
async def consume_item(request: Request, product_id: int, amount: float = Form(1.0)):
    grocy = GrocyClient()
    try:
        await grocy.consume_stock(product_id, amount)
        msg = "Item marked as consumed."
        msg_type = "success"
    except Exception as e:
        msg = f"Error: {e}"
        msg_type = "danger"
    return ingress_redirect(request, f"/ui/expiring?msg={msg}&msg_type={msg_type}")


@router.get("/defaults", response_class=HTMLResponse)
def defaults_page(
    request: Request,
    db: Session = Depends(get_db),
):
    rows = db.query(ExpiryDefault).order_by(
        ExpiryDefault.category, ExpiryDefault.name_pattern
    ).all()
    categories = sorted(set(r.category for r in rows))
    return templates.TemplateResponse(request, "defaults.html", {
        "request": request,
        "defaults": rows,
        "categories": categories,
        "active": "defaults",
        "message": request.query_params.get("msg"),
        "message_type": request.query_params.get("msg_type", "success"),
    })


@router.post("/defaults/create")
def create_default(
    request: Request,
    category: str = Form(...),
    name_pattern: str = Form(...),
    storage_type: str = Form(...),
    default_days: int = Form(...),
    notes: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    row = ExpiryDefault(
        category=category,
        name_pattern=name_pattern,
        storage_type=storage_type,
        default_days=default_days,
        notes=notes or None,
        priority=1,
    )
    db.add(row)
    db.commit()
    return ingress_redirect(request, "/ui/defaults?msg=Rule+added.")


@router.post("/defaults/{default_id}/update")
def update_default(
    request: Request,
    default_id: int,
    category: str = Form(...),
    name_pattern: str = Form(...),
    storage_type: str = Form(...),
    default_days: int = Form(...),
    notes: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    row = db.query(ExpiryDefault).filter(ExpiryDefault.id == default_id).first()
    if row:
        row.category = category
        row.name_pattern = name_pattern
        row.storage_type = storage_type
        row.default_days = default_days
        row.notes = notes or None
        db.commit()
    return ingress_redirect(request, "/ui/defaults?msg=Rule+updated.")


@router.get("/about", response_class=HTMLResponse)
async def about_page(request: Request):
    return templates.TemplateResponse(request, "about.html", {
        "request": request,
        "active": "about",
    })


@router.post("/defaults/{default_id}/delete")
def delete_default(request: Request, default_id: int, db: Session = Depends(get_db)):
    row = db.query(ExpiryDefault).filter(ExpiryDefault.id == default_id).first()
    if row:
        db.delete(row)
        db.commit()
    return ingress_redirect(request, "/ui/defaults?msg=Rule+deleted.&msg_type=warning")
