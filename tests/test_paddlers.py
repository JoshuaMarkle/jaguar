"""
Numerical-stability tests for paddler placement (plan §6.5).

Verifies:
1. evaluate_canoe_weight is deterministic (pure function)
2. apply_realistic_paddler_loads satisfies biomechanics invariants
3. SLSQP paddler placement converges and satisfies min-spacing constraints
"""
from __future__ import annotations

import numpy as np
from np_compat import trapezoid
import pytest
from scipy.optimize import minimize

import config as _cfg


# ─── Synthetic rectangular hull ──────────────────────────────────────────────

def _synthetic_hull(L_ft: float = 20.0, n: int = 41):
    """
    Thin-walled rectangular canoe cross-section.
    Returns all arrays needed by evaluate_canoe_weight.
    """
    x    = np.linspace(0.0, L_ft, n)
    dx   = L_ft / (n - 1)

    # Parabolic beam: 0 at ends, B_max at midship
    B_max_ft = 2.0
    xr = x / L_ft
    width_ft = 4.0 * B_max_ft * xr * (1.0 - xr)

    d_in  = 6.0    # depth
    t_in  = 0.5    # shell thickness
    t_ft  = t_in / 12.0
    d_ft  = d_in / 12.0
    width_in = width_ft * 12.0

    # Thin-wall perimeter area
    A_ft2     = (2.0 * width_ft + 2.0 * d_ft) * t_ft
    I_in4     = np.where(width_in > 0, width_in * d_in**2 / 6.0, 1e-6)
    Q_in3     = width_in * d_in / 4.0
    c_top     = np.full(n, d_in / 2.0)
    c_bot     = np.full(n, d_in / 2.0)
    A_top_ft2 = width_ft * t_ft
    A_bot_ft2 = width_ft * t_ft

    return x, dx, A_ft2, I_in4, Q_in3, c_top, c_bot, width_ft, A_top_ft2, A_bot_ft2, t_in


# ─── Shared fixture ──────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def hull():
    return _synthetic_hull()


# ══════════════════════════════════════════════════════════════════════════════
#  1. Determinism of evaluate_canoe_weight
# ══════════════════════════════════════════════════════════════════════════════

class TestEvaluateCanoeWeightDeterminism:

    def test_same_input_same_output(self, hull):
        from structural import evaluate_canoe_weight
        x, dx, A_ft2, I_in4, Q_in3, c_top, c_bot, width_ft, A_top_ft2, A_bot_ft2, t_in = hull
        L = float(np.max(x))
        vars_ = np.array([3.0, 7.5, 12.0, 15.0, 4.0, 14.0, 78.0, 68.0, 60.0])
        r1 = evaluate_canoe_weight(vars_, x, dx, A_ft2, I_in4, Q_in3, c_top, c_bot,
                                   width_ft, A_top_ft2, A_bot_ft2, t_in)
        r2 = evaluate_canoe_weight(vars_, x, dx, A_ft2, I_in4, Q_in3, c_top, c_bot,
                                   width_ft, A_top_ft2, A_bot_ft2, t_in)
        assert r1 == r2, f"Non-deterministic: {r1} != {r2}"

    def test_positive_weight_or_penalty(self, hull):
        """Return value must be either a positive weight or the 1e6 penalty."""
        from structural import evaluate_canoe_weight
        x, dx, A_ft2, I_in4, Q_in3, c_top, c_bot, width_ft, A_top_ft2, A_bot_ft2, t_in = hull
        vars_ = np.array([3.0, 7.5, 12.0, 15.0, 4.0, 14.0, 78.0, 68.0, 60.0])
        r = evaluate_canoe_weight(vars_, x, dx, A_ft2, I_in4, Q_in3, c_top, c_bot,
                                  width_ft, A_top_ft2, A_bot_ft2, t_in)
        assert r > 0, "Weight/penalty must be positive"

    @pytest.mark.parametrize("seed", [0, 1, 2])
    def test_random_valid_coords_consistent(self, hull, seed):
        """Randomly perturbed but valid coord sets should give consistent (reproducible) results."""
        from structural import evaluate_canoe_weight
        x, dx, A_ft2, I_in4, Q_in3, c_top, c_bot, width_ft, A_top_ft2, A_bot_ft2, t_in = hull
        rng = np.random.default_rng(seed)
        coords4 = np.sort(rng.uniform(2.0, 16.0, 4))
        coords2 = np.sort(rng.uniform(2.0, 16.0, 2))
        vars_ = np.concatenate([coords4, coords2, [78.0, 68.0, 60.0]])
        r1 = evaluate_canoe_weight(vars_, x, dx, A_ft2, I_in4, Q_in3, c_top, c_bot,
                                   width_ft, A_top_ft2, A_bot_ft2, t_in)
        r2 = evaluate_canoe_weight(vars_, x, dx, A_ft2, I_in4, Q_in3, c_top, c_bot,
                                   width_ft, A_top_ft2, A_bot_ft2, t_in)
        assert r1 == r2


# ══════════════════════════════════════════════════════════════════════════════
#  2. Biomechanics invariants for apply_realistic_paddler_loads
# ══════════════════════════════════════════════════════════════════════════════

