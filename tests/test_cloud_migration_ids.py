"""Every Forager migration has its own revision id and the chain has one head.

Two branches merged on 2026-10-15 each added a migration with the same id,
which Alembic reads as two heads (and a live database at that id would skip
one of them)."""
import re
from pathlib import Path

VERSIONS = Path(__file__).resolve().parent.parent / "cloud" / "migrations" / "versions"


def _revs():
    out = {}
    for f in VERSIONS.glob("*.py"):
        t = f.read_text()
        rev = re.search(r"^revision(?::[^=]+)?\s*=\s*['\"]([^'\"]+)", t, re.M).group(1)
        down = re.search(r"^down_revision(?::[^=]+)?\s*=\s*['\"]?([^'\"\n]+)", t, re.M).group(1)
        out.setdefault(rev, []).append((f.name, down.strip("'\" ")))
    return out


def test_revision_ids_are_unique():
    dupes = {r: [n for n, _ in v] for r, v in _revs().items() if len(v) > 1}
    assert not dupes, dupes


def test_one_head():
    revs = _revs()
    downs = {d for v in revs.values() for _, d in v}
    heads = [r for r in revs if r not in downs]
    assert len(heads) == 1, heads
