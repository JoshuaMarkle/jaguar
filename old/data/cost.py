# data/cost.py
"""
Material cost estimation for the concrete canoe.

Computes volume (yd³) and cost (USD) per mix zone from a structural
sweep result dict.  Pure math — no Qt, no I/O.
"""
from __future__ import annotations

from typing import Any

import numpy as np

_FT3_PER_YD3 = 27.0


def estimate_cost(
    result: dict[str, Any],
    cost_per_yd3: dict[int, float],
) -> dict[str, Any]:
    """
    Compute material cost breakdown by mix zone.

    Parameters
    ----------
    result : dict from run_grand_optimization — must contain keys
             'layout'  (n×2 int array, values 1/2/3),
             'A_bot_ft2' (n,) float array,
             'A_top_ft2' (n,) float array,
             'dx'      scalar [ft]
    cost_per_yd3 : mapping {mix_index: $/yd³} for each mix (1, 2, 3)

    Returns
    -------
    dict with keys:
        mix_volumes_ft3   — {1: v1, 2: v2, 3: v3} in ft³
        mix_volumes_yd3   — {1: v1, 2: v2, 3: v3} in yd³
        mix_costs_usd     — {1: c1, 2: c2, 3: c3} in USD
        total_volume_yd3  — float
        total_cost_usd    — float
    """
    layout    = np.asarray(result["layout"])         # (n, 2) int
    A_bot     = np.asarray(result["A_bot_ft2"])      # (n,) ft²
    A_top     = np.asarray(result["A_top_ft2"])      # (n,) ft²
    dx        = float(result["dx"])                  # ft

    mix_vol_ft3: dict[int, float] = {1: 0.0, 2: 0.0, 3: 0.0}
    for mix_idx in (1, 2, 3):
        bot_mask = layout[:, 0] == mix_idx
        top_mask = layout[:, 1] == mix_idx
        vol = (float(A_bot[bot_mask].sum()) + float(A_top[top_mask].sum())) * dx
        mix_vol_ft3[mix_idx] = vol

    mix_vol_yd3 = {k: v / _FT3_PER_YD3 for k, v in mix_vol_ft3.items()}
    mix_cost    = {k: mix_vol_yd3[k] * cost_per_yd3.get(k, 0.0) for k in (1, 2, 3)}

    return {
        "mix_volumes_ft3":  mix_vol_ft3,
        "mix_volumes_yd3":  mix_vol_yd3,
        "mix_costs_usd":    mix_cost,
        "total_volume_yd3": sum(mix_vol_yd3.values()),
        "total_cost_usd":   sum(mix_cost.values()),
    }
