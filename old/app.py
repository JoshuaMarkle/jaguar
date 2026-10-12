"""
ASCE Concrete Canoe — Engineering Suite (PyQt6 UI)

Entry point only. All UI logic lives in ui/.
"""
import logging
import os
import sys

from logging_config import setup_logging
setup_logging()

# Capture stdout/stderr in the FROZEN build before anything can print.  The
# spec builds a windowed app (console=False) and nothing else redirects the
# streams, so without this every warning the suite prints -- boundary-pinned
# winners, a two-point strength fit, "CONFIG DEFAULTED", an unverified gate --
# goes to a null handle. See run_log.py.
from run_log import start_capture as _start_capture
_RUN_LOG = _start_capture()

# A native crash (segfault) kills the interpreter with no Python traceback.
# faulthandler dumps every thread's Python stack to the run log on SIGSEGV etc.,
# so "it crashed after I ran something" names the line that was executing.
import faulthandler
try:
    _FAULT_FH = open(_RUN_LOG or os.path.join("logs", "crash.log"), "a")
    faulthandler.enable(file=_FAULT_FH, all_threads=True)
except Exception:                                # noqa: BLE001
    faulthandler.enable(all_threads=True)

# Chromium's default GL path segfaults on many Linux setups ("GBM is not
# supported", "GPU state invalid").  ANGLE-on-Vulkan avoids GBM and keeps WebGL
# (the 3D hull view) working.  Windows is untouched.  A user-set
# QTWEBENGINE_CHROMIUM_FLAGS always wins (e.g. "--disable-gpu" for no GPU).
if sys.platform.startswith("linux"):
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
                          "--use-angle=vulkan --enable-features=Vulkan")

import PyQt6.QtWebEngineWidgets  # must be imported before QApplication is created
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication
from ui.main_window import CanoeOptimizationSuite
from ui.style import DARK_STYLE

# The config the FIRST-RUN WIZARD reads and writes.  Resolved through the
# shared search, not the module folder: in a frozen build that folder is
# `_internal`, so the wizard would have edited the BUNDLED copy -- a file the
# user cannot find and a reinstall replaces.  When nothing exists yet the
# wizard should create the copy beside the executable, which is the first
# candidate.
from asset_paths import asset_search_paths as _asset_search_paths
_CFG_CANDIDATES = _asset_search_paths("canoe_suite.cfg")
_CFG_PATH = next((c for c in _CFG_CANDIDATES if os.path.exists(c)),
                 _CFG_CANDIDATES[0])


def _resource_path(rel: str) -> str:
    """
    Resolve a bundled data file both in development and inside a frozen
    PyInstaller build (where data files are extracted to sys._MEIPASS).
    """
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


_ICON_PATH = _resource_path(os.path.join("ui", "static", "jaguar.ico"))

if __name__ == "__main__":
    # Required before ANY process pool is created, and it must be the first
    # thing the entry point does.  A frozen (PyInstaller) build re-executes
    # this same executable for every spawned child; without this call the child
    # would re-run the whole application instead of the worker, forking GUIs
    # until the machine dies.  Harmless in the unfrozen interpreter.
    import multiprocessing
    multiprocessing.freeze_support()

    # STARTUP SELF-CHECK, on the GUI path too.  The GUI is where the team
    # actually works, and it is where four of the five asset bugs lived
    # unnoticed for months -- a defaulted config, an empty mix database, a
    # generated default hull, an invented out.mlt.  The log this prints into is
    # tee'd to logs/ by run_log, so a teammate reporting "the number looks
    # wrong" has the provenance in the file already.
    try:
        from asset_paths import print_asset_audit
        print_asset_audit()
    except Exception as _audit_exc:              # noqa: BLE001
        # DELIBERATELY BROAD.  A self-check that cannot run must not stop the
        # application from starting -- the check exists to inform, and refusing
        # to launch over it would be a worse failure than the one it reports.
        print(f"  (asset audit unavailable: "
              f"{type(_audit_exc).__name__}: {_audit_exc})")

    # Windows groups taskbar entries by AppUserModelID; without an explicit
    # one, running from source shows the generic Python icon in the taskbar
    # no matter what QIcon is set.  Harmless no-op elsewhere.
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "edu.virginia.concretecanoe.jaguar")

    app = QApplication(sys.argv)
    app.setApplicationName("JAGUAR")
    # NOTE: no setApplicationDisplayName — Qt APPENDS it to every window
    # title, which duplicated the name in the title bar.
    app.setOrganizationName("UVA Concrete Canoe")
    if os.path.exists(_ICON_PATH):
        app.setWindowIcon(QIcon(_ICON_PATH))   # window + taskbar icon
    app.setStyleSheet(DARK_STYLE)

    from ui.wizard import needs_wizard, FirstRunWizard
    if needs_wizard(_CFG_PATH):
        wiz = FirstRunWizard(_CFG_PATH)
        if wiz.exec() != FirstRunWizard.DialogCode.Accepted:
            sys.exit(0)

    window = CanoeOptimizationSuite()
    window.show()

    # Non-blocking update check — shows a status-bar message if newer release exists
    # The callback fires on a worker thread, which has no Qt event loop, so
    # QTimer.singleShot there never runs.  A signal emitted from the worker is
    # queued onto the GUI thread (the bridge lives there).
    from PyQt6.QtCore import QObject, pyqtSignal

    class _UpdateBridge(QObject):
        available = pyqtSignal(str, str)

    _bridge = _UpdateBridge()
    _bridge.available.connect(lambda current, latest: window.log(
        f"ℹ️  Update available: v{latest}  (you have v{current}) — "
        "visit the project repo to download."
    ))

    def _on_update_available(current: str, latest: str):
        _bridge.available.emit(current, latest)

    from services.update_check import check_for_update
    check_for_update(_on_update_available)

    sys.exit(app.exec())
