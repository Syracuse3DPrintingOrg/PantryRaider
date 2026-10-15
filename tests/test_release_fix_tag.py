"""scripts/release-fix-tag.sh repairs the release when the SD image and Cub
firmware workflows both create it at once, against a stubbed gh."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "release-fix-tag.sh"

pytestmark = pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")

# A tiny fake of the few GitHub API calls the script makes, over a JSON file.
GH = r'''
import json, sys
state_path = sys.argv[1]
args = sys.argv[3:]          # after "api"
method = "GET"
fields = {}
path = None
jq = None
i = 0
while i < len(args):
    a = args[i]
    if a == "--method" or a == "-X":
        method = args[i + 1]; i += 2; continue
    if a in ("-f", "-F"):
        k, v = args[i + 1].split("=", 1)
        fields[k] = {"false": False, "true": True}.get(v, v) if a == "-F" else v
        i += 2; continue
    if a == "--jq":
        jq = args[i + 1]; i += 2; continue
    path = a; i += 1
rels = json.load(open(state_path))
p = path.split("?")[0].split("/")[3:]   # after repos/owner/name
def out(obj):
    print(json.dumps(obj))
if p == ["releases"]:
    res = rels
elif p[:2] == ["releases", "tags"]:
    hit = [r for r in rels if r["tag_name"] == p[2] and not r["draft"]]
    if not hit:
        print('{"message":"Not Found","status":"404"}')
        sys.exit(1)
    res = hit[0]
else:
    rid = int(p[1])
    r = next(r for r in rels if r["id"] == rid)
    if method == "DELETE":
        rels.remove(r); json.dump(rels, open(state_path, "w")); sys.exit(0)
    if method == "PATCH":
        r.update(fields); json.dump(rels, open(state_path, "w")); res = r
    else:
        res = r
json.dump(rels, open(state_path, "w"))
if jq is None:
    out(res)
elif jq == ".id":
    print(res["id"])
elif jq == ".assets | length":
    print(len(res["assets"]))
elif jq.startswith("[.assets[].name | select(endswith("):
    suf = jq.split('endswith("')[1].split('")')[0]
    print(len([a for a in res["assets"] if a["name"].endswith(suf)]))
elif "select(.draft" in jq:
    for r in res:
        if r["draft"] and r["name"] == jq.split('.name == "')[1].split('"')[0] and r["assets"]:
            print(r["id"])
elif "untagged-" in jq:
    tag = jq.split('.name == "')[1].split('"')[0]
    for r in res:
        if r["name"] == tag and r["tag_name"] != tag and r["tag_name"].startswith("untagged-"):
            print(r["id"])
else:
    raise SystemExit("unhandled jq: " + jq)
'''


def _run(tmp_path, releases, *args):
    state = tmp_path / "releases.json"
    state.write_text(json.dumps(releases))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "fakegh.py").write_text(GH)
    (bindir / "gh").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bindir}/fakegh.py" "{state}" "$@"\n')
    os.chmod(bindir / "gh", 0o755)
    (bindir / "sleep").write_text("#!/bin/sh\nexit 0\n")
    os.chmod(bindir / "sleep", 0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
           "GITHUB_REPOSITORY": "o/r", "GH_TOKEN": "x"}
    proc = subprocess.run(["bash", str(SCRIPT), *args], env=env,
                          capture_output=True, text=True)
    return proc, json.loads(state.read_text())


def _rel(id_, tag, draft, assets, name=None):
    return {"id": id_, "tag_name": tag, "name": name or "v1.2.3", "draft": draft,
            "assets": [{"name": a} for a in assets]}


def test_an_empty_published_release_gives_way_to_the_draft_with_the_files(tmp_path):
    proc, rels = _run(tmp_path, [
        _rel(1, "v1.2.3", True, ["pr-v1.2.3-arm64.img.xz", "cub.bin"]),
        _rel(2, "v1.2.3", False, []),
    ], "v1.2.3", "-arm64.img.xz")
    assert proc.returncode == 0, proc.stderr
    assert [(r["id"], r["draft"]) for r in rels] == [(1, False)]


def test_a_healthy_release_is_left_alone(tmp_path):
    proc, rels = _run(tmp_path, [_rel(1, "v1.2.3", False, ["pr-v1.2.3-arm64.img.xz"])],
                      "v1.2.3", "-arm64.img.xz")
    assert proc.returncode == 0, proc.stderr
    assert rels == [_rel(1, "v1.2.3", False, ["pr-v1.2.3-arm64.img.xz"])]


def test_an_untagged_release_goes_back_on_its_tag(tmp_path):
    proc, rels = _run(tmp_path, [_rel(1, "untagged-abc", False, ["cub.bin"])], "v1.2.3")
    assert proc.returncode == 0, proc.stderr
    assert rels[0]["tag_name"] == "v1.2.3"


def test_a_release_without_the_image_fails_the_run(tmp_path):
    proc, _ = _run(tmp_path, [_rel(1, "v1.2.3", False, ["cub.bin"])],
                   "v1.2.3", "-arm64.img.xz")
    assert proc.returncode == 1
    assert "no file ending in -arm64.img.xz" in proc.stderr


def test_a_draft_still_in_progress_is_left_alone(tmp_path):
    """The Cub workflow runs this while the SD image is still building: the
    draft holds only firmware and nothing is published yet."""
    proc, rels = _run(tmp_path, [_rel(1, "v1.2.3", True, ["cub.bin"])], "v1.2.3")
    assert proc.returncode == 0, proc.stderr
    assert rels == [_rel(1, "v1.2.3", True, ["cub.bin"])]
