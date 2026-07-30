"""Entry point: ``python -m foodassistant_streamdeck``.

Loads config, sets up logging, and runs the controller until interrupted.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

# Keep this module's imports light: main() paints the boot splash before
# importing the controller (which drags in httpx and the rest of the heavy
# startup), so the deck shows the brand mark instead of the Elgato factory
# logo for the bulk of service startup (FoodAssistant-krbn). config/actions
# are pure stdlib and cheap; the controller import is the slow one.
from . import __version__
from .config import load, resolved_config_path


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
        print(json.dumps(catalog()))
        return 0
    if "--dump-config" in sys.argv:
        # Emit the RESOLVED config the controller actually uses (defaults applied,
        # invalid key names dropped), so the web editor can mirror the deck rather
        # than the raw on-disk TOML. Only the fields the editor needs.
        import json
        path = None
        if "--config" in sys.argv:
            i = sys.argv.index("--config")
            if i + 1 < len(sys.argv):
                path = sys.argv[i + 1]
        cfg = load(path)
        print(json.dumps({"keys": cfg.keys}))
        return 0
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = load(args.config)
    config_path = str(resolved_config_path(args.config))
    # Paint the splash NOW, before the controller import: on a Pi that import
    # (httpx and friends) is the bulk of the boot gap, during which the deck
    # would otherwise keep showing the Elgato factory logo. The open handle is
    # passed through so the controller adopts it without a reset, keeping the
    # splash on screen until the first real page draw replaces it.
    from . import earlysplash
    deck = earlysplash.open_deck_and_paint(rotation=config.rotation)
    from .controller import main_async
    try:
        return asyncio.run(main_async(config, config_path=config_path, deck=deck))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
