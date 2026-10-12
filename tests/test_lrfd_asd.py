# tests/test_lrfd_asd.py
"""
LRFD / ASD design methodologies.

THE INVARIANT THESE PROTECT: the failure criterion does not depend on the
methodology.  Kupfer-Gerstle is the gate under both -- LRFD and ASD change only
how loads are combined and what divides the nominal strength.  If a future
change makes the criterion itself methodology-dependent, or makes ASD stop
reproducing the pre-LRFD engine, these fail.

The dead/live split underneath is exact rather than approximate because the
beam solve is a LINEAR operator in the applied load; that linearity is itself
pinned here, since the split's correctness rests on it.
"""
from __future__ import annotations

import numpy as np
import pytest

import config
import structural
from geometry import calculate_shell_properties
from structural import (DESIGN_METHOD_ASD, DESIGN_METHOD_LRFD,
                        _append_rocker_overhang, _solve_beam_case,
                        _solve_beam_case_split, capacity_divisor,
                        design_method, load_combinations)


def _hull(n_st=41, n_wl=17, L_ft=20.0, depth_in=13.0, half_beam_in=13.0,
          t_in=0.5):
    x = np.linspace(0.0, L_ft, n_st)
    z = np.linspace(0.0, depth_in, n_wl)
    w = half_beam_in * np.sin(np.pi * x / L_ft)
    y = np.outer(w, np.sqrt(z / depth_in))
    p = calculate_shell_properties(x, z, y, t_in)
    v = p.A_in2 > 0.001
    return x[v], p, v


def _props(d):
    """Mix properties stub that divides by the methodology's capacity divisor,
    exactly as config.get_mix_properties does.  Routing the stub through
    capacity_divisor() is what makes phi/Omega observable in these tests -- a
    stub returning NOMINAL strengths would silently bypass the capacity side
    and every methodology comparison below would be measuring only the load
    factors."""
    div = capacity_divisor()
    return {"density": float(d),
            "strength_t": (18.0 * (float(d) - 55.0) + 15.0) / div,
            "strength_c": (90.0 * (float(d) - 55.0) + 120.0) / div}


def _ev(**kw):
    x, p, v = _hull()
    sheer = p.c_bot[v] + p.c_top[v]
    dx = float(np.max(x)) / (len(x) - 1)
    return structural.evaluate_multizone_weight(
        [3., 7.5, 12., 15.], [5., 13.], [80.0, 60.0, 80.0], (0.34, 0.67),
        x, dx, p.width_ft[v], 0.5, p.cum_area_in2[v], p.cum_Az_in3[v],
        p.cum_Iz_in4[v], p.z_grid_in, sheer, p.c_bot[v], _props,
        coords_3=[5., 10., 15.], rocker_ft=0.6, cum_Iy=p.cum_Iy_in4[v],
        yhalf_grid=p.yhalf_in[v], return_details=True, **kw)


# ══════════════════════════════════════════════════════════════════════════
#  The dead/live split is exact, because the solve is linear
# ══════════════════════════════════════════════════════════════════════════