class TestPaddlerLoadBiomechanics:

    def test_total_load_equals_input_weight(self):
        """Integral of distributed load must equal sum of paddler weights.

        Uses a fine grid (dx = 0.05 ft < 0.25 ft knee footprint) so that
        the trapezoid discretisation does not smear the knee load over
        adjacent elements.
        """
        from structural import apply_realistic_paddler_loads
        L = 20.0
        n = 401  # dx = 0.05 ft  < 0.25 ft knee span
        x  = np.linspace(0.0, L, n)
        dx = L / (n - 1)
        coords  = [5.0, 10.0]
        weights = [150.0, 160.0]
        w = apply_realistic_paddler_loads(x, dx, coords, weights, L)
        integrated = trapezoid(w, x)
        expected   = sum(weights)
        assert abs(integrated - expected) / expected < 0.02, (
            f"Load integral {integrated:.2f} != paddler weights {expected:.2f}")

    def _fine_hull(self):
        L, n = 20.0, 401
        x  = np.linspace(0.0, L, n)
        dx = L / (n - 1)
        return x, dx, L

    def test_load_non_negative(self):
        from structural import apply_realistic_paddler_loads
        x, dx, L = self._fine_hull()
        w = apply_realistic_paddler_loads(x, dx, [5.0, 10.0, 14.0], [160.0]*3, L)
        assert np.all(w >= 0), "Paddler loads must be non-negative"

    def test_single_paddler_centroid_near_coord(self):
        """Centroid of load distribution must be near the knee coordinate."""
        from structural import apply_realistic_paddler_loads
        x, dx, L = self._fine_hull()
        knee_loc = 8.0
        w = apply_realistic_paddler_loads(x, dx, [knee_loc], [200.0], L)
        centroid = trapezoid(w * x, x) / trapezoid(w, x)
        # Centroid shifts toward toes (17 in behind knees = +1.417 ft)
        # So centroid ≈ 0.80*knee + 0.20*(knee + 1.417) = knee + 0.2*1.417
        expected_centroid = knee_loc + 0.20 * (17.0 / 12.0)
        assert abs(centroid - expected_centroid) < 0.5, (
            f"Load centroid {centroid:.3f} too far from expected {expected_centroid:.3f}")


# ══════════════════════════════════════════════════════════════════════════════
#  3. SLSQP paddler placement convergence
# ══════════════════════════════════════════════════════════════════════════════

class TestSLSQPPaddlerPlacement:

    def _run_slsqp(self, hull):
        from structural import evaluate_canoe_weight
        x, dx, A_ft2, I_in4, Q_in3, c_top, c_bot, width_ft, A_top_ft2, A_bot_ft2, t_in = hull
        L = float(np.max(x))
        densities = [78.0, 68.0, 60.0]

        def obj(pv):
            return evaluate_canoe_weight(
                np.concatenate([pv, densities]),
                x, dx, A_ft2, I_in4, Q_in3, c_top, c_bot,
                width_ft, A_top_ft2, A_bot_ft2, t_in,
            )

        x0 = np.array([3.0, 7.5, 12.0, 15.0, 4.0, 14.0])
        bnds = tuple((2.0, L - 3.5) for _ in range(6))
        cons = [
            {'type': 'ineq', 'fun': lambda v: v[1] - v[0] - 3.4},
            {'type': 'ineq', 'fun': lambda v: v[2] - v[1] - 3.4},  # LEGACY-SEATING-SLICE-OK
            {'type': 'ineq', 'fun': lambda v: v[3] - v[2] - 3.4},
            {'type': 'ineq', 'fun': lambda v: v[5] - v[4] - 3.4},
        ]
        # Use the PRODUCTION solver options. This used to hardcode
        # eps=0.5/maxiter=30 -- a divergent copy of settings the sweep had
        # since changed, which is exactly how the flat-objective pathology
        # stayed hidden (a hardcoded eps large enough to straddle the step
        # function made the solver look healthy). Import the real thing so the
        # two can never drift apart again.
        from main import SEATING_SLSQP_OPTS
        return minimize(obj, x0, method='SLSQP', bounds=bnds, constraints=cons,
                        options=dict(SEATING_SLSQP_OPTS))

    def test_slsqp_converges(self, hull):
        res = self._run_slsqp(hull)
        # SLSQP status 0 = success; 9 = iteration limit — both acceptable
        assert res.status in (0, 9), f"SLSQP failed with status {res.status}: {res.message}"

    def test_slsqp_reproducible(self, hull):
        """Two SLSQP runs from the same x0 must give identical results."""
        res1 = self._run_slsqp(hull)
        res2 = self._run_slsqp(hull)
        assert abs(res1.fun - res2.fun) < 1e-6, (
            f"SLSQP non-reproducible: {res1.fun} vs {res2.fun}")

    def test_slsqp_spacing_constraints(self, hull):
        """The optimal solution must respect the minimum 3.4 ft inter-paddler spacing."""
        res = self._run_slsqp(hull)
        if res.status not in (0, 9):
            pytest.skip("SLSQP did not converge")
        v = res.x
        # Self-contained 6-variable problem built in this test, NOT
        # production's constraint builder -- 4 contiguous seats here, so
        # adjacent differences are real gaps.  LEGACY-SEATING-SLICE-OK
        min_gap_4 = min(v[1]-v[0], v[2]-v[1], v[3]-v[2])
        min_gap_2 = v[5] - v[4]
        assert min_gap_4 >= 3.0, f"4-person min gap {min_gap_4:.3f} ft below 3 ft"
        assert min_gap_2 >= 3.0, f"2-person gap {min_gap_2:.3f} ft below 3 ft"
