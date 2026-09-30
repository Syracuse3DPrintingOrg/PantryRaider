"""The public content scan and the release signature check.

scripts/public-scan.sh is the last gate before a snapshot reaches the public
repo, so every rule is exercised here against a throwaway tree with its own
terms, allowlist, and strip lists (the real terms list never leaves the dev
repo, so nothing here reads it). gitleaks is either turned off or replaced by
a stand-in binary on PATH; no test needs Docker, the network, or git.

The last part covers the release key: scripts/release-signers must name the
key with the published fingerprint, and the tag check in publish-image.yml must
accept the real v0.20.0 tag object (vendored below) and refuse a tampered or
misnamed one.

Private-looking sample paths are assembled from pieces: this file ships, and
the scan reads it too.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCAN = ROOT / "scripts" / "public-scan.sh"
SIGNERS = ROOT / "scripts" / "release-signers"
WORKFLOW = ROOT / ".github" / "workflows" / "publish-image.yml"
FINGERPRINT = "SHA256:cS04LyfO51r/AaqHOJhB0GErj6IR4eT8XOJnTvyy1h8"

HOME = "/ho" + "me/"
USERS = "/Us" + "ers/"
TERM = "zebra-" + "private-host"
IP = "10.9.8" + ".7"


def _lists(tmp_path: Path, allow: str = "", strip: str = "") -> dict[str, str]:
    cfg = tmp_path / "cfg"
    cfg.mkdir(exist_ok=True)
    (cfg / "terms.txt").write_text(f"# sample terms\n{TERM}\n(?<![\\d.])10\\.9\\.8\\.7(?!\\d)\n")
    (cfg / "allow.txt").write_text(allow)
    (cfg / "strip.txt").write_text(strip)
    return {"PR_SCAN_TERMS": str(cfg / "terms.txt"),
            "PR_SCAN_ALLOW": str(cfg / "allow.txt"),
            "PR_SCAN_STRIP": str(cfg / "strip.txt")}


def _scan(tree: Path, lists: dict[str, str], *extra: str, gitleaks: str = "off",
          path: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, PR_SCAN_GITLEAKS=gitleaks, **lists)
    if path is not None:
        env["PATH"] = path
    return subprocess.run(["bash", str(SCAN), str(tree), *extra], env=env,
                          capture_output=True, text=True)


def _tree(tmp_path: Path, files: dict[str, str | bytes]) -> Path:
    tree = tmp_path / "tree"
    for rel, body in files.items():
        dest = tree / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            dest.write_bytes(body)
        else:
            dest.write_text(body)
    tree.mkdir(exist_ok=True)
    return tree


def _hits(run: subprocess.CompletedProcess) -> list[str]:
    return [line.strip() for line in run.stdout.splitlines() if line.startswith("  ")]


# --- The content rules ---

def test_a_clean_tree_passes_but_says_gitleaks_did_not_run(tmp_path):
    tree = _tree(tmp_path, {"README.md": "nothing private here\n"})
    run = _scan(tree, _lists(tmp_path))
    assert run.returncode == 3, run.stdout + run.stderr
    assert "GITLEAKS DID NOT RUN" in run.stdout
    assert "No private material found." in run.stdout


def test_a_private_term_is_reported_by_path_and_line(tmp_path):
    tree = _tree(tmp_path, {"docs/setup.md": f"line one\nopen http://{TERM.upper()}:9284\n",
                            "tests/test_x.py": f"HOST = '{IP}'\nOTHER = '{IP}0'\n"})
    run = _scan(tree, _lists(tmp_path))
    assert run.returncode == 1
    assert _hits(run) == ["docs/setup.md:2: private term (terms line 2)",
                          "tests/test_x.py:1: private term (terms line 3)"]


def test_the_output_names_the_rule_but_never_echoes_the_term(tmp_path):
    tree = _tree(tmp_path, {"a.md": f"{TERM}\n"})
    run = _scan(tree, _lists(tmp_path))
    assert run.returncode == 1
    assert TERM not in run.stdout


def test_a_private_term_in_a_file_name_is_reported(tmp_path):
    tree = _tree(tmp_path, {f"notes/{TERM}.md": "fine\n"})
    run = _scan(tree, _lists(tmp_path))
    assert _hits(run) == [f"notes/{TERM}.md:0: private term in the file name (terms line 2)"]


def test_a_private_term_inside_a_binary_file_is_reported(tmp_path):
    blob = b"\x89PNG\r\n\x1a\n\0\0\0" + f"<xmp>{TERM}</xmp>".encode() + b"\0\x01"
    tree = _tree(tmp_path, {"docs/img/shot.png": blob})
    run = _scan(tree, _lists(tmp_path))
    assert run.returncode == 1
    [hit] = _hits(run)
    assert re.fullmatch(r"docs/img/shot\.png:@\d+: private term in a binary file \(terms line 2\)", hit)


@pytest.mark.parametrize("line", [
    f"cd {HOME}alice/src",
    f"path={HOME}bob",
    f"scp host:{HOME}carol/x .",
    f"open file://{HOME}dave/notes.txt",
    f"see {USERS}Erin/Library",
    f'"{HOME}pixel/data"',
])
def test_personal_home_paths_are_reported(tmp_path, line):
    tree = _tree(tmp_path, {"a.md": f"{line}\n"})
    run = _scan(tree, _lists(tmp_path))
    assert _hits(run) == ["a.md:1: personal home path"]


@pytest.mark.parametrize("line", [
    f"cd {HOME}foodassistant/foodassistant-src",
    f"REPO_DIR={HOME}pi/pantry",
    f"{{mealie_url}}/g{HOME}r/some-recipe",
    f"https://example.com{HOME}dashboard",
    "GET /api/users/self",
    "POST /api/users/api-tokens",
    f"http://mealie:9000/api{HOME.lower()}x",
    f"{HOME}$USER/pantry",
    f"{HOME}<user>/pantry",
    f"{USERS.lower()}Erin",
    f"{USERS}<Name>/Library",
])
def test_url_paths_and_the_appliance_accounts_are_not_personal(tmp_path, line):
    tree = _tree(tmp_path, {"a.md": f"{line}\n"})
    run = _scan(tree, _lists(tmp_path))
    assert run.returncode == 3, run.stdout


def test_the_personal_path_rule_is_case_sensitive(tmp_path):
    tree = _tree(tmp_path, {"a.md": f"{HOME.upper()}alice\n{USERS.upper()}Alice\n"})
    assert _scan(tree, _lists(tmp_path)).returncode == 3


def test_rfc1918_addresses_are_counted_but_never_fail(tmp_path):
    tree = _tree(tmp_path, {"a.md": "http://192.168.50.4:9284 and 10.0.0.12\n",
                            "b.md": "172.20.1.1, not 172.40.1.1 or 1.10.0.0.1\n"})
    run = _scan(tree, _lists(tmp_path))
    assert run.returncode == 3
    assert "RFC 1918 addresses (report only): 3 in 2 files." in run.stdout


# --- Allowlist and strip list ---

def test_an_allowlisted_hit_passes(tmp_path):
    allow = f"docs/*.md\t{TERM}:9284\tthe sample host in the setup guide\n"
    tree = _tree(tmp_path, {"docs/setup.md": f"open http://{TERM}:9284\n"})
    run = _scan(tree, _lists(tmp_path, allow=allow))
    assert run.returncode == 3, run.stdout
    assert "Allowlisted: 1 hits." in run.stdout


def test_the_allowlist_is_narrow_by_path_and_by_line(tmp_path):
    allow = "docs/*.md\t:9284\tthe sample host in the setup guide\n"
    tree = _tree(tmp_path, {"docs/setup.md": f"http://{TERM}:9284\nhttp://{TERM}:80\n",
                            "other.md": f"http://{TERM}:9284\n"})
    run = _scan(tree, _lists(tmp_path, allow=allow))
    assert _hits(run) == ["docs/setup.md:2: private term (terms line 2)",
                          "other.md:1: private term (terms line 2)"]


@pytest.mark.parametrize("bad", ["docs/*.md\tregex\n", "docs/*.md\tregex\t \n",
                                 "docs/*.md\t(unclosed\treason\n"])
def test_an_allowlist_line_without_a_reason_or_valid_regex_stops_the_scan(tmp_path, bad):
    tree = _tree(tmp_path, {"a.md": "fine\n"})
    run = _scan(tree, _lists(tmp_path, allow=bad))
    assert run.returncode == 2
    assert "allow.txt:1" in run.stderr


def test_an_unused_allowlist_entry_is_pointed_out(tmp_path):
    tree = _tree(tmp_path, {"a.md": "fine\n"})
    run = _scan(tree, _lists(tmp_path, allow="gone.md\tx\tan old sample\n"))
    assert run.returncode == 3
    assert "allowlist line 1 (gone.md) matched nothing" in run.stdout


def test_stripped_paths_are_neither_listed_nor_scanned(tmp_path):
    tree = _tree(tmp_path, {"AGENTS.md": f"{TERM}\n", "docs/design/x.md": f"{TERM}\n",
                            "docs/design-guide.md": "fine\n", "README.md": "fine\n"})
    lists = _lists(tmp_path, strip="# private\nAGENTS.md\ndocs/design\n")
    listed = _scan(tree, lists, "--list-only")
    assert listed.returncode == 0
    assert listed.stdout.split() == ["README.md", "docs/design-guide.md"]
    assert _scan(tree, lists).returncode == 3


def test_a_missing_terms_list_is_a_setup_error(tmp_path):
    tree = _tree(tmp_path, {"a.md": "fine\n"})
    lists = _lists(tmp_path)
    lists["PR_SCAN_TERMS"] = str(tmp_path / "nope.txt")
    run = _scan(tree, lists)
    assert run.returncode == 2
    assert "no private terms list" in run.stderr


def test_a_symlink_is_scanned_by_its_target_and_never_followed(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text(f"{TERM}\n")
    tree = _tree(tmp_path, {"a.md": "fine\n"})
    (tree / "docs").mkdir()
    (tree / "docs" / "notes").symlink_to(f"{HOME}alice/notes")
    (tree / "link.txt").symlink_to(outside)
    run = _scan(tree, _lists(tmp_path))
    # The first link's target is a personal path; the second points at a file
    # full of private terms, but only its target text counts.
    assert _hits(run) == ["docs/notes:1: personal home path"], run.stdout


def test_the_scanner_does_not_report_itself(tmp_path):
    """The scanner ships, so its own rules and comments must not trip them."""
    tree = _tree(tmp_path, {"scripts/public-scan.sh": SCAN.read_text(),
                            "tests/test_public_scan.py": Path(__file__).read_text()})
    run = _scan(tree, _lists(tmp_path))
    assert run.returncode == 3, run.stdout


# --- gitleaks ---

FAKE_GITLEAKS = """#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
if args[:1] == ["version"]:
    print(os.environ.get("FAKE_GITLEAKS_VERSION", "v8.30.1"))
    sys.exit(0)
