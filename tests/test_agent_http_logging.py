"""The Stream Deck controller and the thermometer reader keep httpx quiet
(FoodAssistant-85f64). httpx logs every request at INFO, and both poll the app
on the same device about once a second, which on a Pi became most of the
journal and, with it, most of what the SD card wrote at rest."""
from __future__ import annotations

import logging

import pytest

from foodassistant_gadgets import __main__ as gd_main
from foodassistant_streamdeck import __main__ as sd_main


@pytest.fixture(autouse=True)
def _restore_levels():
    saved = {n: logging.getLogger(n).level for n in ("httpx", "httpcore")}
    for n in saved:
        logging.getLogger(n).setLevel(logging.NOTSET)
    yield
    for n, level in saved.items():
        logging.getLogger(n).setLevel(level)


@pytest.mark.parametrize("mod", [sd_main, gd_main], ids=["streamdeck", "gadgets"])
def test_request_lines_are_off_unless_verbose(mod):
    mod.quiet_http_logs(False)
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).level == logging.WARNING
        assert not logging.getLogger(name).isEnabledFor(logging.INFO)


@pytest.mark.parametrize("mod", [sd_main, gd_main], ids=["streamdeck", "gadgets"])
def test_verbose_keeps_every_request_line(mod):
    mod.quiet_http_logs(True)
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).level == logging.NOTSET


@pytest.mark.parametrize("mod", [sd_main, gd_main], ids=["streamdeck", "gadgets"])
def test_main_quiets_them_right_after_logging_is_set_up(mod):
    import inspect
    src = inspect.getsource(mod.main)
    assert src.index("logging.basicConfig(") < src.index("quiet_http_logs(args.verbose)")
