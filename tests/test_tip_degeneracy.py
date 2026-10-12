# tests/test_tip_degeneracy.py
"""
Degenerate end stations must not drive any transverse check.

THE BUG THIS PINS, TWICE OVER.  Transverse stress is sigma_y ~ M_lat*y/I_lat.
At a bow/stern tip the section is a thin VERTICAL sliver: the half-breadth
b -> 0 so I_lat ~ b^2*A collapses, while the depth (and the longitudinal I_tr)
does not — so sigma_y diverges as 1/b^2 while sigma_x stays finite.  The
standalone hoop gate was fixed for this once.  The Kupfer biaxial gate then
shipped WITHOUT the same exclusion and reproduced it exactly: on an EMPTY
section (A_full = 0, I_lat = 0) the geometry's 1e-6 half-breadth floor divided
by the 1e-9 inertia floor manufactured ~1500 psi of transverse stress and
failed every configuration in a 10,350-point sweep.

Two conditions have to coincide, which is why it hid for so long:
  * M(x) must not vanish at the tip -- a stem rocker puts cantilever mass
    BEYOND the last waterplane station, so M(tip) != 0.  With rocker = 0 the
    ratio is multiplied by ~0 and the gate looks clean, which is exactly how
    the original convergence study passed.
  * the station must survive the validity mask.

So these tests exercise the gate WITH a rocker and WITH a degenerate tip.
"""
from __future__ import annotations

import numpy as np
import pytest

import structural
from geometry import calculate_shell_properties
from structural import (DEGENERATE_SECTION_A_IN2, kupfer_combined_gate,
                        transverse_hoop_utilization, _cum_at)


def _tapered_hull(n_st=61, n_wl=21, L_ft=20.0, depth_in=13.0, half_beam_in=13.0,
                  t_in=0.375):
    """Hull whose half-breadth goes to ZERO at both ends — real tip geometry."""
    x = np.linspace(0.0, L_ft, n_st)
    z = np.linspace(0.0, depth_in, n_wl)
    w = half_beam_in * np.sin(np.pi * x / L_ft)      # 0 at both tips
    y = np.outer(w, np.sqrt(z / depth_in))
    p = calculate_shell_properties(x, z, y, t_in)
    return x, p


def _kupfer_on(p, x, t_in=0.375, M_tip=None):
    """Run the gate with a moment distribution that does NOT vanish at the tip."""
    sheer = p.c_bot + p.c_top
    nst = len(x)
    ones = np.ones(nst)
    # A moment that stays finite everywhere, including the degenerate ends —
    # this is what a stem-rocker cantilever mass produces.
    M = np.full(nst, 200.0) if M_tip is None else np.asarray(M_tip, float)
    V = np.full(nst, 50.0)
    return kupfer_combined_gate(
        z_grid=p.z_grid_in, cum_area=p.cum_area_in2, cum_Az=p.cum_Az_in3,
        cum_Iy=p.cum_Iy_in4, yhalf=p.yhalf_in, sheer=sheer,
        y_bar=p.c_bot, I_tr=np.maximum(p.I_in4, 1e-9), t_in=t_in,
        zone_bounds=[np.zeros(nst), sheer], zone_n=[ones], zone_ft=[ones * 300.0],
        zone_fc=[ones * 3000.0], instances=[(0, M, V)], k_lat=0.159)


class TestKupferExcludesDegenerateTips:
    def test_tip_station_does_not_govern(self):
        x, p = _tapered_hull()
        out = _kupfer_on(p, x)
        gov = out["gov"]["station_index"]
        assert gov not in (0, len(x) - 1), (
            "a degenerate tip station governed the Kupfer gate — the 1/b^2 "
            "transverse divergence is back")

    def test_tip_utilisation_is_exactly_zero(self):
        x, p = _tapered_hull()
        u = np.asarray(_kupfer_on(p, x)["util"], float)
        assert u[0] == 0.0 and u[-1] == 0.0

    def test_governing_station_has_a_real_section(self):
        """The stress that governs must come from actual geometry.

        The bug reported sigma_y ~ 1500 psi on a station with A_full = 0 and
        I_lat = 0 -- the geometry's 1e-6 half-breadth floor over the 1e-9
        inertia floor.  So the test is not "sigma_y is small" (a real narrow
        station can carry real stress); it is that whatever governs sits on a
        section that EXISTS.
        """
        x, p = _tapered_hull()
        out = _kupfer_on(p, x)
        sheer = p.c_bot + p.c_top
        gi = out["gov"]["station_index"]
        A_full = _cum_at(p.cum_area_in2, p.z_grid_in, sheer)
        b_max = np.max(p.yhalf_in, axis=1)
        assert A_full[gi] > DEGENERATE_SECTION_A_IN2
        assert b_max[gi] > 0.0
        # and the lateral inertia there is real, not the 1e-9 clamp
        dIy = np.diff(p.cum_Iy_in4, axis=1, prepend=0.0)
        assert float(np.sum(dIy[gi])) > 1e-6

    def test_utilisation_is_continuous_into_the_tip(self):
        """The bug showed as a cliff: 0.013 at station n-1, 3.93 at station n."""
        x, p = _tapered_hull()
        u = np.asarray(_kupfer_on(p, x)["util"], float)
        interior = u[1:-1]
        assert u[-1] <= interior.max(), "tip exceeds every real station"
        assert np.max(np.abs(np.diff(interior))) < 0.5 * max(interior.max(), 1e-9) + 0.5

    def test_gate_still_finds_real_mid_hull_demand(self):
        """Excluding the tips must not gut the check."""
        x, p = _tapered_hull()
        out = _kupfer_on(p, x)
        assert out["util_max"] > 0.0
        assert 0 < out["gov"]["station_index"] < len(x) - 1


