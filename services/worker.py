# services/worker.py
"""
Background worker infrastructure for the PyQt6 UI.

WorkerThread runs any callable in a QThread, captures stdout/stderr via
_SignalWriter, and forwards log lines to the GUI console via Qt signals.
"""
from __future__ import annotations

import io
import sys
import threading

from PyQt6.QtCore import QThread, pyqtSignal
from errors import domain_only


class WorkerThread(QThread):
    """Generic background worker that captures stdout and emits log lines."""
    log_signal      = pyqtSignal(str)
    finished_signal = pyqtSignal(object)   # result or None
    error_signal    = pyqtSignal(str)
    progress_signal = pyqtSignal(float, str)   # (fraction 0–1, label)

    def __init__(self, target, args=(), parent=None):
        super().__init__(parent)
        self._target = target
        self._args   = args
        self.stop_event = threading.Event()

    def run(self):
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = _SignalWriter(self.log_signal)
        sys.stderr = _SignalWriter(self.log_signal)
        try:
            result = self._target(*self._args, worker=self)
            self.finished_signal.emit(result)
        except Exception as _exc:
            domain_only(_exc)   # a bug is not a skippable condition
            import traceback
            self.error_signal.emit(traceback.format_exc())
        finally:
            sys.stdout, sys.stderr = old_out, old_err


class _SignalWriter(io.TextIOBase):
    """Drop-in stdout replacement that emits text via a Qt signal."""

    def __init__(self, signal):
        super().__init__()
        self._sig = signal

    def write(self, text):
        if text:
            self._sig.emit(text)
        return len(text) if text else 0

    def flush(self):
        pass
