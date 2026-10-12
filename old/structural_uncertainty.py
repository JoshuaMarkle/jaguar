# structural_uncertainty.py
"""
Monte Carlo uncertainty quantification for canoe weight and peak bending moment.

Perturbs mix densities (±CV%) to build empirical distributions.  Fixed geometry
(thickness, hull shape) keeps each trial cheap — only evaluate_canoe_weight is
called, so 500 samples typically complete in < 5 seconds.

Usage
-----
    from structural_uncertainty import run_monte_carlo
    result = run_monte_carlo(base_vars, x, dx, A_ft2, I_in4, Q_in3,
                             c_top, c_bot, width_ft, A_top_ft2, A_bot_ft2, t_in)
    print(result.summary())
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np


@dataclass
class MCResult:
    """Empirical distributions from N Monte Carlo trials."""
    n_samples: int
    n_feasible: int

    weights: np.ndarray       # [lb] — feasible trials only
    M_peaks: np.ndarray       # peak moment envelope value [ft·lb] — feasible only

    weight_mean: float
    weight_std: float
    weight_p5: float
    weight_p95: float

    M_peak_mean: float
    M_peak_std: float
    M_peak_p5: float
    M_peak_p95: float

    density_cv_used: float    # actual CV applied

    def summary(self) -> str:
        lines = [
            f"Monte Carlo  ({self.n_feasible}/{self.n_samples} feasible,"
            f" density CV={self.density_cv_used*100:.1f}%)",
            "─" * 56,
            f"Weight   mean={self.weight_mean:7.2f} lb  std={self.weight_std:6.2f} lb",
            f"         P5 ={self.weight_p5:7.2f} lb  P95={self.weight_p95:7.2f} lb",
            "─" * 56,
            f"M_peak   mean={self.M_peak_mean:7.1f} ft·lb  std={self.M_peak_std:6.1f} ft·lb",
            f"         P5 ={self.M_peak_p5:7.1f} ft·lb  P95={self.M_peak_p95:7.1f} ft·lb",
        ]
        return "\n".join(lines)


def run_monte_carlo(
    base_vars: np.ndarray,
    x: np.ndarray,
    dx: float,
    A_ft2: np.ndarray,
    I_in4: np.ndarray,
    Q_in3: np.ndarray,
    c_top: np.ndarray,
    c_bot: np.ndarray,
    width_ft: np.ndarray,
    A_top_ft2: np.ndarray,
    A_bot_ft2: np.ndarray,
    t_in: float,
    *,
    n_samples: int = 500,
    density_cv: float = 0.04,
    rng_seed: int | None = 42,
    progress_callback: Callable[[float, str], None] | None = None,
) -> MCResult:
    """
    Run a Monte Carlo study by perturbing mix densities around the optimal design.

    Parameters
    ----------
    base_vars       : array [coord4×4, coord2×2, density×n_mixes]  (7–9 elements)
    x .. t_in       : geometry arrays exactly as passed to evaluate_canoe_weight
    n_samples       : number of Monte Carlo draws (default 500)
    density_cv      : coefficient of variation for each mix density (default 0.04 = 4%)
    rng_seed        : random seed for reproducibility (None for random)
    progress_callback: (fraction, label) — called periodically during sampling
    """
    from structural import evaluate_canoe_weight

    rng = np.random.default_rng(rng_seed)

    base_densities = base_vars[6:].copy()   # 1, 2, or 3 mix densities
    n_mix_types    = len(base_densities)
    coords         = base_vars[0:6].copy()

    weights: list[float] = []
    M_peaks: list[float] = []
    n_infeasible = 0

    report_interval = max(1, n_samples // 20)

    for i in range(n_samples):
        if progress_callback and (i % report_interval == 0):
            progress_callback(i / n_samples, f"MC sample {i}/{n_samples}")

        perturbed_d = base_densities * (1.0 + rng.normal(0.0, density_cv, n_mix_types))
        # Keep densities physically plausible (50–120 pcf)
        perturbed_d = np.clip(perturbed_d, 50.0, 120.0)
        # Preserve heavy ≥ ... ≥ light ordering
        perturbed_d = np.sort(perturbed_d)[::-1]

        trial_vars = np.concatenate([coords, perturbed_d])

        out = evaluate_canoe_weight(
            trial_vars, x, dx, A_ft2, I_in4, Q_in3,
            c_top, c_bot, width_ft, A_top_ft2, A_bot_ft2, t_in,
            return_arrays=True,
        )

        if isinstance(out, (int, float)) or not getattr(out, "is_feasible", False):
            n_infeasible += 1
            continue

        weights.append(out.weight_lbs)
        M_peaks.append(float(np.max(out.M_envelope)))

    if progress_callback:
        progress_callback(1.0, "MC complete")

    w_arr = np.array(weights)
    m_arr = np.array(M_peaks)
    n_feas = len(w_arr)

    def _stats(arr):
        if len(arr) == 0:
            return 0.0, 0.0, 0.0, 0.0
        return (float(arr.mean()), float(arr.std()),
                float(np.percentile(arr, 5)), float(np.percentile(arr, 95)))

    wm, ws, wp5, wp95 = _stats(w_arr)
    mm, ms, mp5, mp95 = _stats(m_arr)

    return MCResult(
        n_samples=n_samples,
        n_feasible=n_feas,
        weights=w_arr,
        M_peaks=m_arr,
        weight_mean=wm, weight_std=ws, weight_p5=wp5, weight_p95=wp95,
        M_peak_mean=mm, M_peak_std=ms, M_peak_p5=mp5, M_peak_p95=mp95,
        density_cv_used=density_cv,
    )
