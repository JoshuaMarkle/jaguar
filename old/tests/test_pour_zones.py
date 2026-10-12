# tests/test_pour_zones.py
"""
Depth-based mix zoning + pour line tests (fix plan C3).

The evaluator assigns each station/fiber the LIGHTEST mix whose allowable
covers the local signed demand, solves the pour hand-off height z*(x) from
the linear bending-stress profile, and (when the cumulative-area table is
provided) splits the weight integral at z*(x) instead of the neutral axis.

Mix properties are monkeypatched — the live concrete_mixes.db regression
must never decide whether these tests pass.
"""
from __future__ import annotations

import numpy as np
import pytest

from geometry import calculate_shell_properties
from structural import evaluate_canoe_weight

# Light-mix tensile allowable chosen BELOW the hull's peak fiber demands
# (~52 psi bottom / ~60 psi top on this test hull) so the assignment must
# mix: heavy where demand exceeds 45 psi, light elsewhere.
_LIGHT_T = 45.0
_FIXED_MIXES = {
    80.0: {"density": 80.0, "strength_t": 380.0,   "strength_c": 1800.0},
    62.0: {"density": 62.0, "strength_t": _LIGHT_T, "strength_c": 800.0},
}


@pytest.fixture()
def fixed_mixes(monkeypatch):
    import config
    monkeypatch.setattr(config, "get_mix_properties",
                        lambda d: dict(_FIXED_MIXES[float(d)]))


# ─── Hull: elliptical sections via the real shell-properties path ────────────

def _hull(n_st=41, n_wl=15, L_ft=20.0, depth_in=12.0, half_beam_in=14.0,
          t_in=0.5):
    x_st = np.linspace(0.0, L_ft, n_st)
    z = np.linspace(0.0, depth_in, n_wl)
    w = half_beam_in * np.sin(np.pi * x_st / L_ft)
    prof = np.sqrt(z / depth_in)
    y_matrix = np.outer(w, prof)                      # [in]

    props = calculate_shell_properties(x_st, z, y_matrix, t_in)
    valid = props.A_in2 > 0.001
    g = dict(
        x=x_st[valid], dx=L_ft / (n_st - 1), t_in=t_in,
        A_ft2=props.A_in2[valid] / 144.0,
        A_top_ft2=props.A_top_in2[valid] / 144.0,
        A_bot_ft2=props.A_bot_in2[valid] / 144.0,
        I_in4=props.I_in4[valid], Q_in3=props.Q_in3[valid],
        c_top=props.c_top[valid], c_bot=props.c_bot[valid],
        width_ft=props.width_ft[valid],
        cum=props.cum_area_in2[valid], zg=props.z_grid_in,
    )
    return g, props, valid


_VARS = np.array([3.0, 7.5, 12.0, 15.0, 4.0, 14.0, 80.0, 62.0])


def _run(g, with_cum=True):
    return evaluate_canoe_weight(
        _VARS, g["x"], g["dx"], g["A_ft2"], g["I_in4"], g["Q_in3"],
        g["c_top"], g["c_bot"], g["width_ft"],
        g["A_top_ft2"], g["A_bot_ft2"], g["t_in"], return_arrays=True,
        cum_area_in2=(g["cum"] if with_cum else None),
        z_grid_in=(g["zg"] if with_cum else None))


# ══════════════════════════════════════════════════════════════════════════
#  Cumulative-area table (geometry side)
# ══════════════════════════════════════════════════════════════════════════

def test_cum_area_table_is_consistent():
    g, props, valid = _hull()
    cum, zg = g["cum"], g["zg"]
    # monotone non-decreasing rows; top of the table = full section area
    assert np.all(np.diff(cum, axis=1) >= -1e-12)
    np.testing.assert_allclose(cum[:, -1], props.A_in2[valid], rtol=1e-9)
    # area below the NA matches the legacy NA split (same z_c <= z rule,
    # up to one segment straddling the axis)
    from structural import _area_below
    A_na = _area_below(cum, zg, g["c_bot"])
    seg = props.A_in2[valid] / (len(zg) - 1)          # ≈ one segment's area
    assert np.all(np.abs(A_na - props.A_bot_in2[valid]) <= seg + 1e-9)


# ══════════════════════════════════════════════════════════════════════════
#  Per-fiber assignment + pour line
# ══════════════════════════════════════════════════════════════════════════

