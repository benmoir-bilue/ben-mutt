from __future__ import annotations

import logging
import logging.handlers
import os

from bem.config import CONFIG_DIR

LOG_FILE = CONFIG_DIR / "bem.log"


def setup(level: int | None = None) -> logging.Logger:
    if level is None:
        name = os.environ.get("BEM_LOG_LEVEL", "DEBUG").upper()
        level = getattr(logging, name, logging.DEBUG)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("bem")
    if logger.handlers:
        return logger
    logger.setLevel(level)
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, encoding="utf-8", maxBytes=2_000_000, backupCount=2
    )
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    logger.addHandler(fh)
    return logger


def get() -> logging.Logger:
    return logging.getLogger("bem")
