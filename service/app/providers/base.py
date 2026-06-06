from abc import ABC, abstractmethod
from ..models.food import AnalysisResult


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
