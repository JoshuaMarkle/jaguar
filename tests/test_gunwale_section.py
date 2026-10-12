# tests/test_gunwale_section.py
"""
Real lower-hull section properties for the gunwale / freeboard-flare check.

These replace three hardcoded constants (A=0.050 m², y=-0.080 m, I=1.5e-5 m⁴)
that were duplicated between structural.py and ui/structural_tab.py.  The mock
modelled the underwater hull as a SOLID section — 0.050 m² is essentially the
ENCLOSED sectional area, while bending only sees the thin shell's MATERIAL
area (~10x less) — and counted only ONE freeboard wall.

Hand-checkable geometry
-----------------------
A hull whose half-breadth is CONSTANT with height is a pair of flat vertical
walls, so every property is closed-form.  For a wall of height H and thickness
t, doubled for port + starboard:

    A      = 2·H·t
    z_bar  = H/2                       (above the keel)
    I_own  = 2 · t·H³/12 = t·H³/6      (about its own centroid)

Subdividing a rectangle and re-summing with the parallel-axis theorem is
exact, so these hold for any station count — no discretisation tolerance.
"""
from __future__ import annotations


import ast
from pathlib import Path

import numpy as np
import pytest

from geometry import calculate_shell_properties
from structural import (LowerHullSection, freeboard_flare_gate,
                        gunwale_composite_section,
                        lower_hull_section_properties,
                        gunwale_rail_section, composite_section_properties,
                        aci_elastic_modulus)

_IN2_M2 = 0.0254 ** 2
_IN4_M4 = 0.0254 ** 4
_ROOT = Path(__file__).resolve().parent.parent

# Waterline sits exactly on a grid line so the cumulative tables land on it.
_D_IN = 10.0        # mold depth  (sheer)
_T_IN = 4.0         # draft       (waterline)
_Y_IN = 10.0        # constant half-breadth -> vertical walls
_THK = 0.5          # shell thickness


def _flat_wall_hull(thickness_in=_THK, depth_in=_D_IN, n_st=9, n_wl=21):
    """Synthetic hull: constant half-breadth => two flat vertical walls."""
    x = np.linspace(0.0, 20.0, n_st)
    z = np.linspace(0.0, depth_in, n_wl)
    y = np.full((n_st, n_wl), _Y_IN)
    return x, z, calculate_shell_properties(x, z, y, thickness_in)


class TestLowerHullSectionProperties:
    """Hand-checked closed-form values for a known offset table."""

    def test_matches_closed_form_flat_wall(self):
        _, _, p = _flat_wall_hull()
        lh = lower_hull_section_properties(
            p.cum_area_in2, p.cum_Az_in3, p.cum_Iz_in4, p.z_grid_in, _T_IN)

        # Hand values for a 4.0 in tall, 0.5 in thick double wall
        exp_A_in2 = 2.0 * _T_IN * _THK                 # 4.0 in²
        exp_y_in = _T_IN / 2.0 - _T_IN                 # -2.0 in (below waterline)
        exp_I_in4 = _THK * _T_IN ** 3 / 6.0            # 5.3333 in⁴

        assert exp_A_in2 == pytest.approx(4.0)
        assert exp_I_in4 == pytest.approx(5.333333, rel=1e-6)

        np.testing.assert_allclose(lh.A_m2, exp_A_in2 * _IN2_M2, rtol=1e-9)
        np.testing.assert_allclose(lh.y_m, exp_y_in * 0.0254, rtol=1e-9)
        np.testing.assert_allclose(lh.I_m4, exp_I_in4 * _IN4_M4, rtol=1e-9)

    def test_centroid_is_below_the_waterline(self):
        _, _, p = _flat_wall_hull()
        lh = lower_hull_section_properties(
            p.cum_area_in2, p.cum_Az_in3, p.cum_Iz_in4, p.z_grid_in, _T_IN)
        assert np.all(lh.y_m < 0.0), "y_m is measured from the waterline, +up"

    def test_is_material_area_not_enclosed_area(self):
        """The mock's 0.050 m² was the ENCLOSED area; bending needs material."""
        _, _, p = _flat_wall_hull()
        lh = lower_hull_section_properties(
            p.cum_area_in2, p.cum_Az_in3, p.cum_Iz_in4, p.z_grid_in, _T_IN)
        enclosed_in2 = 2.0 * _Y_IN * _T_IN             # 80 in² of water
        material_in2 = 2.0 * _T_IN * _THK              # 4 in² of concrete
        np.testing.assert_allclose(lh.A_m2, material_in2 * _IN2_M2, rtol=1e-9)
        assert lh.A_m2[0] < enclosed_in2 * _IN2_M2 / 10.0


