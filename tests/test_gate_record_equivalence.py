# tests/test_gate_record_equivalence.py
"""
The gate scores a design RECONSTRUCTED from a lean record, not the object the
worker evaluated.  That is what makes returning the top-m per task cheap
(0.52 KB against 74.2 KB, because x_raw/z_raw/y_matrix are ~90% of a config
and are identical for every design at the same depth).

IT IS ALSO THE RISK.  If reconstruction diverged anywhere -- a cached parse, a
rounded depth, anything resolution-dependent -- the gate would refine a
different canoe than the one that screened, and NOTHING would say so.  The
screen D/C and weight would look plausible either way.

So this file pins two things, permanently:

  1. NUMERIC   reconstructing from the lean record reproduces the worker's
               own screen-resolution D/C and weight to full precision.
  2. COMPLETE  the lean record carries every key the gate reads.  The test
               AST-scans _evaluate_cfg_at for `cfg[...]` / `cfg.get(...)` and
               fails when one is missing from GATE_RECORD_FIELDS -- so a field
               added to the design but not to the record is what breaks, which
               is the failure that would otherwise be silent.
"""
from __future__ import annotations

import ast
import inspect
import os

import numpy as np
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OFF1 = os.path.join(REPO, "off1.mlt")

pytestmark = pytest.mark.skipif(not os.path.exists(OFF1),
                                reason="needs the real offset file")

L_FT, DRAFT_IN = 20.00984316, 4.5760260931
N_SCREEN = 161

# Several designs, including a depth carrying FULL float precision -- the
# rounding case, which is the divergence most likely to bite: the worker holds
# a parse made at this exact depth, the gate re-parses from the stored value.
DESIGNS = [
    {"mixes": [72.5, 62.5, 72.5], "thickness": 0.375,
     "depth": 14.57035257349158, "pour_fracs": (0.2745, 0.6863)},
    {"mixes": [77.0, 70.0, 77.0], "thickness": 0.375,
     "depth": 13.57035257349158, "pour_fracs": (0.2948, 0.7369)},
    {"mixes": [75.0, 68.0, 75.0], "thickness": 0.375,
     "depth": 15.25, "pour_fracs": (0.30, 0.70)},
]


@pytest.fixture(autouse=True)
def _publish_overhang(monkeypatch):
    """
    run_grand_optimization resolves the stem overhang before evaluating
    anything, and _struct_rocker_ft() now REFUSES to inherit a silent zero --
    so a direct evaluator call has to publish it.  That guard firing here is
    it working as intended; pin the hull's own value so both sides of the
    equivalence see the same cantilever.
    """
    import config
    # Plain assignment, NOT monkeypatch: STEM_OVERHANG_IN is now in
    # RUN_LEVEL_CONFIG_ATTRS, so the autouse conftest snapshot owns its
    # cleanup. Letting monkeypatch manage it too means BOTH try to delete
    # it on teardown and the second raises AttributeError.
    config.STEM_OVERHANG_IN = 1.045163


def _seats():
    """
    Seat counts must MATCH the race: 2-person races carry two seats.  The
    evaluator refuses a seat/weight mismatch ("would silently load the wrong
    crew"), which is another guard doing its job on a test that got this
    wrong first time.
    """
    import config
    out = {}
    for n in config.RACE_CATEGORY_ORDER:
        k = 4 if n.startswith("4") else (3 if n.startswith("3") else 2)
        out[n] = np.linspace(5.0, 15.0, k)
    return out


def _worker_style_eval(design):
    """
    Score a design the way the WORKER does: parse geometry, build the section,
    evaluate off those in-memory arrays.  This is the number the gate must
    reproduce from parameters alone.
    """
    import config
    from geometry import parse_godzilla_offsets, calculate_shell_properties
    from structural import evaluate_multizone_weight
    import main

    x_raw, z_raw, y_mat = parse_godzilla_offsets(
        OFF1, L_ft=L_FT, draft_in=DRAFT_IN,
        mold_depth_in=design["depth"], n_stations=N_SCREEN)
    p = calculate_shell_properties(x_raw, z_raw, y_mat, design["thickness"])
    v = p.A_in2 > 0
    x = x_raw[v]
    dx = float(np.max(x)) / (len(x_raw) - 1)
    det = evaluate_multizone_weight(
        None, None, design["mixes"], tuple(design["pour_fracs"]),
        x, dx, p.width_ft[v], design["thickness"],
        p.cum_area_in2[v], p.cum_Az_in3[v], p.cum_Iz_in4[v], p.z_grid_in,
        p.c_bot[v] + p.c_top[v], p.c_bot[v], config.get_mix_properties,
        crew_cases=config.crew_load_cases(_seats()),
        position_variance_ft=0.0, enable_mesh=False,
        rocker_ft=main._struct_rocker_ft(),
        freeboard_min_in=6.0, flare_max_deg=20.0, waterline_in=DRAFT_IN,
        gw_width_mid=0.0, gw_height_mid=0.0, gw_fillet_ratio=1.0,
        cum_Iy=p.cum_Iy_in4[v], yhalf_grid=p.yhalf_in[v], return_details=True)
    w = det.get("weight")
    return float(det["kupfer_max"]), (float(w) if isinstance(w, float) else None)


