# tests/test_load_case_expansion.py
"""
Five ASCE race load cases, seating arrangements, and CROSS-BASIS DISCIPLINE.

The cross-basis class of bug has now bitten twice, both times fabricating a
plausible finding out of a mismatch rather than a physical effect:

  1. the top-flange trade read a REJECTED evaluation's 1e6 sentinel weight as a
     real mass, which made the crew negligible and invented a +4.71 in stability
     gain;
  2. the same trade then compared a variant measured at the flat 12 in crew CG
     against a base measured at the model's 20.02 in, and reported the 6.24 in
     basis change as a flange effect.

Both were caught by eye.  These tests catch them by construction: any
comparison in this suite must state, and be held to, the basis each side used.
"""
from __future__ import annotations

import numpy as np
import pytest

import config
import structural
from structural import (calculate_moment_envelope, distinct_arrangements,
                        transverse_gmt_design_in)


# ── the five categories are the load cases ──────────────────────────────────
def test_five_load_cases_not_three():
    seats = {n: list(np.linspace(3.0, 15.0, config.RACE_CATEGORY_SIZES[n]))
             for n in config.RACE_CATEGORY_ORDER}
    cases = config.crew_load_cases(seats)
    assert [r[0] for r in cases] == list(config.RACE_CATEGORY_ORDER)
    assert len(cases) == 5


def test_every_case_carries_its_own_weights():
    """
    THE cross-basis guard on the load model: no case may be handed another
    crew's weights.  2M and 2F are the same size, so a per-size lookup would
    silently give 2F the men's weights.
    """
    seats = {n: list(np.linspace(3.0, 15.0, config.RACE_CATEGORY_SIZES[n]))
             for n in config.RACE_CATEGORY_ORDER}
    for name, coords, weights, _pins in config.crew_load_cases(seats):
        want = config.RACE_CATEGORIES[name]["weights_lb"]
        assert weights == [float(w) for w in want], name
        assert len(coords) == len(weights), name
    by_name = {r[0]: r[2] for r in config.crew_load_cases(seats)}
    assert by_name["2M"] != by_name["2F"]
    assert by_name["3M"] != by_name["3F"]


def test_seat_weight_mismatch_is_rejected_loudly():
    """A silent mismatch would load the wrong crew and still return a number."""
    x = np.linspace(0.0, 18.0, 21)
    with pytest.raises(ValueError, match="seats but"):
        calculate_moment_envelope(
            x, 0.9, np.full(21, 8.0), np.full(21, 2.0), return_all=True,
            crew_cases=[("2M", [5.0, 12.0], [175.0, 165.0, 155.0])])


def test_seat_layout_is_one_definition():
    layout = config.crew_seat_layout()
    assert [n for n, _ in layout] == list(config.RACE_CATEGORY_ORDER)
    assert sum(s.stop - s.start for _, s in layout) == config.crew_seat_vector_len()
    assert config.crew_seat_vector_len() == 14
    # slices must tile the vector with no gap or overlap
    covered = []
    for _n, s in layout:
        covered.extend(range(s.start, s.stop))
    assert covered == list(range(config.crew_seat_vector_len()))


def test_load_cases_reject_a_wrong_length_vector():
    with pytest.raises(ValueError, match="expected 14"):
        config.crew_load_cases(list(range(9)))


# ── heaviest crew is COMPUTED ───────────────────────────────────────────────
def test_heaviest_crew_is_computed_not_assumed(monkeypatch):
    """
    The freeboard ceiling used sum(RACE_4_PERSON) on the assumption that the
    four is heaviest.  Make a three heavier and the answer must follow.
    """
    assert config.heaviest_crew_category() == "4_coed"     # with shipped weights
    cats = {k: {kk: list(vv) for kk, vv in v.items()}
            for k, v in config.RACE_CATEGORIES.items()}
    cats["3M"]["weights_lb"] = [300.0, 300.0, 300.0]       # 900 lb
    monkeypatch.setattr(config, "RACE_CATEGORIES", cats)
    assert config.heaviest_crew_category() == "3M"
    assert config.heaviest_crew_lbs() == pytest.approx(900.0)


def test_heaviest_crew_matches_a_real_category():
    cat = config.heaviest_crew_category()
    assert sum(config.RACE_CATEGORIES[cat]["weights_lb"]) == pytest.approx(
        config.heaviest_crew_lbs())


# ── arrangements ────────────────────────────────────────────────────────────
def test_distinct_arrangements_collapse_duplicates():
    assert len(distinct_arrangements([160, 160, 160, 160])) == 1
    assert len(distinct_arrangements([175, 165, 145, 135])) == 24
    assert len(distinct_arrangements([160, 150, 170, 160])) == 12   # one repeat
    assert len(distinct_arrangements([175, 165])) == 2


def _env(crew_cases, arr):
    x = np.linspace(0.0, 18.0, 81)
    dx = 18.0 / 80
    return calculate_moment_envelope(
        x, dx, np.full(81, 8.0), np.full(81, 2.2), return_all=True,
        crew_cases=crew_cases, enumerate_arrangements=arr)


