from pydantic import BaseModel, Field, model_validator
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
    # What one of them holds, when known: 64 and "Fluid ounce" for a bottle of
    # milk. Always a weight or a volume (services/package_size.py); None when
    # the size is unknown or the item is sold loose by weight.
    package_size: Optional[float] = None
    package_unit: Optional[str] = None
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
    # The item number a receipt printed on the line, read by the AI: most US
    # receipts print the product's UPC without its check digit. Only used to
    # look the product up (barcode.resolve_receipt_codes) and never sent back
    # out, since the digits may be misread.
    receipt_code: Optional[str] = Field(default=None, exclude=True)
    # How many single ones one package holds when its size is a count ("12
    # ct" eggs): counted out by apply_package_size, never sent back out.
    package_count: Optional[int] = Field(default=None, exclude=True)
    # Whether a size given with that count is each one's (a "12PK 12 FL OZ" of
    # cans) rather than the whole package's ("10 ct 17.5 oz" of tortillas).
    package_count_each: Optional[bool] = Field(default=None, exclude=True)


    @model_validator(mode="before")
    @classmethod
    def _typed_size(cls, data):
        """A size typed as text ("64 oz", from the import queue or the manual
        form) is read into package_size and package_unit; one that names no
        weight or volume is dropped rather than refusing the item."""
        if isinstance(data, dict) and isinstance(data.get("package_size"), str):
            from ..services.package_size import normalize_size, parse_count
            typed = data["package_size"]
            size = normalize_size(typed, data.get("package_unit"))
            data = {**data, "package_size": size[0] if size else None,
                    "package_unit": size[1] if size else None}
            # "12 ct" or "12PK 12 FL OZ": how many come in it, as well.
            if data.get("package_count") is None:
                from ..services.package_size import size_is_for_each
                data["package_count"] = parse_count(typed)
                data["package_count_each"] = size_is_for_each(typed)
        return data

class FoodItemOverride(BaseModel):
    """All fields optional: only provided fields are applied."""
    name: Optional[str] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None
    package_size: Optional[float] = None
    package_unit: Optional[str] = None
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
