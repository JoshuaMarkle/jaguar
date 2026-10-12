# services/report_service.py
"""
Cross-platform helpers for opening generated files (PDF, CSV, ZIP).

Uses the OS default application on all three major platforms.
No Qt imports — safe to call from background threads.
"""
from __future__ import annotations

import os
import subprocess
import sys
from errors import domain_only


def open_file(path: str) -> None:
    """
    Open *path* with the system default application.

    Works on Windows, macOS, and Linux.  Silently ignores errors so
    callers never crash if the viewer is unavailable.
    """
    if not os.path.exists(path):
        return
    try:
        if sys.platform == "win32":
            os.startfile(path)           # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as _exc:
        domain_only(_exc)   # a bug is not a skippable condition
        pass


def open_pdf(path: str) -> None:
    """Convenience alias — same as open_file."""
    open_file(path)
