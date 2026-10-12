# tests/test_multizone_bound.py
"""
Admissible lower bound for the multi-zone (I-beam) branch-and-bound prune.

The multi-zone weight is  Σ densityₖ·Vₖ  with zone volumes Vₖ ≥ 0 that
telescope to a pour-fraction-independent total V_total, so
    weight ≥ min(combo)·V_total
is a provable lower bound over ALL of a combo's pour-line fractions
(main.min_density_weight_bound).  These tests pin that guarantee: a bounded
walk must return exactly the brute-force winner, and the bound must never
exceed an actual weight — so a future change to evaluate_multizone_weight that
broke the telescoping would fail here instead of silently pruning the optimum.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

# is_rejected must be tested FIRST in every feasibility guard below: the
# REJECTED sentinel is not a number, so np.isnan(w) on it raises TypeError
# rather than answering the question.  The `w >= 1e5` half is the pre-sentinel
# idiom, kept only because a genuine heavy hull can still appear.
from structural import is_rejected
import pytest
from np_compat import trapezoid

from main import min_density_bound_order, min_density_weight_bound
from structural import (evaluate_multizone_weight, multizone_pour_grids,
                        ibeam_zone_stack, _cum_at, gunwale_rail_section)

_ROOT = Path(__file__).resolve().parent.parent
_COORDS4 = np.array([3.0, 7.5, 12.0, 15.0])
_COORDS2 = np.array([4.0, 14.0])
_COORDS3 = np.array([6.0, 10.0, 14.0])
_NUM_ZONES = 3


def _props(d, fos=1.5):
    d = float(d)
    return {"density": d,
            "strength_t": max(10.0, 18.0 * (d - 55.0) + 15.0),
            "strength_c": max(80.0, 90.0 * (d - 55.0) + 120.0)}


# A small density grid — enough combos that the bound actually prunes.
_MIXES = [[m1, m2] for m1 in range(60, 81, 5) for m2 in range(55, 81, 5)
          if m1 >= m2 + 5]


def _geometry(t_in=0.5, d_in=13.578):
    off1, out = _ROOT / "off1.mlt", _ROOT / "out.mlt"
    if not (off1.exists() and out.exists()):
        pytest.skip("reference hull off1.mlt/out.mlt not present")
    from geometry import (parse_godzilla_dimensions, parse_godzilla_offsets,
                          calculate_shell_properties)
    L_ft, draft_in = parse_godzilla_dimensions(str(out))
    x_raw, z_raw, y = parse_godzilla_offsets(
        str(off1), L_ft=L_ft, draft_in=draft_in, mold_depth_in=d_in)
    p = calculate_shell_properties(x_raw, z_raw, y, t_in)
    A, At, Ab, I, Q, ct, cb, wf = p
    v = A > 0
    x = x_raw[v]
    return dict(x=x, dx=np.max(x) / (len(x_raw) - 1), wf=wf[v], t=t_in,
                ca=p.cum_area_in2[v], cAz=p.cum_Az_in3[v], cIz=p.cum_Iz_in4[v],
                zg=p.z_grid_in, sheer=cb[v] + ct[v], na=cb[v], draft=draft_in,
                d_in=d_in)


def _ev(g, stack, fracs):
    return evaluate_multizone_weight(
        _COORDS4, _COORDS2, stack, fracs, g["x"], g["dx"], g["wf"], g["t"],
        g["ca"], g["cAz"], g["cIz"], g["zg"], g["sheer"], g["na"], _props,
        coords_3=_COORDS3, position_variance_ft=0.0, enable_mesh=False,
        freeboard_min_in=6.0, flare_max_deg=15.0, waterline_in=g["draft"])


def _v_total(g):
    A_hi = _cum_at(g["ca"], g["zg"], g["sheer"])
    A_lo = _cum_at(g["ca"], g["zg"], np.zeros(len(g["x"])))
    return float(trapezoid((A_hi - A_lo) / 144.0, g["x"]))


def _brute(g, pour_grids):
    """Full enumeration, original order, first strict-min wins."""
    best = None
    for cm in _MIXES:
        if len({round(float(d), 3) for d in cm}) < 2:
            continue
        stack = ibeam_zone_stack(cm, _NUM_ZONES)
        for fr in pour_grids:
            w = _ev(g, stack, fr)
            if not (is_rejected(w) or np.isnan(w) or w >= 1e5):
                if best is None or w < best[0]:
                    best = (float(w), tuple(stack), tuple(fr))
    return best


def _bounded(g, pour_grids, V):
    """Min-sorted walk with the shared admissible-bound break."""
    best = None
    n_eval = 0
    worst_slack = np.inf          # min(weight - LB) over evaluated combos
    for i in min_density_bound_order(_MIXES):
        cm = _MIXES[i]
        if best is not None and min_density_weight_bound(cm, V) >= best[0]:
            break
        if len({round(float(d), 3) for d in cm}) < 2:
            continue
        stack = ibeam_zone_stack(cm, _NUM_ZONES)
        for fr in pour_grids:
            w = _ev(g, stack, fr)
            n_eval += 1
            if not (is_rejected(w) or np.isnan(w) or w >= 1e5):
                worst_slack = min(worst_slack,
                                  w - min_density_weight_bound(cm, V))
                if best is None or w < best[0]:
                    best = (float(w), tuple(stack), tuple(fr))
    return best, n_eval, worst_slack


class TestMultizoneBound:
    def test_bounded_matches_brute_force(self):
        g = _geometry()
        pg = multizone_pour_grids(g["d_in"], _NUM_ZONES)
        V = _v_total(g)
        ref = _brute(g, pg)
        bnd, n_eval, slack = _bounded(g, pg, V)

        assert ref is not None and bnd is not None, "expected a feasible design"
        assert bnd[0] == pytest.approx(ref[0], abs=1e-9), "winner weight differs"
        assert bnd[1] == ref[1], "winning zone stack differs"
        np.testing.assert_allclose(bnd[2], ref[2], atol=1e-12)  # fracs

    def test_bound_is_admissible(self):
        """The bound must never exceed an actual weight (slack >= 0)."""
        g = _geometry()
        pg = multizone_pour_grids(g["d_in"], _NUM_ZONES)
        V = _v_total(g)
        _, _, worst_slack = _bounded(g, pg, V)
        assert worst_slack >= -1e-6, f"bound exceeded a real weight by {-worst_slack:.4g}"

    def test_bound_actually_prunes(self):
        """Sanity: the walk stops early, else the test proves nothing."""
        g = _geometry()
        pg = multizone_pour_grids(g["d_in"], _NUM_ZONES)
        V = _v_total(g)
        _, n_eval, _ = _bounded(g, pg, V)
        full = sum(1 for cm in _MIXES
                   if len({round(float(d), 3) for d in cm}) >= 2) * len(pg)
        assert n_eval < full, "bound never triggered — grid too small to test pruning"

    def test_v_total_is_fraction_independent(self):
        """Σ zone volumes telescope to V_total regardless of the pour lines —
        the property the whole bound rests on."""
        g = _geometry()
        V = _v_total(g)
        # Reconstruct Σ Vₖ directly from the zone slices for several frac sets.
        for fr in multizone_pour_grids(g["d_in"], _NUM_ZONES):
            z_bounds = [np.zeros(len(g["x"]))]
            for f in fr:
                z_bounds.append(f * g["sheer"])
            z_bounds.append(np.asarray(g["sheer"], float))
            total = 0.0
            prev = _cum_at(g["ca"], g["zg"], z_bounds[0])
            for k in range(_NUM_ZONES):
                A_t = _cum_at(g["ca"], g["zg"], z_bounds[k + 1])
                total += float(trapezoid((A_t - prev) / 144.0, g["x"]))
                prev = A_t
            assert total == pytest.approx(V, rel=1e-9)


# ── gunwale-inclusive bound: weight = shell + rail(top density) ──────────────
_GW = (1.5, 1.75)          # rail width × height [in]


def _ev_gw(g, stack, fracs):
    return evaluate_multizone_weight(
        _COORDS4, _COORDS2, stack, fracs, g["x"], g["dx"], g["wf"], g["t"],
        g["ca"], g["cAz"], g["cIz"], g["zg"], g["sheer"], g["na"], _props,
        coords_3=_COORDS3, position_variance_ft=0.0, enable_mesh=False,
        freeboard_min_in=6.0, flare_max_deg=15.0, waterline_in=g["draft"],
        gw_width_mid=_GW[0], gw_height_mid=_GW[1], gw_fillet_ratio=1.0)


def _v_bound_gw(g):
    """V_total + V_gw — the gunwale-inclusive volume factor."""
    rail = gunwale_rail_section(g["x"], g["t"], _GW[0], _GW[1], 1.0, g["sheer"])
    v_gw = float(trapezoid((rail.A_gw + rail.A_fil) / 144.0, g["x"]))
    return _v_total(g) + v_gw


class TestMultizoneGunwaleBound:
    """With the gunwale modeled, the weight gains V_gw·(top density).  The bound
    absorbs it as min(combo)·(V_total + V_gw) — still admissible because the top
    zone density ≥ min(combo).  Must stay winner-identical to brute force."""

    def test_bounded_matches_brute_force_with_gunwale(self):
        g = _geometry()
        pg = multizone_pour_grids(g["d_in"], _NUM_ZONES)
        V = _v_bound_gw(g)
        # brute force with the rail
        ref = None
        for cm in _MIXES:
            if len({round(float(d), 3) for d in cm}) < 2:
                continue
            st = ibeam_zone_stack(cm, _NUM_ZONES)
            for fr in pg:
                w = _ev_gw(g, st, fr)
                if not (is_rejected(w) or np.isnan(w) or w >= 1e5):
                    if ref is None or w < ref[0]:
                        ref = (float(w), tuple(st), tuple(fr))
        # bounded with the rail
        bnd = None
        worst_slack = np.inf
        for i in min_density_bound_order(_MIXES):
            cm = _MIXES[i]
            if bnd is not None and min_density_weight_bound(cm, V) >= bnd[0]:
                break
            if len({round(float(d), 3) for d in cm}) < 2:
                continue
            st = ibeam_zone_stack(cm, _NUM_ZONES)
            for fr in pg:
                w = _ev_gw(g, st, fr)
                if not (is_rejected(w) or np.isnan(w) or w >= 1e5):
                    worst_slack = min(worst_slack,
                                      w - min_density_weight_bound(cm, V))
                    if bnd is None or w < bnd[0]:
                        bnd = (float(w), tuple(st), tuple(fr))
        assert ref is not None and bnd is not None
        assert bnd[0] == pytest.approx(ref[0], abs=1e-9)
        assert bnd[1] == ref[1]
        np.testing.assert_allclose(bnd[2], ref[2], atol=1e-12)
        # bound stays admissible (never exceeds a real gunwale-inclusive weight)
        assert worst_slack >= -1e-6, f"gunwale bound exceeded weight by {-worst_slack:.4g}"
