"""Runs the integration inside a real Home Assistant (see README in this
folder's parent for the command). Kept apart from the repo's main suite,
which is free of Home Assistant on purpose."""
from pathlib import Path

import custom_components
import pytest

# The test plugin ships its own custom_components package, which would hide
# ours: add our folder to it.
custom_components.__path__.append(str(Path(__file__).resolve().parents[1] / "custom_components"))

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def _custom(enable_custom_integrations):
    yield