def test_enumeration_can_only_raise_the_envelope():
    """
    A max over more load states is monotone.  If enumeration ever LOWERED the
    envelope, the per-station max is not being taken.
    """
    cc = [("4_coed", [3.0, 7.5, 12.0, 15.0], [175.0, 165.0, 145.0, 135.0])]
    off, on = _env(cc, False), _env(cc, True)
    raised = False
    for k in ("sag", "hog"):
        a, b = off["moment"][k], on["moment"][k]
        assert np.all(b >= a - 1e-9), f"{k}: enumeration LOWERED the envelope"
        raised |= bool(np.any(b > a + 1e-9))
    # and it must actually bite somewhere -- a crew with four different weights
    # cannot be arrangement-insensitive at every station of both senses.
    assert raised, "enumeration changed nothing for a fully unequal crew"


def test_uniform_crew_makes_enumeration_a_no_op():
    """Nothing to permute -> the envelope must be bit-identical, not just close."""
    cc = [("4_coed", [3.0, 7.5, 12.0, 15.0], [160.0, 160.0, 160.0, 160.0])]
    off, on = _env(cc, False), _env(cc, True)
    for k in ("sag", "hog"):
        assert np.array_equal(on["moment"][k], off["moment"][k]), k
    assert on["arrangements"]["4_coed"] == 1


def test_arrangement_count_is_reported():
    cc = [("2M", [6.0, 12.0], [175.0, 165.0]),
          ("4_coed", [3.0, 7.5, 12.0, 15.0], [175.0, 165.0, 145.0, 135.0])]
    r = _env(cc, True)
    assert r["arrangements"] == {"2M": 2, "4_coed": 24}
    assert _env(cc, False)["arrangements"] == {"2M": 1, "4_coed": 1}


def test_nominal_ordering_drives_the_reported_per_case_curve():
    """
    The envelope covers every ordering, but the per-case REPORTING curve must
    describe the crew as configured -- otherwise the report shows a moment
    diagram for an arbitrary permutation the team never plans to paddle.
    """
    cc = [("4_coed", [3.0, 7.5, 12.0, 15.0], [175.0, 165.0, 145.0, 135.0])]
    on, off = _env(cc, True), _env(cc, False)
    assert np.allclose(on["moment"]["by_case"]["4_coed"],
                       off["moment"]["by_case"]["4_coed"])


# ── INVARIANTS of the expansion ─────────────────────────────────────────────
_SEATS5 = {"2M": [6.0, 12.0], "2F": [6.0, 12.0],
           "3M": [5.0, 9.0, 13.0], "3F": [5.0, 9.0, 13.0],
           "4_coed": [3.0, 7.5, 12.0, 15.0]}


def _cases(names, pins=None):
    out = []
    for n in names:
        cat = config.RACE_CATEGORIES[n]
        out.append((n, list(_SEATS5[n]), list(cat["weights_lb"]),
                    list((pins or {}).get(n, cat["seat_pins"]))))
    return out


def test_adding_a_load_case_can_only_raise_the_envelope():
    """
    THE expansion invariant: the envelope is a max over cases, so going 3 -> 5
    cannot lower it at any station.  A drop would mean cases are replacing each
    other instead of enveloping.
    """
    three = _env(_cases(["2M", "3M", "4_coed"]), True)
    five = _env(_cases(list(config.RACE_CATEGORY_ORDER)), True)
    for k in ("sag", "hog"):
        assert np.all(five["moment"][k] >= three["moment"][k] - 1e-9), k


def test_envelope_is_monotone_as_cases_accumulate():
    """Every prefix of the case list must be bounded by the next."""
    order = list(config.RACE_CATEGORY_ORDER)
    prev = None
    for i in range(1, len(order) + 1):
        cur = _env(_cases(order[:i]), True)
        if prev is not None:
            for k in ("sag", "hog"):
                assert np.all(cur["moment"][k] >= prev["moment"][k] - 1e-9), (i, k)
        prev = cur


def test_no_cross_contamination_between_categories(monkeypatch):
    """
    Changing ONE crew's weights must not move another crew's curve.  2M and 2F
    are the same size and were the same list before the split -- a per-size
    lookup surviving anywhere would show up here.
    """
    base = _env(_cases(["2M", "2F"]), True)
    cats = {k: {kk: list(vv) for kk, vv in v.items()}
            for k, v in config.RACE_CATEGORIES.items()}
    cats["2F"]["weights_lb"] = [400.0, 390.0]        # absurd, to be unmissable
    monkeypatch.setattr(config, "RACE_CATEGORIES", cats)
    after = _env(_cases(["2M", "2F"]), True)
    assert np.allclose(after["moment"]["by_case"]["2M"],
                       base["moment"]["by_case"]["2M"]), \
        "2F's weights leaked into 2M"
    assert not np.allclose(after["moment"]["by_case"]["2F"],
                           base["moment"]["by_case"]["2F"])


