"""Point d'entree : python -m wago2ha [config.yaml]"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from .bridge import Bridge
from .config import load_config


def main() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    config_path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("CONFIG", "/config/config.yaml")
    cfg = load_config(config_path)
    bridge = Bridge(cfg)

    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        logging.getLogger("wago2ha").info("Arret demande.")


if __name__ == "__main__":
    main()
