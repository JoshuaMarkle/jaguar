# tests/test_signed_envelopes.py
"""
Signed hog/sag envelope tests (fix plan C2).

Sign convention (structural._solve_beam_case: M = ∬ q_net with net-UPWARD
load positive):
    M > 0  →  SAGGING  →  bottom fiber in tension
    M < 0  →  HOGGING  →  top fiber (gunwales) in tension

Before this fix the envelope collapsed to max|M| and both fibers were
checked against that magnitude, so the governing bending sense and race
case were unrecoverable.
"""
from __future__ import annotations

import numpy as np
import pytest

from structural import calculate_moment_envelope, evaluate_canoe_weight


# ─── Synthetic hull (same shape as test_paddlers) ────────────────────────────

def _synthetic_hull(L_ft: float = 20.0, n: int = 81):
    x  = np.linspace(0.0, L_ft, n)
    dx = L_ft / (n - 1)
    xr = x / L_ft
    width_ft = 4.0 * 2.0 * xr * (1.0 - xr)          # parabolic beam, B_max 2 ft

    d_in, t_in = 6.0, 0.5
    t_ft, d_ft = t_in / 12.0, d_in / 12.0
    width_in = width_ft * 12.0

    A_ft2     = (2.0 * width_ft + 2.0 * d_ft) * t_ft
    I_in4     = np.where(width_in > 0, width_in * d_in ** 2 / 6.0, 1e-6)
    Q_in3     = width_in * d_in / 4.0
    c_top     = np.full(n, d_in / 2.0)
    c_bot     = np.full(n, d_in / 2.0)
    A_top_ft2 = width_ft * t_ft
    A_bot_ft2 = width_ft * t_ft
    return x, dx, A_ft2, I_in4, Q_in3, c_top, c_bot, width_ft, A_top_ft2, A_bot_ft2, t_in


@pytest.fixture(scope="module")
def hull():
    return _synthetic_hull()


# ══════════════════════════════════════════════════════════════════════════
#  calculate_moment_envelope — signed envelopes + governing-case trace
# ══════════════════════════════════════════════════════════════════════════

def _envelope(hull, coords_4, coords_2, w=6.0):
    x, dx = hull[0], hull[1]
    width_ft = hull[7]
    w_self = np.full(len(x), w)
    return calculate_moment_envelope(
        x, dx, w_self, width_ft, coords_4, coords_2, return_all=True)


def test_signed_keys_and_consistency(hull):
    res = _envelope(hull, [8.0, 9.5, 11.0, 12.5], [8.5, 11.5])
    mom = res["moment"]
    for key in ("sag", "hog", "gov_sag", "gov_hog"):
        assert key in mom
    assert "cases" in res and res["cases"][:2] == ["race_4", "race_2"]
    # Envelopes are magnitudes; legacy abs envelope is their pointwise max
    assert np.all(mom["sag"] >= 0) and np.all(mom["hog"] >= 0)
    assert np.allclose(mom["envelope"], np.maximum(mom["sag"], mom["hog"]))
    # Governing indices are valid wherever the envelope is non-zero
    nz = mom["sag"] > 1e-9
    assert np.all((mom["gov_sag"][nz] >= 0)
                  & (mom["gov_sag"][nz] < len(res["cases"])))


def test_packed_seating_sags_midship(hull):
    """All paddlers packed at midship must produce positive (sagging)
    moment there — bottom fiber tension."""
    res = _envelope(hull, [8.0, 9.5, 11.0, 12.5], [8.5, 11.5])
    mid = len(hull[0]) // 2
    assert res["moment"]["race_4"][mid] > 0, "packed 4P must sag at midship"
    assert res["moment"]["sag"][mid] > res["moment"]["hog"][mid]


def test_spread_seating_hogs_midship_and_case_is_traced(hull):
    """4P packed midship (sagging) + 2P spread to the ends (hogging):
    the sag envelope at midship must trace to race_4 and the hog envelope
    to race_2 — this is exactly the 'which case governs' question."""
    res = _envelope(hull, [8.0, 9.5, 11.0, 12.5], [2.5, 17.5])
    mom = res["moment"]
    mid = len(hull[0]) // 2

    assert mom["race_2"][mid] < 0, "spread 2P must hog at midship"
    assert mom["sag"][mid] > 0 and mom["hog"][mid] > 0

    i_sag = res["cases"][int(mom["gov_sag"][mid])]
    i_hog = res["cases"][int(mom["gov_hog"][mid])]
    assert i_sag == "race_4"
    assert i_hog == "race_2"


