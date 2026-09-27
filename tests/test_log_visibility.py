"""First-boot provisioning must be visible in `docker logs`.

The logger.info calls in services/first_run.py never reached stdout because
only uvicorn's own loggers had handlers, which left a first-boot field failure
undiagnosable from the container's logs. Importing the app now attaches one
guarded stdout handler to the app's logger namespaces ("foodassistant" and
"app", the two parents every app logger resolves under). These tests pin that
wiring: exactly one handler per namespace no matter how often the app is
imported or the configure call repeats, INFO records from first_run actually
reach the handler's stream, the diagnostics file handler still gets exactly
one copy of each record, and nothing lands on the root logger where it would
double uvicorn's lines or print third-party INFO.
"""
from __future__ import annotations

import importlib
import io
import logging
import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import diagnostics  # noqa: E402

_NAMESPACES = ("foodassistant", "app")


def _tagged(logger_name: str) -> list[logging.Handler]:
    return [h for h in logging.getLogger(logger_name).handlers
            if getattr(h, diagnostics._CONSOLE_TAG, False)]


def test_importing_the_app_attaches_one_stdout_handler_per_namespace():
    import app.main  # noqa: F401
    for name in _NAMESPACES:
        handlers = _tagged(name)
        assert len(handlers) == 1, name
        assert isinstance(handlers[0], logging.StreamHandler)
        assert handlers[0].level == logging.INFO
    # A second import (module cache) and repeat configure calls (--reload,
    # extra lifespans) must never stack a second handler.
    importlib.import_module("app.main")
    diagnostics.configure_console_logging()
    diagnostics.configure_console_logging()
    for name in _NAMESPACES:
        assert len(_tagged(name)) == 1, name


def test_no_console_handler_lands_on_the_root_logger():
    """The handler must sit on the app namespaces only: a root handler would
    also print third-party INFO (httpx logs a line per request) and could
    double lines that already have a handler upstream."""
    import app.main  # noqa: F401
    assert [h for h in logging.getLogger().handlers
            if getattr(h, diagnostics._CONSOLE_TAG, False)] == []


def test_first_run_info_reaches_the_console_handler():
    import app.main  # noqa: F401
    # conftest restores the root logger level between tests, undoing what the
    # import-time call set; re-calling here is a no-op for handlers and puts
    # the level back where a real process has it.
    diagnostics.configure_console_logging()
    log = logging.getLogger("foodassistant.first_run")
    assert log.isEnabledFor(logging.INFO)
    handler = _tagged("foodassistant")[0]
    buf = io.StringIO()
    old = handler.setStream(buf)
    try:
        log.info("console-marker-%s", "grocy")
    finally:
        handler.setStream(old)
    out = buf.getvalue()
    assert "console-marker-grocy" in out
    # Operator-readable line: level and logger name are part of the format.
    assert "INFO" in out and "foodassistant.first_run" in out


def test_app_namespace_loggers_reach_the_console_too():
    """Half the app logs under __name__ ("app.services.*"), not
    "foodassistant.*"; those lines must be just as visible."""
    import app.main  # noqa: F401
    diagnostics.configure_console_logging()  # restore the root level, see above
    handler = _tagged("app")[0]
    buf = io.StringIO()
    old = handler.setStream(buf)
    try:
        logging.getLogger("app.services.usb_backup").info("app-ns-marker")
    finally:
        handler.setStream(old)
    assert "app-ns-marker" in buf.getvalue()


def test_console_and_file_logging_coexist_without_duplicates(tmp_path):
    """Each record shows once on the console and once in the diagnostics
    file: the console handler sits on the namespace, the file handler on the
    root, and propagation connects them without doubling either stream."""
    import app.main  # noqa: F401
    path = diagnostics.configure_file_logging(str(tmp_path), debug=False)
    assert path is not None
    handler = _tagged("foodassistant")[0]
    buf = io.StringIO()
    old = handler.setStream(buf)
    try:
        logging.getLogger("foodassistant.first_run").info("both-streams-marker")
    finally:
        handler.setStream(old)
    for h in logging.getLogger().handlers:
        h.flush()
    assert buf.getvalue().count("both-streams-marker") == 1
    assert path.read_text().count("both-streams-marker") == 1


def test_configure_console_logging_targets_stdout():
    """docker logs follows the process's stdout, so the handler must bind the
    real sys.stdout at configure time. Rebuilt here against a fake stdout to
    prove it, then restored so later tests keep the genuine wiring."""
    def _drop_tagged():
        for name in _NAMESPACES:
            log = logging.getLogger(name)
            for h in _tagged(name):
                log.removeHandler(h)

    _drop_tagged()
    buf = io.StringIO()
    real = sys.stdout
    sys.stdout = buf
    try:
        diagnostics.configure_console_logging()
    finally:
        sys.stdout = real
    logging.getLogger("foodassistant.first_run").info("stdout-marker")
    assert "stdout-marker" in buf.getvalue()
    # Leave the suite with handlers bound to the real stdout again.
    _drop_tagged()
    diagnostics.configure_console_logging()
    for name in _NAMESPACES:
        assert len(_tagged(name)) == 1, name


def test_console_logging_works_without_a_writable_data_dir():
    """configure_file_logging is what used to raise the root level to INFO,
    and it bails on an unwritable data dir. Console visibility must not depend
    on it: configure_console_logging raises the root itself."""
    root = logging.getLogger()
    old_level = root.level
    try:
        root.setLevel(logging.WARNING)
        diagnostics.configure_console_logging()
        assert root.level == logging.INFO
        assert logging.getLogger("foodassistant.first_run").isEnabledFor(
            logging.INFO)
    finally:
        root.setLevel(old_level)
