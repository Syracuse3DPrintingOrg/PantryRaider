"""The Zebra ZPL label PPD has one text in two places.

docker/cups/zebra-zpl.ppd is what the print container ships, and
foodassistant-print-setup writes the same file on a Pi or an installer server
from a heredoc in install_label_ppds. A printer added on either kind of
install gets the same options (print rate, every darkness step), so the two
copies must stay equal line for line.

Run: python -m pytest tests/test_ppd_copies.py -q
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CANONICAL = REPO / "docker" / "cups" / "zebra-zpl.ppd"
SCRIPT = REPO / "scripts" / "image-build" / "foodassistant-print-setup"


def _heredoc_lines() -> list[str]:
    src = SCRIPT.read_text()
    body = src[src.index("install_label_ppds() {"):]
    start = body.index("<<'PPD_EOF'\n") + len("<<'PPD_EOF'\n")
    end = body.index("\nPPD_EOF\n", start)
    return body[start:end].split("\n")


def test_install_label_ppds_heredoc_equals_the_container_ppd():
    canonical = CANONICAL.read_text().split("\n")
    if canonical and canonical[-1] == "":
        canonical = canonical[:-1]
    assert _heredoc_lines() == canonical


def test_both_copies_offer_print_rate_and_every_darkness_step():
    lines = _heredoc_lines()
    assert "*OpenUI *zePrintRate/Print Rate: PickOne" in lines
    for step in ("0", "5", "10", "15", "20", "25", "30"):
        assert f'*zeDarkness {step}/{step}: ""' in lines
