"""Debug logging to a rotating file plus a redacted download (FoodAssistant-asra).

Non-technical users have no SSH and cannot read `docker logs`, so support needs
a way to capture logs from the UI. We attach a single rotating file handler to
the root logger, written under the app data dir, and let Settings raise the
level to DEBUG at runtime. The download redacts any configured secret values
(API keys, passwords, the session/TOTP secrets) so the bundle is safe to share.

Kept import-light and free of FastAPI so the log wiring stays unit-testable.
"""
from __future__ import annotations

import logging
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
