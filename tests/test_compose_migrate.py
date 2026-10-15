"""foodassistant-compose-migrate: bring a device's old compose file up to date.

The fixtures under tests/data/compose-migrate are compose files this project
shipped before (copied from git history), plus one an owner has edited. The
helper runs against copies of them with a stub docker on PATH, so nothing
here needs Docker; one test also checks the result with the real
`docker compose config` when it is installed.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "image-build" / "foodassistant-compose-migrate"
FIXTURES = Path(__file__).resolve().parent / "data" / "compose-migrate"
SHIPPED_APPLIANCE = ROOT / "scripts" / "image-build" / "docker-compose.appliance.yml"
SHIPPED_REMOTE = ROOT / "scripts" / "image-build" / "docker-compose.remote.yml"

WORKERS_CMD = ('    command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", '
               '"9284", "--workers", "1", "--no-proxy-headers"]')


def _load():
    loader = importlib.machinery.SourceFileLoader("compose_migrate", str(HELPER))
    spec = importlib.util.spec_from_loader("compose_migrate", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["compose_migrate"] = mod
    loader.exec_module(mod)
    return mod


cm = _load()


def _docker_stub(tmp_path, *, config_rc=0, version_rc=0, volumes=None):
    """A docker stand-in: `compose version` and `compose config -q` answer
    with the given codes; `compose config --format json` and `volume inspect`
    answer from VOLUMES ({compose key: mountpoint or None}). Calls are logged."""
    volumes = volumes or {}
    vols = {k: {"name": f"proj_{k}"} for k in volumes}
    mounts = {f"proj_{k}": v for k, v in volumes.items() if v is not None}
    stub = tmp_path / "docker-stub"
    log = tmp_path / "docker.log"
    stub.write_text(f"""#!{sys.executable}
import json, sys
args = sys.argv[1:]
open({str(log)!r}, "a").write(" ".join(args) + "\\n")
if args[:2] == ["compose", "version"]:
    sys.exit({version_rc})
if args[:1] == ["compose"] and "--format" in args:
    print(json.dumps({{"volumes": {json.dumps(vols)}}}))
    sys.exit(0)
if args[:1] == ["compose"] and "-q" in args:
    sys.exit({config_rc})
if args[:2] == ["volume", "inspect"]:
    m = {json.dumps(mounts)}.get(args[-1])
    if m is None:
        sys.exit(1)
    print(m)
    sys.exit(0)
