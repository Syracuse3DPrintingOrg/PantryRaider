"""The release verifier (PYVERIFY) every update and install runs.

The verifier is a standard-library python3 program embedded in
foodassistant-update (firstboot.sh and install.sh carry byte-identical
copies). These tests extract it from the updater and run it the way the
updater does, against:

- the real published v0.20.0 tag, loaded from the vendored objects in
  tests/data/release_objects into a throwaway repository (nothing reads this
  repository's own history or tags), with the real scripts/release-signers;
- throwaway keys made per session, for the multi-key list and for the signed
  release record (release.json).
"""
import base64
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UPDATER = ROOT / "scripts" / "image-build" / "foodassistant-update"
SIGNERS = ROOT / "scripts" / "release-signers"
OBJECTS = Path(__file__).resolve().parent / "data" / "release_objects"
PUBLISHED_FP = "SHA256:cS04LyfO51r/AaqHOJhB0GErj6IR4eT8XOJnTvyy1h8"
V0200_COMMIT = "180048de8ff1b73a4a42b19b580c78b99b9fd140"
IMAGE = "ghcr.io/syracuse3dprintingorg/pantryraider"

pytestmark = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("bash", "git", "python3", "ssh-keygen")),
    reason="bash, git, python3 and ssh-keygen are needed to run the verifier",
)


def extract_pyverify(path=UPDATER):
    """The text between <<'PYVERIFY' and its closing line."""
    lines = path.read_text().splitlines(keepends=True)
    start = next(i for i, l in enumerate(lines) if l.rstrip().endswith("<<'PYVERIFY' || true"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i].rstrip("\n") == "PYVERIFY")
    return "".join(lines[start + 1:end])


PYVERIFY = extract_pyverify()


def pyv(*args):
    r = subprocess.run(["python3", "-c", PYVERIFY, *map(str, args)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    assert len(lines) == 1, r.stdout
    return json.loads(lines[0])


def _git(repo, *args, **kw):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, **kw)


def load_release_objects(repo, ref_name=None):
    """git init REPO and load the vendored v0.20.0 objects into it; the tag
    ref is created as refs/tags/<ref_name or the published name>."""
    index = json.loads((OBJECTS / "v0.20.0.objects.json").read_text())
    if not (Path(repo) / ".git").exists():
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    for row in index["objects"]:
        out = _git(repo, "hash-object", "-w", "--literally", "-t", row["type"],
                   str(OBJECTS / row["file"]), text=True).stdout.strip()
        assert out == row["id"], row["file"]
    tag_id = index["objects"][0]["id"]
    _git(repo, "update-ref", "refs/tags/" + (ref_name or index["tag"]), tag_id)
    return tag_id


def _keypair(dirpath, name):
    key = Path(dirpath) / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name,
                    "-f", str(key)], check=True, capture_output=True)
    pub = (Path(str(key) + ".pub")).read_text().split()
    line = f'pantryraider-release namespaces="git,pantryraider-release" {pub[0]} {pub[1]}\n'
    return key, line


@pytest.fixture(scope="session")
def keys(tmp_path_factory):
    d = tmp_path_factory.mktemp("release-keys")
    k1, l1 = _keypair(d, "key1")
    k2, l2 = _keypair(d, "key2")
    return {"dir": d, "k1": k1, "l1": l1, "k2": k2, "l2": l2}


def _sign(key, path, namespace):
    subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(key), "-n", namespace,
                    str(path)], check=True, capture_output=True)
    return Path(str(path) + ".sig")


@pytest.fixture()
def real_repo(tmp_path):
    repo = tmp_path / "objects"
    load_release_objects(repo)
    return repo


# -- the real published tag ----------------------------------------------------

def test_vendored_objects_load_with_their_published_ids(real_repo):
    shown = _git(real_repo, "show", V0200_COMMIT + ":service/app/config.py", text=True).stdout
    assert 'APP_VERSION = "0.20.0"' in shown


def test_published_tag_verifies_with_the_release_key(real_repo):
    out = pyv("verify-tag", real_repo, "v0.20.0", SIGNERS)
    assert out["ok"] is True, out
    assert out["commit"] == V0200_COMMIT
    assert out["version"] == "0.20.0"