def test_pinning_one_category_leaves_the_others_alone():
    """A pin is scoped to its own race, not to every crew of that size."""
    base = _env(_cases(list(config.RACE_CATEGORY_ORDER)), True)
    pinned = _env(_cases(list(config.RACE_CATEGORY_ORDER),
                         pins={"4_coed": [1, None, None, 4]}), True)
    for cat in ("2M", "2F", "3M", "3F"):
        assert np.allclose(pinned["moment"]["by_case"][cat],
                           base["moment"]["by_case"][cat]), cat
        assert (pinned["arrangement_disclosure"][cat]
                == base["arrangement_disclosure"][cat]), cat
    assert (pinned["arrangement_disclosure"]["4_coed"]["n_evaluated"]
            < base["arrangement_disclosure"]["4_coed"]["n_evaluated"])


def test_each_case_curve_depends_only_on_its_own_seats():
    """Moving one crew's seats must not move another crew's curve."""
    base = _env(_cases(["2M", "4_coed"]), True)
    moved = dict(_SEATS5)
    cc = [("2M", list(moved["2M"]), list(config.RACE_CATEGORIES["2M"]["weights_lb"]),
           [None, None]),
          ("4_coed", [2.5, 6.0, 11.0, 15.5],
           list(config.RACE_CATEGORIES["4_coed"]["weights_lb"]), [None] * 4)]
    after = _env(cc, True)
    assert np.allclose(after["moment"]["by_case"]["2M"],
                       base["moment"]["by_case"]["2M"])
    assert not np.allclose(after["moment"]["by_case"]["4_coed"],
                           base["moment"]["by_case"]["4_coed"])


# ── ONE CREW DEFINITION PER RUN (split-brain guards) ────────────────────────
def test_a_case_made_governing_must_move_the_envelope():
    """
    (a) 3 -> 5 moved the envelope by exactly zero, which is correct physics when
    2F/3F are lighter than 2M/3M -- and also exactly what an INERT case looks
    like.  Force a new case to govern; the envelope must react.
    """
    light = _env(_cases(list(config.RACE_CATEGORY_ORDER)), True)
    heavy = _env([
        (n, list(_SEATS5[n]),
         ([400.0, 390.0] if n == "2F" else
          [400.0, 390.0, 380.0] if n == "3F" else
          list(config.RACE_CATEGORIES[n]["weights_lb"])),
         [None] * config.RACE_CATEGORY_SIZES[n])
        for n in config.RACE_CATEGORY_ORDER], True)
    assert np.max(heavy["moment"]["sag"]) > np.max(light["moment"]["sag"]) + 1.0
    # ... and the forced case must actually become governing somewhere
    gov = {heavy["cases"][i] for i in heavy["moment"]["gov_sag"] if i >= 0}
    assert {"2F", "3F"} & gov, f"forced-heavy cases never govern: {sorted(gov)}"


def test_lighter_added_cases_correctly_do_not_move_it():
    """The other half of (a): inertness and non-governance must be separable."""
    three = _env(_cases(["2M", "3M", "4_coed"]), True)
    five = _env(_cases(list(config.RACE_CATEGORY_ORDER)), True)
    assert sum(config.RACE_CATEGORIES["2F"]["weights_lb"]) < \
        sum(config.RACE_CATEGORIES["2M"]["weights_lb"])
    assert sum(config.RACE_CATEGORIES["3F"]["weights_lb"]) < \
        sum(config.RACE_CATEGORIES["3M"]["weights_lb"])
    for k in ("sag", "hog"):
        assert np.allclose(five["moment"][k], three["moment"][k])


def test_ui_crew_weight_reaches_the_load_cases_exactly():
    """
    (b) THE split-brain regression.  A UI-supplied crew weight must arrive in
    the LOAD CASES at its exact value -- not merely be non-default.  Under the
    old code StructuralService wrote RACE_*_PERSON and left RACE_CATEGORIES at
    the file defaults, so this value never reached the envelope at all.
    """
    from domain.models import OptimizationConfig
    from services.structural_service import StructuralService
    import copy

    saved = copy.deepcopy(config.RACE_CATEGORIES)
    try:
        oc = OptimizationConfig(race_4_person_lbs=[201.0, 202.0, 203.0, 204.0],
                                race_2_person_lbs=[211.0, 212.0],
                                race_3_person_lbs=[221.0, 222.0, 223.0])
        by_size = {4: oc.race_4_person_lbs, 2: oc.race_2_person_lbs,
                   3: oc.race_3_person_lbs}
        # Drive the REAL service path, not a re-implementation of it: whatever
        # run_sweep does to turn an OptimizationConfig into crew state is what
        # must land in the load cases.
        StructuralService()._apply_run_config(oc)

        seats = {n: list(np.linspace(3.0, 15.0, config.RACE_CATEGORY_SIZES[n]))
                 for n in config.RACE_CATEGORY_ORDER}
        for name, _c, w, _p in config.crew_load_cases(seats):
            assert w == [float(q) for q in by_size[len(w)]], name
        # and the derived aliases must agree with what the cases got
        assert config.RACE_4_PERSON == [201.0, 202.0, 203.0, 204.0]
        assert config.RACE_2_PERSON == [211.0, 212.0]
        assert config.RACE_3_PERSON == [221.0, 222.0, 223.0]
    finally:
        config.set_race_categories(saved)


