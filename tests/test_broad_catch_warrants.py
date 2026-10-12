# tests/test_broad_catch_warrants.py
"""
THE SEVEN HANDLERS THAT MUST STAY BROAD, AND MUST KEEP SAYING WHY.

A pass across this suite narrowed 24 broad `except Exception` handlers to the
exception each was written for.  That pass was right for 18 of them -- a
handler that catches everything turns a `NameError` into a silent skip, which
is how the refinement gate came to report a winner it had never touched.

Six were re-widened on this rule (`_audit_run` had already been widened on it
before the others were reviewed, making seven pinned here), and the rule is the
whole content of this file:

    A BROAD CATCH IS JUSTIFIED WHEN AN EXCEPTION WOULD DISCARD WORK ALREADY
    COMPLETED, NOT WHEN IT WOULD SKIP WORK NOT YET ATTEMPTED.

Skipping work not yet attempted is cheap and recoverable, so a narrow catch
there costs nothing and buys a loud failure.  Discarding completed work is
neither, and the completed work in this suite is measured in hours.

Writing that in KNOWN_ISSUES.md is not enough.  The next person to run a
tidy-up pass over exception handling will read `except Exception` as a smell,
not as a decision -- the same pass already did exactly that once.  So this
guard fails if any of the six is re-narrowed, and fails if one loses the
comment that explains it, because a broad catch with no warrant is the smell.
"""
from __future__ import annotations

