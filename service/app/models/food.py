from pydantic import BaseModel, Field
from typing import Optional
from datetime import date
from enum import Enum


class StorageType(str, Enum):
    refrigerated = "refrigerated"
    frozen = "frozen"
    room_temp = "room_temp"
    dry = "dry"


class FoodCategory(str, Enum):
    poultry = "Poultry"
    meat = "Meat"
    seafood = "Seafood"
    dairy = "Dairy"
    produce = "Produce"
    grains = "Grains"
    condiments = "Condiments"
    beverages = "Beverages"
    snacks = "Snacks"
    frozen = "Frozen"
    canned = "Canned"
    other = "Other"


class FoodItem(BaseModel):
    name: str
    quantity: float = 1.0
    unit: str = "item"
    best_by_date: Optional[date] = None
    purchased_on: Optional[date] = None
    storage_type: StorageType = StorageType.refrigerated
    category: FoodCategory = FoodCategory.other
    brand: Optional[str] = None
    notes: Optional[str] = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    # The scanned barcode, when the item came in through a scan. Registered on
    # the Grocy product at import so consume-by-barcode can resolve it later.
    barcode: Optional[str] = None
    # How best_by_date was worked out, so it can be recorded to
    # services/best_by_provenance.py once the item lands in Grocy and a
    # product id exists (FoodAssistant-cidz). Not user-facing: "manual" (a
    # date the user typed, or unset) needs no record, "default" is a
    # category-rule estimate (services/defaults.py), "llm" is an AI guess
    # (services/barcode.py). Left None until something actually sets a date.
    best_by_source: Optional[str] = None


class FoodItemOverride(BaseModel):
    """All fields optional: only provided fields are applied."""
    name: Optional[str] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None
    best_by_date: Optional[date] = None
    purchased_on: Optional[date] = None
    storage_type: Optional[StorageType] = None
    category: Optional[FoodCategory] = None
    brand: Optional[str] = None
    notes: Optional[str] = None


class AnalysisResult(BaseModel):
    items: list[FoodItem]
    image_type: str  # "food" or "receipt"
    # Receipt-only metadata: the purchase date and store read off the receipt.
    # Both are None for food photos and for receipts where they are not present.
    purchased_on: Optional[date] = None
    store: Optional[str] = None
    raw_response: Optional[str] = None


class ImportRequest(BaseModel):
    items: list[FoodItem]
    overrides: Optional[dict[int, FoodItemOverride]] = None  # index -> override
