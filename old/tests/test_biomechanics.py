# tests/test_biomechanics.py
"""
Kneeling-paddler crew CG.

Loaded GM_T moves 0.8 in for every inch of crew CG, so this model is the
largest single term in the stability result -- larger by orders of magnitude
than the mix distribution, which measures at 0.00 in.  These tests pin the
segment mechanics, the degrees/radians boundary, the sex and category mappings,
and the posture dominance that is the model's headline finding.
"""
from __future__ import annotations

import math

import pytest

import config
from physics.biomechanics import (
    SEGMENT_MASS_FRACTION, WHOLE_BODY_CG_STANDING_FRAC, LEGS_CG_STANDING_FRAC,
    HIP_HEIGHT_STANDING_FRAC, THIGH_LENGTH_FRAC, POSTURE_HIP_HEIGHT_FRAC,
    TORSO_ANGLE_BAND_DEG, DEFAULT_POSTURE,
    hat_cg_offset_frac, paddler_kg_in, crew_kg_in, crew_kg_band_in)


# ── segment data integrity ──────────────────────────────────────────────────
@pytest.mark.parametrize("sex", ["M", "F"])
def test_mass_fractions_sum_to_one(sex):
    f = SEGMENT_MASS_FRACTION[sex]
    assert f["hat"] + f["legs"] == pytest.approx(1.0, abs=1e-9)


def test_female_carries_more_leg_mass_than_male():
    """
    The reason a sex-blind model is wrong: female mass sits lower.  Plagenhoef
    gives female thigh 11.75% vs male 10.50%, shank 5.35% vs 4.75%.
    """
    assert (SEGMENT_MASS_FRACTION["F"]["legs"]
            > SEGMENT_MASS_FRACTION["M"]["legs"])


def test_female_cg_is_lower_than_male_at_equal_height():
    for posture in POSTURE_HIP_HEIGHT_FRAC:
        m = paddler_kg_in(70.0, "M", 70.0, posture=posture)
        f = paddler_kg_in(70.0, "F", 70.0, posture=posture)
        assert f < m, posture


# ── L_HAT calibration ───────────────────────────────────────────────────────
@pytest.mark.parametrize("sex,expect", [("M", 0.1586), ("F", 0.1475)])
def test_hat_offset_matches_the_hand_derivation(sex, expect):
    assert hat_cg_offset_frac(sex) == pytest.approx(expect, abs=5e-4)


@pytest.mark.parametrize("sex", ["M", "F"])
def test_calibration_reproduces_the_published_standing_cg(sex):
    """
    THE point of the calibration: with the derived L_HAT, a standing body must
    come back out at the published whole-body CG.  This is what makes 0.565 /
    0.545 legitimate inputs -- they are targets, not segment CGs.
    """
    f = SEGMENT_MASS_FRACTION[sex]
    standing = (f["legs"] * LEGS_CG_STANDING_FRAC
                + f["hat"] * (HIP_HEIGHT_STANDING_FRAC + hat_cg_offset_frac(sex)))
    assert standing == pytest.approx(WHOLE_BODY_CG_STANDING_FRAC[sex], abs=1e-9)


def test_ansur_cross_check_on_segment_lengths():
    """Upright-kneel height / stature: model 0.715 vs ANSUR II 0.709 (male)."""
    predicted = THIGH_LENGTH_FRAC + (1.0 - HIP_HEIGHT_STANDING_FRAC)
    assert predicted == pytest.approx(0.709, abs=0.01)


# ── DEGREES -> RADIANS (the classic silent error) ───────────────────────────
def test_torso_angle_is_interpreted_as_degrees():
    """
    90 must mean UPRIGHT, i.e. sin(theta) = 1 exactly.  If the value were passed
    to math.sin unconverted, sin(90 rad) = 0.894 -- a ~10% error that returns a
    perfectly plausible number and would never be noticed.
    """
    H, sex = 70.0, "M"
    got = paddler_kg_in(H, sex, 90.0, posture="B_upright_kneel")
    f = SEGMENT_MASS_FRACTION[sex]
    upright = (f["legs"] * 0.0969 * H
               + f["hat"] * (THIGH_LENGTH_FRAC * H + hat_cg_offset_frac(sex) * H))
    assert got == pytest.approx(upright, abs=1e-9)


def test_degrees_and_radians_are_not_confused():
    """Explicit guard: the 90-degree result must NOT equal the sin(90 rad) one."""
    H, sex, posture = 70.0, "M", "B_upright_kneel"
    got = paddler_kg_in(H, sex, 90.0, posture=posture)
    f = SEGMENT_MASS_FRACTION[sex]
    wrong = (f["legs"] * 0.0969 * H
             + f["hat"] * (THIGH_LENGTH_FRAC * H
                           + hat_cg_offset_frac(sex) * H * math.sin(90.0)))
    assert abs(got - wrong) > 0.5


