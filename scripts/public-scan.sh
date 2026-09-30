#!/usr/bin/env bash
# Scan a tree bound for the public repo for anything that has to stay private:
# the terms in scripts/public-private-terms.txt (in text, in the printable
# strings of binary files, and in file names), personal home paths, and
# secrets (gitleaks). RFC 1918 addresses are counted and reported, never
# failed on.
#
#   scripts/public-scan.sh <tree-dir> [--list-only]
#
# <tree-dir> is the built public snapshot (scripts/sync-public.sh scans it
# before any push) or the dev tree. In a git tree the scan covers the tracked
# files plus untracked files that are not ignored; anywhere else it covers
# every file. Either way the paths in scripts/public-strip.txt are left out, so
# pointing it at the dev tree scans what the next snapshot would publish.
# --list-only prints that file list and stops.
#
# Exit status: 0 clean; 1 a hit that is not allowlisted; 2 a usage or setup
# error; 3 nothing found, but gitleaks could not run here (sync-public.sh
# refuses --push on that).
#
# scripts/public-scan-allow.txt holds path<TAB>regex<TAB>reason lines and is the
# only way to tune the scan, gitleaks findings included. Inline gitleaks:allow
# comments and any .gitleaks.toml or .gitleaksignore in the scanned tree are
# ignored on purpose.
#
# Overrides, used by tests/test_public_scan.py: PR_SCAN_TERMS, PR_SCAN_ALLOW and
# PR_SCAN_STRIP point at other list files, and PR_SCAN_GITLEAKS picks how
# gitleaks runs (auto: the pinned container, then a pinned binary on PATH;
# docker; binary; off).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PR_SCAN_TERMS="${PR_SCAN_TERMS:-$here/public-private-terms.txt}"
export PR_SCAN_ALLOW="${PR_SCAN_ALLOW:-$here/public-scan-allow.txt}"
export PR_SCAN_STRIP="${PR_SCAN_STRIP:-$here/public-strip.txt}"
export PR_SCAN_GITLEAKS="${PR_SCAN_GITLEAKS:-auto}"

exec python3 - "$@" <<'PY'
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# The official image, pinned by its multi-arch index digest (gitleaks v8.30.1).
# A local binary is accepted only at the same version.
GITLEAKS_IMAGE = ("ghcr.io/gitleaks/gitleaks@sha256:"
                  "c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f")
GITLEAKS_VERSION = "8.30.1"

# /home/<user> other than the appliance accounts, and /Users/<Name>. Case
# matters: /users/self is a Mealie API path, not a Mac home. A match preceded
# by a word character, a dot, a tilde, a percent sign, a dash, or a slash is
# part of a URL path (Mealie's /g/home/r/ links, https://host/home/...), except
# after file:// where the path really is local.
PERSONAL_PATH = re.compile(
    r"(?:(?<=file://)|(?<![\w.~%/-]))"
    r"/(?:home/(?!(?:foodassistant|pi)(?![\w.-]))[A-Za-z_][\w.-]*"
    r"|Users/[A-Z][\w.-]*)")
RFC1918 = re.compile(
    r"(?<![\w.])(?:10(?:\.\d{1,3}){3}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}"
    r"|192\.168(?:\.\d{1,3}){2})(?![\d.]*\d)")
PRINTABLE = re.compile(rb"[\t\x20-\x7e]{4,}")


def die(msg):
    print(f"public-scan: {msg}", file=sys.stderr)
    sys.exit(2)


def entries(path):
    """(line number, text) for each line that is not blank or a # comment."""
    out = []
    for n, raw in enumerate(Path(path).read_text().splitlines(), 1):
        if raw.strip() and not raw.lstrip().startswith("#"):
            out.append((n, raw))
    return out


args = sys.argv[1:]
list_only = "--list-only" in args
rest = [a for a in args if a != "--list-only"]
if len(rest) != 1 or rest[0].startswith("-"):
    die("usage: scripts/public-scan.sh <tree-dir> [--list-only]")
tree = Path(rest[0]).resolve()
if not tree.is_dir():
    die(f"{rest[0]} is not a directory")

strip_file = Path(os.environ["PR_SCAN_STRIP"])
strip = [t.strip().strip("/") for _, t in entries(strip_file)] if strip_file.is_file() else []


