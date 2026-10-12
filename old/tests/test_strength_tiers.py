# tests/test_strength_tiers.py
"""
Strength-tier hierarchy and the density floor derived from it.

Three DIFFERENT tests, not three names for one:
    FLEXURAL     modulus of rupture, 3-point bending
    SPLIT-TENSILE  indirect, cylinder loaded diametrically
    COMPRESSIVE  cylinder compression, tension by correlation

Preference is flexural -> split-tensile -> compressive-derived, because the
bending gates compute sigma = M*c/I -- the extreme-fibre stress in flexure --
which is exactly what MOR measures.  The other two are conversions.

These pin the MECHANISM, because the shipped lab data cannot exercise it:
fr_psi is 0.0 in both rows, so the flexural tier is dormant until someone
enters a 3-point-bending result.
"""
from __future__ import annotations

import numpy as np
import pytest

from mix_database import MixDatabase


@pytest.fixture
def db(tmp_path):
    d = MixDatabase(str(tmp_path / "t.db"))
    for row in list(d.get_lab_results()):
        try:
            d.delete_lab_results([row["id"]])
        except Exception:
            pass
    return d


def _add(d, density, fc, ft, fr=0.0):
    # curing_days is now REQUIRED for a row to enter any fit: an unrecorded
    # age is inadmissible, because a break of unknown age could be a 7-day
    # break and those are exactly what the early-age exclusion holds out.
    # These tests exercise TIER FITTING, not age policy, so they log breaks at
    # the design age -- which is what a real 28-day cylinder record carries.
    d.add_lab_result(density_pcf=density, fc_psi=fc, ft_psi=ft, fr_psi=fr,
                     test_date="2026-01-01", batch_id="t",
                     curing_days=d.design_age_days())


def test_split_tensile_is_used_when_no_mor_exists(db):
    _add(db, 65.0, 900.0, 150.0, 0.0)
    _add(db, 72.0, 1750.0, 350.0, 0.0)
    assert db.fr_regression is None, "no MOR points -> no flexural fit"
    _floor, basis = db.tensile_viability_floor()
    assert "split-tensile" in basis


def test_a_synthetic_mor_point_flips_the_tier_to_flexural(db):
    """
    THE mechanism test.  The shipped data has fr_psi = 0.0 everywhere, so the
    flexural tier is dormant; adding two bending results must switch the tier
    in force AND the floor's basis.
    """
    _add(db, 65.0, 900.0, 150.0, 0.0)
    _add(db, 72.0, 1750.0, 350.0, 0.0)
    before_floor, before_basis = db.tensile_viability_floor()
    assert db.fr_regression is None
    assert "split-tensile" in before_basis

    _add(db, 66.0, 950.0, 160.0, 240.0)      # MOR > split-tensile, as expected
    _add(db, 73.0, 1800.0, 360.0, 520.0)
    db._train_regression_curves()

    assert db.fr_regression is not None, "two MOR points must produce a fit"
    after_floor, after_basis = db.tensile_viability_floor()
    assert "flexural" in after_basis, after_basis
    assert after_basis != before_basis
    # and the floor must MOVE -- the property that matters, not its value
    assert after_floor != before_floor


def test_tiers_are_fitted_separately_not_pooled(db):
    """
    MOR and split-tensile measure different things; pooling would produce a fit
    describing neither.  The flexural fit must see ONLY the fr points.
    """
    _add(db, 65.0, 900.0, 150.0, 0.0)
    _add(db, 72.0, 1750.0, 350.0, 0.0)
    _add(db, 66.0, 950.0, 160.0, 240.0)
    _add(db, 73.0, 1800.0, 360.0, 520.0)
    db._train_regression_curves()
    assert db._regression_n_fr == 2, "only the fr>0 rows may enter the MOR fit"
    assert db._regression_n == 4, "the ft fit uses every lab row"
    # the two fits must differ -- pooling would collapse them
    assert not np.allclose(db.fr_regression, db.ft_regression)


