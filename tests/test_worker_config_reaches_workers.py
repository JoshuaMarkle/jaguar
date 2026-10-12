# tests/test_worker_config_reaches_workers.py
"""
THE EXECUTOR MUST NEVER BE ABLE TO CHANGE A RESULT.

`main.resolve_sweep_executor` states that invariant in its own docstring, and
explains that SWEEP_EXECUTOR is kept out of RUN_LEVEL_CONFIG_ATTRS "precisely
because it must never be able to change a result".  But the invariant has a
second half nobody was checking:

    Any config value READ INSIDE A WORKER must be in RUN_LEVEL_CONFIG_ATTRS.

A spawned process re-imports `config` fresh.  It sees the run's settings ONLY
through the RunState carried in the task tuple, which carries exactly
RUN_LEVEL_CONFIG_ATTRS.  So a worker-side read of anything else silently gets
the module default under `process` while getting the configured value under
`thread` -- the executor changing a result, which is the one thing it may not
do.  Nothing raises; the fingerprint assertion passes, because the value is not
in the fingerprint either.

THIS IS NOT HYPOTHETICAL.  It was found by a thread-vs-process benchmark whose
two legs evaluated 7,712 and 7,562 configurations on an identical grid.  Three
names were missing: `SEATING_N_STARTS`, `SEATING_START_SEED` (the seed that
exists so a sweep is reproducible) and `GATE_TOP_M` (which sets what reaches
the refinement gate).  None was set in `canoe_suite.cfg`, so nothing shipped
wrong -- but every one of them was a knob that would have been silently ignored
the moment somebody used it, and the seed is a reproducibility control.

Same family as the default-argument bug already fixed inside
`seating_start_points`: a tunable that cannot be tuned, reporting success.
"""
from __future__ import annotations

import ast
import io
import os

import pytest

from config.run_state import RUN_LEVEL_CONFIG_ATTRS

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Names a worker may read WITHOUT being run-level, each with the reason it is
#: safe.  Keep this list short and argued -- it is the guard's own unaudited
#: surface, and it is listed in the exemption census for that reason.
EXEMPT = {
    # (none today -- add with a written reason, not silently)
}

#: The entry points that execute inside a worker.  `_evaluate_geometry_worker`
#: is the pool's target; the others are called from it.
WORKER_ROOTS = ("_evaluate_geometry_worker",)


