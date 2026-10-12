# tests/test_guard_exemption_census.py
"""
ONE PLACE THAT LISTS EVERY EXEMPTION ACROSS EVERY MECHANICAL GUARD.

The guards are the thing that has actually worked on this codebase, but each
one carries an allow-list, and an allow-list nobody re-reads is precisely the
artifact this project keeps finding: something that LOOKS reviewed and is not
wired to what it claims.  The exemptions are the guards' own unaudited
surface, and they were spread across five files with no way to see the total.

This file does two things:

  * `test_exemption_census` PRINTS every exemption, per guard, with its stated
    reason.  Run it with -s to read the census.
  * `test_the_total_exemption_count_is_bounded` fails when the total grows past
    a stated ceiling, so the surface cannot expand quietly.  Raising the
    ceiling is a deliberate edit that shows up in review.

If a guard is added, register it here.  A guard whose exemptions are invisible
from this census is a guard whose allow-list nobody re-reads.
"""
from __future__ import annotations

import pytest


def _collect():
    """[(guard, kind, key, reason)] across every registered guard."""
    rows = []

    # 1. sentinel collisions -- the largest surface
    from test_sentinel_collisions import (EXEMPT, ACCEPTED, KNOWN_DEBT,
                                          EXEMPT_C_PREFIXES)
    for key, (reason, _sigs) in sorted(EXEMPT.items()):
        rows.append(("sentinel scanner", "exempt", " :: ".join(key), reason))
    for key, (reason, _sigs) in sorted(ACCEPTED.items()):
        rows.append(("sentinel scanner", "accepted", " :: ".join(key), reason))
    for key, (reason, _sigs) in sorted(KNOWN_DEBT.items()):
        rows.append(("sentinel scanner", "DEBT", " :: ".join(key), reason))
    for pref in EXEMPT_C_PREFIXES:
        rows.append(("sentinel scanner", "exempt-prefix", pref,
                     "UI redraw/dialog handlers: returning None from a failed "
                     "repaint is correct, and a wrong pixel is not a wrong "
                     "number."))

    # 2. ShellProperties completeness contract
    from test_verification_guards import INTENTIONALLY_DEGENERATE
    for field, reason in sorted(INTENTIONALLY_DEGENERATE.items()):
        rows.append(("ShellProperties contract", "exempt", field, reason))

    # 3. source guards: BOM + retired float sentinel
    from test_source_guards import _BOM_EXEMPT, _SENTINEL_EXEMPT
    for rel in sorted(_BOM_EXEMPT):
        rows.append(("source guards (BOM)", "exempt", rel,
                     "carries a BOM deliberately; the guard's own staleness "
                     "check fails if that stops being true."))
    for rel in sorted(_SENTINEL_EXEMPT):
        rows.append(("source guards (float sentinel)", "exempt", rel,
                     "defines the sentinel and its sanctioned conversion, or "
                     "describes the pattern."))

    # 4. slice scanner
    from test_load_case_expansion import _SLICE_SCAN_EXEMPT
    for rel in sorted(_SLICE_SCAN_EXEMPT):
        rows.append(("slice scanner", "exempt", rel,
                     "the scanner's own file, which contains the patterns it "
                     "searches for."))

    # 5. lean-record equivalence has NO allow-list by construction: it asserts
    #    completeness by reflection over what the evaluator actually reads.
    #    Recorded here so its absence is visible rather than an oversight.
    rows.append(("lean-record equivalence", "none", "-",
                 "no allow-list: completeness is derived by AST reflection "
                 "over _evaluate_cfg_at, not by an enumerated list."))
    rows.append(("report smoke test", "none", "-",
                 "no allow-list: it runs the real entry point and fails on any "
                 "exception."))
    # 6. broad-catch warrants.  Registered with NO allow-list on purpose: it is
    #    a positive list (seven sites that must STAY broad) plus a ceiling on
    #    the total, so there is nothing to exempt -- widening is what it bounds.
    # 7. worker-side config reads.  Its allow-list is EXEMPT, and it is empty
    #    today -- which is the point: a non-empty one means somebody decided a
    #    worker may safely see a stale value, and that decision should be read
    #    by whoever runs this census.
    from test_worker_config_reaches_workers import EXEMPT as _WORKER_EXEMPT
    if _WORKER_EXEMPT:
        for _k, _why in sorted(_WORKER_EXEMPT.items()):
            rows.append(("worker config reads", "exempt", _k, _why))
    else:
        rows.append(("worker config reads", "none", "-",
                     "no exemptions: every config value read inside a worker "
                     "is in RUN_LEVEL_CONFIG_ATTRS, so the executor cannot "
                     "change a result."))
    from test_broad_catch_warrants import MUST_STAY_BROAD
    rows.append(("broad-catch warrants", "none",
                 f"{len(MUST_STAY_BROAD)} pinned sites",
                 "no allow-list: a positive list of handlers that must stay "
                 "broad, each carrying its warrant at the handler, plus a "
                 "ceiling on broad handlers in those files."))
    return rows


