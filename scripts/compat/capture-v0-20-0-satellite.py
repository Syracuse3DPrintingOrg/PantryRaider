#!/usr/bin/env python3
"""Vendor the satellite pull as it shipped in v0.20.0.

Build-time only: run it from the repository root when the fixture needs to be
made again. It reads service/app/services/satellite.py at the v0.20.0 tag (git
show), takes the exact source of the functions and constants the config pull
uses (ast.get_source_segment), and writes them, with the tag, the path and the
blob sha, to tests/data/compat_v0_20_0_satellite.json.
tests/test_fleet_compat_v0_20_0.py runs that code against this release's
server to prove a Bandit still on v0.20.0 keeps syncing. The test suite never
runs git itself.

    python3 scripts/compat/capture-v0-20-0-satellite.py
"""
from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TAG = "v0.20.0"
PATH = "service/app/services/satellite.py"
OUT = ROOT / "tests" / "data" / "compat_v0_20_0_satellite.json"
NAMES = (
    "_SENSITIVE_SUFFIXES", "_SENSITIVE_EXTRA", "_is_sensitive_field",
    "config_signature", "signature_ok", "_last_server_version",
    "last_server_version", "_apply_config", "_swap_host", "_is_ip_literal",
    "_resolve_host", "_sync_candidates", "_do_sync_from_upstream",
    "_STREAMDECK_SYNCED_FIELDS",
)


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True,
                          capture_output=True, text=True).stdout


def _defined(node) -> str | None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name
    if isinstance(node, ast.Assign) and len(node.targets) == 1 \
            and isinstance(node.targets[0], ast.Name):
        return node.targets[0].id
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    return None


def main() -> None:
    source = _git("show", f"{TAG}:{PATH}")
    blob = _git("rev-parse", f"{TAG}:{PATH}").strip()
    tree = ast.parse(source)
    found: dict[str, str] = {}
    for node in tree.body:
        name = _defined(node)
        if name in NAMES and name not in found:
            found[name] = ast.get_source_segment(source, node)
    missing = [n for n in NAMES if n not in found]
    if missing:
        raise SystemExit(f"not found at {TAG}: {', '.join(missing)}")
    OUT.write_text(json.dumps({
        "_comment": ("The satellite config pull as it shipped in v0.20.0, vendored at "
                     "build time by scripts/compat/capture-v0-20-0-satellite.py."),
        "tag": TAG,
        "file": PATH,
        "blob_sha": blob,
        "names": list(NAMES),
        "sources": {n: found[n] for n in NAMES},
    }, indent=2) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(NAMES)} names, blob {blob[:12]})")


if __name__ == "__main__":
    main()
