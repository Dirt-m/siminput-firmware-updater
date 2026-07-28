"""File logging for the packaged app.

The Windows/macOS builds run with console=False, so an uncaught exception or
device error is otherwise completely invisible — there is no terminal and no
artifact to attach to a bug report. Logs rotate in the same folder as config
backups so users can find them.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

LOG_DIR = Path.home() / "SIMINPUT Backups"

log = logging.getLogger("siminput_updater")


def setup_logging() -> None:
    log.setLevel(logging.INFO)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            LOG_DIR / "updater.log", maxBytes=512 * 1024, backupCount=2,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(handler)
    except OSError:
        # Nowhere to write (weird home dir) — logging stays console-only.
        log.addHandler(logging.NullHandler())
