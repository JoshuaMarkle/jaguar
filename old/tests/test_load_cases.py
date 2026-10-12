# tests/test_load_cases.py
"""
Tests for the paddler load-case features:
  - 3-person race case in the moment/shear envelopes
  - user-specified (fixed) paddler positions bypassing the SLSQP placement
  - seating-drift variance widening the envelopes without moving the
    nominal per-case curves
"""
from __future__ import annotations

import numpy as np
import pytest

import config
from structural import calculate_moment_envelope, _variance_coord_sets

L = 20.0
N = 201

COORDS_4 = [3.0, 7.5, 12.0, 15.0]
COORDS_2 = [4.0, 14.0]
COORDS_3 = [5.0, 10.0, 15.0]


@pytest.fixture
def beam():
    x = np.linspace(0.0, L, N)
    dx = L / (N - 1)
    w_self = np.full(N, 8.0)       # lb/ft uniform hull self-weight
    width_ft = np.full(N, 2.0)     # uniform waterplane half-breadth proxy
    return x, dx, w_self, width_ft


# ═══════════════════════════════════════════════════════════════════
#  3-person load case
# ═══════════════════════════════════════════════════════════════════

def test_race3_case_present_and_balanced(beam):
    x, dx, w_self, width_ft = beam
    res = calculate_moment_envelope(
        x, dx, w_self, width_ft, COORDS_4, COORDS_2,
        return_all=True, paddler_coords_3=COORDS_3)

    M3, V3 = res["moment"]["race_3"], res["shear"]["race_3"]
    assert M3 is not None and V3 is not None
    assert len(M3) == N
    # Balanced beam: moment and shear close at both ends
    assert abs(M3[0]) < 1e-6 and abs(M3[-1]) < 15.0
    # The absolute envelope covers the 3-person case everywhere
    assert np.all(res["moment"]["envelope"] >= np.abs(M3) - 1e-9)
    assert np.all(res["shear"]["max_pos"] >= V3 - 1e-9)
    assert np.all(res["shear"]["max_neg"] <= V3 + 1e-9)


def test_race3_absent_without_coords(beam):
    x, dx, w_self, width_ft = beam
    res = calculate_moment_envelope(
        x, dx, w_self, width_ft, COORDS_4, COORDS_2, return_all=True)
    assert res["moment"]["race_3"] is None
    assert res["shear"]["race_3"] is None


def test_envelope_grows_with_race3(beam):
    x, dx, w_self, width_ft = beam
    env_without = calculate_moment_envelope(
        x, dx, w_self, width_ft, COORDS_4, COORDS_2, return_all=True
    )["moment"]["envelope"]
    env_with = calculate_moment_envelope(
        x, dx, w_self, width_ft, COORDS_4, COORDS_2,
        return_all=True, paddler_coords_3=COORDS_3
    )["moment"]["envelope"]
    assert np.all(env_with >= env_without - 1e-9)


# ═══════════════════════════════════════════════════════════════════
#  Seating-drift variance
# ═══════════════════════════════════════════════════════════════════

def test_variance_widens_envelope_but_not_nominal_curves(beam):
    x, dx, w_self, width_ft = beam
    base = calculate_moment_envelope(
        x, dx, w_self, width_ft, COORDS_4, COORDS_2,
        return_all=True, paddler_coords_3=COORDS_3)
    varied = calculate_moment_envelope(
        x, dx, w_self, width_ft, COORDS_4, COORDS_2,
        return_all=True, paddler_coords_3=COORDS_3,
        position_variance_ft=1.0)

    # Envelopes only grow (perturbed seatings added on top of nominal)
    assert np.all(varied["moment"]["envelope"] >= base["moment"]["envelope"] - 1e-9)
    assert np.all(varied["shear"]["max_pos"] >= base["shear"]["max_pos"] - 1e-9)
    assert np.all(varied["shear"]["max_neg"] <= base["shear"]["max_neg"] + 1e-9)
    # ... and strictly somewhere: packed/spread seatings change the peak
    assert varied["moment"]["envelope"].max() > base["moment"]["envelope"].max()

    # Nominal per-case curves are untouched — the diagrams still show the
    # paddlers at their design stations
    for case in ("race_4", "race_2", "race_3"):
        np.testing.assert_allclose(varied["moment"][case], base["moment"][case])


def test_variance_coord_sets_stay_in_hull():
    sets = _variance_coord_sets([0.5, 10.0, 19.5], 5.0, L)
    assert len(sets) == 4
    for cs in sets:
        assert np.all(cs >= 0.0) and np.all(cs <= L)


# ═══════════════════════════════════════════════════════════════════
#  Fixed paddler positions (user-specified seating)
# ═══════════════════════════════════════════════════════════════════

def _stable_mix_properties(monkeypatch):
    """
    Pin mix properties to fixed values.  These worker tests exercise sweep
    LOGIC (seat bypass, flare-gate walk) — but config.get_mix_properties
    reads the user's live concrete_mixes.db published regression, which
    changes between sessions (found 2026-07-11: a freshly published
    regression gave 60 pcf → 12.8 psi tensile and both tests failed on
    perfectly good code).  Strengths here approximate a healthy regression.
    """
    def fake(density_pcf: float) -> dict:
        d = float(density_pcf)
        return {
            "density": d,
            "strength_t": max(20.0, 8.0 * (d - 40.0) + 40.0),
            "strength_c": max(100.0, 40.0 * (d - 40.0) + 200.0),
        }
    monkeypatch.setattr(config, "get_mix_properties", fake)


