"""Debug logging to a rotating file plus a redacted download (FoodAssistant-asra).

Non-technical users have no SSH and cannot read `docker logs`, so support needs
a way to capture logs from the UI. We attach a single rotating file handler to
the root logger, written under the app data dir, and let Settings raise the
level to DEBUG at runtime. The download redacts any configured secret values
(API keys, passwords, the session/TOTP secrets) so the bundle is safe to share.

This module also owns the container-console side of log wiring
(configure_console_logging): uvicorn only configures handlers for its own
loggers, so without it the app's INFO lines never reach `docker logs` at all.

Kept import-light and free of FastAPI so the log wiring stays unit-testable.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# A marker attribute so we only ever add ONE Pantry Raider file handler to the
# root logger, no matter how many times configure_file_logging runs (startup
# plus every settings save).
_HANDLER_TAG = "foodassistant_file_handler"

# 2 MB per file, three rollovers: enough recent history for a bug report without
# letting logs grow unbounded on a small SD card.
_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 3

# The matching marker for the stdout handler on the app's logger namespaces,
# so repeat calls (imports under --reload, test lifespans) can never stack a
# second handler and print every line twice.
_CONSOLE_TAG = "foodassistant_console_handler"

# The app's own logger namespaces. Modules name their loggers either
# "foodassistant.<area>" or by __name__, which resolves under "app." because
# the package root is app. A handler on these two parents catches every app
# logger while leaving uvicorn's loggers (which have their own handlers) and
# third-party ones like httpx (an INFO line per request) off the console.
_APP_LOGGER_NAMESPACES = ("foodassistant", "app")


def log_path(data_dir: str) -> Path:
    """Absolute path of the active log file under the data dir."""
    return Path(data_dir) / "logs" / "foodassistant.log"


def _existing_handler() -> RotatingFileHandler | None:
    for h in logging.getLogger().handlers:
        if getattr(h, _HANDLER_TAG, False):
            return h  # type: ignore[return-value]
    return None


def configure_file_logging(data_dir: str, debug: bool) -> Path | None:
    """Install (or update) the rotating file handler on the root logger and set
    the level. Returns the log path, or None if the file could not be opened
    (read-only data dir in CI or tests), in which case logging just stays at its
    previous, console-only configuration.
    """
    level = logging.DEBUG if debug else logging.INFO
    path = log_path(data_dir)
    handler = _existing_handler()
    if handler is None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
            )
        except OSError:
            return None
        setattr(handler, _HANDLER_TAG, True)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"
        ))
        logging.getLogger().addHandler(handler)
    handler.setLevel(level)
    # The root logger must pass records at the chosen level for the handler to
    # see them; uvicorn sets its own loggers but the root gates app modules.
    root = logging.getLogger()
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    return path


def configure_console_logging(level: int = logging.INFO) -> None:
    """Attach one stdout handler to the app's logger namespaces so `docker
    logs` shows provisioning and background-task activity.

    uvicorn wires handlers only for its own loggers, so before this the app's
    INFO lines (first-run provisioning above all) never reached the container
    console: a first boot going wrong looked like a silent container
    (FoodAssistant field failure, 2026-07). The handler sits on the two
    namespace parents rather than the root logger so third-party INFO stays
    off the console, and it holds at INFO even when debug file logging is on,
    so the console never floods. Safe to call any number of times.
    """
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s")
    for name in _APP_LOGGER_NAMESPACES:
        log = logging.getLogger(name)
        if not any(getattr(h, _CONSOLE_TAG, False) for h in log.handlers):
            handler = logging.StreamHandler(sys.stdout)
            setattr(handler, _CONSOLE_TAG, True)
            handler.setLevel(level)
            handler.setFormatter(formatter)
            log.addHandler(handler)
    # App loggers are NOTSET and take their effective level from the root,
    # which starts at WARNING: without raising it, the INFO records would
    # never be created at all. configure_file_logging raises the root the same
    # way, but only when the data dir is writable, and console visibility must
    # not depend on that. The namespace loggers themselves stay NOTSET so the
    # debug-logging toggle (which moves the root to DEBUG) keeps feeding DEBUG
    # records to the file handler.
    root = logging.getLogger()
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)


def _redactions(secrets: "list[str]") -> "list[str]":
    """Non-empty, de-duplicated secret values worth scrubbing, longest first so
    a value that contains another is replaced before its substring."""
    seen = {s for s in secrets if s and len(s) >= 4}
    return sorted(seen, key=len, reverse=True)


def redact_text(text: str, secrets: "list[str] | None") -> str:
    """Replace every configured secret value in a text blob with [redacted].

    The single scrubbing point for anything user-shareable (the log download
    and every file in the support bundle), so redaction behaves the same
    everywhere: values shorter than 4 characters are ignored (too generic to
    scrub safely) and longer values are replaced before their substrings.
    """
    for value in _redactions(secrets or []):
        text = text.replace(value, "[redacted]")
    return text


def read_log_text(data_dir: str, secrets: "list[str] | None" = None) -> str:
    """Return the current log plus rolled-over files, oldest first, with any
    configured secret values replaced by [redacted]. Empty string when no log
    has been written yet."""
    base = log_path(data_dir)
    parts: list[str] = []
    # Rolled files are foodassistant.log.3 (oldest) .. .1, then the live file.
    for i in range(_BACKUP_COUNT, 0, -1):
        rolled = base.with_name(f"{base.name}.{i}")
        if rolled.exists():
            parts.append(_safe_read(rolled))
    if base.exists():
        parts.append(_safe_read(base))
    return redact_text("".join(parts), secrets)


def _safe_read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