class TestLoadSplitIsLossless:
    """Buoyancy is a reaction proportional to TOTAL weight, so apportioning it
    would be ambiguous if the solve were nonlinear.  It is not."""

    def _setup(self):
        x, p, v = _hull()
        dx = float(np.max(x)) / (len(x) - 1)
        w_self = p.A_in2[v] * 70.0 / 144.0
        return x, dx, w_self, p.width_ft[v], float(np.max(x))

    @pytest.mark.parametrize("rocker_ft", [0.0, 0.6])
    @pytest.mark.parametrize("adv", [False, True])
    def test_dead_plus_live_reproduces_the_total(self, rocker_ft, adv):
        x, dx, w_self, width, L = self._setup()
        xE, wE, widthE, i0, i1 = _append_rocker_overhang(x, dx, w_self, width,
                                                         rocker_ft)
        coords, wts = [3., 7.5, 12., 15.], [192.0, 180.0, 204.0, 192.0]
        M_t, V_t = _solve_beam_case(xE, dx, wE, widthE, coords, wts, L, adv)
        M_d, V_d, M_l, V_l = _solve_beam_case_split(xE, dx, wE, widthE, coords,
                                                    wts, L, adv)
        assert np.allclose(M_d + M_l, M_t, rtol=1e-11, atol=1e-9)
        assert np.allclose(V_d + V_l, V_t, rtol=1e-11, atol=1e-9)

    def test_live_scales_linearly(self):
        """Doubling the crew doubles the live component exactly."""
        x, dx, w_self, width, L = self._setup()
        xE, wE, widthE, _, _ = _append_rocker_overhang(x, dx, w_self, width, 0.6)
        coords, wts = [3., 7.5, 12., 15.], [192.0, 180.0, 204.0, 192.0]
        _, _, M1, _ = _solve_beam_case_split(xE, dx, wE, widthE, coords, wts,
                                             L, False)
        _, _, M2, _ = _solve_beam_case_split(xE, dx, wE, widthE, coords,
                                             [2 * w for w in wts], L, False)
        assert np.allclose(M2, 2.0 * M1, rtol=1e-12, atol=1e-12)

    def test_dead_is_independent_of_the_crew(self):
        x, dx, w_self, width, L = self._setup()
        xE, wE, widthE, _, _ = _append_rocker_overhang(x, dx, w_self, width, 0.6)
        a, _, _, _ = _solve_beam_case_split(xE, dx, wE, widthE,
                                            [3., 7.5, 12., 15.],
                                            [192.0, 180.0, 204.0, 192.0], L, False)
        b, _, _, _ = _solve_beam_case_split(xE, dx, wE, widthE,
                                            [3., 7.5, 12., 15.],
                                            [10.0, 10.0, 10.0, 10.0], L, False)
        assert np.allclose(a, b, rtol=1e-12, atol=1e-12)

    def test_rocker_mass_lands_in_the_dead_component(self):
        """The stem-rocker cantilever is hull material, so it is DEAD load: it
        must raise the dead moment and leave the live moment alone.

        Built on a waterplane that tapers to zero at both ends -- the physical
        case.  The overhang is attached with zero width (it carries no
        buoyancy), so with a hull whose own tips are already zero-width the
        attachment adds nothing to the buoyancy normalisation and the live
        component is EXACTLY unchanged.  (On a hull whose end stations have
        been filtered to a non-zero width, joining a zero-width overhang does
        perturb the normalisation slightly -- a pre-existing second-order
        effect of how the overhang is attached, unrelated to the dead/live
        split, which is why this test controls for it.)
        """
        n, L = 81, 20.0
        x = np.linspace(0.0, L, n)
        dx = L / (n - 1)
        width = 4.0 * np.sin(np.pi * x / L)        # -> 0 at both tips
        w_self = 2.0 + 0.5 * np.sin(np.pi * x / L)
        coords, wts = [3., 7.5, 12., 15.], [192.0, 180.0, 204.0, 192.0]
        peaks = []
        for rk in (0.0, 0.6):
            xE, wE, widthE, i0, i1 = _append_rocker_overhang(x, dx, w_self,
                                                             width, rk)
            Md, _, Ml, _ = _solve_beam_case_split(xE, dx, wE, widthE, coords,
                                                  wts, L, False)
            peaks.append((np.max(np.abs(Md)), np.max(np.abs(Ml))))
        assert peaks[1][0] > peaks[0][0] * 1.01, \
            "rocker did not raise the DEAD moment"
        assert peaks[1][1] == pytest.approx(peaks[0][1], rel=1e-9), \
            "rocker changed the LIVE moment; it is hull mass, not payload"


# ══════════════════════════════════════════════════════════════════════════
#  Methodology selection
# ══════════════════════════════════════════════════════════════════════════

class TestMethodSelection:
    def test_default_is_lrfd(self):
        assert design_method() == DESIGN_METHOD_LRFD

    def test_asd_uses_the_nominal_combination_only(self, monkeypatch):
        monkeypatch.setattr(config, "DESIGN_METHOD", "ASD")
        assert load_combinations("race_4") == [(1.0, 1.0)]

    def test_lrfd_envelopes_the_counteracting_dead_alternative(self, monkeypatch):
        """Dead relieves live at this hull's governing station, so 0.9D must be
        checked alongside 1.2D -- ASCE 7-22 Sec. 2.3.1."""
        monkeypatch.setattr(config, "DESIGN_METHOD", "LRFD")
        monkeypatch.setattr(config, "GAMMA_DEAD", 1.2)
        monkeypatch.setattr(config, "GAMMA_LIVE", 1.6)
        monkeypatch.setattr(config, "LRFD_COUNTERACTING_DEAD", True)
        assert load_combinations("race_4") == [(1.2, 1.6), (0.9, 1.6)]

    def test_per_case_factors_override_the_globals(self, monkeypatch):
        monkeypatch.setattr(config, "DESIGN_METHOD", "LRFD")
        monkeypatch.setattr(config, "LRFD_COUNTERACTING_DEAD", False)
        monkeypatch.setattr(config, "LOAD_FACTORS_BY_CASE",
                            {"race_4": (1.4, 1.7)})
        assert load_combinations("race_4") == [(1.4, 1.7)]
        assert load_combinations("race_2") == [(1.2, 1.6)]

    def test_capacity_divisor_follows_the_method(self, monkeypatch):
        monkeypatch.setattr(config, "DESIGN_METHOD", "ASD")
        monkeypatch.setattr(config, "OMEGA_SAFETY", 1.5)
        assert capacity_divisor() == pytest.approx(1.5)
        monkeypatch.setattr(config, "DESIGN_METHOD", "LRFD")
        monkeypatch.setattr(config, "PHI_RESISTANCE", 0.60)
        assert capacity_divisor() == pytest.approx(1.0 / 0.60)

    def test_unknown_method_falls_back_to_lrfd(self, monkeypatch):
        monkeypatch.setattr(config, "DESIGN_METHOD", "nonsense")
        assert design_method() == DESIGN_METHOD_LRFD