def test_aliases_are_derived_never_stored():
    """
    (c) The aliases must not exist as module attributes at all -- a stored value
    is what allowed them to drift from RACE_CATEGORIES.  Same shape as the
    duplicated mock section properties and the hardcoded eps copy: two homes for
    one fact.
    """
    for name in ("RACE_4_PERSON", "RACE_2_PERSON", "RACE_3_PERSON"):
        assert name not in config.__dict__, (
            f"{name} is a STORED module attribute again; it must be derived on "
            f"read from RACE_CATEGORIES or the split-brain can return")
        assert getattr(config, name)          # still resolvable


def test_aliases_can_never_disagree_with_categories():
    """
    (c) Construct the case that WOULD have diverged: change only the categories
    and confirm the aliases follow, with no separate write.
    """
    import copy
    saved = copy.deepcopy(config.RACE_CATEGORIES)
    try:
        cats = copy.deepcopy(config.RACE_CATEGORIES)
        cats["4_coed"]["weights_lb"] = [301.0, 302.0, 303.0, 304.0]
        cats["2M"]["weights_lb"] = [311.0, 312.0]
        cats["2F"]["weights_lb"] = [111.0, 112.0]      # lighter -> not chosen
        config.set_race_categories(cats)
        assert config.RACE_4_PERSON == [301.0, 302.0, 303.0, 304.0]
        # per-size alias takes the HEAVIER same-size crew
        assert config.RACE_2_PERSON == [311.0, 312.0]
        # every alias must equal what the categories say, for every size
        assert config.RACE_4_PERSON == config.RACE_CATEGORIES["4_coed"]["weights_lb"]
        for size, alias in ((2, config.RACE_2_PERSON), (3, config.RACE_3_PERSON)):
            same = [c["weights_lb"] for k, c in config.RACE_CATEGORIES.items()
                    if config.RACE_CATEGORY_SIZES[k] == size]
            assert alias == max(same, key=sum)
    finally:
        config.set_race_categories(saved)


# ── REJECTED vs the optimizer penalty: ONE boundary, two behaviours ─────────
def test_rejection_is_not_a_number_anywhere_but_the_solver_boundary():
    """
    The marker exists so a rejected evaluation cannot be arithmetic'd into a
    reported mass.  But an OPTIMIZER needs an orderable value: SLSQP does
    `if fx < self._lowest_f` internally, so REJECTED raises inside SciPy and
    kills the worker.

    Both halves are pinned here so a future fix cannot collapse them back into
    one behaviour -- neither by making the marker numeric (the original bug) nor
    by letting the penalty leak into a report.
    """
    import structural
    # reporting/storage half: not a number, loudly
    assert structural.REJECTED is not None
    assert not structural.REJECTED
    for op in (lambda: structural.REJECTED + 1.0,
               lambda: float(structural.REJECTED),
               lambda: structural.REJECTED < 1.0,
               lambda: sorted([structural.REJECTED, 1.0])):
        with pytest.raises(TypeError, match="REJECTED"):
            op()
    # solver half: finite, orderable, and ranks LAST
    pen = structural.as_optimizer_penalty(structural.REJECTED)
    assert isinstance(pen, float) and np.isfinite(pen)
    assert pen == structural.INFEASIBLE_PENALTY
    assert pen > 1e5
    assert sorted([pen, 1.0, 500.0])[-1] == pen
    # a feasible weight passes through untouched
    assert structural.as_optimizer_penalty(123.5) == pytest.approx(123.5)


def test_slsqp_objective_survives_an_infeasible_candidate():
    """
    THE regression: SciPy compares objective values, so an objective that can
    return REJECTED kills the solve ("0 configurations costed, sweep returned
    None").  Any objective handed to a solver must convert at the boundary.
    """
    from scipy.optimize import minimize
    import structural

    def obj(v):
        # infeasible in a region the optimiser will probe
        raw = structural.REJECTED if v[0] < 0.5 else float(v[0])
        return structural.as_optimizer_penalty(raw)

    r = minimize(obj, x0=[0.2], method="SLSQP", bounds=[(0.0, 2.0)])
    assert np.isfinite(r.fun), "solver did not survive an infeasible candidate"


def test_reporting_path_still_refuses_a_rejected_weight():
    """
    The other half: the penalty must NOT leak into anything that reports a
    weight.  A rejected winner reaching the loaded-freeboard check is what
    produced waterline == mold depth and zero freeboard.
    """
    import structural
    from reporting.latex_report import build_report_model
    with pytest.raises((RuntimeError, TypeError, KeyError, AttributeError)):
        build_report_model({"weight_lbs": structural.REJECTED})


