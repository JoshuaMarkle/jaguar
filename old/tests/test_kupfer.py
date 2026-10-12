# tests/test_kupfer.py
"""
Kupfer-Gerstle biaxial failure envelope — regression tests.

The combined-stress gate replaces the Rankine (max-principal) tensile criterion.
These lock the physics:
  1. pure bending reduces to Mc/I (Kupfer == Rankine in the tension-tension
     quadrant),
  2. pure shear gives sigma_1 = tau (= -sigma_2),
  3. a hand-checked Mohr's-circle combined state,
  4. a tension-compression state where the Kupfer reduction FIRES — it rejects a
     stress that plain Rankine would pass,
plus that the per-fibre evaluator reduces to Mc/I under pure bending and is
NOT resolution-divergent (same discipline as the transverse-hoop fix).
"""
from __future__ import annotations

import numpy as np
import pytest

from structural import (principal_stresses, kupfer_utilization,
                        kupfer_cc_strength, kupfer_combined_gate)


# ── 1-4: the pure criterion ────────────────────────────────────────────────

def test_pure_bending_reduces_to_mc_over_i():
    # uniaxial tension sigma -> sigma_1 = sigma, sigma_2 = 0, util = sigma/f_t
    s1, s2 = principal_stresses(120.0, 0.0, 0.0)
    assert s1 == pytest.approx(120.0) and s2 == pytest.approx(0.0)
    util, quad = kupfer_utilization(s1, s2, ft=200.0, fc=2000.0)
    assert float(util) == pytest.approx(0.6)          # 120/200
    assert int(quad) == 1                             # tension-tension


def test_pure_shear_gives_sigma1_equals_tau():
    s1, s2 = principal_stresses(0.0, 0.0, 45.0)
    assert s1 == pytest.approx(45.0)
    assert s2 == pytest.approx(-45.0)


def test_mohr_circle_hand_check():
    # sx=100, sy=-40, txy=30 -> centre 30, radius sqrt(70^2+30^2)
    s1, s2 = principal_stresses(100.0, -40.0, 30.0)
    R = (70.0 ** 2 + 30.0 ** 2) ** 0.5
    assert s1 == pytest.approx(30.0 + R)
    assert s2 == pytest.approx(30.0 - R)


def test_tension_compression_reduction_fires():
    # Rankine would PASS (sigma_1/f_t = 0.9 < 1); Kupfer must FAIL because the
    # transverse compression degrades the tensile capacity.
    ft, fc = 100.0, 1000.0
    s1, s2 = 90.0, -500.0
    rankine = s1 / ft
    kup, quad = kupfer_utilization(s1, s2, ft, fc)
    assert rankine < 1.0                               # Rankine passes
    assert float(kup) == pytest.approx(0.9 + 0.8 * 0.5)  # 1.30
    assert float(kup) > 1.0                            # Kupfer fails
    assert int(quad) == 2                              # tension-compression


def test_tt_tc_boundary_is_continuous():
    a, _ = kupfer_utilization(90.0, 1e-9, 100.0, 1000.0)
    b, _ = kupfer_utilization(90.0, -1e-9, 100.0, 1000.0)
    assert float(a) == pytest.approx(float(b), abs=1e-6)


def test_cc_biaxial_strengthening_between_1_16_and_1_26():
    fc = 1000.0
    equal = float(kupfer_cc_strength(-1000.0, -1000.0, fc))   # alpha=1 -> 4.65/4 fc
    half  = float(kupfer_cc_strength(-1000.0, -500.0, fc))    # alpha=0.5
    assert equal == pytest.approx(1162.5, rel=1e-3)           # ~1.16 f_c (Kupfer)
    assert 1.16 * fc <= half <= 1.27 * fc                     # peak region
    # a tensile or uniaxial state returns plain f_c (no CC gain)
    assert float(kupfer_cc_strength(50.0, -1000.0, fc)) == pytest.approx(fc)


# ── per-fibre evaluator: reduction + resolution independence ────────────────

def _section(n_stations):
    from geometry import (parse_godzilla_dimensions, parse_godzilla_offsets,
                          calculate_shell_properties)
    L_ft, draft_in = parse_godzilla_dimensions("out.mlt")
    xr, zr, ym = parse_godzilla_offsets("off1.mlt", L_ft=L_ft, draft_in=draft_in,
                                        mold_depth_in=13.0, n_stations=n_stations)
    p = calculate_shell_properties(xr, zr, ym, 0.375)
    v = p.A_in2 > 0
    def V(a): return np.asarray(a)[v]
    return dict(z_grid=p.z_grid_in, cum_area=V(p.cum_area_in2), cum_Az=V(p.cum_Az_in3),
                cum_Iy=V(p.cum_Iy_in4), yhalf=V(p.yhalf_in), sheer=V(p.c_bot) + V(p.c_top),
                y_bar=V(p.c_bot), I_tr=V(p.I_in4))


def _gate(sec, M, Vsh, k_lat):
    nst = len(sec["I_tr"]); one = np.ones(nst)
    return kupfer_combined_gate(
        z_grid=sec["z_grid"], cum_area=sec["cum_area"], cum_Az=sec["cum_Az"],
        cum_Iy=sec["cum_Iy"], yhalf=sec["yhalf"], sheer=sec["sheer"],
        y_bar=sec["y_bar"], I_tr=sec["I_tr"], t_in=0.375,
        zone_bounds=[np.zeros(nst), sec["sheer"]], zone_n=[one],
        zone_ft=[one * 150.0], zone_fc=[one * 3000.0],
        instances=[(0, M * np.ones(nst), Vsh * np.ones(nst))], k_lat=k_lat)


def test_evaluator_pure_bending_is_rankine():
    sec = _section(161)
    r = _gate(sec, M=150.0, Vsh=0.0, k_lat=0.0)
    assert r["util_max"] == pytest.approx(r["rankine_max"], rel=1e-9)
    assert r["gov"]["quadrant"] == "tension-tension"


def test_evaluator_hoop_makes_kupfer_exceed_rankine():
    sec = _section(161)
    r = _gate(sec, M=150.0, Vsh=0.0, k_lat=0.15)
    assert r["util_max"] > r["rankine_max"]            # T-C interaction adds demand
    assert r["gov"]["quadrant"] == "tension-compression"


def test_evaluator_is_not_resolution_divergent():
    # A physical load (M -> 0 at the ends via a parabola) must give a util_max
    # that converges, not one that grows with station count.
    vals = []
    for n in (41, 81, 161, 321):
        sec = _section(n)
        nst = len(sec["I_tr"])
        xr = np.linspace(0.0, 1.0, nst)
        Mparab = 150.0 * (1.0 - (2.0 * xr - 1.0) ** 2)     # 0 at ends, peak mid
        one = np.ones(nst)
        r = kupfer_combined_gate(
            z_grid=sec["z_grid"], cum_area=sec["cum_area"], cum_Az=sec["cum_Az"],
            cum_Iy=sec["cum_Iy"], yhalf=sec["yhalf"], sheer=sec["sheer"],
            y_bar=sec["y_bar"], I_tr=sec["I_tr"], t_in=0.375,
            zone_bounds=[np.zeros(nst), sec["sheer"]], zone_n=[one],
            zone_ft=[one * 150.0], zone_fc=[one * 3000.0],
            instances=[(0, Mparab, np.zeros(nst))], k_lat=0.15)
        vals.append(r["util_max"])
    # finest three must agree within 5% -- convergent, not divergent
    fine = vals[1:]
    assert max(fine) / min(fine) < 1.05, f"resolution-divergent: {vals}"
