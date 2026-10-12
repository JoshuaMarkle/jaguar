"""
Source-level guards for two mistakes that have each recurred.

Both are the same species: a defect whose symptom appears far from its cause,
so fixing the instance never fixes the class.  These scan the tree for the
PATTERN, the way test_load_case_expansion scans for hand-written seating
slices, because in both cases a function-name scan already missed an instance.
"""
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _sources():
    """Every committed .py in the project, excluding caches and venvs."""
    skip = {"__pycache__", ".git", ".venv", "venv", "build", "dist", "node_modules"}
    for p in _ROOT.rglob("*.py"):
        if not any(part in skip for part in p.parts):
            yield p


# ── 1. Byte-order marks ──────────────────────────────────────────────────────
#
# A BOM is invisible in every editor and diff, and it does not break imports --
# Python's source loader strips it.  What it breaks is any tool that reads the
# file as plain UTF-8 and parses it, so the error surfaces in a DIFFERENT file
# from the one that changed:
#
#     SyntaxError: invalid non-printable character U+FEFF
#
# reported by test_gunwale_section, three files away from the edit that caused
# it.  This has happened twice, both times from a well-meaning `utf-8-sig`
# write, which is the encoding that ADDS a BOM on output.
#
# EMPTY, and that is an improvement.  main.py was the sole exception: it had
# carried a BOM since long before these guards, and the exemption existed for
# DIFF COST -- rewriting a 2,600-line file for no behavioural gain -- never for
# behaviour.  A batch edit stripped it (a `utf-8-sig` read paired with a
# `utf-8` write), so that cost is already spent and main.py now conforms like
# every other file.  The guard covers it rather than pretending it is a known
# exception, and this list is empty: no file may carry a BOM.
_BOM_EXEMPT = set()


def test_no_source_file_carries_a_byte_order_mark():
    offenders = []
    for p in _sources():
        rel = p.relative_to(_ROOT).as_posix()
        if rel in _BOM_EXEMPT:
            continue
        if p.read_bytes()[:3] == b"\xef\xbb\xbf":
            offenders.append(rel)
    assert not offenders, (
        "byte-order mark(s) found: " + ", ".join(sorted(offenders)) + ".\n"
        "A BOM is invisible in diffs and breaks any tool that ast.parse()s the "
        "file as plain UTF-8, so it surfaces as a SyntaxError in an unrelated "
        "test.  Write these files with encoding='utf-8', never 'utf-8-sig' -- "
        "the -sig variant is optional on READ but always emits a BOM on WRITE.")


def test_the_bom_exemption_list_is_honest():
    """An exemption for a file that no longer has a BOM should be deleted."""
    stale = [rel for rel in _BOM_EXEMPT
             if (_ROOT / rel).exists()
             and (_ROOT / rel).read_bytes()[:3] != b"\xef\xbb\xbf"]
    assert not stale, (
        f"{stale} no longer carries a BOM; drop it from _BOM_EXEMPT so the "
        f"guard covers it rather than pretending it is a known exception")


# ── 2. The REJECTED sentinel ─────────────────────────────────────────────────
#
# REJECTED is not a number: every arithmetic and ordering operation on it
# raises TypeError naming the mistake.  That is the design -- it is what ended
# a run of defects where a 1e6 float penalty was read as a mass (a +4.71 in
# phantom stability gain, a -999842.82 lb weight delta).
#
# The failure mode that keeps returning is a consumer that asks a NUMERIC
# question of a possibly-rejected value without first asking `is_rejected`:
#
#     np.isnan(w)                 <- TypeError, not an answer
#     if w < best: ...            <- TypeError
#     assert result == 1e6        <- the retired contract
#
# Three such sites survived into the suite (test_multizone_bound x4,
# test_p0::test_failure_penalty, test_paddler_jacobian's objective).  The
# sentinel was working exactly as designed in all three; the call sites were
# not.  This scan is for the pattern, wherever it lives.

_SENTINEL_PATTERNS = [
    (r"==\s*1e6\b", "== 1e6 -- the retired float-penalty contract; "
                    "use structural.is_rejected()"),
    (r"\b1e6\s*==", "1e6 == -- the retired float-penalty contract"),
    (r"INFEASIBLE_PENALTY\s*==", "comparing against INFEASIBLE_PENALTY -- "
                                 "that constant exists only for the SciPy "
                                 "boundary, via as_optimizer_penalty()"),
]

#: The one sanctioned conversion, and the guard's own definitions.
_SENTINEL_EXEMPT = {
    "structural.py",                  # defines the sentinel and the conversion
    "tests/test_source_guards.py",    # this file describes the patterns
}


def test_no_retired_float_penalty_comparisons():
    offenders = []
    for p in _sources():
        rel = p.relative_to(_ROOT).as_posix()
        if rel in _SENTINEL_EXEMPT:
            continue
        for ln, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]          # comments may discuss it
            for pat, why in _SENTINEL_PATTERNS:
                if re.search(pat, code):
                    offenders.append(f"{rel}:{ln}  {why}\n      {line.strip()}")
    assert not offenders, (
        "comparison(s) against the retired 1e6 infeasibility penalty:\n  "
        + "\n  ".join(offenders))


def test_isnan_is_never_asked_before_is_rejected():
    """
    `np.isnan(w)` on the sentinel raises instead of answering, so any
    feasibility guard combining the two must test `is_rejected` FIRST -- and
    because `or` short-circuits, order is the whole fix.
    """
    bad = []
    for p in _sources():
        rel = p.relative_to(_ROOT).as_posix()
        if rel in _SENTINEL_EXEMPT:
            continue
        for ln, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if not re.search(r"isnan\s*\(", code):
                continue
            # Only guards that ALSO know about rejection are in scope; a plain
            # isnan on a known float is fine.
            if "is_rejected" not in code:
                continue
            if code.index("is_rejected") > code.index("isnan"):
                bad.append(f"{rel}:{ln}  isnan() is asked before is_rejected()"
                           f"\n      {line.strip()}")
    assert not bad, (
        "the sentinel must be excluded BEFORE any numeric test, or isnan() "
        "raises TypeError instead of answering:\n  " + "\n  ".join(bad))