sys.exit(0)
""")
    stub.chmod(0o755)
    return str(stub), log


def _stack(tmp_path, fixture, env="TZ=UTC\n"):
    install = tmp_path / "install"
    install.mkdir()
    src = fixture if isinstance(fixture, Path) else FIXTURES / fixture
    shutil.copy(src, install / "docker-compose.yml")
    if env is not None:
        (install / ".env").write_text(env)
    return install


def _run(install, docker, *extra):
    rc = install.parent / "recreate"
    r = subprocess.run([sys.executable, str(HELPER), "--docker", docker,
                        "--recreate-file", str(rc), *extra,
                        str(install / "docker-compose.yml")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout, rc.read_text() if rc.exists() else ""


# The log limit the helper adds, as it lands under a service in an old
# appliance file (four-space keys). The values match the shipped x-logging.
LOG_LINES = [
    "    logging:",
    "      driver: json-file",
    "      options:",
    '        max-size: "10m"',
    '        max-file: "3"',
]


def _without_log_blocks(text: str):
    """The text with each added log block taken out, and the line index each
    block started at (so a test can see which service it went under)."""
    lines = text.split("\n")
    out, starts, i = [], [], 0
    while i < len(lines):
        if lines[i:i + len(LOG_LINES)] == LOG_LINES:
            starts.append(len(out))
            i += len(LOG_LINES)
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out), starts


def _changed_lines(before: str, after: str):
    a, b = before.split("\n"), after.split("\n")
    assert len(a) == len(b)
    return [(x, y) for x, y in zip(a, b) if x != y]


def _beszel_db(path: Path, *, superusers=(), users=0, legacy=False):
    path.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path / "data.db")
    if legacy:
        con.execute("CREATE TABLE _admins (id TEXT, email TEXT)")
        for e in superusers:
            con.execute("INSERT INTO _admins VALUES ('x', ?)", (e,))
    else:
        con.execute("CREATE TABLE _superusers (id TEXT, email TEXT)")
        for e in superusers:
            con.execute("INSERT INTO _superusers VALUES ('x', ?)", (e,))
    con.execute("CREATE TABLE users (id TEXT, email TEXT)")
    for n in range(users):
        con.execute("INSERT INTO users VALUES (?, 'a@b.c')", (str(n),))
    con.commit()
    con.close()


# --- the real device file (imaged 2026-07-16, updated to 0.21.1) -----------

def test_device_file_gets_the_four_targeted_edits_and_nothing_else(tmp_path):
    install = _stack(tmp_path, "appliance-2026-07-16.yml")
    docker, _ = _docker_stub(tmp_path)
    before = (install / "docker-compose.yml").read_text()
    out, recreate = _run(install, docker)
    after = (install / "docker-compose.yml").read_text()

    stripped, starts = _without_log_blocks(after)
    changes = _changed_lines(before, stripped)
    assert [new.strip() for _old, new in changes] == [
        WORKERS_CMD.strip(),
        '- "${GROCY_BIND:-0.0.0.0}:9383:80"',
        '- "${MEALIE_BIND:-0.0.0.0}:9285:9000"',
        '- "${BESZEL_BIND:-127.0.0.1}:8090:8090"',
    ]
    # A log limit went in at the end of the app, Grocy and Mealie, and
    # nowhere else (Ollama and Beszel are not touched).
    blines = before.split("\n")
    assert [blines[i - 1] for i in starts] == [
        "        required: false",
        "    restart: unless-stopped",
        "    restart: unless-stopped",
    ]
    assert [blines[i] for i in starts] == ["", "", ""]
    assert [blines[i + 1] for i in starts] == ["  grocy:", "  mealie:", "  ollama:"]
    # The command is now exactly what a fresh install ships.
    assert WORKERS_CMD in SHIPPED_APPLIANCE.read_text().split("\n")
    assert WORKERS_CMD in after.split("\n")

    backups = list(install.glob("docker-compose.yml.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == before
    # Beszel never ran here: no data, so no .env change. Grocy and Mealie are
    # recreated for their new log limit; the app is recreated by the update.
    assert (install / ".env").read_text() == "TZ=UTC\n"
    assert recreate == "grocy\nmealie\n"
    assert "capped at three files of 10 MB" in out
    assert "Updated docker-compose.yml" in out
    assert "--workers 1" in out
    assert "answers only on this device" in out
    assert not list(install.glob(".docker-compose.yml.*"))


def test_second_run_changes_nothing(tmp_path):
    install = _stack(tmp_path, "appliance-2026-07-16.yml")
    docker, _ = _docker_stub(tmp_path)
    _run(install, docker)
    once = (install / "docker-compose.yml").read_text()
    out, _ = _run(install, docker)
    assert out == ""
    assert (install / "docker-compose.yml").read_text() == once
    assert len(list(install.glob("docker-compose.yml.bak-*"))) == 1


@pytest.mark.parametrize("fixture", ["appliance-2026-07-04.yml", "remote-2026-09-27.yml"])
def test_old_command_gains_workers_and_no_proxy_headers(tmp_path, fixture):
    install = _stack(tmp_path, fixture)
    docker, _ = _docker_stub(tmp_path)
    _run(install, docker)
    assert WORKERS_CMD in (install / "docker-compose.yml").read_text().split("\n")


def test_remote_file_keeps_its_port_80_publish(tmp_path):
    install = _stack(tmp_path, "remote-2026-10-01.yml")
    docker, _ = _docker_stub(tmp_path)
    before = (install / "docker-compose.yml").read_text()
    _run(install, docker)
    changes = _changed_lines(before, (install / "docker-compose.yml").read_text())
    assert len(changes) == 1 and "--workers" in changes[0][1]
    assert '- "80:9284"' in (install / "docker-compose.yml").read_text()


@pytest.mark.parametrize("shipped", [SHIPPED_APPLIANCE, SHIPPED_REMOTE,
                                     ROOT / "docker-compose.prod.yml"])
def test_current_shipped_files_need_nothing(tmp_path, shipped):
    install = _stack(tmp_path, shipped)
    docker, log = _docker_stub(tmp_path)
    before = (install / "docker-compose.yml").read_text()
    out, _ = _run(install, docker)
    assert out == ""
    assert (install / "docker-compose.yml").read_text() == before
    assert not list(install.glob("docker-compose.yml.bak-*"))


# --- the log limit (FoodAssistant-444ft) -------------------------------------

def _shipped_logging():
    return yaml.safe_load(SHIPPED_APPLIANCE.read_text())["x-logging"]


def test_log_limit_matches_the_shipped_file_and_parses(tmp_path):
    install = _stack(tmp_path, "appliance-2026-07-16.yml")
    docker, _ = _docker_stub(tmp_path)
    _run(install, docker)
    svcs = yaml.safe_load((install / "docker-compose.yml").read_text())["services"]
    shipped = yaml.safe_load(SHIPPED_APPLIANCE.read_text())["services"]
    for name in ("service", "grocy", "mealie"):
        # The same values a fresh install gives that service.
        assert shipped[name]["logging"] == _shipped_logging()
        assert svcs[name]["logging"] == _shipped_logging()
    for name in ("ollama", "beszel", "beszel-agent"):
        assert "logging" not in svcs[name]
    # The rest of each service is still there.
    assert svcs["service"]["depends_on"]["grocy"]["required"] is False
    assert svcs["grocy"]["restart"] == "unless-stopped"


def test_log_limit_on_an_older_file_without_beszel(tmp_path):
    install = _stack(tmp_path, "appliance-2026-07-04.yml")
    docker, _ = _docker_stub(tmp_path)
    before = (install / "docker-compose.yml").read_text()
    out, recreate = _run(install, docker)
    stripped, starts = _without_log_blocks((install / "docker-compose.yml").read_text())
    assert len(starts) == 3
    assert len(_changed_lines(before, stripped)) == 3  # command, Grocy and Mealie ports
    assert recreate == "grocy\nmealie\n"
    assert "The logs of the app, Grocy and Mealie are now capped" in out


def test_log_limit_second_run_changes_nothing(tmp_path):
    install = _stack(tmp_path, "appliance-2026-07-04.yml")
    docker, _ = _docker_stub(tmp_path)
    _run(install, docker)
    once = (install / "docker-compose.yml").read_text()
    out, recreate = _run(install, docker)
    assert out == "" and recreate == ""
    assert (install / "docker-compose.yml").read_text() == once
    assert once.count("    logging:\n") == 3
    assert len(list(install.glob("docker-compose.yml.bak-*"))) == 1


def test_a_service_with_its_own_logging_is_left_alone(tmp_path):
    # The owner already set a logging driver on the app; Grocy and Mealie
    # still get the limit, and the app's own setting stays as written.
    text = (FIXTURES / "appliance-2026-07-16.yml").read_text().replace(
        "    restart: unless-stopped\n    depends_on:\n",
        "    restart: unless-stopped\n    logging:\n      driver: journald\n    depends_on:\n", 1)
    assert "driver: journald" in text
    src = tmp_path / "src.yml"
    src.write_text(text)
    install = _stack(tmp_path, src)
    docker, _ = _docker_stub(tmp_path)
    out, _ = _run(install, docker)
    cfg = yaml.safe_load((install / "docker-compose.yml").read_text())
    assert cfg["services"]["service"]["logging"] == {"driver": "journald"}
    assert cfg["services"]["grocy"]["logging"] == _shipped_logging()
    assert "The logs of Grocy and Mealie are now capped" in out


def test_logging_alone_is_enough_to_update_the_file(tmp_path):
    # A file already past the other edits but still without log limits: the
    # log limit goes in and every other byte stays.
    first = tmp_path / "first"
    first.mkdir()
    install = _stack(first, "appliance-2026-07-16.yml")
    docker, _ = _docker_stub(tmp_path)
    _run(install, docker)
    migrated, _ = _without_log_blocks((install / "docker-compose.yml").read_text())
    src = tmp_path / "src.yml"
    src.write_text(migrated)
    again = tmp_path / "again"
    again.mkdir()
    install2 = _stack(again, src)
    out, recreate = _run(install2, docker)
    stripped, starts = _without_log_blocks((install2 / "docker-compose.yml").read_text())
    assert stripped == migrated and len(starts) == 3
    assert out.splitlines()[0].startswith("Updated docker-compose.yml")
    assert recreate == "grocy\nmealie\n"


def test_log_limit_dry_run_says_so_and_writes_nothing(tmp_path):
    install = _stack(tmp_path, "appliance-2026-07-16.yml")
    docker, _ = _docker_stub(tmp_path)
    before = (install / "docker-compose.yml").read_text()
    out, recreate = _run(install, docker, "--dry-run")
    assert out.startswith("Would update docker-compose.yml")
    assert "capped at three files of 10 MB" in out
    assert (install / "docker-compose.yml").read_text() == before
    assert not list(install.glob("docker-compose.yml.bak-*"))
    assert recreate == ""


def test_log_limit_follows_the_file_indentation(tmp_path):
    # Services indented by four and their keys by two more.
    lines = (FIXTURES / "appliance-2026-07-04.yml").read_text().split("\n")
    start = lines.index("services:")
    out_lines = lines[:start + 1]
    for line in lines[start + 1:]:
        if line.startswith("  ") and line.strip():
            line = "  " + line
        out_lines.append(line)
    src = tmp_path / "src.yml"
    src.write_text("\n".join(out_lines))
    yaml.safe_load(src.read_text())
    install = _stack(tmp_path, src)
    docker, _ = _docker_stub(tmp_path)
    _run(install, docker)
    after = (install / "docker-compose.yml").read_text()
    assert '          max-size: "10m"' in after.split("\n")
    assert yaml.safe_load(after)["services"]["service"]["logging"] == _shipped_logging()


# --- an owner's edits survive ------------------------------------------------

def test_user_edits_are_preserved(tmp_path):
    install = _stack(tmp_path, "appliance-user-edited.yml",
                     env="TZ=UTC\nCOMPOSE_PROFILES=with-beszel\n")
    _beszel_db(install / "beszel", superusers=["owner@example.com"], users=1)
    docker, _ = _docker_stub(tmp_path)
    before = (install / "docker-compose.yml").read_text()
    out, recreate = _run(install, docker)
    after = (install / "docker-compose.yml").read_text()

    stripped, starts = _without_log_blocks(after)
    assert len(starts) == 3
    changes = dict(_changed_lines(before, stripped))
    assert changes == {
        '    command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9284", '
        '"--no-proxy-headers"]': WORKERS_CMD,
        '      - "8383:80"   # moved: something else uses 9383 here':
            '      - "${GROCY_BIND:-0.0.0.0}:8383:80"   # moved: something else uses 9383 here',
        '      - "9285:9000"': '      - "${MEALIE_BIND:-0.0.0.0}:9285:9000"',
        "      - '8091:8090'": '      - "${BESZEL_BIND:-127.0.0.1}:8091:8090"',
    }
    # Everything else stays: the extra env line, the Ollama ports, the extra
    # service with its own 8080 publish.
    for keep in ("      - LOG_LEVEL=debug", '      - "11434:11434"', '      - "11435:11434"',
                 "  zigbee2mqtt:", '      - "8080:8080"'):
        assert keep in after.split("\n")
    # The hub has an admin, so it stays on the network through .env.
    env = (install / ".env").read_text()
    assert env.startswith("TZ=UTC\nCOMPOSE_PROFILES=with-beszel\n")
    assert env.rstrip().endswith("BESZEL_BIND=0.0.0.0")
    assert recreate == "grocy\nmealie\n"
    assert "stays open to your network" in out


def test_an_explicit_bind_address_is_left_alone(tmp_path):
    text = (FIXTURES / "appliance-2026-07-16.yml").read_text().replace(
        '      - "9383:80"', '      - "192.168.1.5:9383:80"')
    src = tmp_path / "src.yml"
    src.write_text(text)
    install = _stack(tmp_path, src)
    docker, _ = _docker_stub(tmp_path)
    _run(install, docker)
    assert '      - "192.168.1.5:9383:80"' in (install / "docker-compose.yml").read_text()


def test_a_custom_worker_count_is_not_overwritten(tmp_path):
    text = (FIXTURES / "appliance-2026-07-16.yml").read_text().replace(
        '"9284", "--no-proxy-headers"]', '"9284", "--workers", "2", "--no-proxy-headers"]')
    src = tmp_path / "src.yml"
    src.write_text(text)
    install = _stack(tmp_path, src)
    docker, _ = _docker_stub(tmp_path)
    _run(install, docker)
    assert '"--workers", "2"' in (install / "docker-compose.yml").read_text()


# --- Beszel on a device that already uses it ---------------------------------

def test_beszel_with_an_admin_stays_on_the_network(tmp_path):
    install = _stack(tmp_path, "appliance-2026-07-16.yml")
    _beszel_db(install / "beszel", superusers=["owner@example.com"])
    docker, _ = _docker_stub(tmp_path)
    out, recreate = _run(install, docker)
    assert "BESZEL_BIND=0.0.0.0\n" in (install / ".env").read_text()
    assert '"${BESZEL_BIND:-127.0.0.1}:8090:8090"' in (install / "docker-compose.yml").read_text()
    assert "beszel" not in recreate.split()
    assert "already has an admin account" in out


def test_beszel_with_only_a_user_row_counts_as_set_up(tmp_path):
    install = _stack(tmp_path, "appliance-2026-07-16.yml")
    _beszel_db(install / "beszel", users=1)
    docker, _ = _docker_stub(tmp_path)
    _run(install, docker)
    assert "BESZEL_BIND=0.0.0.0" in (install / ".env").read_text()


def test_beszel_on_older_pocketbase_admins_table(tmp_path):
    install = _stack(tmp_path, "appliance-2026-07-16.yml")
    _beszel_db(install / "beszel", superusers=["owner@example.com"], legacy=True)
    docker, _ = _docker_stub(tmp_path)
    _run(install, docker)
    assert "BESZEL_BIND=0.0.0.0" in (install / ".env").read_text()


def test_beszel_without_an_admin_moves_to_this_device_and_restarts(tmp_path):
    # Only PocketBase's own installer placeholder: nobody has claimed the hub.
    install = _stack(tmp_path, "appliance-2026-07-16.yml")
    _beszel_db(install / "beszel", superusers=[cm.PB_INSTALLER_EMAIL])
    docker, _ = _docker_stub(tmp_path)
    out, recreate = _run(install, docker)
    assert "BESZEL_BIND" not in (install / ".env").read_text()
    assert '"${BESZEL_BIND:-127.0.0.1}:8090:8090"' in (install / "docker-compose.yml").read_text()
    assert recreate == "beszel\ngrocy\nmealie\n"
    assert "no admin account yet" in out


def test_unreadable_beszel_data_keeps_it_on_the_network(tmp_path):
    install = _stack(tmp_path, "appliance-2026-07-16.yml")
    (install / "beszel").mkdir()
    (install / "beszel" / "data.db").write_bytes(b"not a database")
    docker, _ = _docker_stub(tmp_path)
    _run(install, docker)
    assert "BESZEL_BIND=0.0.0.0" in (install / ".env").read_text()


def test_an_existing_beszel_bind_setting_is_respected(tmp_path):
    install = _stack(tmp_path, "appliance-2026-07-16.yml", env="BESZEL_BIND=127.0.0.1\n")
    _beszel_db(install / "beszel", superusers=["owner@example.com"])
    docker, _ = _docker_stub(tmp_path)
    _run(install, docker)
    assert (install / ".env").read_text() == "BESZEL_BIND=127.0.0.1\n"
    assert '"${BESZEL_BIND:-127.0.0.1}:8090:8090"' in (install / "docker-compose.yml").read_text()


def test_beszel_in_a_named_volume_is_found_through_docker(tmp_path):
    # docker-compose.prod.yml (install.sh servers) keeps the hub in a named
    # volume; the helper asks docker where it lives.
    install = _stack(tmp_path, "prod-2026-10-14.yml")
    mount = tmp_path / "volumes" / "beszel-data"
    _beszel_db(mount, superusers=["owner@example.com"])
    docker, _ = _docker_stub(tmp_path, volumes={"beszel-data": str(mount)})
    _run(install, docker)
    assert "BESZEL_BIND=0.0.0.0" in (install / ".env").read_text()
    text = (install / "docker-compose.yml").read_text()
    assert '"${BESZEL_BIND:-127.0.0.1}:8090:8090"' in text
    assert '"${GROCY_BIND:-0.0.0.0}:9383:80"' in text


def test_named_volume_that_does_not_exist_yet(tmp_path):
    install = _stack(tmp_path, "prod-2026-10-14.yml")
    docker, _ = _docker_stub(tmp_path, volumes={"beszel-data": None})
    _run(install, docker)
    assert "BESZEL_BIND" not in (install / ".env").read_text()
    assert '"${BESZEL_BIND:-127.0.0.1}:8090:8090"' in (install / "docker-compose.yml").read_text()


def test_beszel_left_alone_when_its_data_cannot_be_located(tmp_path):
    # The stub knows no volumes, so the named volume cannot be resolved.
    install = _stack(tmp_path, "prod-2026-10-14.yml")
    docker, _ = _docker_stub(tmp_path)
    out, _ = _run(install, docker)
    text = (install / "docker-compose.yml").read_text()
    assert '      - "8090:8090"' in text
    assert '"${GROCY_BIND:-0.0.0.0}:9383:80"' in text
    assert "Left the Beszel port as it is" in out


# --- safety ------------------------------------------------------------------

def test_rejected_result_leaves_the_old_file(tmp_path):
    install = _stack(tmp_path, "appliance-2026-07-16.yml")
    _beszel_db(install / "beszel", superusers=["owner@example.com"])
    docker, _ = _docker_stub(tmp_path, config_rc=1)
    before = (install / "docker-compose.yml").read_text()
    out, recreate = _run(install, docker)
    assert (install / "docker-compose.yml").read_text() == before
    assert (install / ".env").read_text() == "TZ=UTC\n"
    assert not list(install.glob("docker-compose.yml.bak-*"))
    assert not list(install.glob(".docker-compose.yml.*"))
    assert recreate == ""
    assert out.startswith("WARN: kept docker-compose.yml as it was")


def test_missing_docker_compose_leaves_the_old_file(tmp_path):
    install = _stack(tmp_path, "appliance-2026-07-16.yml")
    docker, _ = _docker_stub(tmp_path, version_rc=1)
    before = (install / "docker-compose.yml").read_text()
    out, _ = _run(install, docker)
    assert (install / "docker-compose.yml").read_text() == before
    assert "docker compose is not available" in out


def test_dry_run_writes_nothing(tmp_path):
    install = _stack(tmp_path, "appliance-2026-07-16.yml")
    docker, log = _docker_stub(tmp_path)
    before = (install / "docker-compose.yml").read_text()
    out, _ = _run(install, docker, "--dry-run")
    assert out.startswith("Would update docker-compose.yml")
    assert (install / "docker-compose.yml").read_text() == before
    assert not list(install.glob("docker-compose.yml.bak-*"))


@pytest.mark.skipif(
    not shutil.which("docker")
    or subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0,
    reason="docker compose is not installed")
@pytest.mark.parametrize("fixture", ["appliance-2026-07-16.yml", "appliance-user-edited.yml",
                                     "remote-2026-09-27.yml"])
def test_real_docker_compose_accepts_the_result(tmp_path, fixture):
    install = _stack(tmp_path, fixture)
    out, _ = _run(install, "docker")
    assert "Updated docker-compose.yml" in out, out
    r = subprocess.run(["docker", "compose", "-f", "docker-compose.yml", "config", "--format", "json"],
                       cwd=install, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    cfg = json.loads(r.stdout)
    assert "--workers" in cfg["services"]["service"]["command"]
    assert cfg["services"]["service"]["logging"]["options"] == {"max-size": "10m", "max-file": "3"}
