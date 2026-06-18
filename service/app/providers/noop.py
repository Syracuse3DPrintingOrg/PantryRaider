from .base import VisionProvider
from ..models.food import AnalysisResult

_MSG = "No AI provider configured. Add a Gemini/OpenAI/Anthropic key in Settings."


class NoOpProvider(VisionProvider):
    async def analyze_food(self, image_data: bytes, mime_type: str) -> AnalysisResult:
        raise NotImplementedError(_MSG)

    async def analyze_receipt(self, image_data: bytes, mime_type: str) -> AnalysisResult:
        raise NotImplementedError(_MSG)

    async def health_check(self) -> bool:
        return False

    async def enrich_product(self, info: dict) -> dict | None:
        return None

    async def generate_recipe(self, name: str) -> dict | None:
        raise NotImplementedError(_MSG)

    async def suggest_from_inventory(self, items: list[str], limit: int = 8,
                                     preferences: str = "") -> list[dict] | None:
        raise NotImplementedError(_MSG)

    async def extract_recipe(self, image_data: bytes | None = None,
                             mime_type: str | None = None,
                             page_text: str | None = None) -> dict | None:
        raise NotImplementedError(_MSG)
