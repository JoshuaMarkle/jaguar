# logging_config.py
"""
Logging setup for the ASCE Concrete Canoe Engineering Suite.

Provides a DynamicStreamHandler that always writes to the *current*
sys.stdout, so it correctly routes through the GUI's _SignalWriter
redirect even when logging handlers were created before the redirect
was installed.

Usage (call once at startup, or import from app.py)::

    from logging_config import setup_logging
    setup_logging()          # INFO level, dynamic stdout
    setup_logging(level=logging.DEBUG, log_file="canoe_debug.log")
"""
from __future__ import annotations

import logging
import sys
from typing import Optional


class DynamicStreamHandler(logging.StreamHandler):
    """
    A StreamHandler whose stream is always ``sys.stdout`` at emit time.

    Standard ``StreamHandler(sys.stdout)`` captures the stream object at
    construction; if ``sys.stdout`` is later replaced (e.g., by the GUI's
    ``_SignalWriter``), the handler still writes to the original object.
    This subclass re-evaluates ``sys.stdout`` on every emit call.
    """

    def emit(self, record):
        self.stream = sys.stdout
        super().emit(record)


_LOG_FORMAT = "%(levelname)s [%(name)s] %(message)s"


def setup_logging(
    level: int = logging.INFO,
    log_file: Optional[str] = None,
) -> None:
    """
    Configure the root logger.

    Parameters
    ----------
    level    : logging level for the dynamic stdout handler (default INFO)
    log_file : optional path for a rotating file handler (DEBUG level)
    """
    root = logging.getLogger()
    if root.handlers:
        # Already configured — update level only
        root.setLevel(min(root.level, level))
        return

    root.setLevel(logging.DEBUG)

    # Console handler — uses current sys.stdout at emit time
    console = DynamicStreamHandler()
    console.setLevel(level)
    console.setFormatter(logging.Formatter(_LOG_FORMAT))
    root.addHandler(console)

    if log_file:
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s " + _LOG_FORMAT, datefmt="%H:%M:%S"
        ))
        root.addHandler(fh)

    # Suppress noisy third-party loggers
    for noisy in ("matplotlib", "PIL", "fontTools"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
