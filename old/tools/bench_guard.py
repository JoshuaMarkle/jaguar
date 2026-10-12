"""
Make a benchmark declare its seating mode, and prove it.

WHY THIS IS CODE AND NOT A NOTE.  docs/FOLLOWUPS.md entry 8 already says that
any measurement of the seating multi-start MUST unset FIXED_PADDLER_POSITIONS
and state that it did.  That instruction was then violated three times --
twice while citing the entry -- because every benchmark harness was copied from
tests/conftest.py::small_sweep, which fixes seating DELIBERATELY to skip the
placement solve.  The copy carried the fixed-seating block along silently, and
the resulting runs looked completely ordinary: they completed, produced the
right winner, and said nothing about the path they had never taken.

The pattern this project keeps relearning is that a note does not hold and a
scanner does (the hand-written-seating-slice scan, the BOM guard).  So the
declaration is now a function call that ASSERTS the condition and PRINTS the
evidence.  A harness cannot claim a seating mode it is not in, because the
claim and the check are the same statement.
"""
from __future__ import annotations

SEATING_OPTIMISED = "optimised"
SEATING_FIXED = "fixed"


def declare_seating(mode: str, *, verbose: bool = True) -> dict:
    """
    Assert the live config matches ``mode`` and print the evidence.

    mode = SEATING_OPTIMISED -- the placement solve RUNS.  Requires
    FIXED_PADDLER_POSITIONS to be unset/empty; this is what any measurement of
    seating multi-start work must use.

    mode = SEATING_FIXED -- seating is pinned and the placement solve is
    SKIPPED.  Legitimate for measuring the mix grid, and it is what
    conftest's small_sweep uses, but a run in this mode may not be described as
    exercising the seating solve.

    Returns the observed state so a harness can record it alongside its result.
    """
    import config

    if mode not in (SEATING_OPTIMISED, SEATING_FIXED):
        raise ValueError(f"mode must be {SEATING_OPTIMISED!r} or "
                         f"{SEATING_FIXED!r}, got {mode!r}")

    fixed = getattr(config, "FIXED_PADDLER_POSITIONS", None)
    variance = getattr(config, "PADDLER_POS_VARIANCE_FT", None)
    observed = SEATING_FIXED if fixed else SEATING_OPTIMISED

    state = {
        "declared": mode,
        "observed": observed,
        "FIXED_PADDLER_POSITIONS": (dict(fixed) if isinstance(fixed, dict)
                                    else fixed),
        "PADDLER_POS_VARIANCE_FT": variance,
    }

    if verbose:
        print(f"  [seating] declared={mode}  observed={observed}  "
              f"FIXED_PADDLER_POSITIONS={fixed!r}  "
              f"PADDLER_POS_VARIANCE_FT={variance!r}")

    if observed != mode:
        raise AssertionError(
            f"this harness declared seating {mode!r} but the live config is "
            f"{observed!r}: FIXED_PADDLER_POSITIONS={fixed!r}.\n"
            f"A run with seating FIXED never executes the placement solve, so "
            f"it cannot support any claim about seating multi-starts, their "
            f"cost, or parallelising them -- see docs/FOLLOWUPS.md entry 8, "
            f"where this exact mistake is recorded three times.\n"
            f"Either unset config.FIXED_PADDLER_POSITIONS, or declare "
            f"{SEATING_FIXED!r} and drop the claim.")
    return state


def count_seating_starts() -> int:
    """
    How many multi-starts the placement solve will run.

    Printed by harnesses so 'the seating solve executed' is evidenced by a
    number rather than by the sweep merely having finished.
    """
    import main
    return len(main.seating_start_points(20.0))