def test_only_solver_boundaries_convert_rejections():
    """
    as_optimizer_penalty is the ONE sanctioned conversion.  If it appears
    outside an optimizer objective, a rejection is being laundered into a
    number somewhere it will be reported.
    """
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    hits = []
    for rel in ("main.py", "structural.py", "reporting/latex_report.py",
                "services/structural_service.py"):
        f = root / rel
        if not f.exists():
            continue
        for ln, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"as_optimizer_penalty\s*\(", line.split("#", 1)[0]):
                hits.append((rel, ln))
    # structural.py defines it; main.py uses it once, at the SLSQP boundary.
    uses = [h for h in hits if h[0] != "structural.py"]
    assert len(uses) <= 1, (
        f"as_optimizer_penalty used at {uses}; it is only legitimate at a "
        f"solver boundary. Reporting paths must keep REJECTED.")


# ── POSITIONAL-INDEX REGRESSIONS (the seating vector grew 6 -> 14) ──────────
def test_mix_densities_are_sliced_after_the_seats_not_at_index_6():
    """
    evaluate_canoe_weight read mix densities as vars[6:], hardcoded to the old
    4+2 layout.  With 14 seats that slice returned SEAT COORDINATES as
    densities, so the single-zone path priced hulls out of paddler positions
    and rejected everything.  Invisible to multizone fixtures, which pass
    zone_dens separately.
    """
    from structural import _crew_seat_count
    seats = {n: list(np.linspace(3.0, 15.0, config.RACE_CATEGORY_SIZES[n]))
             for n in config.RACE_CATEGORY_ORDER}
    cases = config.crew_load_cases(seats)
    assert _crew_seat_count(cases) == config.crew_seat_vector_len() == 14
    # a legacy 3-case list must still report 9, not 14
    legacy = [c for c in cases if c[0] in ("2M", "3M", "4_coed")]
    assert _crew_seat_count(legacy) == 9


#: Hand-written slices of the seating vector.  Five separate bugs in this
#: refactor were this one pattern, in five different files, each silently
#: misreading a 14-element vector as the old 4+2 layout.  A legitimate slice
#: must come from config.crew_seat_layout().
#: Where it matters the seating vector is always a BARE LOCAL -- ``coords[:6]``,
#: ``vars[6:]``, ``v[2]``.  It is never an attribute of a call, so requiring a
#: bare identifier before the bracket keeps every real violation and drops the
#: false positives.  Without it this scan matched ``uuid.uuid4().hex[:6]`` in
#: main.py and therefore failed UNCONDITIONALLY -- a guard that is always red
#: reports nothing while still counting as coverage.  The positive control
#: below fails if this scan ever stops catching a genuine slice.
_SUBJ = r"(?<![.\w])[A-Za-z_]\w*"

_HAND_SLICE_PATTERNS = [
    (_SUBJ + r"\[\s*0\s*:\s*4\s*\]", "coords[0:4] -- the old 4-person slice"),
    (_SUBJ + r"\[\s*4\s*:\s*6\s*\]", "coords[4:6] -- the old 2-person slice"),
    (_SUBJ + r"\[\s*6\s*:\s*9\s*\]", "coords[6:9] -- the old 3-person slice"),
    (r"\bvars\[\s*6\s*:", "vars[6:] -- mix densities after a 6-seat vector"),
    (_SUBJ + r"\[\s*:\s*6\s*\]", "[:6] -- the old seating-vector length"),
    (r"v\[2\]\s*-\s*v\[1\]", "v[2]-v[1] -- a cross-crew 'gap'"),
    (r"\brange\(\s*9\s*\)", "range(9) -- the old seating vector length"),
    (r"(?<![A-Za-z_])size\s*=\s*9\b", "size=9 -- the old seating length"),
]

#: Files that legitimately mention the patterns (this test, and the docs that
#: explain the bug).  Everything else must be clean.
_SLICE_SCAN_EXEMPT = {"tests/test_load_case_expansion.py"}

#: A line may opt out with this marker when the slice IS the documented legacy
#: fallback -- the one place that must still understand the old 4+2 vector so
#: pre-expansion callers keep working.  The marker is deliberately verbose: it
#: should be unpleasant enough to add that nobody reaches for it casually.
_SLICE_OK_MARKER = "LEGACY-SEATING-SLICE-OK"


def _scan_for_hand_slices(rel, lines):
    """The scan itself, over ONE file's lines.  Extracted so the positive
    control below exercises the same code the real scan runs -- a control that
    re-implemented the matching could pass while the real scan was blind."""
    import re
    bad = []
    for ln, line in enumerate(lines, 1):
        # The opt-out marker may sit on the line itself or in the comment
        # block immediately above it, so a multi-line justification works.
        window = lines[max(0, ln - 4):ln]
        if any(_SLICE_OK_MARKER in w for w in window):
            continue
        code = line.split("#", 1)[0]          # comments may discuss the bug
        for pat, why in _HAND_SLICE_PATTERNS:
            if re.search(pat, code):
                bad.append(f"{rel}:{ln}  {why}\n      {line.strip()}")
    return bad