pathlib.Path(os.environ["FAKE_GITLEAKS_ARGS"]).write_text(json.dumps(args))
scan = pathlib.Path(args[1])
pathlib.Path(os.environ["FAKE_GITLEAKS_ARGS"] + ".files").write_text(json.dumps(
    sorted(p.relative_to(scan).as_posix() for p in scan.rglob("*") if p.is_file())))
out = []
for path in sorted(scan.rglob("*")):
    if path.is_file():
        for n, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            if "FAKESECRET" in line:
                out.append({"File": str(path), "StartLine": n, "RuleID": "fake-rule"})
pathlib.Path(args[args.index("--report-path") + 1]).write_text(json.dumps(out))
"""


@pytest.fixture
def fake_gitleaks(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "gitleaks"
    exe.write_text(FAKE_GITLEAKS.replace("/usr/bin/env python3", sys.executable, 1))
    exe.chmod(0o755)
    args = tmp_path / "gitleaks-args.json"
    monkeypatch.setenv("FAKE_GITLEAKS_ARGS", str(args))
    return f"{bindir}{os.pathsep}{os.environ['PATH']}", args


def test_a_gitleaks_finding_fails_the_scan(tmp_path, fake_gitleaks):
    path, args = fake_gitleaks
    tree = _tree(tmp_path, {"a.py": "x = 1\ntoken = 'FAKESECRET'\n",
                            ".gitleaks.toml": "[allowlist]\npaths = ['.*']\n"})
    run = _scan(tree, _lists(tmp_path), gitleaks="binary", path=path)
    assert run.returncode == 1, run.stdout + run.stderr
    assert _hits(run) == ["a.py:2: gitleaks fake-rule"]
    assert "FAKESECRET" not in run.stdout
    # The tree cannot mute the scan: an explicit config, no ignore file, and
    # inline gitleaks:allow comments do not count.
    used = json.loads(args.read_text())
    assert "--ignore-gitleaks-allow" in used and "--config" in used
    assert "--redact" in used


def test_a_gitleaksignore_at_the_top_of_the_tree_cannot_mute_gitleaks(tmp_path, fake_gitleaks):
    """gitleaks reads .gitleaksignore at the top of the folder it scans no
    matter what the flags say, so the scan stages it under another name."""
    path, args = fake_gitleaks
    tree = _tree(tmp_path, {".gitleaksignore": "a.py:fake-rule:1\nFAKESECRET\n",
                            "a.py": "FAKESECRET\n", "sub/.gitleaksignore": "x\n"})
    run = _scan(tree, _lists(tmp_path), gitleaks="binary", path=path)
    staged = json.loads(Path(str(args) + ".files").read_text())
    assert ".gitleaksignore" not in staged
    assert ".gitleaksignore.pr-scan" in staged and "sub/.gitleaksignore" in staged
    assert run.returncode == 1
    assert _hits(run) == [".gitleaksignore:2: gitleaks fake-rule", "a.py:1: gitleaks fake-rule"]


def test_a_gitleaks_finding_can_be_allowlisted_with_a_reason(tmp_path, fake_gitleaks):
    path, _args = fake_gitleaks
    tree = _tree(tmp_path, {"tests/test_a.py": "token = 'FAKESECRET-sample'\n"})
    lists = _lists(tmp_path, allow="tests/test_a.py\tFAKESECRET-sample\ta test fixture, not a key\n")
    run = _scan(tree, lists, gitleaks="binary", path=path)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "gitleaks: ran with gitleaks 8.30.1 on PATH." in run.stdout


def test_gitleaks_never_sees_a_stripped_path(tmp_path, fake_gitleaks):
    path, _args = fake_gitleaks
    tree = _tree(tmp_path, {"AGENTS.md": "FAKESECRET\n", "a.md": "fine\n"})
    run = _scan(tree, _lists(tmp_path, strip="AGENTS.md\n"), gitleaks="binary", path=path)
    assert run.returncode == 0, run.stdout


def test_an_unpinned_gitleaks_binary_does_not_count(tmp_path, fake_gitleaks, monkeypatch):
    path, _args = fake_gitleaks
    monkeypatch.setenv("FAKE_GITLEAKS_VERSION", "8.18.0")
    tree = _tree(tmp_path, {"a.md": "FAKESECRET\n"})
    run = _scan(tree, _lists(tmp_path), gitleaks="binary", path=path)
    assert run.returncode == 3
    assert "is not version 8.30.1" in run.stdout


# --- The release key and the tag check in publish-image.yml ---

# The public v0.20.0 tag object, exactly as `git cat-file tag v0.20.0` prints it.
V0_20_0_TAG = """object 180048de8ff1b73a4a42b19b580c78b99b9fd140
type commit
tag v0.20.0
tagger BillNyeDegrasseTyson <billnye@syracuse3dprinting.com> 1790550226 +0000

