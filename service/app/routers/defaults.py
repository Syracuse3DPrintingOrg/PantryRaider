from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from ..database import get_db
from ..models.db_models import ExpiryDefault

router = APIRouter(prefix="/defaults", tags=["defaults"])


class DefaultCreate(BaseModel):
    category: str
    name_pattern: str
    storage_type: str
    default_days: int
    notes: Optional[str] = None
    priority: int = 1


class DefaultUpdate(BaseModel):
    category: Optional[str] = None
    name_pattern: Optional[str] = None
    storage_type: Optional[str] = None
    default_days: Optional[int] = None
    notes: Optional[str] = None
    priority: Optional[int] = None


@router.get("/")
def list_defaults(
    category: Optional[str] = None,
    storage_type: Optional[str] = None,
    db: Session = Depends(get_db),
):
    q = db.query(ExpiryDefault)
    if category:
        q = q.filter(ExpiryDefault.category == category)
    if storage_type:
        q = q.filter(ExpiryDefault.storage_type == storage_type)
    return q.order_by(ExpiryDefault.category, ExpiryDefault.name_pattern).all()


@router.get("/{default_id}")
def get_default(default_id: int, db: Session = Depends(get_db)):
    row = db.query(ExpiryDefault).filter(ExpiryDefault.id == default_id).first()
    if not row:
        raise HTTPException(404, "Default not found")
    return row


@router.post("/", status_code=201)
def create_default(body: DefaultCreate, db: Session = Depends(get_db)):
    row = ExpiryDefault(**body.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.put("/{default_id}")
def update_default(default_id: int, body: DefaultUpdate, db: Session = Depends(get_db)):
    row = db.query(ExpiryDefault).filter(ExpiryDefault.id == default_id).first()
    if not row:
        raise HTTPException(404, "Default not found")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(row, field, value)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/{default_id}", status_code=204)
def delete_default(default_id: int, db: Session = Depends(get_db)):
    row = db.query(ExpiryDefault).filter(ExpiryDefault.id == default_id).first()
    if not row:
        raise HTTPException(404, "Default not found")
    db.delete(row)
    db.commit()
