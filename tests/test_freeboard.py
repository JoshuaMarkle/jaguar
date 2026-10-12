# tests/test_freeboard.py
"""
Loaded freeboard is an ASCE compliance check, not a geometric convenience:
it must root-find the waterline where displaced volume balances hull + crew,
using the real per-station offsets.

Guards the placeholder that reported the UNLOADED geometric freeboard
(mold depth - design draft) in the compliance table, which flatters the
result because the loaded waterline is far below the design draft.
"""
from __future__ import annotations

import numpy as np
import pytest

from structural import (WATER_LB_FT3, displaced_volume_ft3,
                        loaded_freeboard_check)


# ── analytic geometry: a rectangular box hull ────────────────────────────────
# 120 in long, 24 in full beam (12 in half-breadth), 12 in deep, prismatic.
# Volume below z is exactly  120 * 24 * z  in^3 = 20*z/12 ... computed below.
BOX_L_FT = 10.0                     # 120 in
BOX_HALF_B_IN = 12.0                # 24 in full beam
BOX_DEPTH_IN = 12.0


def _box(n_st=41, n_z=61):
    x_ft = np.linspace(0.0, BOX_L_FT, n_st)
    z_in = np.linspace(0.0, BOX_DEPTH_IN, n_z)
    yhalf = np.full((n_st, n_z), BOX_HALF_B_IN)
    return x_ft, z_in, yhalf


def _box_vol_ft3(z_in):
    """Exact: L * B * z, converted to ft^3."""
    return (BOX_L_FT * 12.0) * (2 * BOX_HALF_B_IN) * z_in / 1728.0


def test_known_displaced_volume_box_hull():
    x, z, y = _box()
    for z_wl in (1.0, 3.5, 6.0, 12.0):
        got = displaced_volume_ft3(z_wl, x, z, y)
        assert got == pytest.approx(_box_vol_ft3(z_wl), rel=1e-6)


def test_volume_is_zero_at_keel_and_monotonic():
    x, z, y = _box()
    assert displaced_volume_ft3(0.0, x, z, y) == 0.0
    vols = [displaced_volume_ft3(zz, x, z, y) for zz in np.linspace(0, 12, 25)]
    assert np.all(np.diff(vols) > 0)


def test_waterline_solves_the_buoyancy_balance():
    # Box hull: pick a weight, check the solved waterline reproduces it.
    x, z, y = _box()
    W = 400.0
    r = loaded_freeboard_check(W, BOX_DEPTH_IN, x, z, y, min_freeboard_in=0.0)
    assert not r["swamps"]
    assert r["buoyancy_lbs"] == pytest.approx(W, rel=1e-6)
    # analytic: z = W / (rho * L * B)
    z_exact = W / (WATER_LB_FT3 * (BOX_L_FT * 12.0) * (2 * BOX_HALF_B_IN) / 1728.0)
    assert r["waterline_in"] == pytest.approx(z_exact, rel=1e-5)
    assert r["freeboard_in"] == pytest.approx(BOX_DEPTH_IN - z_exact, rel=1e-5)


def test_heavier_load_sinks_deeper_and_cuts_freeboard():
    x, z, y = _box()
    light = loaded_freeboard_check(300.0, BOX_DEPTH_IN, x, z, y, 0.0)
    heavy = loaded_freeboard_check(600.0, BOX_DEPTH_IN, x, z, y, 0.0)
    assert heavy["waterline_in"] > light["waterline_in"]
    assert heavy["freeboard_in"] < light["freeboard_in"]


def test_shallower_hull_gives_less_freeboard_at_same_load():
    # THE sanity check from the design review: a shallower hull under the same
    # load must yield LESS freeboard. If this inverts, the subtraction is
    # backwards somewhere.
    x, z, y = _box()
    deep = loaded_freeboard_check(400.0, 15.0, x, z, y, 0.0)
    shallow = loaded_freeboard_check(400.0, BOX_DEPTH_IN, x, z, y, 0.0)
    assert shallow["freeboard_in"] < deep["freeboard_in"]
    # same waterline either way -- depth does not change how deep it floats
    assert shallow["waterline_in"] == pytest.approx(deep["waterline_in"], rel=1e-9)


def test_swamping_is_reported_not_returned_as_negative_freeboard():
    x, z, y = _box()
    # Far more weight than the whole box can displace.
    r = loaded_freeboard_check(100_000.0, BOX_DEPTH_IN, x, z, y, 6.0)
    assert r["swamps"] is True
    assert r["ok"] is False
    assert r["freeboard_in"] == 0.0          # never a negative freeboard


def test_gate_compares_against_the_configured_minimum():
    x, z, y = _box()
    r = loaded_freeboard_check(400.0, BOX_DEPTH_IN, x, z, y, min_freeboard_in=6.0)
    assert r["ok"] == (r["freeboard_in"] >= 6.0)
    strict = loaded_freeboard_check(400.0, BOX_DEPTH_IN, x, z, y,
                                    min_freeboard_in=r["freeboard_in"] + 1.0)
    assert not strict["ok"]


def test_report_uses_loaded_not_geometric_freeboard(small_sweep_model):
    """
    Buoyancy balance and loaded-vs-geometric freeboard on a REAL winner.

    Uses the shared coarse sweep: the assertions are that buoyancy equals hull
    plus crew at the solved waterline and that the loaded figure is stricter
    than the geometric one.  Both are properties of the buoyancy solve, not of
    the station count.
    """
    m, res = small_sweep_model
    fbk = m["freeboard"]
    # loaded weight really is hull + the full configured crew
    import config
    assert fbk["total_weight_lbs"] == pytest.approx(
        m["weight_lbs"] + sum(config.RACE_4_PERSON))
    # the loaded waterline is BELOW the gunwale and the freeboard is the
    # remainder -- and strictly less flattering than the geometric value
    assert 0 < fbk["waterline_in"] < m["depth_in"]
    assert fbk["freeboard_in"] == pytest.approx(
        m["depth_in"] - fbk["waterline_in"])
    assert fbk["freeboard_in"] < m["freeboard_geom_in"]
    # buoyancy at the solved waterline balances the load
    assert fbk["buoyancy_lbs"] == pytest.approx(fbk["total_weight_lbs"], rel=1e-4)