def test_envelope_covers_every_case_pointwise(hull):
    """sag/hog envelopes must bound every per-case signed curve."""
    res = _envelope(hull, [3.0, 7.5, 12.0, 15.0], [4.0, 14.0])
    mom = res["moment"]
    for case in ("race_4", "race_2"):
        M = mom[case]
        assert np.all(mom["sag"] >= np.maximum(M, 0.0) - 1e-9)
        assert np.all(mom["hog"] >= np.maximum(-M, 0.0) - 1e-9)


# ══════════════════════════════════════════════════════════════════════════
#  evaluate_canoe_weight — fiber-correct checks + utilization arrays
#
#  Mix properties are MONKEYPATCHED to fixed values: the production
#  config.get_mix_properties reads the user's live concrete_mixes.db
#  (published regression), which changes between sessions and must never
#  decide whether these tests pass.
# ══════════════════════════════════════════════════════════════════════════

_VARS = np.array([3.0, 7.5, 12.0, 15.0,   # 4P knee positions [ft]
                  4.0, 14.0,               # 2P knee positions [ft]
                  80.0, 62.0])             # mix densities [pcf]

_FIXED_MIXES = {
    80.0: {"density": 80.0, "strength_t": 380.0, "strength_c": 1800.0},
    62.0: {"density": 62.0, "strength_t": 150.0, "strength_c": 800.0},
}


@pytest.fixture()
def fixed_mixes(monkeypatch):
    import config
    monkeypatch.setattr(config, "get_mix_properties",
                        lambda d: dict(_FIXED_MIXES[float(d)]))


def test_result_carries_signed_envelopes_and_utilization(hull, fixed_mixes):
    x, dx, A_ft2, I_in4, Q_in3, c_top, c_bot, width_ft, A_top, A_bot, t_in = hull
    sr = evaluate_canoe_weight(
        _VARS, x, dx, A_ft2, I_in4, Q_in3, c_top, c_bot, width_ft,
        A_top, A_bot, t_in, return_arrays=True)

    assert hasattr(sr, "M_envelope"), f"expected feasible result, got {sr!r}"
    assert sr.M_sag_env is not None and sr.M_hog_env is not None
    assert np.allclose(sr.M_envelope,
                       np.maximum(sr.M_sag_env, sr.M_hog_env))
    assert sr.case_names and sr.case_names[0] == "race_4"

    # Feasible ⇒ every TENSION utilization ≤ 1 (FOS already inside the
    # allowables); compression utilization is advisory but must be sane.
    for u in (sr.util_bot_t, sr.util_top_t):
        assert u is not None
        assert np.all(np.isfinite(u)) and np.all(u >= 0.0)
        assert np.all(u <= 1.0 + 1e-9)
    for u in (sr.util_bot_c, sr.util_top_c):
        assert u is not None
        assert np.all(np.isfinite(u)) and np.all(u >= 0.0)


def test_fiber_correct_is_not_more_conservative(hull, fixed_mixes):
    """The old check applied max|M| to BOTH fibers; the signed-moment part
    of the fix can only relax that (each fiber sees its own governing
    sense).  τ, by contrast, was CORRECTED upward (full |V| envelope,
    C4.4) — this hull has comfortable shear headroom, so it must remain
    feasible under the corrected demand."""
    x, dx, A_ft2, I_in4, Q_in3, c_top, c_bot, width_ft, A_top, A_bot, t_in = hull
    w = evaluate_canoe_weight(
        _VARS, x, dx, A_ft2, I_in4, Q_in3, c_top, c_bot, width_ft,
        A_top, A_bot, t_in)
    import structural
    assert not structural.is_rejected(w) and w > 0


def test_infeasible_still_penalised(hull, fixed_mixes):
    """Crushed inertia must still be REJECTED (no mass, not a heavy hull)."""
    x, dx, A_ft2, I_in4, Q_in3, c_top, c_bot, width_ft, A_top, A_bot, t_in = hull
    w = evaluate_canoe_weight(
        _VARS, x, dx, A_ft2, I_in4 * 1e-6, Q_in3, c_top, c_bot, width_ft,
        A_top, A_bot, t_in)
    import structural
    assert w is structural.REJECTED
