# tests/test_fit_guard.py
"""
The strength-fit sanity guard.

A fit that contradicts how concrete behaves is an error in the DATA, and it
must not become the tier the sweep gates on.  This is the guard that stopped
one: a modulus-of-rupture fit at 6.9x the compressive strength, with a negative
slope, drove a whole design to the lightest mixes on the grid and made the
governing check read 0.08 where the honest allowable put it near 2.0.

What is tested here:

  * each trigger fires on the numbers that produced it
  * a blocked tier is genuinely OUT OF FORCE — get_mix_properties falls back
  * REJECT is the default with no user present (this file IS that case)
  * an override releases the tier AND leaves a trace in every sink
  * an override is per-process and never persists into a fresh open

Every test uses a THROWAWAY database file — never the user's live
concrete_mixes.db.
"""
from __future__ import annotations

import pytest

from mix_database import MixDatabase


@pytest.fixture()
def db(tmp_path):
    d = MixDatabase(db_path=str(tmp_path / "guard.db"))
    # Two sound compressive/tensile points, so only the tier under test is bad.
    # curing_days is REQUIRED for a row to enter any fit -- an unrecorded age
    # is inadmissible, because a break of unknown age could be a 7-day break.
    # _add_beam below already recorded it; these two did not, and the whole
    # fixture was silently building rows production ignores.
    _age = d.design_age_days()
    d.add_lab_result("2026-07-10", 65.0, fc_psi=917.9, ft_psi=158.2,
                     curing_days=_age, refit=False)
    d.add_lab_result("2026-07-10", 71.9, fc_psi=1750.0, ft_psi=350.0,
                     curing_days=_age)
    yield d
    d.close()


def _add_beam(db, density, fr, batch="beam", refit=True):
    return db.add_lab_result("2026-07-18", density, fr_psi=fr, batch_id=batch,
                             specimen_type="beam", curing_days=28, refit=refit)


def _codes(db):
    return {f.code for f in db.blocking_findings()}


# ══════════════════════════════════════════════════════════════════════════
#  The three triggers
# ══════════════════════════════════════════════════════════════════════════

def test_flexural_fit_above_compressive_is_blocked(db):
    """The real failure: MOR at multiples of f'c. It cannot gate a design."""
    _add_beam(db, 62.98, 3306.0, "Perline 6/20 A", refit=False)
    _add_beam(db, 77.27, 3456.6, "Perline + Mesh 6/27 B")
    assert "fr_exceeds_fc" in _codes(db)
    finding = next(f for f in db.blocking_findings()
                   if f.code == "fr_exceeds_fc")
    assert "x the compressive strength" in finding.headline
    assert finding.row_ids                      # names the rows to go fix


def test_negative_slope_is_blocked(db):
    """Lighter concrete is not stronger. That is an error, not a data point."""
    _add_beam(db, 62.0, 400.0, "light", refit=False)
    _add_beam(db, 78.0, 100.0, "heavy")
    assert "negative_slope" in _codes(db)
    f = next(f for f in db.blocking_findings() if f.code == "negative_slope")
    assert "NEGATIVE slope" in f.headline and "f_r =" in f.headline


def test_weak_fit_on_few_points_is_blocked(db):
    """R² below the floor on a handful of points is scatter, not a trend."""
    for i, (d_, fr) in enumerate([(62.0, 60.0), (68.0, 150.0), (74.0, 65.0)]):
        _add_beam(db, d_, fr, f"scatter-{i}", refit=(i == 2))
    assert db.r_squared_fr < db.FIT_MIN_R2
    assert "weak_fit" in _codes(db)


def test_a_sound_flexural_fit_is_left_alone(db):
    """MOR rising with density at a credible fraction of f'c: no findings."""
    for i, (d_, fr) in enumerate([(65.0, 120.0), (71.9, 230.0), (68.0, 175.0)]):
        _add_beam(db, d_, fr, f"good-{i}", refit=(i == 2))
    assert db.blocking_findings() == []
    assert db.fr_regression is not None
    assert db.get_mix_properties(68.0)["flexural_source"] == "fr"