@pytest.fixture
def published_overhang(monkeypatch):
    """
    STEM_OVERHANG_IN is published ONLY inside run_grand_optimization
    (main.py:2004/2077).  A test that calls _evaluate_geometry_worker directly
    therefore depends on an EARLIER FILE having left it on the config module --
    which is why these two passed in a full run and failed alone.

    _struct_rocker_ft() refuses to inherit a silent zero, which is precisely
    the directly-called-evaluator case, so the test states its own
    precondition instead of borrowing another file's.
    """
    # Plain assignment, NOT monkeypatch: STEM_OVERHANG_IN is now in
    # RUN_LEVEL_CONFIG_ATTRS, so the autouse conftest snapshot owns its
    # cleanup. Letting monkeypatch manage it too means BOTH try to delete
    # it on teardown and the second raises AttributeError.
    config.STEM_OVERHANG_IN = 1.045163


def test_fixed_positions_bypass_optimizer(monkeypatch, published_overhang):
    """
    Runs the real sweep worker on the repo's off1.mlt with user-specified
    seats: the returned config must carry the exact positions (no SLSQP)
    and the drift variance must be ignored in fixed mode.
    """
    from main import _evaluate_geometry_worker
    from geometry import parse_godzilla_dimensions

    L_ft, draft_in = parse_godzilla_dimensions("out.mlt")
    fixed = {"4": [3.0, 7.0, 11.0, 15.0],
             "2": [5.0, 13.0],
             "3": [4.0, 9.5, 14.0]}
    monkeypatch.setattr(config, "FIXED_PADDLER_POSITIONS", fixed)
    # Variance must be ignored when the user dictates physical seat positions
    monkeypatch.setattr(config, "PADDLER_POS_VARIANCE_FT", 1.0)
    _stable_mix_properties(monkeypatch)

    # draft + 9.5 in, not + 8.0: the loaded-freeboard gate is now
    # per-configuration, and at draft+8.0 (12.58 in mold depth) the
    # heaviest hull that clears the 6 in ASCE rule with a 640 lb crew
    # is 70 lb -- no real design exists there, whatever it is made of.
    # draft+9.5 admits 281 lb, so these fixtures test what they claim
    # to test (worker mechanics) rather than the freeboard rule.
    args = (0.75, draft_in + 9.5, [[80.0, 60.0]], L_ft, draft_in, "off1.mlt",
            0.0, 0.0, False, 1.0, 6.0, 30.0, None, False, 3)   # stop_event=None, multizone=False
    weight, best_config, _n, _gate_cands = _evaluate_geometry_worker(args)

    assert best_config is not None, "fixed-seating configuration was rejected"
    assert best_config["seating_fixed"] is True
    # The seating vector now spans all FIVE ASCE races, so the legacy per-size
    # keys seed every crew of that size: "2" fills both 2M and 2F, "3" fills
    # both 3M and 3F.  Assert the mapping explicitly rather than a flat vector,
    # so a future layout change cannot silently reorder the seats.
    seats = best_config["seats"]
    np.testing.assert_allclose(seats["4_coed"], fixed["4"])
    for cat in ("2M", "2F"):
        np.testing.assert_allclose(seats[cat], fixed["2"], err_msg=cat)
    for cat in ("3M", "3F"):
        np.testing.assert_allclose(seats[cat], fixed["3"], err_msg=cat)
    np.testing.assert_allclose(best_config["coords_3"], fixed["3"])
    assert best_config["pos_var_ft"] == 0.0


def test_mesh_enabled_task_not_rejected(monkeypatch, published_overhang):
    """
    Regression: with composite mesh enabled, very light mixes pass the shell
    check but their bare strength failed the (previously mesh-blind) flare
    gate, and the gate rejected the ENTIRE task instead of falling back to a
    heavier mix — sweeps reported '0 configurations survived' and the winner
    re-evaluation crashed with 'float has no attribute depth_in'.
    """
    from main import _evaluate_geometry_worker
    from geometry import parse_godzilla_dimensions

    monkeypatch.setattr(config, "FIXED_PADDLER_POSITIONS", None)
    monkeypatch.setattr(config, "PADDLER_POS_VARIANCE_FT", 0.5)
    _stable_mix_properties(monkeypatch)

    L_ft, draft_in = parse_godzilla_dimensions("out.mlt")
    mixes = [[44.5, 41.0], [60.0, 50.0], [80.0, 60.0]]
    args = (0.5, draft_in + 9.5, mixes, L_ft, draft_in, "off1.mlt",
            0.0, 0.0, True, 1.0, 6.0, 30.0, None, False, 3)   # enable_mesh=True, stop_event=None, multizone=False
    weight, best_config, _n, _gate_cands = _evaluate_geometry_worker(args)

    assert best_config is not None, \
        "mesh-enabled task rejected outright (flare gate regression)"
    assert weight < 1e5
    assert best_config["enable_mesh"] is True


# ═══════════════════════════════════════════════════════════════════
#  Plot functions accept the new shapes
# ═══════════════════════════════════════════════════════════════════

def test_plot_functions_handle_race3_and_case_filter(beam):
    x, dx, w_self, width_ft = beam
    res = calculate_moment_envelope(
        x, dx, w_self, width_ft, COORDS_4, COORDS_2,
        return_all=True, paddler_coords_3=COORDS_3)

    from visualization import plot_structural_envelope, plot_shear_envelope
    M_tuple = (res["moment"]["envelope"], res["moment"]["race_4"],
               res["moment"]["race_2"], res["moment"]["race_3"])
    for cases in (None, {"race_3"}, {"envelope", "race_2"}, set()):
        fig = plot_structural_envelope(x, M_tuple, cases=cases)
        assert fig is not None
        fig.clf()
        fig2 = plot_shear_envelope(x, res["shear"], cases=cases)
        assert fig2 is not None
        fig2.clf()