Pantry Raider v0.20.0 (public beta)
-----BEGIN SSH SIGNATURE-----
U1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAgc9bNoCKHNHHtj0mVei1pl87R2s
NM5P+VfG+h48Xzat0AAAADZ2l0AAAAAAAAAAZzaGE1MTIAAABTAAAAC3NzaC1lZDI1NTE5
AAAAQO2UbOhfHNkWe4jDInwVsebNfk1MVpRKUgxaz7KzoIPyEXycJc910pEY73wflyvFd/
+Ch8eyFLFVLUwk0gT+WwU=
-----END SSH SIGNATURE-----
"""


def test_the_release_signers_file_names_the_published_key():
    lines = [line for line in SIGNERS.read_text().splitlines() if line.strip()]
    assert len(lines) == 1
    principal, options, ktype, blob = lines[0].split()
    assert principal == "pantryraider-release"
    assert options == 'namespaces="git,pantryraider-release"'
    assert ktype == "ssh-ed25519"
    digest = hashlib.sha256(base64.b64decode(blob)).digest()
    assert "SHA256:" + base64.b64encode(digest).decode().rstrip("=") == FINGERPRINT


def test_security_md_publishes_the_same_fingerprint():
    assert FINGERPRINT in (ROOT / "SECURITY.md").read_text()


def _build_steps() -> list[dict]:
    wf = yaml.safe_load(WORKFLOW.read_text())
    return wf["jobs"]["build-and-push"]["steps"]


def _verify_step() -> dict:
    return next(s for s in _build_steps() if s.get("name") == "Verify the release signature")


def _pin_check() -> str:
    run = _verify_step()["run"]
    block = re.search(r"^signers=scripts/release-signers\n.*?^fi\n", run, re.S | re.M)
    assert block and "pinned=" in block.group(0), "the pinned key check moved; update this test"
    return block.group(0)


def test_the_workflow_pins_the_same_key_as_release_signers():
    pinned = re.search(r"^pinned='(.*)'$", _pin_check(), re.M).group(1)
    assert pinned == SIGNERS.read_text().strip()


@pytest.mark.parametrize("extra,ok", [
    ("", True),
    ("# a comment\n\n", True),
    ('attacker namespaces="git" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHvP776IDpW5/AccZEw3/g/z0VslpFf7E2+iRc3lh4u+\n', False),
])
def test_a_signers_file_that_trusts_another_key_stops_the_build(tmp_path, extra, ok):
    """A pushed commit cannot add its own key to scripts/release-signers and
    then pass the check with it."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "release-signers").write_text(SIGNERS.read_text() + extra)
    run = subprocess.run(["bash", "-euo", "pipefail", "-c", _pin_check()], cwd=tmp_path,
                         capture_output=True, text=True)
    assert (run.returncode == 0) is ok, run.stdout + run.stderr
    if not ok:
        assert "does not match the release key pinned" in run.stdout