import ast
import io
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: (relative path, enclosing function, a phrase unique to that handler's body).
#: The phrase anchors the check to the RIGHT handler in files that legitimately
#: contain several, and it is taken from the handler's own message rather than
#: its comment so a rewritten comment does not silently move the anchor.
MUST_STAY_BROAD = [
    ("main.py", "_audit_run",
     "audit log NOT written",
     "the finished sweep, over an audit line"),
    ("main.py", "run_grand_optimization",
     "crew-CG model unavailable",
     "the finished sweep, over a printed GM_T"),
    ("main.py", "run_grand_optimization",
     "real-KG suite skipped",
     "the finished sweep, over the stability block"),
    ("main.py", "run_grand_optimization",
     "mix development spec skipped",
     "the sweep AND the re-pricing runs stacked on top of it"),
    ("services/structural_service.py", "run_sweep",
     "provenance tag unavailable",
     "the finished sweep, over a version string"),
    ("reporting/latex_report.py", "_solid_keel_audit",
     '"computed": False',
     "the report being built, over one optional audit"),
    # NOTE: this module is currently dead -- nothing imports it and it is not
    # in the frozen build (see KNOWN_ISSUES).  Pinned anyway: if it is revived
    # the warrant should still be there, and if it is deleted this case fails
    # loudly rather than rotting.  Delete the case WITH the module, not before.
    ("reporting/report_generator.py", "_add_hull_section",
     "Hull geometry not available",
     "seven later report sections, over section two"),
]

#: Every one of the six must name its reasoning at the handler.  At least one of
#: these has to appear in a comment attached to it.  The list is deliberately
#: short: this checks that SOMEONE WROTE A WARRANT, not that they used approved
#: wording.
WARRANT_MARKERS = ("DELIBERATELY BROAD", "already completed", "already paid",
                   "already succeeded", "already run", "already exists",
                   "is over by the time", "in hand")


def _src(rel: str) -> str:
    with io.open(os.path.join(REPO, rel), encoding="utf-8-sig") as fh:
        return fh.read()


def _handlers(rel: str):
    """[(enclosing_function, handler_node, handler_source)] for one file."""
    src = _src(rel)
    lines = src.split("\n")
    tree = ast.parse(src)
    owner = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                owner.setdefault(id(child), node.name)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            body = "\n".join(lines[node.lineno - 1:node.end_lineno])
            out.append((owner.get(id(node), "<module>"), node, body))
    return out


def _is_broad(node: ast.ExceptHandler) -> bool:
    t = node.type
    return (t is None) or (isinstance(t, ast.Name)
                           and t.id in ("Exception", "BaseException"))


@pytest.mark.parametrize("rel,fn,phrase,cost", MUST_STAY_BROAD,
                         ids=[f"{r}::{f}" for r, f, _p, _c in MUST_STAY_BROAD])
def test_the_handler_is_still_broad(rel, fn, phrase, cost):
    """Re-narrowing any of these throws away completed work on a bad day."""
    found = [(n, body) for owner, n, body in _handlers(rel)
             if owner == fn and phrase in body]
    assert len(found) == 1, (
        f"{rel}::{fn}: expected exactly one handler containing {phrase!r}, "
        f"found {len(found)}. Either the handler moved or the anchor is stale "
        f"-- fix the anchor, do not delete the case.")
    node, body = found[0]
    assert _is_broad(node), (
        f"{rel}::{fn} (line {node.lineno}) has been RE-NARROWED to "
        f"`except {ast.unparse(node.type)}`.\n\n"
        f"  What an escaping exception discards here: {cost}.\n\n"
        f"  The rule: a broad catch is justified when an exception would\n"
        f"  discard work already completed, not when it would skip work not\n"
        f"  yet attempted. This site is the former. See KNOWN_ISSUES.md,\n"
        f"  'Exception handling -- the narrowing pass'.")


@pytest.mark.parametrize("rel,fn,phrase,cost", MUST_STAY_BROAD,
                         ids=[f"{r}::{f}" for r, f, _p, _c in MUST_STAY_BROAD])
def test_the_handler_still_carries_its_warrant(rel, fn, phrase, cost):
    """
    A broad catch with no stated reason is indistinguishable from carelessness,
    and is what the narrowing pass was right to remove 18 times.  These six earn
    their breadth by explaining it AT THE HANDLER, where the next reader is.
    """
    src_lines = _src(rel).split("\n")
    node = [n for owner, n, body in _handlers(rel)
            if owner == fn and phrase in body][0]
    # The comment may sit inside the handler or on the lines just above the
    # `except`; accept either, and look no further than the handler's own span.
    window = "\n".join(src_lines[max(0, node.lineno - 4):node.end_lineno])
    assert any(m in window for m in WARRANT_MARKERS), (
        f"{rel}::{fn} (line {node.lineno}) is broad but no longer says why.\n"
        f"  Expected one of {WARRANT_MARKERS} near the handler.\n"
        f"  Restore the warrant rather than deleting this assertion: an\n"
        f"  unexplained broad catch is the thing the narrowing pass targeted.")


def test_the_narrowing_pass_still_holds_everywhere_else():
    """
    THE OTHER DIRECTION.  This guard must not become a licence to widen.  The
    18 narrowed handlers are not enumerated (they move), but their COUNT is
    bounded: the four files carrying the six exemptions may not accumulate
    broad handlers beyond what has been reviewed.
    """
    files = sorted({rel for rel, _f, _p, _c in MUST_STAY_BROAD})
    broad = []
    for rel in files:
        for owner, node, _body in _handlers(rel):
            if _is_broad(node):
                broad.append(f"{rel}::{owner}:{node.lineno}")
    # Today: 29 across these four files.  Was 27; two more were added
    # with the headless CLI work -- harden_stdio (a stream that cannot
    # be reconfigured must not stop a sweep) and the mix-database
    # provenance print (failing to COUNT rows must not stop a sweep
    # whose database has already opened).  Both are the same rule:
    # they guard reporting ON work that is already done or ready.
    #
    # Earlier note: 27 across these four files.  Was 24; the phase-timing work added
    # three more in main.py -- announce_phase's guard around the caller's
    # progress widget, and the two PhaseClock.report() guards.  All three are
    # the same rule: they run AFTER the sweep, so an exception in them would
    # discard hours of completed work to save a progress tick or a timing
    # table.  Raised deliberately, which is what this ceiling is for.
    #
    # The original note: 24 across these four files -- the six above plus the pre-existing
    # reviewed sites (each carries a `domain_only(exc)` re-raise or its own
    # warrant).  The ceiling sits just above that, deliberately: headroom wide
    # enough to be unreachable is a guard that cannot fail, which is the exact
    # defect this file's positive control exists to prevent.
    ceiling = 29
    assert len(broad) <= ceiling, (
        f"{len(broad)} broad handlers across {files}, ceiling {ceiling}.\n"
        + "\n".join("  " + b for b in broad)
        + "\n\nEither narrow one, or raise the ceiling deliberately. The rule "
          "is that a broad catch guards COMPLETED work; if this one guards "
          "work not yet attempted, narrow it instead.")


def test_the_check_can_still_fail(tmp_path):
    """
    POSITIVE CONTROL.  A scanner that cannot fail is the defect this repo keeps
    finding, so plant both failures and require the machinery to catch them.
    """
    planted = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except ValueError as e:\n"
        '        print("audit log NOT written")\n'
        "\n"
        "def g():\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as e:\n"
        '        print("provenance tag unavailable")\n'
    )
    p = tmp_path / "planted.py"
    p.write_text(planted, encoding="utf-8")
    tree = ast.parse(planted)
    lines = planted.split("\n")
    owner, handlers = {}, []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for child in ast.walk(node):
                owner.setdefault(id(child), node.name)
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            handlers.append((owner.get(id(node)), node,
                             "\n".join(lines[node.lineno - 1:node.end_lineno])))

    narrowed = [n for o, n, b in handlers if "audit log NOT written" in b]
    assert narrowed and not _is_broad(narrowed[0]), \
        "the breadth check cannot see a re-narrowed handler"

    unwarranted = [n for o, n, b in handlers
                   if "provenance tag unavailable" in b]
    window = "\n".join(lines[max(0, unwarranted[0].lineno - 4):
                             unwarranted[0].end_lineno])
    assert not any(m in window for m in WARRANT_MARKERS), \
        "the warrant check cannot see a broad handler with no reason given"