def test_sin_symmetry_is_absent_in_the_reported_band():
    """
    Monotonic across [45, 90]: leaning further forward can only lower the CG.
    (sin is symmetric about 90, so a caller passing >90 would fold back; the
    reported band deliberately stops at 90 = upright.)
    """
    vals = [paddler_kg_in(70.0, "M", t) for t in (45.0, 60.0, 70.0, 90.0)]
    assert vals == sorted(vals)


# ── posture dominance: the headline finding ─────────────────────────────────
def test_posture_moves_cg_far_more_than_torso_angle():
    """
    ~8 in of hip datum vs ~2 in across the whole torso range.  This is why
    hip_height_in is the parameter worth measuring and theta is secondary.
    """
    lo, hi = TORSO_ANGLE_BAND_DEG
    theta_span = abs(paddler_kg_in(70.0, "M", hi, posture="B_upright_kneel")
                     - paddler_kg_in(70.0, "M", lo, posture="B_upright_kneel"))
    posture_span = abs(paddler_kg_in(70.0, "M", hi, posture="B_upright_kneel")
                       - paddler_kg_in(70.0, "M", hi, posture="A_heels_sitting"))
    assert posture_span > 3.0 * theta_span
    assert posture_span > 6.0


def test_upright_kneel_is_the_default():
    assert DEFAULT_POSTURE == "B_upright_kneel"
    assert POSTURE_HIP_HEIGHT_FRAC["B_upright_kneel"] == pytest.approx(
        THIGH_LENGTH_FRAC)


def test_measured_hip_height_overrides_the_posture_preset():
    """One tape measure must beat the anthropometric guess."""
    preset = paddler_kg_in(70.0, "M", 70.0, posture="B_upright_kneel")
    measured = paddler_kg_in(70.0, "M", 70.0, posture="B_upright_kneel",
                             hip_height_in=9.0)
    assert measured < preset
    # and it must be the ONLY thing that changed: same value via posture A's
    # hip height reproduces posture A's answer up to the legs term
    f = SEGMENT_MASS_FRACTION["M"]
    delta = f["hat"] * (THIGH_LENGTH_FRAC * 70.0 - 9.0)
    assert preset - measured == pytest.approx(delta, abs=1e-9)


def test_worked_example_matches_the_reported_derivation():
    """H=70 male: posture A ~13.0 in, posture B ~20.7 in at theta=70."""
    assert paddler_kg_in(70.0, "M", 70.0, posture="A_heels_sitting") == \
        pytest.approx(13.02, abs=0.05)
    assert paddler_kg_in(70.0, "M", 70.0, posture="B_upright_kneel") == \
        pytest.approx(20.65, abs=0.05)


# ── datum ───────────────────────────────────────────────────────────────────
def test_datum_offset_raises_cg_one_for_one():
    """
    GM_T is measured from the KEEL but the model works from the hull FLOOR, so
    shell thickness (plus any knee pad) must be added or the crew sits one
    shell too low.
    """
    a = paddler_kg_in(70.0, "M", 70.0)
    b = paddler_kg_in(70.0, "M", 70.0, datum_offset_in=0.5)
    assert b - a == pytest.approx(0.5, abs=1e-12)


# ── crew aggregation ────────────────────────────────────────────────────────
def test_crew_kg_is_mass_weighted():
    hs, sx, ws = [70.0, 65.0], ["M", "F"], [180.0, 120.0]
    got = crew_kg_in(hs, sx, ws, 70.0)
    want = ((180.0 * paddler_kg_in(70.0, "M", 70.0)
             + 120.0 * paddler_kg_in(65.0, "F", 70.0)) / 300.0)
    assert got == pytest.approx(want, abs=1e-12)


def test_crew_kg_is_invariant_to_seating_arrangement():
    """
    THE coed claim, pinned: a mass-weighted mean cannot depend on order, so for
    crew KG the arrangement genuinely does not matter.  Deliberate, not an
    unhandled case.  (The bending envelope is a different question.)
    """
    hs, sx, ws = [70.0, 70.0, 65.0, 65.0], ["M", "M", "F", "F"], [180.0, 175.0, 140.0, 135.0]
    base = crew_kg_in(hs, sx, ws, 70.0)
    for perm in ((1, 0, 3, 2), (3, 2, 1, 0), (2, 0, 3, 1), (0, 2, 1, 3)):
        got = crew_kg_in([hs[i] for i in perm], [sx[i] for i in perm],
                         [ws[i] for i in perm], 70.0)
        assert got == pytest.approx(base, abs=1e-12), perm


def test_crew_arrays_must_agree_in_length():
    with pytest.raises(ValueError, match="disagree"):
        crew_kg_in([70.0, 70.0], ["M"], [180.0, 175.0], 70.0)


def test_rejects_bad_sex_and_height():
    with pytest.raises(ValueError, match="sex"):
        paddler_kg_in(70.0, "X", 70.0)
    with pytest.raises(ValueError, match="height"):
        paddler_kg_in(0.0, "M", 70.0)
    with pytest.raises(ValueError, match="posture"):
        paddler_kg_in(70.0, "M", 70.0, posture="C_lounging")