def test_no_hand_written_seating_slices_anywhere():
    """
    THE recurring defect of this refactor, caught in FIVE places: production's
    spacing check, its start filter, the winner's final re-evaluation, the
    multi-zone winner rebuild, and the Jacobian test's own filter.

    A function-name scan missed the fifth (it checked evaluate_canoe_weight and
    the bug was in evaluate_multizone_weight), so this scans for the PATTERN.
    Any hand-written slice of the seating vector fails, wherever it lives.
    """
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    targets = ["main.py", "structural.py", "geometry.py",
               "reporting/latex_report.py", "services/structural_service.py",
               "ui/structural_tab.py"]
    targets += [f"tests/{p.name}" for p in (root / "tests").glob("test_*.py")]
    bad = []
    for rel in targets:
        if rel in _SLICE_SCAN_EXEMPT:
            continue
        f = root / rel
        if not f.exists():
            continue
        bad += _scan_for_hand_slices(rel, f.read_text(encoding="utf-8")
                                     .splitlines())
    assert not bad, (
        "hand-written seating-vector slice(s) found; derive the layout from "
        "config.crew_seat_layout() instead:\n  " + "\n  ".join(bad))


def test_the_slice_scan_still_catches_a_real_one():
    """
    POSITIVE CONTROL.  The scan above only means something while it can still
    fail, and it spent this refactor unable to: a stray ``uuid4().hex[:6]``
    matched the bare ``[:6]`` pattern, so it reported the same false positive
    on every run and a genuine slice would have been lost in the noise.

    So: real violations must still be caught, ordinary code must not be, and
    the documented opt-out must still work.
    """
    caught = _scan_for_hand_slices("fake.py", [
        "    a = coords[0:4]",
        "    b = coords[4:6]",
        "    c = coords[6:9]",
        "    d = vars[6:]",
        "    e = starts[:6]",
        "    gap = v[2] - v[1]",
        "    for i in range(9):",
        "    arr = np.empty(size=9)",
    ])
    assert len(caught) == 8, f"scan went blind; caught only {len(caught)}:\n" \
                             + "\n".join(caught)

    # ... and these must NOT trip it.
    clean = _scan_for_hand_slices("fake.py", [
        "    run_id = uuid.uuid4().hex[:6]",       # the real false positive
        "    tag = hashlib.sha256(b).hexdigest()[:6]",
        "    seats = config.crew_seat_layout()",
        "    n = len(coords)",
    ])
    assert clean == [], "scan reports a false positive:\n" + "\n".join(clean)

    # ... and the documented escape hatch still suppresses a genuine match.
    opted_out = _scan_for_hand_slices("fake.py", [
        f"    # {_SLICE_OK_MARKER}: the legacy 4+2 fallback",
        "    legacy = coords[0:4]",
    ])
    assert opted_out == [], "the opt-out marker stopped working"


def test_no_call_site_slices_the_seating_vector_by_hand():
    """
    THE recurring defect of this refactor, caught in FOUR places: production's
    spacing check, production's start filter, the final winner re-evaluation,
    and the Jacobian test's own filter.  Each hand-wrote the 4+2 layout as
    vars[0:4] / vars[4:6] / vars[6:] / v[2]-v[1], all of which silently
    misread a 14-seat vector.

    Every evaluate_canoe_weight call in main.py that passes a full seating
    vector must hand over crew_cases, or it falls back to the legacy slices.
    """
    import inspect
    import re
    import main
    src = inspect.getsource(main)
    calls = [m.start() for m in re.finditer(r"evaluate_canoe_weight\(", src)]
    assert calls, "no evaluate_canoe_weight call sites found -- test is stale"
    bad = []
    for pos in calls:
        # crude but sufficient: scan the argument text to the matching close
        depth, i = 0, src.index("(", pos)
        for j in range(i, len(src)):
            depth += (src[j] == "(") - (src[j] == ")")
            if depth == 0:
                break
        args = src[i:j]
        if "crew_cases" not in args:
            line = src[:pos].count("\n") + 1
            bad.append(line)
    assert not bad, (
        f"evaluate_canoe_weight called without crew_cases at main.py line(s) "
        f"{bad}; that falls back to the legacy 4+2 slicing and will read seat "
        f"coordinates as mix densities")


def test_spacing_check_never_compares_across_crew_boundaries():
    """
    The post-solve spacing sanity check used v[2]-v[1], which under the five-
    race layout is 2F's BOW seat minus 2M's STERN seat -- negative for any sane
    seating, so every optimizer-mode geometry was dropped before a mix was
    costed.  Gaps are only meaningful WITHIN a crew.
    """
    layout = config.crew_seat_layout()
    # a physically sensible vector: every crew spread across the hull
    v = []
    for _n, sl in layout:
        v.extend(np.linspace(3.0, 15.0, sl.stop - sl.start))
    v = np.asarray(v, dtype=float)
    within = [v[j + 1] - v[j]
              for _n, s in layout for j in range(s.start, s.stop - 1)]
    assert within and min(within) >= 3.4, "fixture seating is itself invalid"
    # the OLD check would have compared across a boundary and gone negative
    assert v[2] - v[1] < 0.0, (
        "expected a negative cross-crew 'gap' -- if this is positive the layout "
        "changed and this regression needs rewriting, not deleting")


