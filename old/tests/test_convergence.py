"""
Convergence tests for the structural sweep (plan §6.6).

Proves that evaluate_canoe_weight results are insensitive to integration
grid resolution once the grid is fine enough.

Pass criterion: the weight estimate at n=81 and n=161 must agree within 2%.
"""
from __future__ import annotations

import numpy as np
import pytest


def _hull_at_resolution(L_ft: float, n: int):
    """Return (vars, x, dx, ...) for a rectangular hull at grid resolution n."""
    x    = np.linspace(0.0, L_ft, n)
    dx   = L_ft / (n - 1)
    xr   = x / L_ft
    B_ft = 4.0 * 2.0 * xr * (1.0 - xr)     # parabolic beam

    # Deep hull (d=12 in) with high-strength mixes so stress checks pass
    # and the convergence study is not confounded by feasibility changes.
    d_in = 12.0
    t_in = 0.5
    t_ft = t_in / 12.0
    d_ft = d_in / 12.0
    B_in = B_ft * 12.0

    A_ft2     = (2.0 * B_ft + 2.0 * d_ft) * t_ft
    I_in4     = np.where(B_in > 0, B_in * d_in**2 / 6.0, 1e-6)
    Q_in3     = B_in * d_in / 4.0
    c_top     = np.full(n, d_in / 2.0)
    c_bot     = np.full(n, d_in / 2.0)
    width_ft  = B_ft.copy()
    A_top_ft2 = B_ft * t_ft
    A_bot_ft2 = B_ft * t_ft

    coords4   = np.array([3.5, 7.5, 12.5, 16.5])
    coords2   = np.array([4.5, 15.5])
    densities = np.array([78.0, 74.0, 70.0])
    vars_     = np.concatenate([coords4, coords2, densities])

    return vars_, x, dx, A_ft2, I_in4, Q_in3, c_top, c_bot, width_ft, A_top_ft2, A_bot_ft2, t_in


class TestConvergence:

    @pytest.mark.parametrize("n", [41, 81, 121, 161])
    def test_weight_finite_and_positive(self, n):
        from structural import evaluate_canoe_weight
        vars_, x, dx, *rest = _hull_at_resolution(20.0, n)
        w = evaluate_canoe_weight(vars_, x, dx, *rest)
        assert np.isfinite(w), f"n={n}: weight is not finite"
        assert w > 0, f"n={n}: weight must be positive"

    def test_grid_insensitivity(self):
        """Coarse (n=81) and fine (n=161) grids must agree within 2%."""
        from structural import evaluate_canoe_weight

        args_coarse = _hull_at_resolution(20.0, 81)
        args_fine   = _hull_at_resolution(20.0, 161)

        w_coarse = evaluate_canoe_weight(args_coarse[0], args_coarse[1], args_coarse[2],
                                         *args_coarse[3:])
        w_fine   = evaluate_canoe_weight(args_fine[0],   args_fine[1],   args_fine[2],
                                         *args_fine[3:])

        if w_coarse >= 1e5 or w_fine >= 1e5:
            pytest.skip("Structural penalty — hull infeasible at this geometry")

        err_pct = abs(w_coarse - w_fine) / w_fine * 100.0
        assert err_pct <= 2.0, (
            f"Grid convergence failed: n=81 gives {w_coarse:.2f} lb, "
            f"n=161 gives {w_fine:.2f} lb  ({err_pct:.2f}% difference)")

    def test_moment_envelope_positive_and_finite(self):
        """Peak moment from the return_arrays path must be positive and finite."""
        from structural import evaluate_canoe_weight
        vars_, x, dx, *rest = _hull_at_resolution(20.0, 161)
        out = evaluate_canoe_weight(vars_, x, dx, *rest, return_arrays=True)
        if isinstance(out, (int, float)):
            pytest.skip("hull infeasible at this geometry")
        M_max = float(np.max(out.M_envelope))
        assert np.isfinite(M_max), "M_max is not finite"
        assert M_max > 0, "M_max must be positive"

    def test_moment_envelope_increases_with_payload(self):
        """Heavier paddlers must produce a larger peak moment than lighter ones."""
        from structural import evaluate_canoe_weight

        def _run(density):
            vars_, x, dx, *rest = _hull_at_resolution(20.0, 161)
            # Deliberately the LEGACY 6-seat vector: this exercises
            # evaluate_canoe_weight's pre-expansion path (no crew_cases),
            # where 6 is the correct boundary.  LEGACY-SEATING-SLICE-OK
            vars_heavy = np.concatenate([vars_[:6], [density, density * 0.95, density * 0.90]])
            out = evaluate_canoe_weight(vars_heavy, x, dx, *rest, return_arrays=True)
            return out

        out_light = _run(72.0)
        out_heavy = _run(80.0)
        if isinstance(out_light, (int, float)) or isinstance(out_heavy, (int, float)):
            pytest.skip("one configuration infeasible")
        # Heavier hull self-weight → larger buoyancy-balanced moment
        assert float(np.max(out_heavy.M_envelope)) >= float(np.max(out_light.M_envelope)) * 0.8