def test_band_spans_both_postures_and_both_angles():
    band = crew_kg_band_in([70.0, 70.0, 65.0, 65.0], ["M", "M", "F", "F"],
                           [180.0, 180.0, 140.0, 140.0])
    assert len(band) == 4
    assert {p for p, _, _ in band} == {"A_heels_sitting", "B_upright_kneel"}
    assert {t for _, t, _ in band} == set(TORSO_ANGLE_BAND_DEG)


# ── race categories: explicit mapping, never positional ─────────────────────
def test_all_five_asce_categories_present():
    assert set(config.RACE_CATEGORIES) == {"2M", "2F", "3M", "3F", "4_coed"}


@pytest.mark.parametrize("cat,size", [("2M", 2), ("2F", 2), ("3M", 3),
                                      ("3F", 3), ("4_coed", 4)])
def test_category_sizes_and_sexes(cat, size):
    sexes, heights, weights = config.race_category_crew(cat)
    assert len(sexes) == len(heights) == len(weights) == size


def test_coed_is_two_male_two_female():
    sexes, _, _ = config.race_category_crew("4_coed")
    assert sorted(sexes) == ["F", "F", "M", "M"]


@pytest.mark.parametrize("cat,sex", [("2M", "M"), ("3M", "M"),
                                     ("2F", "F"), ("3F", "F")])
def test_single_sex_categories(cat, sex):
    sexes, _, _ = config.race_category_crew(cat)
    assert set(sexes) == {sex}


def test_unknown_category_raises():
    with pytest.raises(KeyError, match="unknown race category"):
        config.race_category_crew("5_coed")


def test_weights_come_from_the_existing_race_lists():
    """One source of truth: no second place crew weights live."""
    _, _, w4 = config.race_category_crew("4_coed")
    assert w4 == [float(x) for x in config.RACE_4_PERSON]
    _, _, w2 = config.race_category_crew("2M")
    assert w2 == [float(x) for x in config.RACE_2_PERSON]


def test_same_size_crews_are_fully_independent():
    """
    The limitation this replaces: weights used to be keyed by crew SIZE, so 2M
    and 2F were forced to share a list.  They are now separate crews in sex,
    stature AND weight -- which is the whole point of the five-category split.
    """
    s_m, h_m, w_m = config.race_category_crew("2M")
    s_f, h_f, w_f = config.race_category_crew("2F")
    assert set(s_m) == {"M"} and set(s_f) == {"F"}
    assert w_m != w_f, "2M and 2F must be able to carry different weights"
    assert h_m != h_f
    # same for the threes
    _, _, w3m = config.race_category_crew("3M")
    _, _, w3f = config.race_category_crew("3F")
    assert w3m != w3f


# ── config validation fails loudly ──────────────────────────────────────────
def _cfg(tmp_path, body):
    p = tmp_path / "canoe_suite.cfg"
    p.write_text(body, encoding="utf-8")
    from config.settings import Settings
    return Settings.load(str(p))


def test_missing_category_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="missing"):
        _cfg(tmp_path, "crew:\n  race_categories:\n"
                       "    \"2M\": {sexes: [M, M], heights_in: [70, 70]}\n")


def test_wrong_crew_size_is_rejected(tmp_path):
    body = "crew:\n  race_categories:\n"
    for n, s in (("2M", "M"), ("2F", "F"), ("3M", "M"), ("3F", "F")):
        k = int(n[0])
        body += (f"    \"{n}\": {{sexes: [{', '.join([s]*k)}], "
                 f"heights_in: [{', '.join(['70']*k)}]}}\n")
    body += "    \"4_coed\": {sexes: [M, M, F], heights_in: [70, 70, 65]}\n"
    with pytest.raises(ValueError, match="implies 4 paddlers"):
        _cfg(tmp_path, body)


def test_coed_composition_is_enforced(tmp_path):
    body = "crew:\n  race_categories:\n"
    for n, s in (("2M", "M"), ("2F", "F"), ("3M", "M"), ("3F", "F")):
        k = int(n[0])
        body += (f"    \"{n}\": {{sexes: [{', '.join([s]*k)}], "
                 f"heights_in: [{', '.join(['70']*k)}]}}\n")
    body += "    \"4_coed\": {sexes: [M, M, M, F], heights_in: [70, 70, 70, 65]}\n"
    with pytest.raises(ValueError, match="2 male \\+ 2 female"):
        _cfg(tmp_path, body)


def test_posture_and_angle_are_live_from_cfg(tmp_path):
    s = _cfg(tmp_path, "crew:\n  kneel_posture: A_heels_sitting\n"
                       "  torso_angle_deg: 55.0\n"
                       "  kneel_hip_height_in: 9.25\n")
    assert s.crew.kneel_posture == "A_heels_sitting"
    assert s.crew.torso_angle_deg == pytest.approx(55.0)
    assert s.crew.kneel_hip_height_in == pytest.approx(9.25)


def test_hip_height_defaults_to_unmeasured(tmp_path):
    """None means 'nobody has measured this yet', which the report must say."""
    s = _cfg(tmp_path, "crew:\n  torso_angle_deg: 70.0\n")
    assert s.crew.kneel_hip_height_in is None
