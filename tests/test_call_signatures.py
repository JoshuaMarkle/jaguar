# tests/test_call_signatures.py
"""
THE UI -> SERVICE -> SWEEP CONTRACT, CHECKED AGAINST THE REAL SIGNATURES.

A pricing checkbox was wired from the Structural tab to the service, and the
service was taught to forward it to the sweep -- but `run_sweep()` was never
given the parameter. Every GUI sweep died instantly:

    TypeError: StructuralService.run_sweep() got an unexpected keyword
               argument 'price_mix_development'

It survived 65 test files, a full guard suite, a bit-for-bit source-vs-frozen
comparison and five exercised GUI subsystems, because **no test crosses the
ui -> services boundary**. The suite tests the sweep, and it tests the service's
internals, and the CLI exercises `run_grand_optimization` directly -- so the one
call the whole team actually makes was the one nobody made.

It is also the same failure as five API guesses made while writing a throwaway
harness the same day: code calling working code with arguments nobody checked
against the actual signature. A human reading the diff sees a plausible kwarg;
only `inspect` sees the truth.

This file compares, statically:

    what ui/ PASSES   ->  what the service ACCEPTS
    what the service FORWARDS  ->  what the sweep ACCEPTS

No Qt, no GUI, no sweep run. It is fast enough to never be skipped, which
matters more than depth here: the bug was a missing name, not subtle behaviour.
"""
from __future__ import annotations

import ast
import inspect
import io
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    with io.open(os.path.join(REPO, rel), encoding="utf-8-sig") as fh:
        return fh.read()


def _kwargs_of_calls(rel, func_name, attr=True):
    """
    {kwarg names} for every call to ``func_name`` in ``rel``.

    `attr=True` matches `something.run_sweep(...)`; False matches a bare
    `run_grand_optimization(...)`.
    """
    out = []
    for n in ast.walk(ast.parse(_src(rel))):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        hit = (isinstance(f, ast.Attribute) and f.attr == func_name) if attr \
            else (isinstance(f, ast.Name) and f.id == func_name)
        if hit:
            out.append(({k.arg for k in n.keywords if k.arg}, n.lineno))
    return out


# ── the boundary that broke ────────────────────────────────────────────────

def test_the_gui_actually_calls_the_service():
    """
    POSITIVE CONTROL. If the call site moved or was renamed, every assertion
    below would pass while checking nothing -- and this file exists precisely
    because "nothing checked it" is how the bug shipped.
    """
    calls = _kwargs_of_calls("ui/structural_tab.py", "run_sweep")
    assert calls, "no run_sweep() call found in ui/structural_tab.py"
    assert any(kw for kw, _ln in calls), \
        "the call passes no keyword arguments; the scan is looking at the " \
        "wrong thing"


def test_every_kwarg_the_gui_passes_is_accepted_by_run_sweep():
    """
    THE BUG. A name the service does not accept is a TypeError on the main
    path the whole team uses, and it is invisible to every test that does not
    cross this boundary.
    """
    from services.structural_service import StructuralService
    accepted = set(
        inspect.signature(StructuralService.run_sweep).parameters) - {"self"}
    for kwargs, line in _kwargs_of_calls("ui/structural_tab.py", "run_sweep"):
        unknown = sorted(kwargs - accepted)
        assert not unknown, (
            f"ui/structural_tab.py:{line} passes {unknown} to "
            f"StructuralService.run_sweep(), which does not accept "
            f"{'them' if len(unknown) > 1 else 'it'}.\n"
            f"  Accepted: {sorted(accepted)}\n"
            f"  This is a TypeError on every GUI sweep.")


def test_everything_the_service_forwards_is_accepted_by_the_sweep():
    """The other half of the chain, checked the same way."""
    from main import run_grand_optimization
    accepted = set(inspect.signature(run_grand_optimization).parameters)
    calls = _kwargs_of_calls("services/structural_service.py",
                             "run_grand_optimization", attr=False)
    assert calls, "the service no longer calls run_grand_optimization"
    for kwargs, line in calls:
        unknown = sorted(kwargs - accepted)
        assert not unknown, (
            f"services/structural_service.py:{line} forwards {unknown} to "
            f"run_grand_optimization(), which does not accept "
            f"{'them' if len(unknown) > 1 else 'it'}.\n"
            f"  Accepted: {sorted(accepted)}")


def test_the_check_can_still_fail():
    """
    POSITIVE CONTROL on the ASSERTION. Plant a call with a bogus kwarg and
    require the comparison to catch it -- a scanner that cannot fail is this
    repo's recurring defect.
    """
    planted = "svc.run_sweep(cfg, cb, not_a_real_parameter=True)\n"
    found = []
    for n in ast.walk(ast.parse(planted)):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "run_sweep"):
            found = sorted({k.arg for k in n.keywords if k.arg})
    assert found == ["not_a_real_parameter"], \
        "the kwarg extractor cannot see a planted keyword argument"

    from services.structural_service import StructuralService
    accepted = set(
        inspect.signature(StructuralService.run_sweep).parameters) - {"self"}
    assert "not_a_real_parameter" not in accepted


# ── the quieter half: accepted but never used ──────────────────────────────

def test_run_sweep_uses_every_parameter_it_accepts():
    """
    A parameter a caller can pass and the callee ignores is worse than a
    TypeError: the GUI checkbox appears to work and changes nothing. The
    pricing flag was one line away from exactly that -- it was being read off
    `opt_config` while the GUI passed it as a kwarg, so whichever half had
    landed alone, the switch would have been dead.
    """
    from services.structural_service import StructuralService
    body = inspect.getsource(StructuralService.run_sweep)
    params = [p for p in inspect.signature(
        StructuralService.run_sweep).parameters if p != "self"]
    unused = [p for p in params if body.count(p) < 2]  # signature + >=1 use
    assert not unused, (
        f"run_sweep accepts {unused} and never uses "
        f"{'them' if len(unused) > 1 else 'it'} -- a control that looks wired "
        f"and is not")


def test_optimization_config_carries_no_runtime_reporting_flags():
    """
    OptimizationConfig is SAVED AND SHARED as a design preset. A run-time
    reporting choice stored on it would travel to a teammate and silently turn
    on a phase measured at 93% of a run's wall time.
    """
    from domain.models import OptimizationConfig
    fields = set(OptimizationConfig.__dataclass_fields__)
    for runtime_only in ("price_mix_development", "existing_mixes_only",
                         "advanced_loading", "precision_level"):
        assert runtime_only not in fields, (
            f"{runtime_only} is a run-time choice and must not live on a "
            f"shareable design preset; pass it to run_sweep() instead")