def test_the_signature_check_runs_before_anything_is_pushed():
    names = [s.get("name") for s in _build_steps()]
    at = names.index("Verify the release signature")
    assert names[at - 1] == "Checkout"
    assert at < names.index("Log in to GHCR") < names.index("Build and push")


def test_the_signature_check_covers_tags_and_every_other_ref():
    run = _verify_step()["run"]
    assert 'git fetch --no-tags --depth=1 origin "+$REF:$REF"' in run
    assert 'git cat-file -t "$REF")" != tag' in run
    assert "verify-commit HEAD" in run
    assert "APP_VERSION" in run
    assert _verify_step()["env"]["REF"] == "${{ github.ref }}"


def test_the_build_job_is_still_gated_on_the_public_repo():
    wf = yaml.safe_load(WORKFLOW.read_text())
    gate = "github.repository == 'Syracuse3DPrintingOrg/PantryRaider'"
    assert wf["jobs"]["build-and-push"]["if"] == gate
    assert wf["jobs"]["test"]["if"] == gate


def test_every_action_is_pinned_by_commit_sha():
    wf = yaml.safe_load(WORKFLOW.read_text())
    for job in wf["jobs"].values():
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", step["uses"]), step["uses"]


def _verify_tag(tmp_path: Path, obj: str, want: str, signers: Path = SIGNERS):
    fn = re.search(r"^verify_tag_object\(\) \{\n.*?^\}\n", _verify_step()["run"], re.S | re.M)
    assert fn, "verify_tag_object moved; update this test"
    (tmp_path / "tag.txt").write_text(obj)
    script = fn.group(0) + 'verify_tag_object "$1" "$2" "$3"\n'
    return subprocess.run(["bash", "-euo", "pipefail", "-c", script, "verify",
                           str(tmp_path / "tag.txt"), want, str(signers)],
                          capture_output=True, text=True)


