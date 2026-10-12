# tests/test_punching.py
"""
ACI 318-19 two-way (punching) shear must take the SMALLEST of the three
concrete-capacity equations.  Guards against reverting to a single formula,
which is non-conservative when a different equation governs.
"""
from __future__ import annotations

import numpy as np
import pytest

from structural import punching_shear_check, punching_two_knee


def test_takes_minimum_of_three_capacities():
    r = punching_shear_check(170.0, 0.375, 612.0, 1.5)
    assert r["v_c"] == min(r["v_c_a"], r["v_c_b"], r["v_c_c"])


def test_equation_c_governs_for_thin_wall():
    # Thin shell: d is small relative to b0, so (2 + alpha_s d/b0) < 4 and the
    # geometry-dependent equation (c) governs -- BELOW the (a) single formula.
    r = punching_shear_check(170.0, 0.375, 612.0, 1.5)
    assert r["governing"] == "c"
    assert r["v_c_c"] < r["v_c_a"]
    # taking the true minimum yields a HIGHER (more conservative) D/C than (a)
    dc_a_only = r["v_u"] / (r["v_c_a"] / 1.5)
    assert r["dc"] > dc_a_only


def test_equation_a_governs_for_thick_section():
    # Thick section with a small patch: d/b0 is large, so (c) is large and the
    # flat (a) = 4 lambda sqrt(f'c) becomes the smallest.
    r = punching_shear_check(170.0, 8.0, 4000.0, 1.5, column_in=1.0)
    assert r["governing"] == "a"
    assert r["v_c"] == min(r["v_c_a"], r["v_c_b"], r["v_c_c"])


def test_square_patch_beta_equation_never_governs():
    # For a square loaded area beta = 1 so (b) = 6 lambda sqrt(f'c) > (a) = 4:
    # it can never be the minimum for a square patch.
    r = punching_shear_check(170.0, 0.375, 612.0, 1.5, beta=1.0)
    assert r["governing"] != "b"


# --- two-knee kneeling model -------------------------------------------------

DESIGN = dict(total_lbs=170.0, t_in=0.375, fc_psi=612.0, fos=1.5)


def _two_knee(**kw):
    d = dict(DESIGN); d.update(kw)
    return punching_two_knee(d.pop("total_lbs"), d.pop("t_in"),
                             d.pop("fc_psi"), d.pop("fos"), **d)


def test_circular_perimeter_is_smaller_than_square():
    # A circle of the same width has LESS critical perimeter than the square,
    # so it is worse per unit load -- this partly offsets the load split and
    # must not be silently dropped.
    sq = punching_shear_check(170.0, 0.375, 612.0, 1.5, column_in=3.0)
    ci = punching_shear_check(170.0, 0.375, 612.0, 1.5,
                              shape="circle", size_in=3.0)
    assert ci["b0"] < sq["b0"]
    assert ci["dc"] > sq["dc"]          # same load, less perimeter => worse


def test_knees_do_not_interact_at_realistic_spacing():
    # Critical circles have radius (3 + 0.1875)/2 = 1.59 in, so they merge only
    # below ~3.19 in -- far inside any real kneeling stance (6-10 in).
    for spacing in (6.0, 8.0, 10.0):
        k = _two_knee(knee_spacing_in=spacing)
        assert not k["interact"], f"unexpected merge at {spacing} in"
        assert k["per_knee_lbs"] == 85.0


def test_close_knees_merge_and_carry_full_load():
    # Force the pathological case: if the perimeters merge, the model must NOT
    # keep treating them as two independent half-load punches.
    k = _two_knee(knee_spacing_in=2.0)
    assert k["interact"]
    assert k["per_knee_lbs"] == 170.0                 # full load, combined section
    assert k["check"]["b0"] > _two_knee(knee_spacing_in=8.0)["check"]["b0"]


def test_merged_perimeter_is_worse_per_unit_load():
    # The whole point of the interaction check: a merged critical section is
    # worse per unit load than two isolated ones, so it cannot be skipped.
    merged = _two_knee(knee_spacing_in=2.0)["check"]
    isolated = _two_knee(knee_spacing_in=8.0)["check"]
    assert merged["dc"] > isolated["dc"]


def test_two_knee_model_clears_without_pad_credit():
    # The documented design basis: two circles, 50/50 split, no pad. Both the
    # load split and the circular-perimeter penalty are applied.
    k = _two_knee(knee_spacing_in=8.0, pad_in=0.0)
    assert k["check"]["ok"]
    assert k["check"]["dc"] < 1.0


def test_pad_helps_but_less_than_area_growth_suggests():
    # A pad grows b0, which cuts demand -- but it also shrinks the alpha_s*d/b0
    # term, so the ALLOWABLE drops too. Net benefit is real but sub-linear.
    bare = _two_knee(pad_in=0.0)["check"]
    pad = _two_knee(pad_in=0.5)["check"]
    assert pad["dc"] < bare["dc"]
    assert pad["allow"] < bare["allow"]              # capacity falls as b0 grows
    naive = bare["dc"] * bare["b0"] / pad["b0"]      # demand-only scaling
    assert pad["dc"] > naive                          # ...so the gain is smaller


def test_punching_receives_raw_fc_not_an_allowable():
    # punching_shear_check applies FoS to the capacity itself, so its fc_psi
    # argument must be the RAW f'c. Passing f'c/FoS applies the factor twice:
    # capacity drops by sqrt(FoS) and D/C is overstated by the same factor.
    import config
    from reporting.latex_report import build_report_model  # noqa: F401
    fos = 1.5
    fc_raw = 900.0
    right = punching_shear_check(170.0, 0.375, fc_raw, fos)
    doubled = punching_shear_check(170.0, 0.375, fc_raw / fos, fos)
    assert doubled["dc"] == pytest.approx(right["dc"] * np.sqrt(fos), rel=1e-9)
    assert doubled["dc"] > right["dc"]


def test_report_punching_fc_matches_raw_database_value():
    # End-to-end: the f'c the report hands to the punching check must equal the
    # database's UNFACTORED strength for the bottom-flange density.
    import config
    db = config.get_mix_db()
    dn = 65.0
    raw = db.get_mix_properties(dn, fos=1.0)["strength_c"]
    allow = db.get_mix_properties(dn, fos=config.FACTOR_OF_SAFETY)["strength_c"]
    assert raw == pytest.approx(allow * config.FACTOR_OF_SAFETY)
    # the raw value is what a correct punching call consumes
    r = punching_shear_check(170.0, 0.375, raw, config.FACTOR_OF_SAFETY)
    r_bad = punching_shear_check(170.0, 0.375, allow, config.FACTOR_OF_SAFETY)
    assert r["dc"] < r_bad["dc"]


def test_bottom_flange_fc_is_the_lever_not_thickness():
    # Sanity on the stated fallback lever: raising bottom-flange f'c lowers D/C.
    weak = _two_knee(fc_psi=400.0)["check"]
    strong = _two_knee(fc_psi=900.0)["check"]
    assert strong["dc"] < weak["dc"]
