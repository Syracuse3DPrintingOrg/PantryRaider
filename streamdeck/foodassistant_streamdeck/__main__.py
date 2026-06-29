"""Entry point: ``python -m foodassistant_streamdeck``.

Loads config, sets up logging, and runs the controller until interrupted.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import __version__
from .config import load, resolved_config_path
from .controller import main_async


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="foodassistant-streamdeck",
        description="Run a Stream Deck as a FoodAssistant controller.",
    )
    p.add_argument("--config", help="Path to a TOML config file.")
    p.add_argument(
        "--verbose", "-v", action="store_true", help="Log every action and poll."
    )
    p.add_argument("--version", action="version", version=__version__)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if "--dump-actions" in sys.argv:
        import json
        from .actions import catalog
        from .layout import default_key_names, supported_key_counts
        # The default key arrangement per supported deck size, so the web grid
        # editor can pre-populate a fresh grid from the same layout the
        # controller uses (rather than a hardcoded copy in the page's JS).
        defaults = {
            str(kc): default_key_names(kc) for kc in supported_key_counts()
        }
        print(json.dumps({"actions": catalog(), "default_keys": defaults}))
        return 0
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = load(args.config)
    config_path = str(resolved_config_path(args.config))
    try:
        return asyncio.run(main_async(config, config_path=config_path))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
