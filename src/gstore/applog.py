"""File logging for the app.

Gstore had none, and it showed: a 20 GB download failed with a dialog reading
only "timed out" — the whole of `str(socket.timeout())` — and there was no way
to find out which chunk, how far it had got, or how many retries it had burned.
Transfers here run for hours and fail intermittently, which is exactly the
situation a log exists for.

The log lives beside the index in the app-data directory, rotates so it cannot
grow without bound, and is also echoed to stderr so a terminal launch shows it
live.
"""

from __future__ import annotations

import logging
import logging.handlers

from gstore.config import data_dir

LOG_FILENAME = "gstore.log"
_MAX_BYTES = 2 * 1024 * 1024
_BACKUPS = 3

_configured = False


def log_path():
    """Absolute path to the current log file."""
    return data_dir() / LOG_FILENAME


def setup(level: int = logging.INFO) -> None:
    """Attach the rotating file handler. Safe to call more than once."""
    global _configured
    if _configured:
        return

    root = logging.getLogger("gstore")
    root.setLevel(level)
    root.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        handler = logging.handlers.RotatingFileHandler(
            log_path(), maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8"
        )
        handler.setFormatter(fmt)
        root.addHandler(handler)
    except OSError:
        # An unwritable app-data directory must not stop the app starting;
        # the stderr handler below still gives something to go on.
        pass

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    stream.setLevel(logging.WARNING)
    root.addHandler(stream)

    _configured = True


def get(name: str) -> logging.Logger:
    """Logger for a module, e.g. ``get(__name__)``."""
    return logging.getLogger(name if name.startswith("gstore") else f"gstore.{name}")
