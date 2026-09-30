"""Tests for scripts/install.sh, the one-command Docker installer.

The compose file it downloads bind-mounts folders under ./docker/ (the Grocy
start-up repair script). Docker creates a missing bind-mount source as an
empty root-owned folder, so anything the installer forgets to fetch silently
never runs. These tests hold the installer to the compose file: every file in
a mounted ./docker/ folder must be fetched and made executable.

The run-through tests put stub `curl`, `docker`, and `sleep` commands on PATH,
so they need no network and no Docker, and serve downloads from this checkout.

Run: python -m pytest tests/test_server_install_script.py -q
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "install.sh"
COMPOSE = REPO / "docker-compose.prod.yml"
RAW_PREFIX = "https://raw.githubusercontent.com/Syracuse3DPrintingOrg/PantryRaider/main/"


def _docker_bind_dirs() -> list[str]:
    """Repo-relative folders under docker/ that the prod compose bind-mounts."""
    compose = yaml.safe_load(COMPOSE.read_text())
    found = set()
    for svc in (compose.get("services") or {}).values():
        for vol in svc.get("volumes") or []:
            src = vol.split(":", 1)[0] if isinstance(vol, str) else vol.get("source", "")
            if src.startswith("./docker/"):
                found.add(src[2:].rstrip("/"))
    return sorted(found)


def test_script_parses():
    proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_compose_still_mounts_grocy_init():
    # Guards the test below against passing vacuously if the mount moves.
    assert "docker/grocy-init" in _docker_bind_dirs()


def test_every_mounted_docker_file_is_fetched_and_made_executable():
    text = SCRIPT.read_text()
    for folder in _docker_bind_dirs():
        files = sorted(p for p in (REPO / folder).iterdir() if p.is_file())
        assert files, folder
        for f in files:
            rel = f"{folder}/{f.name}"
            assert re.search(r'fetch\s+"\$REPO_RAW/' + re.escape(rel) + r'"\s+"?' + re.escape(rel), text), (
                f"scripts/install.sh does not fetch {rel}"
            )
            assert re.search(r"chmod\s+755\s+\"?" + re.escape(rel), text), (
                f"scripts/install.sh does not chmod 755 {rel}"
            )


def test_mentions_docker_24_requirement():
    assert "Docker 24" in SCRIPT.read_text()


# ---------------------------------------------------------------------------
# Run-through with stubs
# ---------------------------------------------------------------------------

def _stub_bin(tmp_path: Path, watchtower_restarting: str) -> Path:
    """watchtower_restarting is a space-separated list of answers the stub
    `docker inspect` gives in turn; the last one repeats."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "docker.log"
    count = tmp_path / "inspect.count"
    count.write_text("0")
    stubs = {
        # Serve raw.githubusercontent.com URLs from this checkout.
        "curl": f"""#!/usr/bin/env bash
url=""; out=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
rel="${{url#{RAW_PREFIX}}}"
[ -f "{REPO}/$rel" ] || exit 22
cp -f "{REPO}/$rel" "$out"
chmod 644 "$out"
""",
        "docker": f"""#!/usr/bin/env bash
echo "$*" >> "{log}"
if [ "$1" = inspect ]; then
  n=$(cat "{count}"); echo $((n + 1)) > "{count}"
  set -- {watchtower_restarting}
  shift $(( n < $# ? n : $# - 1 ))
  echo "$1"
fi
exit 0
""",
        "sleep": "#!/usr/bin/env bash\nexit 0\n",
    }
    for name, body in stubs.items():
        p = bin_dir / name
        p.write_text(body)
        p.chmod(0o755)
    return bin_dir


def _run(tmp_path: Path, watchtower_restarting: str = "false") -> subprocess.CompletedProcess:
    bin_dir = _stub_bin(tmp_path, watchtower_restarting)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "INSTALL_DIR": "stack"}
    return subprocess.run(
        ["bash", str(SCRIPT)], cwd=tmp_path, env=env, capture_output=True, text=True
    )


def test_run_lays_down_executable_grocy_repair_script(tmp_path):
    proc = _run(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    target = tmp_path / "stack" / "docker" / "grocy-init" / "10-repair-auth-class.sh"
    assert target.is_file(), proc.stdout + proc.stderr
    assert target.read_bytes() == (REPO / "docker/grocy-init/10-repair-auth-class.sh").read_bytes()
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert "up -d" in (tmp_path / "docker.log").read_text()


def test_rerun_refreshes_repair_script(tmp_path):
    assert _run(tmp_path).returncode == 0
    target = tmp_path / "stack" / "docker" / "grocy-init" / "10-repair-auth-class.sh"
    target.write_text("stale\n")
    target.chmod(0o644)
    env_file = tmp_path / "stack" / ".env"
    env_file.write_text("KEEP=me\n")
    proc = _run(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert target.read_bytes() == (REPO / "docker/grocy-init/10-repair-auth-class.sh").read_bytes()
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    # The user's .env is never overwritten on a re-run.
    assert env_file.read_text() == "KEEP=me\n"


def test_warns_when_updater_keeps_restarting(tmp_path):
    proc = _run(tmp_path, watchtower_restarting="true")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout + proc.stderr
    assert "Docker 24" in out
    assert "updater" in out.lower()
    assert "inspect" in (tmp_path / "docker.log").read_text()


def test_no_updater_warning_when_healthy(tmp_path):
    proc = _run(tmp_path, watchtower_restarting="false")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "not starting" not in (proc.stdout + proc.stderr)


def test_warns_when_updater_is_caught_between_restarts(tmp_path):
    # A looping container is briefly running between restarts, so a single
    # look can read false; the installer must look again.
    proc = _run(tmp_path, watchtower_restarting="false true")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "not starting" in proc.stdout + proc.stderr


def test_rerun_with_root_owned_grocy_init_still_starts(tmp_path):
    # Older installs got docker/grocy-init from Docker itself: empty and owned
    # by root. A re-run without sudo must still start the stack and say how to
    # fix the folder, not stop on a download error.
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere, so the case cannot be simulated")
    folder = tmp_path / "stack" / "docker" / "grocy-init"
    folder.mkdir(parents=True)
    folder.chmod(0o555)
    env_file = tmp_path / "stack" / ".env"
    env_file.write_text("KEEP=me\n")
    try:
        proc = _run(tmp_path)
    finally:
        folder.chmod(0o755)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "up -d" in (tmp_path / "docker.log").read_text()
    assert "sudo chown" in proc.stderr
    assert not (folder / "10-repair-auth-class.sh").exists()
    assert env_file.read_text() == "KEEP=me\n"
