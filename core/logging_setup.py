"""
core/logging_setup.py
=====================
One logging configuration for the whole process: rotating daily file plus
console. Call ``setup()`` once from main.
"""

from __future__ import annotations

import logging
import os
import sys

import config
from core import clock

_CONFIGURED = False


class _ISTFormatter(logging.Formatter):
    """Timestamps in IST regardless of the host timezone."""

    def formatTime(self, record, datefmt=None):     # noqa: N802
        dt = clock.now()
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S")


def setup(tag: str = "engine") -> logging.Logger:
    global _CONFIGURED

    root = logging.getLogger()
    if _CONFIGURED:
        return logging.getLogger(tag)

    level = getattr(logging, config.LOGGING.get("level", "INFO"), logging.INFO)
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = _ISTFormatter(
        "%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if config.LOGGING.get("console", True):
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)

    if config.LOGGING.get("file", True):
        os.makedirs(config.PATHS["log_dir"], exist_ok=True)
        path = os.path.join(
            config.PATHS["log_dir"],
            f"{tag}_{clock.datestamp()}.log",
        )
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)

    # Kite's ticker and twisted are chatty at INFO.
    logging.getLogger("kiteconnect").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("twisted").setLevel(logging.WARNING)

    _CONFIGURED = True
    return logging.getLogger(tag)


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)
