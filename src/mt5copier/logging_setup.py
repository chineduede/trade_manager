"""Logging configuration.

Two things this does that ``zele-mt5``'s logger did not: it uses ``logging.getLogger(name)``
rather than caching one logger in a module global (which made the first caller's name and file
win for every later caller), and it rotates. A copier is expected to run for weeks.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

_FMT = "%(asctime)s %(levelname)-7s [%(processName)s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured: set[str] = set()


def configure(
    log_file: Path,
    *,
    level: int = logging.INFO,
    to_stderr: bool = False,
    max_bytes: int = 5_000_000,
    backups: int = 5,
) -> None:
    """Attach handlers to the root logger. Idempotent per log file.

    ``to_stderr`` is off by default because the Textual UI owns the terminal; a stray
    log line would corrupt the rendered frame.
    """
    key = str(log_file)
    if key in _configured:
        return

    log_file.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)

    handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(_FMT, datefmt=_DATEFMT))
    root.addHandler(handler)

    if to_stderr:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter(_FMT, datefmt=_DATEFMT))
        root.addHandler(stream)

    _configured.add(key)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger. Safe to call any number of times from anywhere."""
    return logging.getLogger(name)