def _worker_reachable(tree):
    """Functions in main.py reachable from a worker root, transitively."""
    fns = {n.name: n for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    seen, frontier = set(), set(WORKER_ROOTS)
    while frontier:
        nm = frontier.pop()
        if nm in seen or nm not in fns:
            continue
        seen.add(nm)
        for c in ast.walk(fns[nm]):
            if (isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                    and c.func.id in fns):
                frontier.add(c.func.id)
    return {nm: fns[nm] for nm in seen}


def _config_reads(fn_node):
    """UPPER_CASE config names this function reads, by either spelling."""
    out = set()
    for c in ast.walk(fn_node):
        if (isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                and c.func.id == "getattr" and len(c.args) >= 2
                and isinstance(c.args[0], ast.Name) and c.args[0].id == "config"
                and isinstance(c.args[1], ast.Constant)
                and isinstance(c.args[1].value, str)):
            out.add(c.args[1].value)
        elif (isinstance(c, ast.Attribute) and isinstance(c.value, ast.Name)
              and c.value.id == "config" and c.attr.isupper()):
            out.add(c.attr)
    return out


def _scan():
    src = io.open(os.path.join(REPO, "main.py"), encoding="utf-8-sig").read()
    tree = ast.parse(src)
    reach = _worker_reachable(tree)
    reads = {}
    for nm, node in reach.items():
        for name in _config_reads(node):
            reads.setdefault(name, set()).add(nm)
    return reach, reads


def test_the_scan_actually_reaches_the_worker_tree():
    """
    POSITIVE CONTROL on the SCAN ITSELF.  If the call-graph walk silently found
    nothing, every assertion below would pass while checking nothing -- the
    exact way this repo's slice scanner once spent a refactor unable to fail.
    """
    reach, reads = _scan()
    assert "_evaluate_geometry_worker" in reach
    assert len(reach) >= 5, (
        f"only {len(reach)} worker-reachable functions found; the call-graph "
        f"walk has stopped working")
    assert "seating_start_points" in reach, (
        "the seating solve is called from the worker and must be in the scan "
        "-- it is where two of the three original misses lived")
    assert len(reads) >= 5, (
        f"only {len(reads)} config reads found across the worker tree; the "
        f"read detector has stopped working")


def test_every_worker_side_config_read_is_run_level():
    """
    THE INVARIANT.  A miss here means that value reaches a thread worker and
    not a process worker.
    """
    _reach, reads = _scan()
    missing = {k: sorted(v) for k, v in sorted(reads.items())
               if k not in RUN_LEVEL_CONFIG_ATTRS and k not in EXEMPT}
    assert not missing, (
        "config read inside a worker but NOT in RUN_LEVEL_CONFIG_ATTRS:\n"
        + "\n".join(f"    {k}  (read in {', '.join(v)})"
                    for k, v in missing.items())
        + "\n\n  A spawned process re-imports config fresh and sees only what\n"
          "  RunState carries. These values would take effect under `thread`\n"
          "  and be silently ignored under `process` -- the executor changing\n"
          "  a result, which resolve_sweep_executor documents as the one\n"
          "  thing it must never do.\n"
          "  Add the name to RUN_LEVEL_CONFIG_ATTRS, or to EXEMPT here WITH A\n"
          "  WRITTEN REASON why a worker may safely see a stale value.")


@pytest.mark.parametrize("name", ["SEATING_N_STARTS", "SEATING_START_SEED",
                                  "GATE_TOP_M", "STEM_OVERHANG_BY_T"])
def test_the_names_this_guard_was_written_for(name):
    """The four found by hand; pinned so they cannot quietly drop back out."""
    assert name in RUN_LEVEL_CONFIG_ATTRS


def test_the_check_can_still_fail(tmp_path):
    """
    POSITIVE CONTROL on the ASSERTION.  Plant a worker-side read of a name that
    is not run-level and require the detector to see it.
    """
    planted = (
        "import config\n"
        "def _evaluate_geometry_worker(args):\n"
        "    return _helper()\n"
        "def _helper():\n"
        "    return getattr(config, 'NOT_A_RUN_LEVEL_ATTR', 3)\n"
    )
    tree = ast.parse(planted)
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    seen, frontier = set(), {"_evaluate_geometry_worker"}
    while frontier:
        nm = frontier.pop()
        if nm in seen or nm not in fns:
            continue
        seen.add(nm)
        for c in ast.walk(fns[nm]):
            if (isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                    and c.func.id in fns):
                frontier.add(c.func.id)
    assert "_helper" in seen, "the call-graph walk missed a one-hop call"
    found = set()
    for nm in seen:
        found |= _config_reads(fns[nm])
    assert "NOT_A_RUN_LEVEL_ATTR" in found, (
        "the read detector cannot see a getattr(config, ...) inside a "
        "function the worker calls")
    assert "NOT_A_RUN_LEVEL_ATTR" not in RUN_LEVEL_CONFIG_ATTRS


class TestVerificationDiagnostics:
    """
    A FAILURE THAT NAMES NOTHING IS A FAILURE NOBODY CAN FIX.

    `RunState.verify()` used to diff only the keys present in the parent's
    state, so a KEY-SET difference -- the child's capture carrying a name the
    parent's does not -- reported as `Differing: []`: "the config does not
    match" followed by nothing at all.

    That is not hypothetical.  It happened when `config/run_state.py` was
    EDITED WHILE A SWEEP WAS RUNNING: the parent had imported
    RUN_LEVEL_CONFIG_ATTRS at launch, and the process pool's children spawned
    afterwards and imported the newer list.  The child captured one extra
    attribute, the fingerprints differed, and the message named nothing.  It
    cost a real debugging session and briefly looked like a process-mode bug.

    Production never edits its own source mid-sweep, so this is a DEVELOPMENT
    hazard rather than a shipped defect -- but the undiagnosable message was
    the actual problem, and that is what these pin.
    """

    def _state_and_drift(self, extra_name):
        import config
        import config.run_state as rs
        from config.run_state import RunState
        parent = RunState.capture()
        original = rs.RUN_LEVEL_CONFIG_ATTRS
        setattr(config, extra_name, 7)
        rs.RUN_LEVEL_CONFIG_ATTRS = tuple(original) + (extra_name,)
        return parent, rs, original, config

    def test_an_extra_key_in_the_child_is_NAMED(self):
        import pytest as _pytest
        name = "A_NEWLY_ADDED_ATTR_FOR_TEST"
        parent, rs, original, config = self._state_and_drift(name)
        try:
            with _pytest.raises(RuntimeError) as ei:
                parent.verify()
            msg = str(ei.value)
            assert name in msg, (
                "verify() failed but did not name the extra attribute; this is "
                "the `Differing: []` message all over again")
            assert "NOT in the parent" in msg
        finally:
            rs.RUN_LEVEL_CONFIG_ATTRS = original
            delattr(config, name)

    def test_a_matching_state_still_verifies_clean(self):
        """The other direction: no false alarm on an unchanged config."""
        from config.run_state import RunState
        RunState.capture().verify()      # must not raise
