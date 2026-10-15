"""The publishing workflows: what moves :latest, what is never pushed twice,
and what gets a provenance attestation.

:latest is the tag Compose and Unraid installs follow, so it moves only when
the highest vX.Y.Z release tag is built, never for a push to main and never
backwards for a patch to an older line. A version already in GHCR is not
built and pushed again, because scripts/sign-release.sh records its digest in
the signed release.json and devices pin that digest. The SD image, its
checksum, install.sh and the Cub firmware all carry attestations that the
signing step checks, and every job that publishes them waits on the one shared
release signature check, so an unsigned tag never gets an image or an asset.

The workflow files are parsed with PyYAML, and the decide script is pulled out
of its step and run against stand-in git and docker binaries on PATH, so
nothing here needs the network, Docker or this repository's tags.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
PUBLISH = WORKFLOWS / "publish-image.yml"
RELEASE_IMAGE = WORKFLOWS / "release-image.yml"
CUB = WORKFLOWS / "build-cub-firmware.yml"
VERIFY = WORKFLOWS / "verify-release-signature.yml"
VERIFY_USES = "./.github/workflows/verify-release-signature.yml"
PUBLIC_GATE = "github.repository == 'Syracuse3DPrintingOrg/PantryRaider'"
ATTEST = "actions/attest-build-provenance@"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _triggers(wf: dict) -> dict:
    # PyYAML reads the bare key `on` as the boolean True.
    return wf.get("on", wf.get(True)) or {}


def _steps(path: Path, job: str) -> list[dict]:
    return _load(path)["jobs"][job]["steps"]


def _step(path: Path, job: str, name: str) -> dict:
    return next(s for s in _steps(path, job) if s.get("name") == name)


# --- publish-image.yml ---

def test_publish_has_no_release_trigger():
    """Publishing a GitHub Release used to build and push the version tag a
    second time, after its digest had been signed."""
    on = _triggers(_load(PUBLISH))
    assert "release" not in on
    assert on["push"]["tags"] == ["v*"]
    assert on["push"]["branches"] == ["main"]


def test_the_decide_step_sits_between_the_login_and_the_build():
    names = [s.get("name") for s in _steps(PUBLISH, "build-and-push")]
    decide = names.index("Decide the tags")
    assert names.index("Log in to GHCR") < decide
    assert decide < names.index("Derive image metadata") < names.index("Build and push")
    assert _step(PUBLISH, "build-and-push", "Decide the tags")["id"] == "decide"


def test_latest_follows_only_the_decide_step():
    meta = _step(PUBLISH, "build-and-push", "Derive image metadata")["with"]
    rules = [line.strip() for line in meta["tags"].splitlines() if line.strip()]
    latest = [r for r in rules if "value=latest" in r]
    assert latest == ["type=raw,value=latest,enable=${{ steps.decide.outputs.latest == 'true' }}"]
    assert not any("is_default_branch" in r for r in rules)
    assert "type=ref,event=branch" in rules
    assert "type=semver,pattern={{version}}" in rules
    # The action would otherwise add :latest to every semver tag by itself.
    assert "latest=false" in meta["flavor"].split()


def test_an_existing_version_is_not_pushed_but_is_still_attested():
    build = _step(PUBLISH, "build-and-push", "Build and push")
    assert build["if"] == "steps.decide.outputs.skip != 'true'"
    attest = _step(PUBLISH, "build-and-push", "Attest build provenance")
    assert "if" not in attest
    assert attest["with"]["subject-digest"] == (
        "${{ steps.build.outputs.digest || steps.decide.outputs.digest }}")
    assert attest["uses"].startswith(ATTEST)


def test_the_publish_jobs_stay_gated_on_the_public_repo():
    jobs = _load(PUBLISH)["jobs"]
    assert jobs["build-and-push"]["if"] == PUBLIC_GATE
    assert jobs["test"]["if"] == PUBLIC_GATE


# --- The decide script, run with stand-in git and docker ---

STUB_GIT = """#!PYTHON
import os, sys
args = sys.argv[1:]
if args[:1] == ["ls-remote"]:
    for i, tag in enumerate(os.environ["STUB_TAGS"].split()):
        print(f"{i:040d}\\trefs/tags/{tag}")
    sys.exit(0)