# ── SEAT PINNING ────────────────────────────────────────────────────────────
def test_pins_reduce_the_arrangement_set():
    """The worked example: pin two of four, 24 orderings drop to 2."""
    W = [175.0, 165.0, 145.0, 135.0]
    assert len(distinct_arrangements(W, None)) == 24
    assert len(distinct_arrangements(W, [1, None, None, 4])) == 2
    assert len(distinct_arrangements(W, [1, 2, 3, 4])) == 1


def test_pinned_paddler_actually_sits_in_that_seat():
    W = [175.0, 165.0, 145.0, 135.0]
    for row in distinct_arrangements(W, [1, None, None, 4]):
        assert row[0] == 175.0      # paddler 1 -> seat 1
        assert row[3] == 135.0      # paddler 4 -> seat 4


def test_pins_are_a_subset_of_the_unpinned_set():
    """
    Pinning may only REMOVE orderings.  If it ever produced one the unpinned
    enumeration lacks, the pinned envelope would not be comparable to it.
    """
    W = [175.0, 165.0, 145.0, 135.0]
    full = set(distinct_arrangements(W, None))
    for pins in ([1, None, None, None], [None, 2, None, 4], [1, 2, 3, 4]):
        assert set(distinct_arrangements(W, pins)) <= full, pins


@pytest.mark.parametrize("pins,msg", [
    ([1, 1, None, None], "more than one paddler"),
    ([5, None, None, None], "outside 1..4"),
    ([0, None, None, None], "outside 1..4"),
    ([1, None], "seat pins for 4"),
])
def test_bad_pins_fail_loudly(pins, msg):
    """A silently-dropped pin would enumerate a set the user did not ask for."""
    with pytest.raises(ValueError, match=msg):
        distinct_arrangements([175.0, 165.0, 145.0, 135.0], pins)


def test_pinning_is_less_conservative_than_not_pinning():
    """
    THE disclosure this feature exists to make.  A pinned envelope is a max over
    FEWER load states, so it can only be lower -- the design is valid for the
    pinned subset only.
    """
    W = [175.0, 165.0, 145.0, 135.0]
    seats = [3.0, 7.5, 12.0, 15.0]
    full = _env([("4_coed", seats, W, [None] * 4)], True)
    pinned = _env([("4_coed", seats, W, [1, None, None, 4])], True)
    lowered = False
    for k in ("sag", "hog"):
        a, b = full["moment"][k], pinned["moment"][k]
        assert np.all(b <= a + 1e-9), f"{k}: pinning RAISED the envelope"
        lowered |= bool(np.any(b < a - 1e-9))
    assert lowered, "pinning covered fewer orderings but changed nothing"
    assert (pinned["arrangement_disclosure"]["4_coed"]["n_evaluated"]
            < full["arrangement_disclosure"]["4_coed"]["n_evaluated"])


def test_disclosure_reports_covered_vs_possible():
    W = [175.0, 165.0, 145.0, 135.0]
    r = _env([("4_coed", [3.0, 7.5, 12.0, 15.0], W, [1, None, None, 4])], True)
    d = r["arrangement_disclosure"]["4_coed"]
    assert d["n_evaluated"] == 2
    assert d["n_possible"] == 24          # what the crew could actually do
    assert d["fully_enumerated"] is False
    assert d["pinned_seats"] == {1: 1, 4: 4}


def test_disclosure_flags_a_fully_enumerated_run():
    W = [175.0, 165.0, 145.0, 135.0]
    r = _env([("4_coed", [3.0, 7.5, 12.0, 15.0], W, [None] * 4)], True)
    d = r["arrangement_disclosure"]["4_coed"]
    assert d["fully_enumerated"] is True
    assert d["n_evaluated"] == d["n_possible"] == 24
    assert d["pinned_seats"] == {}


def test_disclosure_is_honest_when_enumeration_is_off():
    """
    Enumeration off covers ONE ordering out of many -- that is a conservatism
    reduction too, and must not report itself as fully enumerated.
    """
    W = [175.0, 165.0, 145.0, 135.0]
    r = _env([("4_coed", [3.0, 7.5, 12.0, 15.0], W, [None] * 4)], False)
    d = r["arrangement_disclosure"]["4_coed"]
    assert d["n_evaluated"] == 1
    assert d["n_possible"] == 24
    assert d["fully_enumerated"] is False


def test_pins_flow_through_the_one_seat_layout():
    seats = {n: list(np.linspace(3.0, 15.0, config.RACE_CATEGORY_SIZES[n]))
             for n in config.RACE_CATEGORY_ORDER}
    for name, _c, w, pins in config.crew_load_cases(seats):
        assert len(pins) == len(w), name
        assert pins == list(config.RACE_CATEGORIES[name]["seat_pins"]), name
    # the unpinned comparison basis differs ONLY in the pins
    a = config.crew_load_cases(seats, with_pins=True)
    b = config.crew_load_cases(seats, with_pins=False)
    for ra, rb in zip(a, b):
        assert ra[0] == rb[0] and ra[1] == rb[1] and ra[2] == rb[2]
        assert all(p is None for p in rb[3])