class TestPerConfigDependence:
    """The properties must track the config under evaluation."""

    def test_scales_linearly_with_thickness(self):
        out = {}
        for t in (0.375, 0.5, 0.75):
            _, _, p = _flat_wall_hull(thickness_in=t)
            lh = lower_hull_section_properties(
                p.cum_area_in2, p.cum_Az_in3, p.cum_Iz_in4, p.z_grid_in, _T_IN)
            out[t] = (float(lh.A_m2[0]), float(lh.I_m4[0]))

        # A and I are both linear in t for a thin shell
        assert out[0.5][0] / out[0.375][0] == pytest.approx(0.5 / 0.375, rel=1e-9)
        assert out[0.75][1] / out[0.5][1] == pytest.approx(0.75 / 0.5, rel=1e-9)

    def test_independent_of_mold_depth(self):
        """The lower hull lives entirely below the waterline."""
        a = _flat_wall_hull(depth_in=10.0)[2]
        b = _flat_wall_hull(depth_in=16.0)[2]
        lh_a = lower_hull_section_properties(
            a.cum_area_in2, a.cum_Az_in3, a.cum_Iz_in4, a.z_grid_in, _T_IN)
        lh_b = lower_hull_section_properties(
            b.cum_area_in2, b.cum_Az_in3, b.cum_Iz_in4, b.z_grid_in, _T_IN)
        np.testing.assert_allclose(lh_a.A_m2, lh_b.A_m2, rtol=1e-9)
        np.testing.assert_allclose(lh_a.I_m4, lh_b.I_m4, rtol=1e-9)


class TestCompositeSection:
    """The gate's composite must BE the solver's section, not a rival model."""

    def test_zero_flare_reproduces_calculate_shell_properties(self):
        """
        A zero-flare freeboard IS the straight extrusion the geometry builds,
        so the composite must reproduce calculate_shell_properties exactly.
        This is what stops the gate drifting from the sweep's own section.
        """
        _, _, p = _flat_wall_hull()
        lh_all = lower_hull_section_properties(
            p.cum_area_in2, p.cum_Az_in3, p.cum_Iz_in4, p.z_grid_in, _T_IN)
        i = 0
        lh = LowerHullSection(float(lh_all.A_m2[i]), float(lh_all.y_m[i]),
                              float(lh_all.I_m4[i]))

        I_t, c_gun = gunwale_composite_section(lh, _THK, _D_IN - _T_IN, 0.0)

        assert I_t == pytest.approx(p.I_in4[i] * _IN4_M4, rel=1e-9)
        assert c_gun == pytest.approx(p.c_top[i] * 0.0254, rel=1e-9)

    def test_zero_flare_matches_closed_form(self):
        """Independent hand-check of the same composite."""
        _, _, p = _flat_wall_hull()
        lh_all = lower_hull_section_properties(
            p.cum_area_in2, p.cum_Az_in3, p.cum_Iz_in4, p.z_grid_in, _T_IN)
        lh = LowerHullSection(float(lh_all.A_m2[0]), float(lh_all.y_m[0]),
                              float(lh_all.I_m4[0]))
        I_t, c_gun = gunwale_composite_section(lh, _THK, _D_IN - _T_IN, 0.0)

        # Full 10 in wall: I = t·D³/6 about mid-height, c = D/2
        assert I_t == pytest.approx(_THK * _D_IN ** 3 / 6.0 * _IN4_M4, rel=1e-9)
        assert c_gun == pytest.approx(_D_IN / 2.0 * 0.0254, rel=1e-9)

    def test_counts_both_freeboard_walls(self):
        """
        The section has port AND starboard freeboard walls; the old model
        counted one, understating the freeboard's stiffness contribution.
        """
        lh = LowerHullSection(0.0, 0.0, 0.0)        # isolate the walls
        t_in, h_in = 0.5, 6.0
        I_t, _ = gunwale_composite_section(lh, t_in, h_in, 0.0)
        one_wall = (t_in * 0.0254) * (h_in * 0.0254) ** 3 / 12.0
        assert I_t == pytest.approx(2.0 * one_wall, rel=1e-9)

    def test_flare_follows_the_geometry_segment_model(self):
        """
        geometry.calculate_shell_properties uses (t·ds³/12)·sin²θ per segment.
        A wall flared θ off vertical has ds = h/cosθ and sinθ_seg = cosθ, so
        I_own = t·h³/(12·cosθ).  Both walls -> t·h³/(6·cosθ).
        """
        lh = LowerHullSection(0.0, 0.0, 0.0)
        t_in, h_in, theta = 0.5, 6.0, 15.0
        I_t, _ = gunwale_composite_section(lh, t_in, h_in, theta)
        t_m, h_m = t_in * 0.0254, h_in * 0.0254
        expected = t_m * h_m ** 3 / (6.0 * np.cos(np.radians(theta)))
        assert I_t == pytest.approx(expected, rel=1e-9)


