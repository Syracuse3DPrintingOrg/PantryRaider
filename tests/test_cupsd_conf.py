"""docker/cups/cupsd.conf, the print service config every new compose writes.

Adding a printer needs no password (the app runs lpadmin with no credential),
so the config itself has to keep the rest tight: it answers only the names
the app uses, keeps its configuration and log files to the socket, and has no
web admin page.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CUPSD_CONF = ROOT / "docker" / "cups" / "cupsd.conf"


def _lines() -> list[str]:
    return [line.strip() for line in CUPSD_CONF.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def _directive(name: str) -> list[list[str]]:
    return [line.split()[1:] for line in _lines() if line.split()[0] == name]


def _location(path: str) -> list[str]:
    lines = _lines()
    start = lines.index(f"<Location {path}>")
    end = lines.index("</Location>", start)
    return lines[start + 1:end]


def test_server_alias_names_the_app_addresses_and_never_everything() -> None:
    aliases = [name for values in _directive("ServerAlias") for name in values]
    assert set(aliases) == {"cups", "foodassistant-cups"}
    assert "*" not in aliases


def test_no_require_user_anywhere() -> None:
    # services/printing.py runs lpadmin with no credential, so a user
    # requirement would break in-app printer setup.
    assert not re.search(r"^\s*Require\s+user", CUPSD_CONF.read_text(),
                         re.MULTILINE | re.IGNORECASE)


def test_config_and_log_files_are_for_the_socket_only() -> None:
    for path in ("/admin/conf", "/admin/log"):
        body = _location(path)
        assert "Order allow,deny" in body, path
        assert not any(line.startswith("Allow") for line in body), path


def test_admin_is_limited_to_the_local_network() -> None:
    for path in ("/", "/admin"):
        assert _location(path) == ["Order allow,deny", "Allow @LOCAL"], path


def test_web_admin_and_browsing_are_off() -> None:
    assert _directive("WebInterface") == [["No"]]
    assert _directive("Browsing") == [["Off"]]
    # Shared only within the private printing network (nothing is published
    # and Browsing is off), so an older app image printing over cups:631 still
    # reaches a queue added under this config.
    assert _directive("DefaultShared") == [["Yes"]]
    assert _directive("ErrorPolicy") == [["retry-job"]]


def test_listens_on_the_private_network_and_the_socket() -> None:
    assert sorted(v[0] for v in _directive("Listen")) == ["*:631", "/run/cups/cups.sock"]


def test_config_can_be_embedded_in_compose_as_is() -> None:
    text = CUPSD_CONF.read_text()
    # Compose would interpolate a "$", and the heredoc ends at a CUPSD line.
    assert "$" not in text
    assert "CUPSD" not in text.splitlines()
    assert text.endswith("\n")