needs_ssh_keygen = pytest.mark.skipif(shutil.which("ssh-keygen") is None,
                                      reason="ssh-keygen is not installed")


@needs_ssh_keygen
def test_the_real_v0_20_0_tag_verifies(tmp_path):
    run = _verify_tag(tmp_path, V0_20_0_TAG, "v0.20.0")
    assert run.returncode == 0, run.stdout + run.stderr
    assert FINGERPRINT in run.stdout + run.stderr


@needs_ssh_keygen
def test_a_tag_under_another_name_is_refused(tmp_path):
    run = _verify_tag(tmp_path, V0_20_0_TAG, "v0.20.1")
    assert run.returncode != 0
    assert "named 'v0.20.0', not 'v0.20.1'" in run.stdout


@needs_ssh_keygen
def test_a_tampered_tag_is_refused(tmp_path):
    forged = V0_20_0_TAG.replace("object 180048de", "object 280048de")
    run = _verify_tag(tmp_path, forged, "v0.20.0")
    assert run.returncode != 0
    assert "not signed by a key in" in run.stdout


def test_an_unsigned_tag_is_refused(tmp_path):
    unsigned = V0_20_0_TAG.split("-----BEGIN SSH SIGNATURE-----")[0]
    run = _verify_tag(tmp_path, unsigned, "v0.20.0")
    assert run.returncode != 0
    assert "carries no SSH signature" in run.stdout


@needs_ssh_keygen
def test_a_tag_signed_by_another_key_is_refused(tmp_path):
    other = tmp_path / "other-signers"
    other.write_text('pantryraider-release namespaces="git,pantryraider-release" '
                     "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHvP776IDpW5/AccZEw3/g/z0VslpFf7E2+iRc3lh4u+\n")
    run = _verify_tag(tmp_path, V0_20_0_TAG, "v0.20.0", signers=other)
    assert run.returncode != 0