class TestFlareGate:
    def test_returns_stress_and_smallest_passing_angle(self):
        _, _, p = _flat_wall_hull()
        lh_all = lower_hull_section_properties(
            p.cum_area_in2, p.cum_Az_in3, p.cum_Iz_in4, p.z_grid_in, _T_IN)
        lh = LowerHullSection(float(lh_all.A_m2[0]), float(lh_all.y_m[0]),
                              float(lh_all.I_m4[0]))

        ok, deg, stress_psi, gov = freeboard_flare_gate(
            200.0, _THK, _D_IN - _T_IN, 15.0, 1e9, lh)
        assert ok and deg == 0.0          # generous allowable -> no flare needed
        assert gov == 0                   # single (scalar) station

        # Hand-check the reported stress: sigma = M·c/I
        I_t, c_gun = gunwale_composite_section(lh, _THK, _D_IN - _T_IN, 0.0)
        expected_pa = 200.0 * 1.35582 * c_gun / I_t
        assert stress_psi == pytest.approx(expected_pa / 6894.76, rel=1e-9)

    def test_impossible_allowable_fails_closed(self):
        _, _, p = _flat_wall_hull()
        lh_all = lower_hull_section_properties(
            p.cum_area_in2, p.cum_Az_in3, p.cum_Iz_in4, p.z_grid_in, _T_IN)
        lh = LowerHullSection(float(lh_all.A_m2[0]), float(lh_all.y_m[0]),
                              float(lh_all.I_m4[0]))
        ok, deg, stress, gov = freeboard_flare_gate(
            1e6, _THK, _D_IN - _T_IN, 15.0, 1e-6, lh)
        # Failure still reports the best-achievable worst-case stress (not None)
        # so callers can say by how much it misses.
        assert not ok and deg is None
        assert stress is not None and stress > 1e-6 / 6894.76