@pytest.mark.parametrize("op", ["<", ">", "<=", ">="])
def test_the_sentinel_still_refuses_ordering(op):
    """
    The guards above are only worth having while the sentinel actually bites.
    If REJECTED ever became orderable, every scan here would pass over code
    that silently treats an infeasible design as a very heavy one.
    """
    from structural import REJECTED
    with pytest.raises(TypeError, match="REJECTED"):
        eval(f"REJECTED {op} 1.0", {"REJECTED": REJECTED})


def test_the_sentinel_still_refuses_isnan():
    import numpy as np
    from structural import REJECTED
    with pytest.raises(TypeError):
        np.isnan(REJECTED)


# ── 3. The banner must not be able to lie ────────────────────────────────────
#
# A sweep prints its executor, its run id, its config fingerprint and its code
# version.  Those exist to answer "what produced this output?" after the fact,
# so a banner that disagrees with what actually ran is worse than no banner --
# it converts an unanswered question into a confidently wrong answer.  A
# session was lost to exactly that: a fingerprint labelled `run` that was
# really a CONFIG hash, so two different sweeps printed the same "run id".

def test_executor_is_resolved_in_exactly_one_place():
    """
    The banner and the pool must come from ONE resolution, or they can drift.
    """
    import inspect
    import re
    import main

    src = inspect.getsource(main.run_grand_optimization)
    # The pool is chosen from _kind, and _kind comes from the resolver.
    assert "resolve_sweep_executor()" in src, (
        "run_grand_optimization no longer resolves the executor through "
        "resolve_sweep_executor(); the banner and the pool can now disagree")
    # No second, independent read of the setting inside the sweep.
    strays = [ln.strip() for ln in src.splitlines()
              if "SWEEP_EXECUTOR" in ln and "resolve_sweep_executor" not in ln
              and not ln.strip().startswith("#")]
    assert not strays, (
        f"the executor is read a second time inside the sweep: {strays}; "
        f"resolve through resolve_sweep_executor() so the banner cannot drift "
        f"from the pool that actually runs")


def test_banner_prints_the_resolved_executor():
    """The printed name is the same value the pool is built from."""
    import inspect
    import main

    src = inspect.getsource(main.run_grand_optimization)
    banner = [ln for ln in src.splitlines() if "executor \u00b7" in ln
              or "executor ·" in ln]
    assert banner, "the sweep banner no longer names the executor"
    assert any("{_kind}" in ln for ln in banner), (
        "the banner prints a literal executor name instead of the resolved "
        "_kind; it would keep saying one thing while the pool did another")
    pool = [ln for ln in src.splitlines() if "ProcessPoolExecutor" in ln]
    # The selection moved into the `_PoolFor(kind)` helper when the pool phase
    # became a re-runnable closure for the stall watchdog, so the comparison
    # now reads `kind == "process"` against the parameter rather than `_kind`
    # against the local.  The PROPERTY is unchanged and is what this asserts:
    # the class is chosen from the same resolved value the banner prints.
    assert any('kind == "process"' in ln for ln in pool), (
        "the pool is selected from something other than the resolved executor")
    # And the fallback must SAY so.  The banner is printed once, before the
    # pool runs; if the watchdog restarts on threads, a run that printed
    # "process executor" would otherwise carry that as its only record.
    assert any("restarted after a stall" in ln for ln in src.splitlines()), (
        "the stall fallback does not re-state the executor, so the run's "
        "banner would keep claiming an executor the pool is no longer using")


@pytest.mark.parametrize("value,expect", [
    ("thread", "thread"), ("process", "process"),
    ("  Process ", "process"), ("THREAD", "thread"),
])
def test_resolver_normalises(value, expect):
    import main

    class _Cfg:
        SWEEP_EXECUTOR = value
    assert main.resolve_sweep_executor(_Cfg) == expect


@pytest.mark.parametrize("bad", ["processes", "fork", "gpu", "threadpool"])
def test_resolver_rejects_unknown(bad):
    """Falling back would let someone wait out a thread run believing
    otherwise -- the failure this whole banner exists to prevent."""
    import main

    class _Cfg:
        SWEEP_EXECUTOR = bad
    with pytest.raises(ValueError, match="thread"):
        main.resolve_sweep_executor(_Cfg)


def test_executor_is_not_a_design_parameter():
    """
    It is a MACHINE choice.  If it ever became run-level, a saved design preset
    could behave differently on someone else's hardware -- and the RunState
    fingerprint would start changing for reasons that are not design.
    """
    from config.run_state import RUN_LEVEL_CONFIG_ATTRS
    assert "SWEEP_EXECUTOR" not in RUN_LEVEL_CONFIG_ATTRS
    assert "SWEEP_MAX_WORKERS" not in RUN_LEVEL_CONFIG_ATTRS


def test_run_id_is_unique_per_invocation():
    """
    The fingerprint was mislabelled `run` and could not do this: it hashes the
    run-level CONFIG, so two sweeps over different grids printed the same value
    (grid extent is not run-level config).  Correct as a config id, useless as
    a run id.
    """
    import main
    ids = {main._new_run_id() for _ in range(200)}
    assert len(ids) == 200, "run ids collided; they cannot identify a run"