def test_cross_basis_holds_for_the_pin_cost_comparison():
    """
    Requirement: pinned vs unpinned must differ ONLY in the pins -- same crew
    weights, same seats, same crew CG.  Anything else makes the reported cost
    of the pin a basis artefact.
    """
    seats = {n: list(np.linspace(3.0, 15.0, config.RACE_CATEGORY_SIZES[n]))
             for n in config.RACE_CATEGORY_ORDER}
    for ra, rb in zip(config.crew_load_cases(seats, True),
                      config.crew_load_cases(seats, False)):
        assert_same_basis({"weights": ra[2], "seats": ra[1]},
                          {"weights": rb[2], "seats": rb[1]})


# ── CROSS-BASIS DISCIPLINE ──────────────────────────────────────────────────
def test_gmt_comparison_on_mismatched_crew_cg_is_detectable():
    """
    Reproduces bug (2) as a TEST rather than an observation: two designs, same
    physics, compared on different crew-CG bases, producing a large fake gain.
    A comparison helper must therefore refuse unequal bases.
    """
    kb, bm, crew = 5.72, 8.78, 640.0
    base = transverse_gmt_design_in(kb, bm, 6.24, 161.6, crew_lbs=crew,
                                    crew_kg_in=20.02)
    same_basis = transverse_gmt_design_in(kb, bm, 6.20, 157.9, crew_lbs=crew,
                                          crew_kg_in=20.02)
    wrong_basis = transverse_gmt_design_in(kb, bm, 6.20, 157.9, crew_lbs=crew,
                                           crew_kg_in=12.00)
    assert abs(same_basis - base) < 0.10          # the real effect is tiny
    assert wrong_basis - base > 5.0               # the fake one is enormous
    with pytest.raises(ValueError, match="basis"):
        assert_same_basis({"crew_kg_in": 20.02}, {"crew_kg_in": 12.00})


def assert_same_basis(a, b, keys=("crew_kg_in", "crew_lbs", "seats",
                                  "weights", "stations")):
    """
    Refuse a comparison whose two sides were measured differently.

    Any A-vs-B number this suite reports -- a design variant, a refinement
    check, a load-case delta -- is meaningless unless both sides used the same
    crew CG, the same crew weights, the same seating and the same resolution.
    Twice now a mismatch has been reported as a physical finding, so the check
    is mechanical rather than a habit of noticing.
    """
    for k in keys:
        if k in a or k in b:
            va, vb = a.get(k), b.get(k)
            same = (np.array_equal(np.asarray(va), np.asarray(vb))
                    if isinstance(va, (list, tuple, np.ndarray))
                    or isinstance(vb, (list, tuple, np.ndarray))
                    else va == vb)
            if not same:
                raise ValueError(
                    f"cross-basis comparison: {k!r} differs between the two "
                    f"sides ({va!r} vs {vb!r}); equalise the basis or do not "
                    f"report the delta")
    return True


def test_assert_same_basis_accepts_matching_bases():
    assert assert_same_basis(
        {"crew_kg_in": 20.02, "crew_lbs": 620.0, "stations": 161},
        {"crew_kg_in": 20.02, "crew_lbs": 620.0, "stations": 161})


@pytest.mark.parametrize("key,va,vb", [
    ("crew_kg_in", 20.02, 12.0),
    ("crew_lbs", 620.0, 640.0),
    ("stations", 161, 322),
    ("weights", [175.0, 165.0], [145.0, 135.0]),
])
def test_assert_same_basis_catches_each_mismatch(key, va, vb):
    with pytest.raises(ValueError, match="cross-basis"):
        assert_same_basis({key: va}, {key: vb})


def test_the_trade_and_its_base_share_a_crew_cg():
    """
    Regression for bug (2) at the call site: main's top-flange trade must be
    handed the SAME crew CG the stability suite used, not the config default.
    """
    import inspect
    import main
    src = inspect.getsource(main.run_grand_optimization)
    assert "crew_kg_in=_stability[\"kg_crew_in\"]" in src, (
        "the top-flange trade must inherit the stability suite's crew CG; "
        "letting it fall back to config.CREW_VERTICAL_CG_IN reports a basis "
        "change as a flange effect")


def test_the_freeboard_and_stability_share_a_crew():
    """
    Both must use the COMPUTED heaviest crew.  If the ceiling used a computed
    max while stability used 4_coed, the hull would be gated against one crew
    and reported against another.
    """
    import inspect
    import main
    src = inspect.getsource(main.run_grand_optimization)
    assert "heaviest_crew_lbs()" in src
    assert "heaviest_crew_category()" in src
    assert "sum(config.RACE_4_PERSON)" not in src, (
        "the 4-person crew is no longer assumed heaviest")
