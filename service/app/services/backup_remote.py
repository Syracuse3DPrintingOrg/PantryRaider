"""Validate the cloud-backup remote before it reaches an rclone command line.

settings.rclone_remote is typed by the user in Settings and later becomes a
positional argument to `rclone copyto` / `rclone lsd` (routers/admin.py). An
unchecked value starting with "-" would be parsed by rclone as a flag, and
rclone has flags that read or write arbitrary files, so a saved setting could
otherwise smuggle options into the process (security review, Jul 2026). The
same check runs at both boundaries: on save (routers/setup.py) so a bad value
is never stored, and again right before each exec as defense in depth.

Accepted shapes, matching what rclone itself takes as a destination:
  * "remote:path" or "remote:" where the remote name starts with a letter,
    digit, or underscore (rclone remote names are made of letters, digits,
    "_", "-", "." and space, and cannot start with "-").
  * An absolute path ("/backups/pantry") for a mounted local destination.

Pure logic, no I/O, so it stays cheap to test.
"""
from __future__ import annotations

import re

# remote name followed by ":", then any path (possibly empty).
_REMOTE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9 _.-]*:")


def valid_remote(value: str) -> bool:
    """True when value is a safe rclone destination (remote:path or /path)."""
    v = (value or "").strip()
    if not v or v.startswith("-"):
        return False
    return bool(_REMOTE_RE.match(v)) or v.startswith("/")
