"""
Regression tests for the geometric tangent-and-arc Dubins racing line in
``physics/race_path.py``.

These tests pin down the contract written in
``.claude/plans/dubins_hotfix.md §5``.  They are the regression that
catches the chord-heading-discontinuity bug fixed in this hotfix.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from physics.race_path import analyse_course
from race_course import ALL_COURSES, RaceCourse, Waypoint


# ── Helpers ────────────────────────────────────────────────────────────────

def _max_heading_jump_deg(xy: np.ndarray) -> float:
    """Largest angular jump between consecutive sample chords [deg]."""
    diffs = np.diff(xy, axis=0)
    L = np.linalg.norm(diffs, axis=1)
    diffs = diffs[L > 1e-9]
    if len(diffs) < 2:
        return 0.0
    h = np.arctan2(diffs[:, 1], diffs[:, 0])
    dj = np.diff(h)
    dj = (dj + math.pi) % (2.0 * math.pi) - math.pi
    return float(np.max(np.abs(dj)) * 180.0 / math.pi)


def _curvature_baseline_deg(step: float, rho: float) -> float:
    """Heading rotation between adjacent samples on a circle of radius rho."""
    return step / rho * 180.0 / math.pi


# ── Tests ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("course_name", list(ALL_COURSES.keys()))
def test_g1_continuity_on_default_courses(course_name):
    """No heading-jump spike above the curvature baseline anywhere on the
    default courses.  This is the regression that catches the old
    chord-heading discontinuity at every interior buoy."""
    course = ALL_COURSES[course_name]()
    rho = 2.0
    step = 0.05  # tight sampling so curvature baseline is small
    r = analyse_course(course, min_turn_radius=rho, sample_step_m=step)

    jump = _max_heading_jump_deg(r["path_xy"])
    baseline = _curvature_baseline_deg(step, rho)
    # Allow 1.2× the curvature baseline as headroom for floating-point
    # and the variable-spacing first/last sub-sample of each segment.
    # The old buggy path would jump >5° here even at fine sampling.
    assert jump < 1.2 * baseline, (
        f"{course_name}: max heading jump {jump:.3f}° exceeds 1.2× "
        f"curvature baseline {baseline:.3f}° — likely G1 break"
    )


@pytest.mark.parametrize("course_name", list(ALL_COURSES.keys()))
def test_length_consistency_on_default_courses(course_name):
    """total_path_length_m == total_arc_length_m + total_tangent_length_m."""
    course = ALL_COURSES[course_name]()
    r = analyse_course(course, min_turn_radius=2.0)
    err = abs(r["total_path_length_m"]
              - (r["total_arc_length_m"] + r["total_tangent_length_m"]))
    assert err < 1e-6, f"{course_name}: length mismatch {err:.3e} m"


@pytest.mark.parametrize("course_name", list(ALL_COURSES.keys()))
def test_capability_targets_sum_to_one(course_name):
    """Cs + Ct == 1 for every course."""
    course = ALL_COURSES[course_name]()
    r = analyse_course(course, min_turn_radius=2.0)
    assert abs(r["Cs"] + r["Ct"] - 1.0) < 1e-9


def test_internal_tangent_infeasible_raises_with_both_labels():
    """Two buoys at half-radius spacing requesting LSR must fail loudly
    and name both buoys."""
    rho = 2.0
    # Place two buoys 0.5·ρ apart; require an alternating L→R turn so
    # the helper picks the internal tangent.  Layout:
    #   start (0,0) → buoy_A on left side → buoy_B on right side → finish.
    #   Buoys are <2ρ apart on the y-axis, blocking the LSR tangent.
    course = RaceCourse(
        name="Infeasible LSR",
        waypoints=[
            Waypoint(0.0, 0.0, label="start"),
            # First buoy: turn left here (cross > 0 with the next).
            Waypoint(50.0, 1.0, turn_radius=rho, turn_angle=10.0,
                     label="buoy_A"),
            # Second buoy 0.5·ρ away on the opposite side: forces L→R but
            # circles are far too close (D = sqrt(0.5² + (ρ)²) < 2ρ).
            Waypoint(50.5, -1.0, turn_radius=rho, turn_angle=10.0,
                     label="buoy_B"),
            Waypoint(100.0, 0.0, label="finish"),
        ],
    )
    with pytest.raises(ValueError) as exc:
        analyse_course(course, min_turn_radius=rho)
    msg = str(exc.value)
    assert "buoy_A" in msg
    assert "buoy_B" in msg


def test_single_buoy_90deg_closed_form():
    """One 90° buoy at radius ρ.  With the canoe's turning circle
    centred *at* the buoy, the tangent points are offset from the chord
    directions by ``arcsin(ρ/D)`` on each side, so the actual arc sweep
    is ``π/2 + 2·arcsin(ρ/D)`` — not the bare 90° the buoy demands."""
    rho = 2.0
    leg = 20.0
    course = RaceCourse(
        name="One 90° buoy",
        waypoints=[
            Waypoint(0.0, 0.0, label="start"),
            Waypoint(leg, 0.0, turn_radius=rho, turn_angle=90.0,
                     label="apex"),
            Waypoint(leg, leg, label="finish"),
        ],
    )
    r = analyse_course(course, min_turn_radius=rho)
    # Tangent from (0,0) to circle of radius ρ at (leg, 0):
    # length = sqrt(leg² − ρ²); same for the exit tangent.
    expected_tan = 2.0 * math.sqrt(leg * leg - rho * rho)
    expected_sweep = math.pi / 2.0 + 2.0 * math.asin(rho / leg)
    expected_arc = rho * expected_sweep
    assert abs(r["total_tangent_length_m"] - expected_tan) < 1e-6, (
        f"tangent length {r['total_tangent_length_m']:.6f} m != "
        f"expected {expected_tan:.6f} m"
    )
    assert abs(r["total_arc_length_m"] - expected_arc) < 1e-6, (
        f"arc length {r['total_arc_length_m']:.6f} m != "
        f"expected {expected_arc:.6f} m"
    )
    assert r["dubins_words"] == []   # one circle → zero transitions


def test_straight_line_no_arc():
    """A course with two pass-through waypoints (no buoys) is a straight
    line — zero arc length, tangent length equal to the chord."""
    course = RaceCourse(
        name="Straight",
        waypoints=[
            Waypoint(0.0, 0.0, label="start"),
            Waypoint(100.0, 0.0, label="mid"),    # pass-through
            Waypoint(200.0, 0.0, label="finish"),
        ],
    )
    r = analyse_course(course, min_turn_radius=2.0)
    assert r["total_arc_length_m"] == 0.0
    assert abs(r["total_tangent_length_m"] - 200.0) < 1e-6
    assert r["dubins_words"] == []


def test_min_turn_radius_validation():
    """min_turn_radius must be strictly positive."""
    course = ALL_COURSES["sprint"]()
    with pytest.raises(ValueError, match="min_turn_radius must be > 0"):
        analyse_course(course, min_turn_radius=0.0)
    with pytest.raises(ValueError, match="min_turn_radius must be > 0"):
        analyse_course(course, min_turn_radius=-1.0)


def test_buoy_radius_clamped_up_to_min():
    """A buoy whose ``turn_radius`` is below ``min_turn_radius`` is
    rounded at the larger ρ (canoe cannot turn tighter)."""
    rho = 5.0
    leg = 50.0
    course = RaceCourse(
        name="Tight buoy",
        waypoints=[
            Waypoint(0.0, 0.0, label="start"),
            # Buoy requests r=1, but ρ=5 enforces r=5.
            Waypoint(leg, 0.0, turn_radius=1.0, turn_angle=90.0,
                     label="apex"),
            Waypoint(leg, leg, label="finish"),
        ],
    )
    r = analyse_course(course, min_turn_radius=rho)
    expected_arc = rho * (math.pi / 2.0 + 2.0 * math.asin(rho / leg))
    assert abs(r["total_arc_length_m"] - expected_arc) < 1e-6


# ── Bounded START / FINISH (y-range) zones ────────────────────────────────

def test_waypoint_validates_y_range():
    """y_min must be ≤ y_max."""
    Waypoint(0.0, 0.0, y_min=1.0, y_max=2.0)            # ok
    Waypoint(0.0, 0.0, y_min=1.0, y_max=1.0)            # ok (degenerate)
    with pytest.raises(ValueError, match="y_min"):
        Waypoint(0.0, 0.0, y_min=2.0, y_max=1.0)


# NOTE ON THE SPRINT COURSE.  Three tests here used to assert that the sprint
# course exposes Lane 3 / Lane 4 bounds ([22.5, 37.5] / [37.5, 52.5]) around a
# buoy at y=30.  Production's make_sprint_course() describes a DIFFERENT
# course: buoy at (92.5, 15.0), Lane 1 (y 0-15, centre 7.5) out and Lane 2
# (y 15-30, centre 22.5) back, with unbounded START/FINISH.  Both layouts are
# internally consistent -- production's buoy sits exactly at the midpoint of
# its two lane centres, so a 180-degree sweep runs lane-to-lane -- so this is a
# SPEC question (which lane assignment the ASCE rules actually mandate), not a
# code defect, and it cannot be settled from the source.
#
# Rather than encode an unverified lane spec, the bounded-waypoint FEATURE is
# now covered against the SLALOM course, which genuinely declares y-bounds in
# production.  If the sprint should also be lane-bounded, add the bounds to
# make_sprint_course() and assert them here.

def test_slalom_course_has_bounded_start_and_finish():
    """Slalom is the course that really declares y-bounds in production."""
    course = ALL_COURSES["slalom"]()
    start = course.waypoints[0]
    assert start.label == "START" and start.is_bounded
    assert (start.y_min, start.y_max) == (0.0, 15.0)


def test_bounded_start_is_pinned_inside_its_declared_zone():
    """A bounded START must place the path inside [y_min, y_max]."""
    course = ALL_COURSES["slalom"]()
    start = course.waypoints[0]
    r = analyse_course(course, min_turn_radius=2.0)
    y0 = r["path_xy"][0][1]
    assert start.y_min - 1e-9 <= y0 <= start.y_max + 1e-9, (
        f"bounded START placed at y={y0}, outside [{start.y_min}, {start.y_max}]"
    )


def test_bounding_never_lengthens_the_tangent_vs_a_pinned_start():
    """
    Freeing START to slide within its lane can only shorten the tangent path.

    Property test rather than a magnitude assertion: the previous version
    hardcoded a 0.5-1.0 m saving specific to a course geometry that does not
    exist in production.
    """
    bounded = ALL_COURSES["slalom"]()
    pinned = RaceCourse(
        name="Slalom-pinned-start",
        waypoints=[Waypoint(w.x, w.y, turn_radius=w.turn_radius,
                            turn_angle=w.turn_angle, label=w.label)
                   for w in bounded.waypoints],
    )
    rb = analyse_course(bounded, min_turn_radius=2.0)
    rp = analyse_course(pinned, min_turn_radius=2.0)
    assert rb["total_tangent_length_m"] <= rp["total_tangent_length_m"] + 1e-6


def test_bounded_zone_picks_circle_y_when_in_range():
    """Synthetic course where the buoy y is *inside* the start zone:
       the optimal y must equal the buoy's y exactly."""
    rho = 2.0
    course = RaceCourse(
        name="Buoy in start zone",
        waypoints=[
            # START line at x=0, y∈[20, 40].
            Waypoint(0.0, 30.0, y_min=20.0, y_max=40.0, label="START"),
            # Buoy at y=27 (inside the start zone).
            Waypoint(50.0, 27.0, turn_radius=rho, turn_angle=90.0,
                     label="buoy"),
            Waypoint(50.0, 50.0, label="FINISH"),
        ],
    )
    r = analyse_course(course, min_turn_radius=rho)
    start_pt = r["path_xy"][0]
    # Optimal start y = circle centre y = 27 (buoy is at the circle centre).
    assert abs(start_pt[1] - 27.0) < 1e-9, (
        f"expected start y=27.0, got {start_pt[1]}"
    )


