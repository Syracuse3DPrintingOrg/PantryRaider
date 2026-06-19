import json
import re
from abc import ABC, abstractmethod
from ..models.food import AnalysisResult


def parse_json_response(raw: str):
    """Parse a model's JSON reply, tolerating markdown code fences."""
    text = raw.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    return json.loads(text)


class VisionProvider(ABC):
    @abstractmethod
    async def analyze_food(self, image_data: bytes, mime_type: str) -> AnalysisResult:
        """Analyze a photo of food items."""

    @abstractmethod
    async def analyze_receipt(self, image_data: bytes, mime_type: str) -> AnalysisResult:
        """Parse a receipt image and extract food line items."""

    @abstractmethod
    async def health_check(self) -> bool:
        """Return True if the provider is reachable and configured."""

    async def enrich_product(self, info: dict) -> dict | None:
        """Normalize barcode-lookup product data (text-only, no image).

        Takes raw Open Food Facts fields and returns a dict with name,
        category, storage_type, shelf_life_days, and brand: or None if
        the provider doesn't support text enrichment.
        """
        return None

    async def generate_recipe(self, name: str) -> dict | None:
        """Generate a full recipe from a dish name. Returns the same schema as
        extract_recipe, or None if the provider doesn't support text generation."""
        return None

    async def suggest_from_inventory(self, items: list[str], limit: int = 8,
                                      preferences: str = "") -> list[dict] | None:
        """Suggest recipes from a list of available ingredients. Returns a list of
        {name, description, uses} dicts, or None if unsupported."""
        return None

    async def extract_recipe(self, image_data: bytes | None = None,
                             mime_type: str | None = None,
                             page_text: str | None = None) -> dict | None:
        """Extract a structured recipe from a photo OR from webpage text.

        Returns a dict with name, description, servings, total_time,
        ingredients (list[str]), instructions (list[str]): or None if
        the provider doesn't support recipe extraction.
        """
        return None