# ══════════════════════════════════════════════════════════════════════════
#  The criterion itself is unchanged
# ══════════════════════════════════════════════════════════════════════════

class TestCriterionIsMethodIndependent:
    def test_both_methods_report_a_kupfer_and_a_rankine_value(self, monkeypatch):
        for meth in ("ASD", "LRFD"):
            monkeypatch.setattr(config, "DESIGN_METHOD", meth)
            d = _ev()
            assert d["kupfer_max"] is not None
            assert d["rankine_max"] is not None, \
                "Rankine must stay reported for comparison under both methods"

    def test_kupfer_governs_not_rankine_under_both(self, monkeypatch):
        """Kupfer >= Rankine on the same stress state, always -- the biaxial
        envelope is the stricter criterion and it is what gates."""
        for meth in ("ASD", "LRFD"):
            monkeypatch.setattr(config, "DESIGN_METHOD", meth)
            d = _ev()
            assert d["kupfer_max"] >= d["rankine_max"] - 1e-12

    def test_asd_is_less_demanding_than_lrfd_on_this_live_dominated_hull(
            self, monkeypatch):
        """~111 lb of hull against ~640 lb of crew: the live load dominates, so
        1.6 on live outweighs the relief of dividing by 1.5 instead of 1/phi."""
        monkeypatch.setattr(config, "DESIGN_METHOD", "ASD")
        monkeypatch.setattr(config, "OMEGA_SAFETY", 1.5)
        u_asd = _ev()["kupfer_max"]
        monkeypatch.setattr(config, "DESIGN_METHOD", "LRFD")
        monkeypatch.setattr(config, "PHI_RESISTANCE", 0.60)
        u_lrfd = _ev()["kupfer_max"]
        assert u_lrfd > u_asd

    def test_phi_scales_the_lrfd_utilisation_inversely(self, monkeypatch):
        monkeypatch.setattr(config, "DESIGN_METHOD", "LRFD")
        monkeypatch.setattr(config, "PHI_RESISTANCE", 0.90)
        u90 = _ev()["kupfer_max"]
        monkeypatch.setattr(config, "PHI_RESISTANCE", 0.60)
        u60 = _ev()["kupfer_max"]
        assert u60 == pytest.approx(u90 * 0.90 / 0.60, rel=1e-9)

    def test_acceptance_margin_applies_under_both_methods(self, monkeypatch):
        from structural import kupfer_acceptance_limit
        for meth in ("ASD", "LRFD"):
            monkeypatch.setattr(config, "DESIGN_METHOD", meth)
            monkeypatch.setattr(config, "KUPFER_ACCEPTANCE_MAX", 0.95)
            assert kupfer_acceptance_limit() == pytest.approx(0.95)

    def test_lateral_load_is_factored_with_the_vertical(self, monkeypatch):
        """M_lat = k_lat*|M| and the instances carry the FACTORED moment, so the
        transverse component scales with the longitudinal one -- a consistent
        load state at each fibre, which is the point of a biaxial criterion."""
        monkeypatch.setattr(config, "DESIGN_METHOD", "ASD")
        h_asd = np.max(_ev()["hoop_util"])
        monkeypatch.setattr(config, "DESIGN_METHOD", "LRFD")
        h_lrfd = np.max(_ev()["hoop_util"])
        assert h_lrfd > h_asd, "hoop demand did not follow the load factors"


class TestReportMetadata:
    def test_struct_data_records_what_was_applied(self, monkeypatch):
        monkeypatch.setattr(config, "DESIGN_METHOD", "LRFD")
        monkeypatch.setattr(config, "PHI_RESISTANCE", 0.60)
        d = _ev()
        dz = d["struct_data"]["design"]
        assert dz["method"] == "LRFD"
        assert dz["phi"] == pytest.approx(0.60)
        assert dz["combinations"]["race_4"] == [(1.2, 1.6), (0.9, 1.6)]

    def test_asd_metadata_records_omega_not_phi(self, monkeypatch):
        monkeypatch.setattr(config, "DESIGN_METHOD", "ASD")
        monkeypatch.setattr(config, "OMEGA_SAFETY", 1.5)
        dz = _ev()["struct_data"]["design"]
        assert dz["method"] == "ASD"
        assert dz["phi"] is None and dz["omega"] == pytest.approx(1.5)