def stripped(rel):
    return any(rel == s or rel.startswith(s + "/") for s in strip)


def candidates():
    if (tree / ".git").exists():
        run = subprocess.run(["git", "-C", str(tree), "ls-files", "-z", "--cached",
                              "--others", "--exclude-standard"],
                             capture_output=True, check=True)
        rels = {r for r in run.stdout.decode("utf-8", "surrogateescape").split("\0") if r}
    else:
        rels = set()
        for dirpath, dirnames, filenames in os.walk(tree):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for name in filenames:
                rels.add((Path(dirpath) / name).relative_to(tree).as_posix())
    # A symlink is scanned as its target text (what git stores), never followed.
    return sorted(r for r in rels
                  if not stripped(r) and ((tree / r).is_symlink() or (tree / r).is_file()))


def content(rel):
    path = tree / rel
    if path.is_symlink():
        return os.fsencode(os.readlink(path))
    return path.read_bytes()


files = candidates()
if list_only:
    for rel in files:
        print(rel)
    sys.exit(0)

terms_file = Path(os.environ["PR_SCAN_TERMS"])
if not terms_file.is_file():
    die(f"no private terms list at {terms_file}; the scan cannot run without it")
terms = []
for n, text in entries(terms_file):
    try:
        terms.append((n, re.compile(text.strip(), re.I)))
    except re.error as exc:
        die(f"{terms_file}:{n}: not a valid pattern ({exc})")
if not terms:
    die(f"{terms_file} lists no terms")

allow = []
allow_file = Path(os.environ["PR_SCAN_ALLOW"])
if allow_file.is_file():
    for n, text in entries(allow_file):
        parts = text.split("\t")
        if len(parts) != 3 or not all(p.strip() for p in parts):
            die(f"{allow_file}:{n}: expected path<TAB>regex<TAB>reason")
        try:
            allow.append([n, parts[0].strip(), re.compile(parts[1]), 0])
        except re.error as exc:
            die(f"{allow_file}:{n}: not a valid pattern ({exc})")

hits = []          # (path, where, rule, text the allowlist regex is matched against)
private_count = [0, 0]
line_cache = {}


def lines_of(rel):
    if rel not in line_cache:
        body = content(rel).decode("utf-8", "replace")
        line_cache[rel] = body.splitlines()
    return line_cache[rel]


for rel in files:
    for n, rx in terms:
        if rx.search(rel):
            hits.append((rel, 0, f"private term in the file name (terms line {n})", rel))
    data = content(rel)
    if b"\0" in data[:8000]:
        for m in PRINTABLE.finditer(data):
            run = m.group().decode("ascii")
            for n, rx in terms:
                if rx.search(run):
                    hits.append((rel, f"@{m.start()}",
                                 f"private term in a binary file (terms line {n})", run))
        continue
    body = data.decode("utf-8", "replace")
    found = len(RFC1918.findall(body))
    if found:
        private_count[0] += found
        private_count[1] += 1
    for n, rx in terms:
        if rx.search(body):
            for ln, line in enumerate(lines_of(rel), 1):
                if rx.search(line):
                    hits.append((rel, ln, f"private term (terms line {n})", line))
    if PERSONAL_PATH.search(body):
        for ln, line in enumerate(lines_of(rel), 1):
            if PERSONAL_PATH.search(line):
                hits.append((rel, ln, "personal home path", line))