def test_published_commit_verifies_with_the_release_key(real_repo):
    out = pyv("verify-commit", real_repo, V0200_COMMIT, SIGNERS)
    assert out["ok"] is True, out
    assert out["commit"] == V0200_COMMIT
    assert out["version"] == "0.20.0"


def test_fingerprint_of_the_release_key_list():
    out = pyv("fingerprints", SIGNERS)
    assert out == {"ok": True, "reason": "", "fingerprints": PUBLISHED_FP}


def test_fingerprint_matches_the_ssh_definition(keys, tmp_path):
    # SHA256 of the key blob, base64 without padding: what ssh-keygen -l prints.
    signers = tmp_path / "two"
    signers.write_text("# release keys\n\n" + keys["l1"] + keys["l2"])
    out = pyv("fingerprints", signers)
    want = []
    for line in (keys["l1"], keys["l2"]):
        blob = base64.b64decode(line.split()[-1])
        want.append("SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("="))
    assert out["fingerprints"] == ",".join(want)
    r = subprocess.run(["ssh-keygen", "-l", "-f", str(keys["k1"]) + ".pub"],
                       capture_output=True, text=True, check=True)
    assert want[0] in r.stdout


def test_fingerprints_of_a_list_with_no_key(tmp_path):
    empty = tmp_path / "signers"
    empty.write_text("# no keys yet\n\n")
    assert pyv("fingerprints", empty)["ok"] is False


def test_the_same_tag_under_another_name_is_refused(tmp_path):
    repo = tmp_path / "moved"
    load_release_objects(repo, ref_name="v0.20.1")
    out = pyv("verify-tag", repo, "v0.20.1", SIGNERS)
    assert out["ok"] is False
    assert out["reason"] == "name_mismatch"


def test_a_tag_name_that_is_not_there_is_refused(real_repo):
    out = pyv("verify-tag", real_repo, "v9.9.9", SIGNERS)
    assert out["ok"] is False


def test_a_tampered_tag_is_refused(tmp_path, real_repo):
    raw = (OBJECTS / "v0.20.0.tag").read_bytes()
    assert b"v0.20.0" in raw
    # Same signature, a different object it claims to name.
    tampered = raw.replace(b"object " + V0200_COMMIT.encode(),
                           b"object " + V0200_COMMIT[:-1].encode() + b"1", 1)
    assert tampered != raw
    f = tmp_path / "tampered.tag"
    f.write_bytes(tampered)
    tid = _git(real_repo, "hash-object", "-w", "--literally", "-t", "tag", str(f),
               text=True).stdout.strip()
    _git(real_repo, "update-ref", "refs/tags/v0.20.0", tid)
    out = pyv("verify-tag", real_repo, "v0.20.0", SIGNERS)
    assert out["ok"] is False
    assert out["reason"] == "bad_signature"


def test_a_tampered_commit_is_refused(tmp_path, real_repo):
    raw = (OBJECTS / "v0.20.0.commit").read_bytes()
    head, sep, body = raw.partition(b"\n\n")
    f = tmp_path / "tampered.commit"
    f.write_bytes(head + sep + body + b"one more line\n")
    cid = _git(real_repo, "hash-object", "-w", "--literally", "-t", "commit", str(f),
               text=True).stdout.strip()
    out = pyv("verify-commit", real_repo, cid, SIGNERS)
    assert out["ok"] is False
    assert out["reason"] == "bad_signature"


def test_a_signers_list_without_the_key_is_refused(keys, tmp_path, real_repo):
    other = tmp_path / "other-signers"
    other.write_text(keys["l1"])
    assert pyv("verify-tag", real_repo, "v0.20.0", other)["reason"] == "bad_signature"
    assert pyv("verify-commit", real_repo, V0200_COMMIT, other)["reason"] == "bad_signature"


def test_a_list_of_two_accepts_either_key(keys, tmp_path, real_repo):
    two = tmp_path / "two"
    two.write_text("# backup key first\n" + keys["l1"] + SIGNERS.read_text())
    assert pyv("verify-tag", real_repo, "v0.20.0", two)["ok"] is True
    # And a signature made by the second key of a throwaway pair.
    pair = tmp_path / "pair"
    pair.write_text(keys["l1"] + keys["l2"])
    doc = tmp_path / "doc.txt"
    doc.write_text("signed by the second key\n")
    sig = _sign(keys["k2"], doc, "git")
    assert pyv("verify-file", doc, sig, pair, "git")["ok"] is True
    only_first = tmp_path / "only-first"
    only_first.write_text(keys["l1"])
    assert pyv("verify-file", doc, sig, only_first, "git")["ok"] is False


# -- the signed release record -------------------------------------------------

def _record(**over):
    doc = {
        "format": 1,
        "version": "0.21.1",
        "tag": "v0.21.1",
        "commit": "a" * 40,
        "image": {"name": IMAGE, "index_digest": "sha256:" + "b" * 64},
        "assets": {},
    }
    doc.update(over)
    return json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n"


def test_release_record_verifies_in_its_own_namespace(keys, tmp_path):
    signers = tmp_path / "signers"
    signers.write_text(keys["l1"])
    rec = tmp_path / "release.json"
    rec.write_text(_record())
    sig = _sign(keys["k1"], rec, "pantryraider-release")
    assert pyv("verify-file", rec, sig, signers, "pantryraider-release")["ok"] is True
    # A git-namespace check never accepts a release-record signature.
    assert pyv("verify-file", rec, sig, signers, "git")["ok"] is False
    # Nor does a changed byte.
    rec.write_text(_record(version="0.21.2"))
    assert pyv("verify-file", rec, sig, signers, "pantryraider-release")["ok"] is False


def test_check_manifest_accepts_a_matching_record(tmp_path):
    rec = tmp_path / "release.json"
    rec.write_text(_record())
    out = pyv("check-manifest", rec, "0.21.1", "a" * 40)
    assert out["ok"] is True
    assert out["digest"] == "sha256:" + "b" * 64


@pytest.mark.parametrize("over,reason", [
    ({"version": "0.21.2"}, "version"),
    ({"tag": "v0.21.2"}, "tag"),
    ({"commit": "c" * 40}, "commit"),
    ({"image": {"name": "ghcr.io/example/other", "index_digest": "sha256:" + "b" * 64}}, "image_name"),
    ({"image": {"name": IMAGE, "index_digest": "sha256:" + "B" * 64}}, "digest"),
    ({"image": {"name": IMAGE, "index_digest": "sha256:abc"}}, "digest"),
    ({"format": 2}, "format"),
])
def test_check_manifest_refuses_a_changed_record(tmp_path, over, reason):
    rec = tmp_path / "release.json"
    rec.write_text(_record(**over))
    out = pyv("check-manifest", rec, "0.21.1", "a" * 40)
    assert out["ok"] is False
    assert out["reason"] == reason


def test_check_manifest_refuses_an_unreadable_record(tmp_path):
    rec = tmp_path / "release.json"
    rec.write_text("not json")
    assert pyv("check-manifest", rec, "0.21.1", "a" * 40)["ok"] is False


# -- small helpers ---------------------------------------------------------------

@pytest.mark.parametrize("a,b,want", [
    ("0.21.0", "0.20.2", 1), ("v0.20.2", "0.20.2", 0), ("0.9.1", "0.10.0", -1),
])
def test_vcmp_compares_numerically(a, b, want):
    assert pyv("vcmp", a, b)["result"] == want


def test_vcmp_refuses_an_unparseable_version():
    assert pyv("vcmp", "0.21", "0.21.0")["ok"] is False


def test_fetch_reads_a_file_url_and_reports_a_missing_one(tmp_path):
    src = tmp_path / "release.json"
    src.write_text("{}\n")
    dest = tmp_path / "copy.json"
    assert pyv("fetch", src.as_uri(), dest)["ok"] is True
    assert dest.read_text() == "{}\n"
    out = pyv("fetch", (tmp_path / "nope.json").as_uri(), tmp_path / "x")
    assert out == {"ok": False, "reason": "missing"}


def test_unknown_subcommand_is_a_usage_error():
    assert pyv("verify-everything")["reason"] == "usage"