class TestAllStationsGate:
    """
    The gate must find the worst station, which is NOT necessarily the
    peak-moment station: a smaller section further along carries less moment
    but can develop more stress.
    """

    @staticmethod
    def _stress_psi(M_ftlb, lh, t_in, fb_in, deg=0.0):
        I_t, c_gun = gunwale_composite_section(lh, t_in, fb_in, deg)
        return np.atleast_1d(
            np.abs(np.asarray(M_ftlb, float)) * 1.35582 * c_gun
            / np.maximum(I_t, 1e-12)) / 6894.76

    def _contrived(self):
        """
        3 stations.  Station 0 has the LARGEST moment but a big, stiff section;
        station 2 has a smaller moment but a tiny section -> highest stress.
        Station 1 is unremarkable.
        """
        t_in, fb_in = 0.5, 8.0
        #                     stiff       mid        TINY
        A_m2 = np.array([0.030,      0.010,     0.0008])
        I_m4 = np.array([8.0e-5,     6.0e-6,    2.0e-7])
        y_m  = np.array([-0.12,      -0.08,     -0.03])
        lh = LowerHullSection(A_m2, y_m, I_m4)
        M_ftlb = np.array([260.0,    120.0,     150.0])   # peak moment at idx 0
        return M_ftlb, lh, t_in, fb_in

    def test_governing_station_is_not_the_max_moment_station(self):
        M_ftlb, lh, t_in, fb_in = self._contrived()
        sigma = self._stress_psi(M_ftlb, lh, t_in, fb_in)

        i_moment = int(np.argmax(np.abs(M_ftlb)))
        i_stress = int(np.argmax(sigma))
        # The whole point of the contrivance: they differ.
        assert i_moment == 0
        assert i_stress == 2
        assert i_stress != i_moment

        # Generous allowable so it passes; the gate must still name station 2.
        ok, deg, worst_psi, gov = freeboard_flare_gate(
            M_ftlb, t_in, fb_in, 0.0, 1e9, lh)
        assert ok and gov == 2
        assert worst_psi == pytest.approx(float(sigma[2]), rel=1e-9)

    def test_catches_overstress_a_max_moment_only_check_would_miss(self):
        M_ftlb, lh, t_in, fb_in = self._contrived()
        sigma = self._stress_psi(M_ftlb, lh, t_in, fb_in)

        # Allowable set ABOVE the max-moment station's stress but BELOW the
        # true governing station's -> a max-moment-only gate would pass, the
        # all-stations gate must fail.
        allow = 0.5 * (float(sigma[0]) + float(sigma[2]))
        assert float(sigma[0]) < allow < float(sigma[2])

        ok, deg, worst_psi, gov = freeboard_flare_gate(
            M_ftlb, t_in, fb_in, 0.0, allow, lh)      # no flare relief (max=0)
        assert not ok, "all-stations gate must reject the overstressed station"
        assert gov == 2
        assert worst_psi == pytest.approx(float(sigma[2]), rel=1e-9)

    def test_flare_relief_can_rescue_the_worst_station(self):
        """A larger flare angle stiffens the wall and brings the worst station
        back under the limit — the gate should report the smallest such angle.
        Threshold is set between the 0° and 15° stress so the test does not
        depend on the exact amount of relief the contrived numbers give."""
        M_ftlb, lh, t_in, fb_in = self._contrived()
        s0  = float(self._stress_psi(M_ftlb, lh, t_in, fb_in, deg=0.0)[2])
        s15 = float(self._stress_psi(M_ftlb, lh, t_in, fb_in, deg=15.0)[2])
        assert s15 < s0                        # flare relieves station 2
        allow = 0.5 * (s0 + s15)               # 0° fails, ≤15° passes
        ok, deg, worst_psi, gov = freeboard_flare_gate(
            M_ftlb, t_in, fb_in, 15.0, allow, lh)
        assert ok and deg > 0.0 and gov == 2
        assert worst_psi <= allow + 1e-9