def gitleaks_version(exe):
    try:
        run = subprocess.run([exe, "version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    return run.stdout.strip().lstrip("v")


def run_gitleaks():
    """Findings as (path, line, rule id), or None and the reason it did not run."""
    mode = os.environ["PR_SCAN_GITLEAKS"]
    if mode == "off":
        return None, "turned off with PR_SCAN_GITLEAKS=off"
    with tempfile.TemporaryDirectory(prefix="pr-public-scan-") as tmp:
        stage, out, cfg = (Path(tmp, d) for d in ("scan", "out", "cfg"))
        for d in (stage, out, cfg):
            d.mkdir()
        # Only the candidate files, so the dev tree's ignored and stripped paths
        # never reach gitleaks. The explicit config and the empty ignore folder
        # keep a .gitleaks.toml or .gitleaksignore in the tree from muting it.
        # gitleaks still reads a .gitleaksignore at the top of the folder it
        # scans whatever the flags say, so that one file is staged under
        # another name (and still scanned).
        renamed = {}
        for rel in files:
            staged = rel
            if rel == ".gitleaksignore":
                staged = ".gitleaksignore.pr-scan"
                renamed[staged] = rel
            dest = stage / staged
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content(rel))
        (cfg / "gitleaks.toml").write_text("[extend]\nuseDefault = true\n")
        os.chmod(out, 0o777)

        def flags(scan, cfgdir, report):
            return ["dir", scan, "--no-banner", "--no-color", "--redact",
                    "--exit-code", "0", "--ignore-gitleaks-allow", "--log-level", "error",
                    "--config", f"{cfgdir}/gitleaks.toml", "--gitleaks-ignore-path", cfgdir,
                    "--report-format", "json", "--report-path", report]

        reasons = []
        attempts = []
        if mode in ("auto", "docker"):
            docker = shutil.which("docker")
            if docker and subprocess.run([docker, "info"], capture_output=True).returncode == 0:
                attempts.append(("the pinned container", [
                    docker, "run", "--rm", "--network", "none",
                    "--user", f"{os.getuid()}:{os.getgid()}",
                    "-v", f"{stage}:/scan:ro", "-v", f"{cfg}:/cfg:ro", "-v", f"{out}:/out",
                    GITLEAKS_IMAGE, *flags("/scan", "/cfg", "/out/report.json")], "/scan"))
            else:
                reasons.append("Docker is not available")
        if mode in ("auto", "binary"):
            exe = shutil.which("gitleaks")
            if not exe:
                reasons.append("no gitleaks binary on PATH")
            elif gitleaks_version(exe) != GITLEAKS_VERSION:
                reasons.append(f"the gitleaks on PATH is not version {GITLEAKS_VERSION}")
            else:
                attempts.append((f"gitleaks {GITLEAKS_VERSION} on PATH",
                                 [exe, *flags(str(stage), str(cfg), str(out / "report.json"))],
                                 str(stage)))
        for label, cmd, prefix in attempts:
            report = out / "report.json"
            if report.exists():
                report.unlink()
            run = subprocess.run(cmd, capture_output=True, text=True, cwd=tmp)
            if run.returncode != 0 or not report.is_file():
                reasons.append(f"{label} failed ({run.stderr.strip()[-200:] or run.returncode})")
                continue
            findings = []
            for item in json.loads(report.read_text() or "[]"):
                path = item.get("File", "")
                if path.startswith(prefix + "/"):
                    path = path[len(prefix) + 1:]
                path = renamed.get(path, path)
                findings.append((path, int(item.get("StartLine") or 0),
                                 item.get("RuleID", "unknown")))
            return findings, label
        return None, "; ".join(reasons) or f"PR_SCAN_GITLEAKS={mode} is not a known mode"


findings, gitleaks_note = run_gitleaks()
if findings is not None:
    for path, ln, rule in findings:
        try:
            text = lines_of(path)[ln - 1] if ln > 0 else ""
        except (OSError, IndexError):
            text = ""
        hits.append((path, ln, f"gitleaks {rule}", text))

blocked = []
allowed = 0
for path, where, rule, text in sorted(set(hits), key=lambda h: (h[0], str(h[1]).zfill(9), h[2])):
    entry = next((a for a in allow
                  if fnmatch.fnmatchcase(path, a[1]) and a[2].search(text)), None)
    if entry:
        entry[3] += 1
        allowed += 1
    else:
        blocked.append(f"{path}:{where}: {rule}")

print(f"Scanned {len(files)} files under {tree}.")
print(f"RFC 1918 addresses (report only): {private_count[0]} in {private_count[1]} files.")
if findings is None:
    print(f"GITLEAKS DID NOT RUN: {gitleaks_note}.")
else:
    print(f"gitleaks: ran with {gitleaks_note}.")
for n, glob, _rx, used in allow:
    if not used:
        print(f"Note: allowlist line {n} ({glob}) matched nothing; prune it if it is stale.")
if allowed:
    print(f"Allowlisted: {allowed} hits.")
if blocked:
    print(f"{len(blocked)} hits that are not allowlisted:")
    for line in blocked:
        print(f"  {line}")
    sys.exit(1)
print("No private material found.")
sys.exit(3 if findings is None else 0)
PY
