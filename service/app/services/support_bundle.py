"""One-click support bundle (FoodAssistant-w7mb).

Builds an in-memory zip of everything support usually asks for one file at a
time: the app version, a redacted settings dump, the diagnostics log, the small
state files (scanner mode, pantry audit, live timers), the last update-check
result, and Python/package versions. On a Pi appliance the admin endpoint also
folds in a host report gathered by the root bridge (unit states, journal tail,
display and input probes); those sections arrive here as plain text and are
added under host/.

Every text that goes into the zip passes through diagnostics.redact_text with
the configured secret values, and the settings dump additionally blanks every
SECRET_SETTING_KEYS field by name, so the bundle is safe to attach to a bug
report. Kept pure (no FastAPI, no live settings import at module level) so the
builder is unit-testable with a plain object.
"""
from __future__ import annotations

import io
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .diagnostics import read_log_text, redact_text

# Libraries whose versions matter for a bug report. Missing ones are noted, not
# errors: the ollama-only stack has no anthropic package, and vice versa.
_KEY_PACKAGES = [
    "fastapi", "uvicorn", "pydantic", "pydantic-settings", "httpx", "jinja2",
    "sqlalchemy", "pillow", "qrcode", "python-multipart", "itsdangerous",
]

# Small state files under data_dir that help reconstruct what the device was
# doing. They hold a scanner mode, an audit scan count, and an active recipe:
# no credentials live in any of them, but they still pass through redact_text
# like everything else.
_STATE_FILES = ["scanner_mode.json", "audit_session.json", "current_recipe.json"]


def secret_values(settings_obj) -> list[str]:
    """The configured secret values worth scrubbing, from SECRET_SETTING_KEYS."""
    from ..config import SECRET_SETTING_KEYS
    return [str(getattr(settings_obj, k, "") or "") for k in SECRET_SETTING_KEYS]


def redacted_settings_dump(raw: str) -> str:
    """settings.json with every secret field blanked by name.

    Same key-blanking the backup download applies, kept here so the bundle
    does not depend on the admin router. An unparseable file is returned
    as-is; the value-based redact_text pass still scrubs known secrets.
    """
    from ..config import SECRET_SETTING_KEYS
    try:
        data = json.loads(raw)
    except Exception:
        return raw
    for k in SECRET_SETTING_KEYS:
        if k in data and data[k]:
            data[k] = "[redacted]"
    return json.dumps(data, indent=2, sort_keys=True)


def python_environment_text() -> str:
    """sys.version plus the versions of the packages support cares about."""
    from importlib import metadata
    lines = [f"python: {sys.version}", ""]
    for name in _KEY_PACKAGES:
        try:
            lines.append(f"{name}: {metadata.version(name)}")
        except Exception:
            lines.append(f"{name}: not installed")
    return "\n".join(lines) + "\n"


def _timers_snapshot() -> str:
    """The live timer registry as JSON. Timers are in-memory (no state file),
    so the bundle snapshots them at build time."""
    from . import timers
    try:
        return json.dumps(timers.list_timers(), indent=2)
    except Exception as e:
        return f"could not snapshot timers: {e}"


def _read_state_file(data_dir: str, name: str) -> str | None:
    p = Path(data_dir) / name
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"could not read {name}: {e}"


def build_bundle_files(settings_obj, host_sections: "dict[str, str] | None" = None) -> "dict[str, str]":
    """Assemble the bundle as {archive name: text}, everything already redacted.

    host_sections is the bridge's report (section name to text), included under
    host/ when the caller obtained one; None means no bridge (server install)
    and a string note under host/ explains an unreachable bridge.
    """
    from ..config import APP_VERSION
    from ..hardware import is_raspberry_pi

    secrets = secret_values(settings_obj)
    data_dir = str(settings_obj.data_dir)
    files: dict[str, str] = {}

    files["manifest.json"] = json.dumps({
        "app": "foodassistant",
        "brand": "Pantry Raider",
        "version": APP_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "deployment_mode": str(getattr(settings_obj, "deployment_mode", "") or ""),
        "is_raspberry_pi": bool(is_raspberry_pi()),
    }, indent=2)

    # Redacted settings: blank secret fields by name, then scrub by value too
    # (a secret pasted into a non-secret field still gets caught).
    raw_settings = _read_state_file(data_dir, "settings.json")
    files["settings.redacted.json"] = redacted_settings_dump(raw_settings or "{}")

    log_text = read_log_text(data_dir, secrets)
    files["logs/foodassistant.log"] = log_text or (
        "No log captured yet. Debug logging was off, or the app has not "
        "written anything since it started.\n")

    for name in _STATE_FILES:
        text = _read_state_file(data_dir, name)
        if text is not None:
            files[f"state/{name}"] = text
    files["state/timers.json"] = _timers_snapshot()

    files["update-check.json"] = json.dumps({
        "last_checked": getattr(settings_obj, "update_last_checked", 0.0),
        "last_latest": getattr(settings_obj, "update_last_latest", ""),
        "last_available": getattr(settings_obj, "update_last_available", False),
        "auto_update": bool(getattr(settings_obj, "auto_update", False)),
    }, indent=2)

    files["python-environment.txt"] = python_environment_text()

    if host_sections:
        for name, text in sorted(host_sections.items()):
            safe = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in str(name))
            files[f"host/{safe}.txt"] = str(text)
    elif is_raspberry_pi():
        files["host/README.txt"] = (
            "This device is a Pi appliance but its host bridge did not answer, "
            "so the host report (service states, journal, display probes) is "
            "missing from this bundle.\n")

    # The one scrub point: every byte of text in the zip goes through the same
    # redaction the log download uses.
    return {name: redact_text(text, secrets) for name, text in files.items()}


def build_zip_bytes(files: "dict[str, str]") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in sorted(files.items()):
            zf.writestr(name, text)
    return buf.getvalue()