class TestMultizoneGunwaleAnchor:
    """
    Zero-flare-style anchor: the multi-zone composite section with a gunwale
    (one zone, uniform modulus n=1) must reproduce the single-zone gunwale
    section EXACTLY — the same guarantee the shell zero-flare check gives,
    proving the two paths fold the SHARED rail identically.  Uses the flat-wall
    hull (no degenerate tips) so the match holds at every station.
    """

    def _section_both_ways(self, gw_w, gw_h, gw_fil):
        _, _, p = _flat_wall_hull()
        A, At, Ab, I, Q, ct, cb, wf = p
        x = np.linspace(0.0, 20.0, len(A))
        sheer = cb + ct
        rail = gunwale_rail_section(x, _THK, gw_w, gw_h, gw_fil, sheer)
        assert rail is not None

        # single-zone fold (evaluate_canoe_weight algebra, shell-NA datum)
        gw_c = rail.gw_cent - cb
        fil_c = rail.fil_cent - cb
        A_tot = A + rail.A_gw + rail.A_fil
        ybar = (rail.A_gw * gw_c + rail.A_fil * fil_c) / A_tot
        I_sz = (I + A * ybar ** 2
                + rail.I_gw_own + rail.A_gw * (gw_c - ybar) ** 2
                + rail.I_fil_own + rail.A_fil * (fil_c - ybar) ** 2)
        ctop_sz, cbot_sz = ct - ybar, cb + ybar

        # multi-zone fold (composite, 1 zone + gunwale, keel datum, n_gw = 1)
        E = float(aci_elastic_modulus(70.0, 1000.0))
        z_bounds = [np.zeros(len(x)), np.asarray(sheer, float)]
        S = rail.A_gw * rail.gw_cent + rail.A_fil * rail.fil_cent
        Iz = (rail.A_gw * rail.gw_cent ** 2 + rail.I_gw_own
              + rail.A_fil * rail.fil_cent ** 2 + rail.I_fil_own)
        ybar_mz, I_mz, ctop_mz, cbot_mz = composite_section_properties(
            z_bounds, [E], p.cum_area_in2, p.cum_Az_in3, p.cum_Iz_in4,
            p.z_grid_in, (rail.A_gw + rail.A_fil), S, Iz)
        return (I_sz, ctop_sz, cbot_sz), (I_mz, ctop_mz, cbot_mz)

    @pytest.mark.parametrize("gw", [(1.5, 1.75, 1.0), (0.5, 0.5, 0.0),
                                    (2.5, 3.0, 2.0)])
    def test_matches_single_zone_gunwale_section(self, gw):
        (I_sz, ct_sz, cb_sz), (I_mz, ct_mz, cb_mz) = self._section_both_ways(*gw)
        np.testing.assert_allclose(I_mz, I_sz, rtol=1e-9)
        np.testing.assert_allclose(ct_mz, ct_sz, rtol=1e-9, atol=1e-9)
        np.testing.assert_allclose(cb_mz, cb_sz, rtol=1e-9, atol=1e-9)


class TestNoDuplication:
    """
    The UI and structural.py must share ONE implementation.  These are source
    -level guards: the drift they prevent is someone re-adding a private copy,
    which a behavioural test on the shared function cannot catch.
    """

    def test_mock_constants_are_gone(self):
        """
        Parsed via ast so only live CODE is inspected — comments and
        docstrings may legitimately still describe the old mock (and do).
        """
        for rel in ("structural.py", "ui/structural_tab.py"):
            tree = ast.parse((_ROOT / rel).read_text(encoding="utf-8"))

            names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
            for tok in ("A_low", "y_low", "I_low"):
                assert tok not in names, f"{rel} still defines the mock {tok}"

            floats = {n.value for n in ast.walk(tree)
                      if isinstance(n, ast.Constant) and isinstance(n.value, float)}
            assert 1.5e-5 not in floats, (
                f"{rel} still hardcodes the mock I_low (1.5e-5 m^4)")

    def test_ui_calls_the_shared_gate(self):
        src = (_ROOT / "ui" / "structural_tab.py").read_text(encoding="utf-8")
        assert "from structural import freeboard_flare_gate" in src, (
            "the UI must call the shared gate, not reimplement it")
        # The composite maths must not be re-derived in the UI
        assert "parallel-axis" not in src.lower()
        assert "y_na" not in src

    def test_structural_owns_the_only_composite(self):
        src = (_ROOT / "structural.py").read_text(encoding="utf-8")
        assert src.count("def gunwale_composite_section") == 1
        assert src.count("def lower_hull_section_properties") == 1