class TestBothTransverseChecksAgreeOnWhichStationsExist:
    def test_same_degenerate_threshold(self):
        """One constant, so the two gates cannot drift apart again."""
        x, p = _tapered_hull()
        sheer = p.c_bot + p.c_top
        A_full = _cum_at(p.cum_area_in2, p.z_grid_in, sheer)
        nst = len(x)
        ones = np.ones(nst)
        hoop = transverse_hoop_utilization(
            p.cum_Iy_in4, p.yhalf_in, p.z_grid_in, [np.zeros(nst), sheer],
            [3.0e6], [300.0], 0.375, A_full, np.full(nst, 200.0))
        ku = np.asarray(_kupfer_on(p, x)["util"], float)
        ho = np.asarray(hoop["util"], float)
        # Identify the excluded stations by the SHARED predicate, not by one
        # half of it.  This used to test `A_full <= DEGENERATE_SECTION_A_IN2`,
        # which stopped selecting anything once the stem taper gave the tip
        # stations real area -- they are now excluded by the BREADTH half
        # instead (b -> 0 at a stem, which is what makes sigma_y ~ 1/b^2
        # diverge in the first place).  The property under test is unchanged:
        # whatever the mask excludes, both gates must report zero there.
        from structural import structural_station_mask
        degenerate = ~structural_station_mask(p.cum_area_in2, p.z_grid_in,
                                              sheer, p.yhalf_in)
        assert degenerate.any(), "fixture has no degenerate station to test"
        assert np.all(ku[degenerate] == 0.0)
        assert np.all(ho[degenerate] == 0.0)


class TestLongitudinalComponentExcludesTips:
    """The SAME artifact in the longitudinal check, found after fixing Kupfer.

    sigma = M*d/I_tr, and at a tip the composite I_tr collapses onto its 1e-9
    clamp while the fibre distance does not.  On the live hull this read 60.52
    at both tips against an interior peak of 0.79.  It is worse than a cosmetic
    error: on the legacy path (no lateral geometry -> no Kupfer) this check
    still GATES, so the artifact rejected the sweep's own winner and reported
    its weight as the 1e6 penalty.
    """

    def _evaluate(self, with_lateral: bool):
        from structural import evaluate_multizone_weight
        x, p = _tapered_hull(n_st=61)
        sheer = p.c_bot + p.c_top
        nst = len(x)
        dx = float(np.max(x)) / (nst - 1)
        props = lambda d: {"density": float(d),
                           "strength_t": 18.0 * (float(d) - 55.0) + 15.0,
                           "strength_c": 90.0 * (float(d) - 55.0) + 120.0}
        extra = dict(cum_Iy=p.cum_Iy_in4, yhalf_grid=p.yhalf_in) if with_lateral else {}
        return evaluate_multizone_weight(
            [3., 7.5, 12., 15.], [5., 13.], [80.0, 60.0, 80.0], (0.34, 0.67),
            x, dx, p.width_ft, 0.375,
            p.cum_area_in2, p.cum_Az_in3, p.cum_Iz_in4, p.z_grid_in,
            sheer, p.c_bot, props, coords_3=[5., 10., 15.],
            rocker_ft=0.6, return_details=True, **extra)

    def test_tip_longitudinal_utilisation_is_zero(self):
        d = self._evaluate(with_lateral=True)
        wu = np.asarray(d["worst_util"], float)
        assert wu[0] == 0.0 and wu[-1] == 0.0

    def test_tips_do_not_dominate_the_interior(self):
        d = self._evaluate(with_lateral=True)
        wu = np.asarray(d["worst_util"], float)
        assert np.argmax(wu) not in (0, len(wu) - 1)

    def test_legacy_path_agrees_with_the_gated_path(self):
        """Same design, same verdict, whether or not the lateral arrays are
        supplied — the winner must not be rejected by the fallback."""
        a = self._evaluate(with_lateral=True)
        b = self._evaluate(with_lateral=False)
        assert a["feasible"] == b["feasible"]
        assert a["weight"] == pytest.approx(b["weight"], rel=1e-9)
        assert np.max(a["worst_util"]) == pytest.approx(np.max(b["worst_util"]))

    def test_winner_weight_is_not_the_penalty_value(self):
        for with_lateral in (True, False):
            d = self._evaluate(with_lateral=with_lateral)
            assert d["weight"] < 1e5, (
                "a feasible design came back with the 1e6 penalty weight")


class TestConvergence:
    @pytest.mark.parametrize("n_st", [31, 61, 121, 241])
    def test_utilisation_does_not_run_away_with_station_count(self, n_st):
        """The old hoop bug and this one both showed up as a utilisation that
        grew with resolution, because a finer grid puts more stations inside
        the degenerate taper."""
        x, p = _tapered_hull(n_st=n_st)
        assert _kupfer_on(p, x)["util_max"] < 2.0

    def test_refinement_converges_rather_than_diverging(self):
        vals = []
        for n_st in (31, 61, 121, 241):
            x, p = _tapered_hull(n_st=n_st)
            vals.append(_kupfer_on(p, x)["util_max"])
        # Successive refinements must settle, not keep climbing by large steps.
        assert vals[-1] < 3.0 * vals[0], f"utilisation diverging: {vals}"
        assert abs(vals[-1] - vals[-2]) < 0.5 * max(vals[-1], 1e-9)