# ══════════════════════════════════════════════════════════════════════════
#  Blocked means OUT OF FORCE, and reject is the default
# ══════════════════════════════════════════════════════════════════════════

def test_a_blocked_tier_does_not_reach_the_bending_gate(db):
    _add_beam(db, 62.98, 3306.0, "A", refit=False)
    _add_beam(db, 77.27, 3456.6, "B")
    assert db._raw_fits["fr"][0] is not None      # it was measured
    assert db.fr_regression is None               # and it is not in force
    p = db.get_mix_properties(64.0)
    assert p["flexural_source"] == "ft"
    assert p["strength_flexural"] == pytest.approx(p["strength_t"])
    assert "flexural MOR" not in db.tensile_viability_floor()[1]


def test_reject_is_the_default_with_no_user_present(db):
    """This test file is the headless case. Nothing releases the fit."""
    _add_beam(db, 62.98, 3306.0, "A", refit=False)
    _add_beam(db, 77.27, 3456.6, "B")
    assert db.blocked_tiers == {"fr"}
    assert db.fit_override_record() is None
    assert any("BLOCKED" in ln for ln in db.fit_guard_banner())


def test_a_blocked_fc_does_not_drag_ft_to_empirical(db):
    """Per-tier fallback: one bad tier must not take a sound one with it."""
    ft_before = db.get_mix_properties(70.0)["strength_t"]
    db.fc_regression = None                       # simulate a blocked f'c
    assert db.get_mix_properties(70.0)["strength_t"] == pytest.approx(ft_before)


# ══════════════════════════════════════════════════════════════════════════
#  Override: releases the tier, and leaves a trace everywhere
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def overridden(db):
    _add_beam(db, 62.98, 3306.0, "Perline 6/20 A", refit=False)
    _add_beam(db, 77.27, 3456.6, "Perline + Mesh 6/27 B")
    findings = db.blocking_findings()
    db.set_fit_override(findings, source="interactive override (test)")
    return db


def test_override_puts_the_tier_back_in_force(overridden):
    assert overridden.blocked_tiers == set()
    assert overridden.fr_regression is not None
    assert overridden.get_mix_properties(64.0)["flexural_source"] == "fr"


def test_override_records_who_and_what(overridden):
    rec = overridden.fit_override_record()
    assert rec["source"] == "interactive override (test)"
    assert "fr" in rec["tiers"]
    assert rec["codes"] and rec["findings"] and rec["row_ids"]


def test_override_shows_in_the_banner_and_the_stats_line(overridden):
    banner = " ".join(overridden.fit_guard_banner())
    assert "OVERRIDDEN STRENGTH FIT" in banner
    assert "interactive override (test)" in banner
    assert "OVERRIDDEN FIT IN FORCE" in overridden.get_regression_stats()


def test_override_reaches_the_saved_design_state(overridden):
    """The third sink: a saved design must carry its own provenance."""
    from domain.models import CanoeDesignState
    st = CanoeDesignState()
    st.mark_structural({"t_in": 0.375, "depth_in": 14.0,
                        "best_mixes": [72.0, 62.0, 72.0],
                        "weight_lbs": 127.3, "L_ft": 20.0, "draft_in": 4.6,
                        "regression_version": "v-test",
                        "fit_override": overridden.fit_override_record()})
    saved = st.to_dict()["structural"]["fit_override"]
    assert saved is not None
    assert saved["source"] == "interactive override (test)"


def test_override_does_not_survive_a_fresh_open(overridden, tmp_path):
    """Per-process only. A later run must decide again, not inherit."""
    path = overridden.db_path
    overridden.close()
    again = MixDatabase(db_path=path)
    try:
        assert again.fit_override_record() is None
        assert again.blocked_tiers == {"fr"}
        assert again.fr_regression is None
    finally:
        again.close()


def test_clearing_an_override_re_blocks(overridden):
    overridden.clear_fit_override()
    assert overridden.blocked_tiers == {"fr"}
    assert overridden.fr_regression is None
