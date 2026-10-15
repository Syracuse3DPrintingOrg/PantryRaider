"""The Forager host firewall keeps WireGuard peers to answers only."""
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "cloud" / "vps" / "forager-wg0-firewall"


def _run(tmp_path, *args, existing=()):
    """Run the script against a fake iptables that records -I calls and says
    the rules in ``existing`` are already there."""
    log = tmp_path / "calls"
    have = tmp_path / "have"
    have.write_text("\n".join(existing))
    fake = tmp_path / "iptables"
    fake.write_text(
        '#!/bin/sh\n'
        f'echo "$*" >> {log}\n'
        'case "$1" in\n'
        f'  -C) shift; grep -qxF -- "$*" {have} ;;\n'
        '  -L) exit 0 ;;\n'
        '  *) exit 0 ;;\n'
        'esac\n')
    fake.chmod(0o755)
    env = {"PATH": f"{tmp_path}:/usr/bin:/bin"}
    out = subprocess.run(["sh", str(SCRIPT), *args], env=env, capture_output=True, text=True)
    calls = log.read_text().splitlines() if log.exists() else []
    return out, [c for c in calls if c.startswith("-I")]


def test_adds_the_answer_rule_above_the_drop(tmp_path):
    out, inserts = _run(tmp_path)
    assert out.returncode == 0
    # Inserted at position 1 in this order, so ACCEPT ends up above DROP.
    assert inserts.index("-I INPUT 1 -i wg0 -j DROP") < inserts.index(
        "-I INPUT 1 -i wg0 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT")
    assert "-I FORWARD 1 -i wg0 -j DROP" in inserts
    assert "-I DOCKER-USER 1 -i wg0 -j DROP" in inserts


def test_adds_nothing_twice(tmp_path):
    existing = ["INPUT -i wg0 -j DROP",
                "INPUT -i wg0 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
                "FORWARD -i wg0 -j DROP", "DOCKER-USER -i wg0 -j DROP"]
    out, inserts = _run(tmp_path, existing=existing)
    assert out.returncode == 0 and inserts == []
    out, _ = _run(tmp_path, "check", existing=existing)
    assert out.returncode == 0 and "in place" in out.stdout