sys.exit(3)
"""

STUB_DOCKER = """#!PYTHON
import json, os, sys
digest = os.environ.get("STUB_DIGEST", "")
if sys.argv[1:4] != ["buildx", "imagetools", "inspect"]:
    sys.exit(3)
if os.environ.get("STUB_DOCKER_ERR"):
    print("ERROR: " + os.environ["STUB_DOCKER_ERR"], file=sys.stderr)
    sys.exit(1)
if not digest:
    print(f"ERROR: {sys.argv[4]}: not found", file=sys.stderr)
    sys.exit(1)
print(json.dumps({"schemaVersion": 2, "digest": digest,
                  "manifests": [{"digest": "sha256:" + "ef" * 32}]}))
"""

TAGS = "v0.19.4 v0.20.0 v0.20.2 v0.21.0 v0.21.0-rc1 v0.9.9 vnext"


def _run_decide(tmp_path: Path, ref: str, digest: str = "", **extra: str):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name, body in (("git", STUB_GIT), ("docker", STUB_DOCKER)):
        exe = bindir / name
        exe.write_text(body.replace("PYTHON", sys.executable, 1))
        exe.chmod(0o755)
    out = tmp_path / "github_output"
    out.write_text("")
    env = {k: v for k, v in os.environ.items() if not k.startswith("STUB_")}
    env.update(PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
               REF=ref, IMAGE="ghcr.io/syracuse3dprintingorg/pantryraider",
               GITHUB_OUTPUT=str(out), STUB_TAGS=TAGS, STUB_DIGEST=digest, **extra)
    script = _step(PUBLISH, "build-and-push", "Decide the tags")["run"]
    run = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    return run, out


def _decide(tmp_path: Path, ref: str, digest: str = "") -> dict[str, str]:
    run, out = _run_decide(tmp_path, ref, digest)
    assert run.returncode == 0, run.stdout + run.stderr
    return dict(line.split("=", 1) for line in out.read_text().splitlines())


needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")


@needs_bash
def test_the_highest_tag_moves_latest(tmp_path):
    assert _decide(tmp_path, "refs/tags/v0.21.0") == {
        "latest": "true", "skip": "false", "digest": ""}


@needs_bash
def test_a_patch_to_an_older_line_does_not_move_latest(tmp_path):
    out = _decide(tmp_path, "refs/tags/v0.20.2")
    assert out["latest"] == "false"
    assert out["skip"] == "false"


@needs_bash
@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/tags/v0.21.0-rc1"])
def test_main_and_odd_tags_never_move_latest(tmp_path, ref):
    assert _decide(tmp_path, ref) == {"latest": "false", "skip": "false", "digest": ""}


@needs_bash
def test_a_version_already_in_the_registry_is_skipped_with_its_digest(tmp_path):
    digest = "sha256:" + "ab" * 32
    assert _decide(tmp_path, "refs/tags/v0.21.0", digest) == {
        "latest": "true", "skip": "true", "digest": digest}


@needs_bash
@pytest.mark.parametrize("error", [
    "failed to do request: Head \"https://ghcr.example/v2/\": dial tcp: i/o timeout",
    "unexpected status from HEAD request: 401 Unauthorized",
])
def test_a_registry_error_stops_the_run_instead_of_rebuilding(tmp_path, error):
    """Only a registry that says the version is missing may lead to a build:
    a timeout must not re-push a version whose digest is already signed."""
    run, out = _run_decide(tmp_path, "refs/tags/v0.21.0", STUB_DOCKER_ERR=error)
    assert run.returncode != 0
    assert "Could not tell whether" in run.stdout
    assert "skip=" not in out.read_text()


# --- release-image.yml ---

PUBLIC_TAG_GATE = "github.ref_type == 'tag' && " + PUBLIC_GATE


def test_the_sd_image_workflow_can_attest():
    perms = _load(RELEASE_IMAGE)["permissions"]
    assert perms["id-token"] == "write"
    assert perms["attestations"] == "write"
    assert perms["artifact-metadata"] == "write"
    assert perms["contents"] == "write"


def test_the_sd_image_its_checksum_and_install_sh_are_attested_on_public_tags():
    steps = _steps(RELEASE_IMAGE, "build-image")
    installer = next(s for s in steps if s.get("id") == "installer")
    assert installer["if"] == PUBLIC_TAG_GATE
    assert "cp -f install.sh upload/install.sh" in installer["run"]
    [attest] = [s for s in steps if s.get("uses", "").startswith(ATTEST)]
    assert attest["if"] == PUBLIC_TAG_GATE
    subjects = attest["with"]["subject-path"].splitlines()
    assert subjects == ["${{ steps.pack.outputs.artifact }}",
                        "${{ steps.pack.outputs.checksum }}",
                        "${{ steps.installer.outputs.path }}"]
    names = [s.get("name") for s in steps]
    assert names.index(installer["name"]) < names.index(attest["name"])


def test_install_sh_is_a_release_asset():
    release = _step(RELEASE_IMAGE, "build-image",
                    "Create the release and attach the image (tag push)")
    assert "${{ steps.installer.outputs.path }}" in release["with"]["files"].splitlines()


# --- build-cub-firmware.yml ---

def test_the_cub_firmware_is_attested_by_the_public_publish_job():
    job = _load(CUB)["jobs"]["publish"]
    assert PUBLIC_GATE in job["if"]
    for perm in ("id-token", "attestations", "artifact-metadata", "contents"):
        assert job["permissions"][perm] == "write"
    names = [s.get("name") for s in job["steps"]]
    [attest] = [s for s in job["steps"] if s.get("uses", "").startswith(ATTEST)]
    assert attest["with"]["subject-path"] == "firmware/*.bin"
    assert PUBLIC_GATE in attest["if"]
    assert names.index("Download built firmware") < names.index(attest["name"])


# --- The shared release signature check ---

def test_the_signature_check_is_a_reusable_workflow_only():
    wf = _load(VERIFY)
    on = _triggers(wf)
    assert list(on) == ["workflow_call"]
    inputs = on["workflow_call"]["inputs"]
    assert inputs["ref"]["required"] is True
    assert inputs["sha"]["default"] == ""
    assert inputs["enforce"]["type"] == "boolean"
    assert inputs["enforce"]["default"] is True
    assert list(wf["jobs"]) == ["verify"]


def test_every_check_step_is_skipped_only_when_nothing_is_published():
    """enforce false is the one way past the check, and it is what a run that
    publishes nothing passes; every real step runs whenever it is true."""
    steps = _steps(VERIFY, "verify")
    for step in steps:
        if step.get("name") == "Nothing to verify":
            assert step["if"] == "${{ !inputs.enforce }}"
            assert "uses" not in step
        else:
            assert step["if"] == "inputs.enforce", step.get("name")
    assert [s.get("name") for s in steps][:2] == ["Checkout", "Verify the release signature"]


def _pinned_key_lines(text: str) -> list[str]:
    return [line for line in text.splitlines()
            if re.search(r"\bssh-(ed25519|rsa)\s+AAAA", line)]


def test_the_release_keys_are_pinned_in_one_place_in_ci():
    """Trusting another key means editing one workflow, not three that can
    drift apart."""
    holders = {}
    for path in sorted((ROOT / ".github").rglob("*")):
        if path.is_file():
            text = path.read_text(errors="replace")
            if "<<'PINNED'" in text or _pinned_key_lines(text):
                holders[path.relative_to(ROOT).as_posix()] = text
    assert list(holders) == [".github/workflows/verify-release-signature.yml"]
    run = _step(VERIFY, "verify", "Verify the release signature")["run"]
    assert run.count("<<'PINNED'") == 1
    assert _pinned_key_lines(run)


def _caller(path: Path) -> dict:
    job = _load(path)["jobs"]["verify-signature"]
    assert job["uses"] == VERIFY_USES
    assert job["permissions"] == {"contents": "read"}
    assert "steps" not in job
    return job


def _needs(job: dict) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else needs


def test_the_image_build_waits_on_the_signature_check_for_every_ref():
    job = _caller(PUBLISH)
    assert job["if"] == PUBLIC_GATE
    assert job["with"] == {"ref": "${{ github.ref }}", "sha": "${{ github.sha }}"}
    assert "verify-signature" in _needs(_load(PUBLISH)["jobs"]["build-and-push"])


def test_the_sd_image_waits_on_the_signature_check_on_a_tag():
    job = _caller(RELEASE_IMAGE)
    assert job["with"]["enforce"] == "${{ github.ref_type == 'tag' }}"
    assert job["with"]["sha"] == "${{ github.sha }}"
    assert "verify-signature" in _needs(_load(RELEASE_IMAGE)["jobs"]["build-image"])
    # Only one job, so nothing else could attach an asset without waiting.
    assert set(_load(RELEASE_IMAGE)["jobs"]) == {"verify-signature", "build-image"}


def test_the_cub_firmware_is_published_only_after_the_signature_check():
    job = _caller(CUB)
    enforce = job["with"]["enforce"]
    assert "github.ref_type == 'tag'" in enforce
    assert "inputs.tag != ''" in enforce
    assert "format('refs/tags/{0}', inputs.tag)" in job["with"]["ref"]
    publish = _load(CUB)["jobs"]["publish"]
    assert "verify-signature" in _needs(publish)
    # always() keeps a failed board from blocking the others, so the result
    # has to be checked by hand: a skipped or failed check must stop it.
    assert "needs.verify-signature.result == 'success'" in publish["if"]
    # The compile check does not wait on it.
    assert "verify-signature" not in _needs(_load(CUB)["jobs"]["build"])


PUBLISHING_ACTIONS = ("softprops/action-gh-release@", "docker/build-push-action@", ATTEST)
PUBLISHING_COMMANDS = ("gh release", "uploads.github.com", "/releases/")
# The label printer bridge is a dev-only, hand-run build with its own release
# tag, never a vX.Y.Z app release, and it never reaches the public repo.
NOT_APP_RELEASES = {"supvan-bridge.yml"}


def _publishes(job: dict) -> bool:
    for step in job.get("steps", []):
        if step.get("uses", "").startswith(PUBLISHING_ACTIONS):
            return True
        if any(c in step.get("run", "") for c in PUBLISHING_COMMANDS):
            return True
    return False


def test_every_job_that_publishes_waits_on_the_signature_check():
    """Any job that pushes an image, attests, or touches a release has to list
    the check under needs, including one added later."""
    found = 0
    for path in sorted(WORKFLOWS.glob("*.yml")):
        if path.name in NOT_APP_RELEASES:
            continue
        for name, job in _load(path)["jobs"].items():
            if _publishes(job):
                found += 1
                assert "verify-signature" in _needs(job), (path.name, name)
                assert _load(path)["jobs"]["verify-signature"]["uses"] == VERIFY_USES
    assert found == 3


# --- Every workflow ---

def test_every_action_in_every_workflow_is_pinned_by_commit_sha():
    found = 0
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for name, job in _load(path)["jobs"].items():
            if "uses" in job:
                # A reusable workflow from this same commit needs no pin.
                if job["uses"].startswith("./.github/workflows/"):
                    assert (ROOT / job["uses"]).is_file(), (path.name, name)
                    continue
                assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", job["uses"]), (path.name, name)
            for step in job.get("steps", []):
                if "uses" in step:
                    found += 1
                    assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", step["uses"]), (
                        path.name, step["uses"])
    assert found
