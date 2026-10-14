"""Entry point: ``python -m foodassistant_gadgets``.

Loads config, sets up logging, and runs the reader until interrupted. The
outer loop is the never-give-up backstop: whatever the radio or the app does,
the service logs it and starts over rather than exiting.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

from . import __version__
from .config import load


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="foodassistant-gadgets",
        description="Read Bluetooth kitchen thermometers for Pantry Raider.",
    )
    p.add_argument("--config", help="Path to a TOML config file.")
    p.add_argument(
        "--verbose", "-v", action="store_true", help="Log every reading and push."
    )
    p.add_argument("--version", action="version", version=__version__)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("foodassistant.gadgets")
    # Import here so `--version` and config errors do not require bleak.
    from .daemon import main_async
    while True:
        try:
            cfg = load(args.config)
            return asyncio.run(main_async(cfg))
        except KeyboardInterrupt:
            return 0
        except Exception:
            log.exception("Reader crashed; restarting in 10 seconds")
            time.sleep(10)


if __name__ == "__main__":
    raise SystemExit(main())
