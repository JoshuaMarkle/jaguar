# tests/test_gui_smoke.py
"""
THE GUI, BUILT AND CALLED, WITHOUT A SCREEN.

Twice now a change was verified from source, passed the whole suite, and
shipped broken in the GUI:

  * a pricing checkbox wired ui -> service where `run_sweep()` never took the
    parameter -- `TypeError` on EVERY sweep the team ran
  * (earlier) a warning banner whose only reference to `logger` lived inside
    its own exception handler

The CLI path was fine both times. The suite tests the sweep, the service
internals and the physics; the CLI exercises `run_grand_optimization`
directly; and nobody crosses ui -> services, which is the one call the whole
team makes.

`tests/test_call_signatures.py` catches the STATIC half -- a kwarg the callee
does not accept. This file catches what static analysis cannot: a widget
renamed, never created, or created on a branch that did not run.

WHY A SUBPROCESS. Building Qt inside the pytest process KILLED IT -- the run
printed "collected 4 items", produced no results, and exited 0. A test that
takes the process down while reporting success is worse than no test: CI goes
green. Qt therefore runs in its own interpreter, and this file asserts on its
exit code and a sentinel line. A crash is then a FAILURE with the child's
output attached, which is what it should have been all along.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("PyQt6", reason="PyQt6 not installed; GUI smoke skipped")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SENTINEL = "GUI_SMOKE_OK"

CHILD = textwrap.dedent(
    '''
    import os, sys, inspect, ast, io
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, %(repo)r)

    # STUB THE 3-D VIEW. Importing QtWebEngine under the offscreen platform
    # crashed this child with 0xC0000409 and NO output -- a hard fail-fast,
    # not a Python exception. WebEngine headless is well known for it.
    #
    # The 3-D hull renderer is not what this checks. Replacing it with a plain
    # QWidget lets the tab build so the SWEEP CALL SITE can be inspected, which
    # is the part that shipped broken twice. Stated plainly so nobody reads
    # this file as covering the 3-D tab: it does not.
    import types
    from PyQt6.QtWidgets import QApplication, QWidget
    app = QApplication.instance() or QApplication([])
    _stub = types.ModuleType("ui.hull3d_view")
    class Hull3DView(QWidget):
        def __init__(self, *a, **k):
            super().__init__()
        def __getattr__(self, _n):
            return lambda *a, **k: None
    _stub.Hull3DView = Hull3DView
    sys.modules["ui.hull3d_view"] = _stub

    from unittest.mock import MagicMock
    from ui.structural_tab import StructuralTab
    tab = StructuralTab(MagicMock())

    problems = []

    # 1. every self.<widget> the run_sweep call site reads must EXIST on the
    #    built tab. A rename is invisible to a signature check: the kwarg name
    #    stays valid while the value blows up.
    src = io.open(os.path.join(%(repo)r, "ui", "structural_tab.py"),
                  encoding="utf-8").read()
    wanted = set()
    for n in ast.walk(ast.parse(src)):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "run_sweep"):
            for kw in n.keywords:
                for sub in ast.walk(kw.value):
                    if (isinstance(sub, ast.Attribute)
                            and isinstance(sub.value, ast.Name)
                            and sub.value.id == "self"):
                        wanted.add(sub.attr)
    if not wanted:
        problems.append("found no self.<widget> reads in the run_sweep call")
    for w in sorted(wanted):
        if getattr(tab, w, None) is None:
            problems.append("missing widget: " + w)

    # 2. pricing must default OFF -- it was 93%% of a run's wall time.
    if tab.price_mix_dev_var.isChecked():
        problems.append("pricing checkbox defaults ON")

    # 3. the call the GUI would make must BIND against the real signature.
    from services.structural_service import StructuralService
    sig = inspect.signature(StructuralService.run_sweep)
    try:
        sig.bind(MagicMock(), MagicMock(), None,
                 stop_event=None,
                 precision_level=tab.precision_slider.value(),
                 existing_mixes_only=tab.existing_mixes_only_var.isChecked(),
                 price_mix_development=tab.price_mix_dev_var.isChecked(),
                 advanced_loading=tab.advanced_loading_var.isChecked())
    except TypeError as exc:
        problems.append("run_sweep would reject the GUI's call: " + str(exc))

    for p in problems:
        print("PROBLEM: " + p)
    if not problems:
        print(%(sentinel)r)
    sys.stdout.flush()
    os._exit(0 if not problems else 3)
    '''
) % {"repo": REPO, "sentinel": SENTINEL}


@pytest.fixture(scope="module")
def child():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
               PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, "-c", CHILD], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=300)


def test_the_child_actually_ran(child):
    """
    POSITIVE CONTROL on the harness. An empty result means Qt took the process
    down -- which is exactly what happened when this ran in-process, silently,
    exiting 0. Silence must never read as success.
    """
    assert child.stdout.strip() or child.stderr.strip(), (
        "the GUI child produced NO output at all; it died before it could "
        "report, and a silent death must not pass for a pass")


def test_the_structural_tab_builds_and_its_call_would_bind(child):
    problems = [ln for ln in child.stdout.splitlines()
                if ln.startswith("PROBLEM:")]
    assert child.returncode == 0 and SENTINEL in child.stdout, (
        "GUI smoke failed (exit "
        f"{child.returncode}).\n"
        + ("\n".join("  " + p for p in problems) if problems else "")
        + "\n--- child stderr ---\n" + child.stderr[-2500:])
