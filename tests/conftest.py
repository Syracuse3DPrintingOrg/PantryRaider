import copy
import os
import sys
from pathlib import Path

import pytest

# Make `app` importable the same way the container does (workdir /app == service/)
sys.path.insert(0, str(Path(__file__).parent.parent / "service"))

# Make the Stream Deck controller package importable for its pure-logic tests.
sys.path.insert(0, str(Path(__file__).parent.parent / "streamdeck"))

# Same for the Bluetooth thermometer reader's pure decoders (gadgets/).
sys.path.insert(0, str(Path(__file__).parent.parent / "gadgets"))

# Create the app's tables on its own engine up front. A few tests exercise the
# real SessionLocal (not an in-memory override), and on a clean database (CI,
# or a fresh checkout) those tables would not exist yet. create_all is
# idempotent, so this is a no-op when the tables already exist.
from app.database import engine, ensure_schema, Base  # noqa: E402
from app.models import db_models  # noqa: E402,F401 - registers models with Base

Base.metadata.create_all(bind=engine)
# A pre-existing local database may predate post-release column additions
# (create_all never alters an existing table); backfill them like startup does.
ensure_schema(engine)


@pytest.fixture
def anyio_backend():
    """Run @pytest.mark.anyio async tests on asyncio only (no trio dependency)."""
    return "asyncio"


# --- Cross-test isolation -------------------------------------------------
#
# The app keeps a single global `settings` object, and many test modules
# mutate it, some with plain `settings.x = ...` or `settings.apply(...)`
# with no restore at all. Historically that made the suite order dependent
# (for example, test_restore.py leaked gemini_api_key="live-key", and any
# later test that hit /health then tried to build a real Gemini provider).
# The pair of autouse fixtures below snapshots the settings state and the
# working directory at module and at test granularity and restores both, so
# no module can leak state into another and no test can leak into the next
# test regardless of collection order.
#
# The root logger gets the same treatment for the diagnostics file handler:
# app startup (any TestClient(app)) installs the tagged rotating handler for
# that test's data_dir and never removes it, which used to make a later
# configure_file_logging() call reuse a handler pointed at a dead tmp dir.

def _snapshot_settings():
    from app.config import settings

    return copy.deepcopy(settings.__dict__)


def _snapshot_log_handlers():
    import logging

    from app.services import diagnostics

    root = logging.getLogger()
    tagged = {h for h in root.handlers if getattr(h, diagnostics._HANDLER_TAG, False)}
    return tagged, root.level


def _restore_log_handlers(snapshot) -> None:
    import logging

    from app.services import diagnostics

    tagged, level = snapshot
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, diagnostics._HANDLER_TAG, False) and h not in tagged:
            root.removeHandler(h)
            h.close()
    root.setLevel(level)


def _restore_settings(snapshot) -> None:
    from app.config import settings

    if settings.__dict__ == snapshot:
        return
    settings.__dict__.clear()
    settings.__dict__.update(copy.deepcopy(snapshot))
    # Cached providers may have been built from the leaked settings; drop
    # them so the next test builds from the restored state.
    from app import dependencies

    dependencies.reset_providers()


@pytest.fixture(autouse=True, scope="module")
def _module_settings_isolation():
    """Undo settings/cwd/log-handler changes made by module-scoped fixtures."""
    snapshot = _snapshot_settings()
    logs = _snapshot_log_handlers()
    cwd = os.getcwd()
    yield
    _restore_settings(snapshot)
    _restore_log_handlers(logs)
    os.chdir(cwd)


@pytest.fixture(autouse=True)
def _test_settings_isolation():
    """Undo settings/cwd changes made by an individual test.

    This snapshots AFTER module-scoped fixtures have run, so per-module
    client fixtures that configure settings keep working for every test
    in their module; only per-test drift is rolled back.
    """
    snapshot = _snapshot_settings()
    logs = _snapshot_log_handlers()
    cwd = os.getcwd()
    yield
    _restore_settings(snapshot)
    _restore_log_handlers(logs)
    os.chdir(cwd)


@pytest.fixture(autouse=True)
def _readiness_isolation():
    """Reset the first-boot readiness gate's module state around every test.

    The gate keeps sticky flags (answered / dismissed / provisioned) in a
    module-level dict, so a test that dismissed the gate used to silently turn
    it off for every later test in the process. That masked real failures for
    weeks: settings-page tests rendering a pi_hosted install with no inventory
    key only passed because SOME earlier test had left the gate dismissed, and
    re-ordering the suite (or running a file alone) surfaced them
    (FoodAssistant-6v9q ship). Every test now starts with a pristine gate; a
    fixture that wants the gate out of the way says so honestly by setting
    grocy_api_key, exactly like a real configured appliance.
    """
    from app.services import readiness
    readiness.reset()
    yield
    readiness.reset()
