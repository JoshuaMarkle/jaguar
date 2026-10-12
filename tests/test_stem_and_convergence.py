# tests/test_stem_and_convergence.py
"""
Three fixes that all share one shape: a physical quantity that was being
decided by the analysis grid instead of by the hull.

  * the stem stations took A = 1e-6 (a step of the full section area) silently
  * the tip cantilever mass was zero at n<=1 and only reached its physical
    value as n grew, so the same hull gained mass between 321 and 641 stations
  * _struct_rocker_ft() returned 0.0 when the overhang was never published,
    so a direct evaluator call modelled a hull with no stem and said nothing
"""
from __future__ import annotations

import os

import numpy as np
import pytest

from geometry import parse_godzilla_offsets, calculate_shell_properties
import structural

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OFF1 = os.path.join(REPO, "off1.mlt")
L, DRAFT, DEPTH, T = 20.00984316, 4.5760260931, 14.57, 0.375
ROCKER_FT = 1.045163 / 12.0

pytestmark = pytest.mark.skipif(not os.path.exists(OFF1),
                                reason="needs the real offset file")


def _props(n):
    xr, zr, ym = parse_godzilla_offsets(OFF1, L_ft=L, draft_in=DRAFT,
                                        mold_depth_in=DEPTH, n_stations=n)
    return xr, calculate_shell_properties(xr, zr, ym, T)


# ── the stem taper ────────────────────────────────────────────────────────

@pytest.mark.parametrize("n", [None, 161, 641])
def test_stem_station_is_tapered_not_zeroed(n):
    """A(0) must be a real taper off the hull, never the 1e-6 placeholder."""
    x, p = _props(n)
    for i in (0, len(x) - 1):
        assert p.A_in2[i] > 1.0, "stem station still carries the EPS placeholder"
    # It tapers DOWN to the stem: below its neighbour, not equal to it.
    assert p.A_in2[0] < p.A_in2[1]
    assert p.A_in2[-1] < p.A_in2[-2]


def test_the_taper_is_extrapolated_from_the_two_nearest_stations():
    x, p = _props(641)
    slope = (p.A_in2[2] - p.A_in2[1]) / (x[2] - x[1])
    assert p.A_in2[0] == pytest.approx(p.A_in2[1] - slope * (x[1] - x[0]),
                                       rel=1e-9)


def test_the_degenerate_branch_is_loud(caplog):
    """It may never absorb a station silently again."""
    import logging
    with caplog.at_level(logging.WARNING, logger="geometry"):
        _props(161)
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "degenerate stem station" in msg
    assert "TAPERED" in msg
    assert "0.0" in msg          # names the x location


# ── tip mass is geometry, not grid ────────────────────────────────────────

def _tip_mass(n_st, rocker_ft=ROCKER_FT):
    x = np.linspace(0.0, 20.0, n_st)
    dx = 20.0 / (n_st - 1)
    w = np.full(n_st, 2.0)
    xe, we, wide, i0, i1 = structural._append_rocker_overhang(
        x, dx, w, np.full(n_st, 1.0), rocker_ft)
    return float(np.sum(we[:i0])) * dx if i0 else 0.0


def test_tip_mass_is_the_same_physical_quantity_at_every_resolution():
    """
    THE bug: tip mass was w0*dx*(n-1)/2 -- zero at n=1 -- so the cantilever
    switched ON between 321 and 641 stations on an unchanged hull, relieving
    sagging and flattering every drift measurement that ended at 641.
    """
    target = 0.5 * 2.0 * ROCKER_FT          # w0 * rocker / 2, from geometry
    masses = [_tip_mass(n) for n in (41, 81, 161, 321, 641, 1281)]
    for m in masses:
        assert m == pytest.approx(target, rel=1e-9)
    assert max(masses) - min(masses) < 1e-12