def test_bounded_zone_clamps_to_far_edge_when_circle_above_range():
    """If the circle's y is *above* the bound's y_max, the optimal y is
    clamped to y_max (the closest legal y to the circle centre)."""
    rho = 2.0
    course = RaceCourse(
        name="Circle above zone",
        waypoints=[
            # START line at x=0, y∈[0, 10].
            Waypoint(0.0, 5.0, y_min=0.0, y_max=10.0, label="START"),
            # Circle far above: y=50.  Optimal y_start = 10 (clamped).
            Waypoint(50.0, 50.0, turn_radius=rho, turn_angle=90.0,
                     label="buoy"),
            Waypoint(50.0, 100.0, label="FINISH"),
        ],
    )
    r = analyse_course(course, min_turn_radius=rho)
    start_pt = r["path_xy"][0]
    assert abs(start_pt[1] - 10.0) < 1e-9


def test_no_buoys_keeps_strict_y_for_bounded_zones():
    """If there are no interior buoys, no circle-based optimisation is
    possible — the bounded START/FINISH should fall back to the
    waypoint's strict y (no surprises in the legacy degenerate path)."""
    course = RaceCourse(
        name="Bounded straight line",
        waypoints=[
            Waypoint(0.0, 30.0, y_min=22.5, y_max=37.5, label="START"),
            Waypoint(100.0, 45.0, y_min=37.5, y_max=52.5, label="FINISH"),
        ],
    )
    r = analyse_course(course, min_turn_radius=2.0)
    start_pt, end_pt = r["path_xy"][0], r["path_xy"][-1]
    assert (start_pt[0], start_pt[1]) == (0.0, 30.0)
    assert (end_pt[0], end_pt[1]) == (100.0, 45.0)


def test_dubins_words_match_buoy_pattern_in_slalom():
    """The slalom alternates buoys side to side, so consecutive
    transitions must alternate between LSR and RSL (canoe goes left then
    right then left then right …)."""
    course = ALL_COURSES["slalom"]()
    r = analyse_course(course, min_turn_radius=2.0)
    words = r["dubins_words"]
    assert len(words) > 0
    for w in words:
        assert w in ("LSR", "RSL", "LSL", "RSR")
    # Alternation: every internal-tangent word should swap left/right.
    for prev, nxt in zip(words, words[1:]):
        if prev == "LSR" and nxt == "RSL":
            continue
        if prev == "RSL" and nxt == "LSR":
            continue
        # Allow consecutive same-direction (LSL→LSL) but the slalom
        # course is specifically alternating, so flag anything else.
        assert prev[0] == nxt[0] or prev[2] == nxt[0], (
            f"unexpected slalom word transition: {prev} → {nxt}"
        )
