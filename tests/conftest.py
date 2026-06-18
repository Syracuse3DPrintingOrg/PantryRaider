import sys
from pathlib import Path

import pytest

# Make `app` importable the same way the container does (workdir /app == service/)
sys.path.insert(0, str(Path(__file__).parent.parent / "service"))

# Make the Stream Deck controller package importable for its pure-logic tests.
sys.path.insert(0, str(Path(__file__).parent.parent / "streamdeck"))


@pytest.fixture
def anyio_backend():
    """Run @pytest.mark.anyio async tests on asyncio only (no trio dependency)."""
    return "asyncio"