def _full_cfg(design):
    """A config shaped like the worker's, geometry arrays and all."""
    from geometry import parse_godzilla_offsets
    x_raw, z_raw, y_mat = parse_godzilla_offsets(
        OFF1, L_ft=L_FT, draft_in=DRAFT_IN,
        mold_depth_in=design["depth"], n_stations=N_SCREEN)
    cfg = dict(design)
    cfg.update({"seats": _seats(), "multizone": True, "pos_var_ft": 0.0,
                "enable_mesh": False, "gw_width_mid": 0.0,
                "gw_height_mid": 0.0, "gw_fillet_ratio": 1.0,
                "x_raw": x_raw, "z_raw": z_raw, "y_matrix": y_mat})
    return cfg


@pytest.mark.parametrize("design", DESIGNS,
                         ids=[f"d={d['depth']:.5f}" for d in DESIGNS])
def test_reconstruction_reproduces_the_workers_own_numbers(design):
    """
    THE TEST THE WHOLE top-m DESIGN RESTS ON.  Full precision, not approx:
    a lean record is only safe if reconstruction is EXACT.  Anything less
    means the gate refines a design the sweep never scored.
    """
    import main
    u_worker, w_worker = _worker_style_eval(design)

    lean = main._lean_gate_record(_full_cfg(design), weight=w_worker)
    assert "y_matrix" not in lean and "x_raw" not in lean, (
        "the lean record must not carry geometry; that is the point of it")

    got = main._evaluate_cfg_at(lean, OFF1, L_FT, DRAFT_IN, N_SCREEN,
                                6.0, 20.0, False)
    assert got is not None, "the gate could not score the reconstructed design"
    u_gate, w_gate = got

    assert u_gate == u_worker, (
        f"D/C diverged on reconstruction: worker {u_worker!r} vs gate "
        f"{u_gate!r} (delta {u_gate - u_worker:.3e}). The gate is refining a "
        "DIFFERENT design than the one that screened.")
    if w_worker is not None:
        assert w_gate == w_worker, (
            f"weight diverged on reconstruction: {w_worker!r} vs {w_gate!r}")


def test_a_full_precision_depth_survives_the_round_trip():
    """
    The rounding case, called out explicitly.  The stored depth carries 14
    significant figures; if anything caches or rounds on it, the gate parses a
    slightly different hull and every downstream number shifts a little --
    the most plausible silent divergence in this design.
    """
    import main
    d = 14.57035257349158
    lean = main._lean_gate_record(_full_cfg({
        "mixes": [72.5, 62.5, 72.5], "thickness": 0.375, "depth": d,
        "pour_fracs": (0.2745, 0.6863)}))
    assert repr(lean["depth"]) == repr(d), "the depth was not stored verbatim"
    a = main._evaluate_cfg_at(lean, OFF1, L_FT, DRAFT_IN, N_SCREEN, 6.0, 20.0, False)
    b = main._evaluate_cfg_at(lean, OFF1, L_FT, DRAFT_IN, N_SCREEN, 6.0, 20.0, False)
    assert a == b, "scoring the same lean record twice did not agree"


def test_the_lean_record_carries_every_key_the_gate_reads():
    """
    COMPLETENESS, by reflection rather than by memory.  Scans the gate's own
    evaluator for keys read off `cfg`; anything it reads must be in
    GATE_RECORD_FIELDS.  A field added to the design and not to the record is
    then a test failure, not a number that quietly changes meaning.
    """
    import main
    tree = ast.parse(inspect.getsource(main._evaluate_cfg_at))
    read = set()
    for node in ast.walk(tree):
        # cfg['key']
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                and node.value.id == "cfg"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)):
            read.add(node.slice.value)
        # cfg.get('key', ...)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "cfg"
                and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            read.add(node.args[0].value)
    assert read, "the reflection found no cfg keys; this test has stopped working"
    missing = sorted(read - set(main.GATE_RECORD_FIELDS))
    assert not missing, (
        f"_evaluate_cfg_at reads {missing} off cfg, but the lean gate record "
        "does not carry it. Either add it to GATE_RECORD_FIELDS or stop "
        "reading it -- otherwise the gate scores a design missing that field.")
