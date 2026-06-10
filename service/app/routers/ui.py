import secrets
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from typing import Optional

from ..config import settings
from ..database import get_db
from ..models.db_models import ExpiryDefault
from ..services.grocy import GrocyClient

router = APIRouter(prefix="/ui", tags=["ui"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if not settings.auth_password or request.session.get("authed"):
        return RedirectResponse("/ui/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login")
def login(request: Request, password: str = Form(...)):
    if settings.auth_password and secrets.compare_digest(password, settings.auth_password):
        request.session["authed"] = True
        return RedirectResponse("/ui/", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "Incorrect password"},
        status_code=401,
    )


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/ui/login", status_code=303)


@router.get("/inventory", response_class=HTMLResponse)
async def inventory_page(request: Request):
    return templates.TemplateResponse("inventory.html", {
        "request": request,
        "active": "inventory",
    })


@router.get("/add", response_class=HTMLResponse)
async def add_page(request: Request):
    return templates.TemplateResponse("add.html", {
        "request": request,
        "active": "add",
    })


@router.get("/", response_class=HTMLResponse)
async def expiring_page(request: Request, days: int = 7):
    grocy = GrocyClient()
    try:
        items = await grocy.get_expiring(days)
    except Exception:
        items = []
    return templates.TemplateResponse("expiring.html", {
        "request": request,
        "items": items,
        "days": days,
        "active": "expiring",
        "message": request.query_params.get("msg"),
        "message_type": request.query_params.get("msg_type", "success"),
    })


@router.post("/consume/{product_id}")
async def consume_item(product_id: int, amount: float = Form(1.0)):
    grocy = GrocyClient()
    try:
        await grocy.consume_stock(product_id, amount)
        msg = "Item marked as consumed."
        msg_type = "success"
    except Exception as e:
        msg = f"Error: {e}"
        msg_type = "danger"
    return RedirectResponse(f"/ui/?msg={msg}&msg_type={msg_type}", status_code=303)


@router.get("/defaults", response_class=HTMLResponse)
def defaults_page(
    request: Request,
    db: Session = Depends(get_db),
):
    rows = db.query(ExpiryDefault).order_by(
        ExpiryDefault.category, ExpiryDefault.name_pattern
    ).all()
    categories = sorted(set(r.category for r in rows))
    return templates.TemplateResponse("defaults.html", {
        "request": request,
        "defaults": rows,
        "categories": categories,
        "active": "defaults",
        "message": request.query_params.get("msg"),
        "message_type": request.query_params.get("msg_type", "success"),
    })


@router.post("/defaults/create")
def create_default(
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
    return RedirectResponse("/ui/defaults?msg=Rule+added.", status_code=303)


@router.post("/defaults/{default_id}/update")
def update_default(
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
    return RedirectResponse("/ui/defaults?msg=Rule+updated.", status_code=303)


@router.post("/defaults/{default_id}/delete")
def delete_default(default_id: int, db: Session = Depends(get_db)):
    row = db.query(ExpiryDefault).filter(ExpiryDefault.id == default_id).first()
    if row:
        db.delete(row)
        db.commit()
    return RedirectResponse("/ui/defaults?msg=Rule+deleted.&msg_type=warning", status_code=303)