def test_assignment_is_lightest_adequate(fixed_mixes):
    g, _, _ = _hull()
    sr = _run(g)
    assert hasattr(sr, "pour_layout"), f"expected feasible result, got {sr!r}"

    # Feasible: every tension utilization within 1
    assert np.all(sr.util_bot_t <= 1.0 + 1e-9)
    assert np.all(sr.util_top_t <= 1.0 + 1e-9)

    # Wherever the HEAVY mix (index 0) was assigned to a fiber, the light
    # mix must genuinely be inadequate there (lightest-adequate rule):
    # demand = util * allow(assigned) must exceed the light allowable.
    for col, util in ((0, sr.util_bot_t), (1, sr.util_top_t)):
        heavy = sr.pour_layout[:, col] == 0
        p = util * np.where(heavy, 380.0, _LIGHT_T)
        assert np.all(p[heavy] > _LIGHT_T - 1e-6), \
            "heavy mix assigned where the light mix would have sufficed"
    # Both mixes must actually appear (high-moment midship vs quiet ends)
    assert np.any(sr.pour_layout == 0), "no heavy-mix assignments at all"
    assert np.any(sr.pour_layout == 1), "no light-mix assignments at all"


def test_pour_line_between_keel_and_na_for_sagging(fixed_mixes):
    g, _, _ = _hull()
    sr = _run(g)
    lay = sr.pour_layout
    z = sr.pour_line_in

    mixed = lay[:, 0] != lay[:, 1]
    same = ~mixed
    assert np.all(np.isnan(z[same])), "single-mix stations must report NaN"
    assert np.any(mixed), "expected mixed-depth stations on this hull"

    # heavier below (sagging-governed): hand-off strictly below/at the NA
    hb = mixed & (lay[:, 0] == 0) & (lay[:, 1] == 1)
    if np.any(hb):
        assert np.all(z[hb] >= -1e-9)
        assert np.all(z[hb] <= sr.na_line[hb] + 1e-9)
    # heavier above (hogging-governed): hand-off at/above the NA
    ha = mixed & (lay[:, 0] == 1) & (lay[:, 1] == 0)
    if np.any(ha):
        assert np.all(z[ha] >= sr.na_line[ha] - 1e-9)
        assert np.all(z[ha] <= sr.sheer_line_in[ha] + 1e-9)


def test_pour_line_matches_stress_profile(fixed_mixes):
    """z* must sit exactly where the light mix's principal stress hits its
    allowable ( p(z) ≤ a  ⇔  σ(z) ≤ σ_eff = a − τ²/a ), for whichever
    hand-off direction occurs.  No gunwales, so section properties pass
    through unmodified."""
    g, _, _ = _hull()
    sr = _run(g)
    lay, z = sr.pour_layout, sr.pour_line_in

    # τ from the full |V| envelope (C4.4)
    V_mag = np.maximum(sr.V_max_pos, -sr.V_max_neg)
    tau = V_mag * g["Q_in3"] / (g["I_in4"] * 2.0 * g["t_in"])
    a = _LIGHT_T
    sig_eff = np.maximum(a - tau ** 2 / a, 0.0)

    hb = (lay[:, 0] == 0) & (lay[:, 1] == 1) & np.isfinite(z)  # heavy below
    ha = (lay[:, 0] == 1) & (lay[:, 1] == 0) & np.isfinite(z)  # heavy above
    assert np.any(hb) or np.any(ha), "no mixed-depth stations to verify"

    if np.any(hb):
        sig_bot = (sr.M_sag_env * 12.0) * g["c_bot"] / g["I_in4"]
        frac = np.clip(sig_eff / np.maximum(sig_bot, 1e-9), 0.0, 1.0)
        np.testing.assert_allclose(z[hb], (g["c_bot"] * (1.0 - frac))[hb],
                                   atol=1e-6)
    if np.any(ha):
        sig_top = (sr.M_hog_env * 12.0) * g["c_top"] / g["I_in4"]
        frac = np.clip(sig_eff / np.maximum(sig_top, 1e-9), 0.0, 1.0)
        np.testing.assert_allclose(z[ha], (g["c_bot"] + g["c_top"] * frac)[ha],
                                   atol=1e-6)


def test_depth_split_saves_weight(fixed_mixes):
    """Splitting the weight at z*(x) (heavy band only as deep as demand
    requires) must never be heavier than the legacy NA split."""
    g, _, _ = _hull()
    w_cum = _run(g, with_cum=True).weight_lbs
    w_na = _run(g, with_cum=False).weight_lbs
    assert w_cum <= w_na + 1e-6, (w_cum, w_na)
    assert w_cum > 0
