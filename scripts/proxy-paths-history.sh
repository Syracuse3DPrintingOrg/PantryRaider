#!/usr/bin/env bash
# List every Grocy and Mealie call a satellite has sent through the main
# server's /api/proxy hop, across all release tags, and check each one against
# the proxy's allow list (service/app/services/proxy_policy.py).
#
# Run it during release review, before changing the allow list or turning the
# proxy's enforce mode on: a path that some fielded release still calls must
# stay allowed, or that release loses the feature behind it.
#
# Usage:
#   scripts/proxy-paths-history.sh            # table: backend, method, path, releases
#   scripts/proxy-paths-history.sh --json     # the same rows as JSON
#   scripts/proxy-paths-history.sh --check    # also classify each path; exit 1 if
#                                             # any is not allowed or is denied
#   scripts/proxy-paths-history.sh --since v0.18.0   # only tags from this one on
#
# Reads tags with git show; never changes the repo. The JSON output is the
# format of tests/data/r1_fleet_proxy_client_paths.json, so refreshing that
# fixture is: scripts/proxy-paths-history.sh --json > tests/data/r1_fleet_proxy_client_paths.json
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="table"
CHECK=0
SINCE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --json) MODE="json" ;;
    --check) CHECK=1 ;;
    --since) shift; SINCE="${1:-}" ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "proxy-paths-history: unknown option $1" >&2; exit 2 ;;
  esac
  shift
done

exec python3 - "$REPO_ROOT" "$MODE" "$CHECK" "$SINCE" <<'PY'
import json
import re
import subprocess
import sys

root, mode, check, since = sys.argv[1], sys.argv[2], sys.argv[3] == "1", sys.argv[4]


def git(*args):
    return subprocess.run(("git",) + args, cwd=root, capture_output=True,
                          text=True).stdout


def vkey(tag):
    return [int(p) if p.isdigit() else 0 for p in tag.lstrip("v").split(".")]


tags = [t for t in git("tag", "--list", "v[0-9]*").split()
        if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
tags.sort(key=vkey)
if since:
    tags = [t for t in tags if vkey(t) >= vkey(since)]
tags.append("HEAD")

# A client call: self._request("PUT", f"/objects/products/{id}", ...),
# _get / _post / _cached_list / _ensure_object / _list_all with a literal
# path, or
# _scoped (Mealie's households/ or groups/ prefix).
CALL = re.compile(
    r"""\._(request|get|post|cached_list|ensure_object|scoped|list_all)\(\s*"""
    r"""(?:"(GET|POST|PUT|DELETE|PATCH)"\s*,\s*)?f?(["'])(/(?:[^"'{]|\{[^}]*\})*)\3""")
IMAGE = re.compile(r"""/api/media/recipes/\{[^}]*\}/images/([A-Za-z0-9._-]+)""")
DEFAULT_METHOD = {"get": ["GET"], "post": ["POST"], "cached_list": ["GET"],
                  "ensure_object": ["GET", "POST"], "list_all": ["GET"]}

# Stand-in values for the placeholders, so each row is a real path the
# classifier can judge.
SAMPLE_ID = "12"
SAMPLE_BARCODE = "4006381 333931"
SAMPLE_SLUG = "chili-con-carne"
SAMPLE_UUID = "3f2a9c1e-5b7d-4e8a-9c0b-1d2e3f4a5b6c"


def concrete(backend, template):
    def fill(m):
        before = template[:m.start()]
        if backend == "grocy":
            return SAMPLE_BARCODE if before.endswith("by-barcode/") else SAMPLE_ID
        if before.endswith("/recipes/") and "media" not in before:
            return SAMPLE_SLUG
        return SAMPLE_UUID
    return re.sub(r"\{[^}]*\}", fill, template)


rows = {}


def add(backend, method, template, tag):
    template = "api" + template.split("?")[0]
    template = re.sub(r"\{[^}]*\}", "{x}", template)
    row = rows.setdefault((backend, method, template), {
        "backend": backend, "method": method, "template": template,
        "path": concrete(backend, template), "releases": []})
    row["releases"].append(tag)


for tag in tags:
    files = [f for f in git("ls-tree", "-r", "--name-only", tag, "service/app").split()
             if f.endswith(".py")]
    for f in files:
        src = git("show", f"{tag}:{f}")
        for m in IMAGE.finditer(src):
            add("mealie", "GET", "/media/recipes/{x}/images/" + m.group(1), tag)
        if "._" not in src:
            continue
        backend = "mealie" if f.endswith("services/mealie.py") else "grocy"
        for m in CALL.finditer(src):
            fn, method, _, path = m.groups()
            methods = [method] if method else DEFAULT_METHOD.get(fn, [])
            if fn == "scoped":
                for scope in ("/households", "/groups"):
                    for meth in methods:
                        add("mealie", meth, scope + path, tag)
                continue
            if fn == "request" and backend == "mealie" and path.startswith(("/households", "/groups")):
                continue  # the scoped helper's own prefixing, counted above
            for meth in methods:
                add(backend, meth, path, tag)

out = []
for key in sorted(rows):
    row = rows[key]
    rel = sorted(set(row.pop("releases")), key=lambda t: (t == "HEAD", vkey(t)))
    row["first"], row["last"], row["count"] = rel[0], rel[-1], len(rel)
    out.append(row)

failed = []
if check:
    sys.path.insert(0, root + "/service")
    from app.services import proxy_policy
    for row in out:
        verdict, tpl = proxy_policy.classify(row["backend"], row["method"], row["path"])
        row["verdict"] = verdict
        if verdict != "allow":
            failed.append(row)

if mode == "json":
    print(json.dumps(out, indent=1))
else:
    for row in out:
        extra = f"  [{row['verdict']}]" if check else ""
        print(f"{row['backend']:6} {row['method']:6} {row['template']:55} "
              f"{row['first']} .. {row['last']} ({row['count']}){extra}")
if failed:
    print(f"proxy-paths-history: {len(failed)} client path(s) are not on the allow list",
          file=sys.stderr)
    sys.exit(1)
PY