# Ceiling on the REVIEWED-EXEMPTION surface.  Deliberately just above today's
# count so growth is a visible edit rather than a drift.
MAX_EXEMPTIONS = 32

#: DEBT ratchet.  Debt is NOT an exemption: an exemption carries a warrant
#: ("this is safe, and here is why"), debt carries a deferral ("we will look at
#: this"), and deferrals do not expire on their own.  With the guard green and
#: nothing forcing a decision, an unreviewed site is functionally identical to
#: an exemption nobody wrote a reason for.
#:
#: It is 0 today, and that is the point: a NON-ZERO debt count now always means
#: "something nobody has looked at yet".  It started at 10 -- two were scanner
#: artifacts, two were promoted to EXEMPT on the same-direction test, four were
#: reviewed and moved to ACCEPTED, and two were fixed.
#:
#: THIS NUMBER MAY ONLY GO DOWN.  Adding a site to KNOWN_DEBT fails here until
#: someone either fixes a different one or raises this deliberately -- which is
#: a visible edit with a name on it, not a drift.
MAX_DEBT = 0


def test_exemption_census(capsys):
    """Prints the census. `pytest -s -k census` to read it."""
    rows = _collect()
    with capsys.disabled():
        print("\n\n=== MECHANICAL GUARD EXEMPTION CENSUS ===")
        guard = None
        for g, kind, key, reason in rows:
            if g != guard:
                print(f"\n{g}")
                guard = g
            first = (reason or "").strip().split(". ")[0]
            print(f"   [{kind:13s}] {key}")
            print(f"        {first[:96]}")
        real = [r for r in rows if r[1] != "none"]
        debt = [r for r in real if r[1] == "DEBT"]
        print(f"\n   TOTAL reviewed exemptions: {len(real)}"
              f"   (of which scheduled debt: {len(debt)})")
        print(f"   ceiling: {MAX_EXEMPTIONS}\n")
    assert rows, "the census found no guards; it has stopped being wired up"


def test_the_debt_count_only_ratchets_down():
    """
    The guards' deferrals must shrink, never grow.  A debt entry that sits for
    months is an exemption without a warrant -- it looks reviewed because it is
    on a list, and nothing ever asks again.
    """
    debt = [r for r in _collect() if r[1] == "DEBT"]
    assert len(debt) <= MAX_DEBT, (
        f"{len(debt)} deferred sites, ratchet is {MAX_DEBT}. Debt may only go "
        "DOWN: fix one, or promote it to EXEMPT with a real warrant. Raising "
        "MAX_DEBT is a deliberate edit and needs a reason in the commit.")
    if len(debt) < MAX_DEBT:
        pytest.fail(
            f"debt is down to {len(debt)} -- lower MAX_DEBT to {len(debt)} so "
            "the ratchet holds the new floor. This failure is the ratchet "
            "working: progress must be locked in, or it drifts back.")


def test_the_total_exemption_count_is_bounded():
    """
    The guards' own unaudited surface must not grow quietly.  Raising
    MAX_EXEMPTIONS is a deliberate edit that shows up in review -- which is
    the whole difference between a reviewed allow-list and an accumulated one.
    """
    real = [r for r in _collect() if r[1] != "none"]
    assert len(real) <= MAX_EXEMPTIONS, (
        f"{len(real)} reviewed exemptions across the guards, ceiling "
        f"{MAX_EXEMPTIONS}. Either remove one, or raise the ceiling "
        "deliberately and say why in the commit.")


def test_every_exemption_anywhere_states_a_reason():
    """One rule across all guards, not per-guard."""
    thin = [(g, k, r) for g, kind, k, r in _collect()
            if kind != "none" and (not r or len(r.strip()) < 40)]
    assert not thin, (
        "exemptions without a real stated reason:\n  "
        + "\n  ".join(f"{g} :: {k}" for g, k, _r in thin))


def test_debt_is_a_minority_of_the_surface():
    """
    Scheduled debt is fine; debt OUTNUMBERING reviewed entries would mean the
    guards are mostly deferring rather than enforcing.

    ACCEPTED is NOT debt: an accepted site has been looked at and its risk
    knowingly taken (it degrades output, not a design number). Debt means
    UNDECIDED, which is why it can and should reach zero.
    """
    real = [r for r in _collect() if r[1] != "none"]
    debt = [r for r in real if r[1] == "DEBT"]
    assert len(debt) <= len(real) / 2, (
        f"{len(debt)} of {len(real)} exemptions are unfixed debt; the guards "
        "are deferring more than they enforce.")