def test_a_real_overhang_never_rounds_away_to_nothing():
    """At 41 stations round(rocker/dx) is 0; the mass must survive anyway."""
    n, carries = structural.overhang_stations(ROCKER_FT, 20.0 / 40)
    assert n >= 1 and carries
    assert _tip_mass(41) > 0.0


def test_zero_rocker_is_still_a_no_op():
    assert structural.overhang_stations(0.0, 0.125) == (0, False)
    assert _tip_mass(161, rocker_ft=0.0) == 0.0


# ── the silent-zero rocker ────────────────────────────────────────────────

def test_struct_rocker_raises_when_the_overhang_was_never_published(monkeypatch):
    """A direct evaluator call must not inherit a silent hull-with-no-stem."""
    import config, main
    monkeypatch.delattr(config, "STEM_OVERHANG_IN", raising=False)
    monkeypatch.delattr(config, "STRUCT_ROCKER_IN", raising=False)
    with pytest.raises(RuntimeError, match="stem overhang is not published"):
        main._struct_rocker_ft()


def test_struct_rocker_reads_the_published_value(monkeypatch):
    import config, main
    config.STEM_OVERHANG_IN = 1.045163   # conftest snapshot restores it
    assert main._struct_rocker_ft() == pytest.approx(1.045163 / 12.0)


# ── the stem carries MASS but never STRESS ────────────────────────────────

def test_the_tapered_stem_is_still_excluded_from_the_stress_gates():
    """
    The taper and the tip-degeneracy exclusion have to coexist, and the first
    attempt broke the second: scaling yhalf alongside cum_area lifted the stem
    past BOTH halves of structural_station_mask, and the tip then governed the
    Kupfer gate at util 0.4513 where the contract requires 0.

    Mass and stress are separate questions here.  The stem holds material, so
    cum_area must be real; it has ~zero half-breadth, so transverse hull-girder
    bending does not describe it and sigma_y ~ 1/b^2 would diverge.  yhalf
    stays at the floor, which is both the physics and what keeps the exclusion.
    """
    from structural import structural_station_mask, DEGENERATE_SECTION_A_IN2
    x, p = _props(161)
    sheer = p.c_bot + p.c_top
    mask = structural_station_mask(p.cum_area_in2, p.z_grid_in, sheer,
                                   p.yhalf_in)

    assert not mask[0] and not mask[-1], (
        "a tapered stem station is being treated as real structure; the "
        "transverse gates will divide a finite moment by a vanishing I_lat")
    assert mask[1:-1].all(), "a real mid-hull station was excluded"

    # ...and it is the BREADTH test doing the excluding, because the taper
    # deliberately lifts the stem past the AREA test.
    from structural import _cum_at
    A_full = _cum_at(p.cum_area_in2, p.z_grid_in, sheer)
    assert A_full[0] > DEGENERATE_SECTION_A_IN2, \
        "the stem should carry real area now -- that is the taper's whole point"
    assert p.yhalf_in[0].max() < 1e-3, \
        "the stem must keep a vanishing half-breadth or the exclusion lapses"


def test_the_overhang_note_is_not_emitted_per_evaluation(caplog):
    """
    A warning inside _append_rocker_overhang fires once per configuration per
    load case: a 35-configuration sweep emitted it 143,479 times.  That is not
    a warning, it is a way to bury one.  The note belongs to the RUN, and
    run_grand_optimization prints it once in the overhang banner.
    """
    import logging
    x = np.linspace(0.0, 20.0, 161)
    dx = 20.0 / 160
    with caplog.at_level(logging.WARNING, logger="structural"):
        for _ in range(50):
            structural._append_rocker_overhang(
                x, dx, np.full(161, 2.0), np.full(161, 1.0), ROCKER_FT)
    noisy = [r for r in caplog.records if "overhang" in r.getMessage().lower()]
    assert not noisy, (
        f"{len(noisy)} overhang warning(s) from 50 evaluations; this runs in "
        "the sweep's hot path and must stay silent")
