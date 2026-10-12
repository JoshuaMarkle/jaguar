"""
Capture everything the suite prints, in the FROZEN build where nothing else
can.

WHY THIS EXISTS.  packaging/jaguar.spec builds a windowed app (`console=False`)
and nothing in ui/ redirects the streams, so in the executable every print()
goes to a null handle.  That silently discarded, among others:

  * "WINNER AT SEARCH BOUNDARY" -- the warning that caught a winner pinned to
    the top of its depth range and reported as if it were a free optimum
  * the strength-fit caveat ("fitted to n = 2 points ... an EXTRAPOLATION,
    not a measurement")
  * "[!! CONFIG DEFAULTED]"
  * the refinement gate's audit, including "the winner below is NOT
    refinement-verified"

A discarded warning is worse than a wrong number: it removes the mechanism
that would have told you the number was untrustworthy.

The log is the FULL record.  The handful of run-critical warnings ALSO go to
the GUI (see ui.warning_banner), because nobody opens a log file beside an
executable.
"""
from __future__ import annotations

import datetime as _dt
import os
import sys

_LOG_PATH: str | None = None


class _Tee:
    """Write to the original stream (if any) AND the log file."""

    def __init__(self, stream, fh):
        self._stream = stream
        self._fh = fh

    def write(self, data):
        if self._stream is not None:
            try:
                self._stream.write(data)
            except Exception:
                pass
        try:
            self._fh.write(data)
            self._fh.flush()          # a crash must not lose the last lines
        except Exception:
            pass

    def flush(self):
        for t in (self._stream, self._fh):
            try:
                if t is not None:
                    t.flush()
            except Exception:
                pass

    def isatty(self):
        return False


def log_dir() -> str:
    """
    Where run logs go: beside the executable when frozen, else ./logs.

    Beside the EXE rather than in a temp dir on purpose -- a teammate asked
    for "the log" can find it without being told a path.
    """
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    d = os.path.join(base, "logs")
    os.makedirs(d, exist_ok=True)
    return d


def current_log_path() -> str | None:
    """The active run log, or None when capture was never started."""
    return _LOG_PATH


def start_capture(force: bool = False) -> str | None:
    """
    Tee stdout/stderr into a timestamped log file.

    Only acts in a frozen build unless ``force``: running from source already
    has a console, and stealing its streams makes development worse.
    Returns the log path, or None if capture did not start.
    """
    global _LOG_PATH
    if _LOG_PATH is not None:
        return _LOG_PATH
    if not force and not getattr(sys, "frozen", False):
        return None
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(log_dir(), f"jaguar-{stamp}.log")
    try:
        fh = open(path, "a", encoding="utf-8", buffering=1)
    except Exception:
        return None
    fh.write(f"JAGUAR run log  {stamp}\n")
    fh.write(f"frozen={bool(getattr(sys, 'frozen', False))}  "
             f"executable={sys.executable}\n")
    fh.write("=" * 72 + "\n")
    sys.stdout = _Tee(sys.stdout, fh)
    sys.stderr = _Tee(sys.stderr, fh)
    _LOG_PATH = path
    return path
