"""
Tests for structural_uncertainty.run_monte_carlo (plan §6.4).

Verifies:
1. MCResult has the right shape and sensible statistics
2. Feasibility rate is reasonable for a well-designed hull
3. P5 < mean < P95 invariant holds
4. Result is reproducible (same seed → same output)
"""
from __future__ import annotations

import numpy as np
import pytest

import config

# ── Service-level load calibration ──────────────────────────────────────────
@pytest.fixture(autouse=True)
def _service_level_loads(monkeypatch):
    """Pin these tests to ASD (service-level) loads.

    The suite's default methodology is LRFD, whose factored demand on this hull
    is ~1.7x the service-level demand these fixtures were sized against -- the
    stub mixes and geometries below were chosen so a design merely *clears*, and
    clearing service loads does not imply clearing factored ones.

    The tests in this file characterise evaluator MECHANICS (a feasible stack
    returns a positive weight, the distinct-mix cap gates, the sweep worker
    finds a configuration), which are orthogonal to how loads and capacities are
    factored.  Pinning ASD keeps that orthogonality EXPLICIT rather than an
    accident of whatever the default happens to be -- the failure mode that
    made six of these break when the default moved to LRFD.

    LRFD/ASD behaviour itself is covered by tests/test_lrfd_asd.py.
    """
    monkeypatch.setattr(config, "DESIGN_METHOD", "ASD", raising=False)
    monkeypatch.setattr(config, "OMEGA_SAFETY", 1.5, raising=False)



def _make_hull(L_ft: float = 20.0, n: int = 81):
    x  = np.linspace(0.0, L_ft, n)
    dx = L_ft / (n - 1)
    xr = x / L_ft
    B_ft = 4.0 * 2.0 * xr * (1.0 - xr)

    d_in = 12.0; t_in = 0.5; t_ft = t_in / 12.0; d_ft = d_in / 12.0
    B_in = B_ft * 12.0
    A_ft2     = (2.0 * B_ft + 2.0 * d_ft) * t_ft
    I_in4     = np.where(B_in > 0, B_in * d_in**2 / 6.0, 1e-6)
    Q_in3     = B_in * d_in / 4.0
    c_top     = np.full(n, d_in / 2.0)
    c_bot     = np.full(n, d_in / 2.0)
    width_ft  = B_ft.copy()
    A_top_ft2 = B_ft * t_ft
    A_bot_ft2 = B_ft * t_ft

    coords4 = np.array([0.2 * L_ft, 0.35 * L_ft, 0.65 * L_ft, 0.8 * L_ft])
    coords2 = np.array([0.3 * L_ft, 0.7 * L_ft])
    densities = np.array([78.0, 74.0, 70.0])
    base_vars = np.concatenate([coords4, coords2, densities])

    return base_vars, x, dx, A_ft2, I_in4, Q_in3, c_top, c_bot, width_ft, A_top_ft2, A_bot_ft2, t_in


class TestMonteCarlo:

    def _run(self, seed=42, n=100):
        from structural_uncertainty import run_monte_carlo
        base_vars, x, dx, A_ft2, I_in4, Q_in3, c_top, c_bot, width_ft, A_top_ft2, A_bot_ft2, t_in = _make_hull()
        return run_monte_carlo(
            base_vars, x, dx, A_ft2, I_in4, Q_in3, c_top, c_bot, width_ft, A_top_ft2, A_bot_ft2, t_in,
            n_samples=n, density_cv=0.04, rng_seed=seed,
        )

    def test_result_shape(self):
        mc = self._run(n=100)
        assert mc.n_samples == 100
        assert 0 <= mc.n_feasible <= 100
        assert len(mc.weights) == mc.n_feasible
        assert len(mc.M_peaks) == mc.n_feasible

    def test_feasibility_reasonable(self):
        """A well-designed hull should have ≥ 50% feasible samples."""
        mc = self._run(n=200)
        feas_rate = mc.n_feasible / mc.n_samples
        assert feas_rate >= 0.5, (
            f"Feasibility rate {feas_rate:.0%} unexpectedly low — check hull geometry")

    def test_p5_lt_mean_lt_p95(self):
        mc = self._run(n=200)
        if mc.n_feasible < 10:
            pytest.skip("too few feasible samples")
        assert mc.weight_p5 < mc.weight_mean < mc.weight_p95, (
            "Weight quantile order violated")
        assert mc.M_peak_p5 < mc.M_peak_mean < mc.M_peak_p95, (
            "M_peak quantile order violated")

    def test_reproducible(self):
        """Same seed must produce identical statistics."""
        mc1 = self._run(seed=7, n=50)
        mc2 = self._run(seed=7, n=50)
        assert mc1.n_feasible == mc2.n_feasible
        assert mc1.weight_mean == mc2.weight_mean

    def test_summary_string(self):
        mc = self._run(n=50)
        s = mc.summary()
        assert "Monte Carlo" in s
        assert "Weight" in s
        assert "M_peak" in s