def test_floor_follows_the_data_not_a_constant(db):
    """
    The floor must move when the regression moves.  A 'derived' floor keyed to
    a frozen constant is the looks-derived-but-isn't pattern.
    """
    _add(db, 65.0, 900.0, 150.0, 0.0)
    _add(db, 72.0, 1750.0, 350.0, 0.0)
    f1, _ = db.tensile_viability_floor()
    _add(db, 61.5, 700.0, 40.0, 0.0)          # a weak light batch shifts the fit
    db._train_regression_curves()
    f2, _ = db.tensile_viability_floor()
    assert f1 != f2, "floor did not follow a changed regression"


def test_floor_is_not_taken_from_the_compressive_fit(db):
    """
    The regression that matters is TENSILE.  Built so f'c and f_t cross zero far
    apart: a compressive-keyed floor would sit well below any usable tension.
    """
    _add(db, 60.0, 500.0, 5.0, 0.0)
    _add(db, 75.0, 2000.0, 350.0, 0.0)
    db._train_regression_curves()
    sc, ic = db.fc_regression
    st, it = db.ft_regression
    fc_floor = (10.0 - ic) / sc
    floor, basis = db.tensile_viability_floor()
    assert abs(floor - fc_floor) > 0.5, (
        f"floor {floor} tracks the COMPRESSIVE crossing {fc_floor}")
    assert floor == pytest.approx(max(round((1.0 - it) / st, 1), 40.0), abs=0.11)


def test_falls_back_through_the_documented_tier_order(db):
    """
    With no LAB rows the fit falls back to the `mixes` design-target table
    (a fresh DB seeds three), and only with neither does it reach the
    hardcoded empirical floor.  Either way the basis must SAY which it used --
    a floor whose provenance is invisible is the pattern this replaces.
    """
    floor, basis = db.tensile_viability_floor()
    assert 40.0 <= floor <= 80.0, floor
    assert any(k in basis for k in ("split-tensile", "flexural", "fallback")), basis
    assert db._regression_source in ("mixes", "lab_results", "empirical")


# ── THE ANCHORING INVARIANT ─────────────────────────────────────────────────
def test_changing_the_floor_never_changes_the_surviving_densities(db, monkeypatch):
    """
    THE invariant that a value assertion would have missed.

    The floor must FILTER an anchored grid, never DEFINE it.  Implemented as
    np.arange(floor, ...) it re-phased every point: raising the floor
    57.5 -> 59.3 turned 57.5/59.5/.../71.5 into 59.3/61.3/.../73.3, the winning
    stack [71.5, 63.5, 71.5] stopped existing, and the winner moved +0.53 lb
    (167.5118 -> 168.0412).  Pruning a dead band is only free if every
    surviving density keeps its value.
    """
    _add(db, 65.0, 900.0, 150.0, 0.0)
    _add(db, 72.0, 1750.0, 350.0, 0.0)

    def grid_at(floor_pcf):
        monkeypatch.setattr(db, "tensile_viability_floor",
                            lambda min_ft_psi=1.0: (floor_pcf, "forced"))
        combos = db.get_structural_sweep_grid(max_density=80.0, n_mixes=2, step=2.0)
        return sorted({d for c in combos for d in c})

    low = grid_at(50.0)
    high = grid_at(62.0)
    assert high, "raising the floor emptied the grid"
    # every surviving density must be one the lower floor also offered
    assert set(high) <= set(low), (
        f"raising the floor INVENTED densities {sorted(set(high) - set(low))}; "
        f"the grid was re-phased instead of filtered")
    # and it must actually have removed the dead ones
    assert min(high) >= 62.0 - 1e-9
    assert set(low) - set(high), "raising the floor removed nothing"


def test_the_grid_phase_is_fixed_not_derived(db, monkeypatch):
    """
    Two different floors below the same grid point must yield identical grids.
    If the phase followed the floor, these would differ.
    """
    _add(db, 65.0, 900.0, 150.0, 0.0)
    _add(db, 72.0, 1750.0, 350.0, 0.0)

    def grid_at(floor_pcf):
        monkeypatch.setattr(db, "tensile_viability_floor",
                            lambda min_ft_psi=1.0: (floor_pcf, "forced"))
        combos = db.get_structural_sweep_grid(max_density=80.0, n_mixes=2, step=2.0)
        return sorted({d for c in combos for d in c})

    assert grid_at(58.0) == grid_at(59.4), (
        "the grid phase moved with the floor -- it must be anchored")
