# tests/test_sweep_diagnostics.py
"""
Per-gate sweep diagnostics (structural.SweepTally).

A sweep that finds nothing used to print one generic guess — "the required
flare angle exceeded limits, or the concrete was too weak" — which fits a
marginal near-miss and a hopeless search space equally badly.  These tests pin
the replacement: a rejection histogram, the ranges actually searched, and the
closest-to-feasible configuration's D/C on every gate.

The distinction that matters, and that these tests protect:
    governing D/C 1.03  → one more step of thickness/density closes it
    governing D/C 6.45  → nothing in this space is close
"""
from __future__ import annotations

import numpy as np
import pytest

from geometry import calculate_shell_properties
from structural import SweepTally, evaluate_multizone_weight


def _hull(n_st=41, n_wl=17, L_ft=20.0, depth_in=13.0, half_beam_in=13.0,
          t_in=0.5):
    x = np.linspace(0.0, L_ft, n_st)
    z = np.linspace(0.0, depth_in, n_wl)
    w = half_beam_in * np.sin(np.pi * x / L_ft)
    y = np.outer(w, np.sqrt(z / depth_in))
    p = calculate_shell_properties(x, z, y, t_in)
    v = p.A_in2 > 0.001
    return dict(
        x=x[v], dx=L_ft / (n_st - 1), width_ft=p.width_ft[v], t_in=t_in,
        ca=p.cum_area_in2[v], cAz=p.cum_Az_in3[v], cIz=p.cum_Iz_in4[v],
        zg=p.z_grid_in, sheer=p.c_bot[v] + p.c_top[v], na=p.c_bot[v],
        cIy=p.cum_Iy_in4[v], yh=p.yhalf_in[v])


def _props(strong=True):
    """Mix property stub — ``strong`` decides whether anything can survive."""
    k = 1.0 if strong else 0.02
    return lambda d, fos=1.5: {
        "density": float(d),
        "strength_t": max(10.0, 18.0 * (float(d) - 55.0) + 15.0) * k,
        "strength_c": max(80.0, 90.0 * (float(d) - 55.0) + 120.0) * k,
    }


# ══════════════════════════════════════════════════════════════════════════
#  The tally itself
# ══════════════════════════════════════════════════════════════════════════

class TestSweepTally:
    def test_counts_are_per_gate_and_sum_to_costed(self):
        t = SweepTally()
        for _ in range(5):
            t.record("kupfer_tensile", {"Kupfer biaxial": 2.0})
        for _ in range(3):
            t.record("freeboard_flare", {"freeboard wall": 1.4}, partial=True)
        t.record(None, {"Kupfer biaxial": 0.7, "freeboard wall": 0.5})
        assert t.rejects == {"kupfer_tensile": 5, "freeboard_flare": 3}
        assert t.n_feasible == 1
        assert t.n_costed == sum(t.rejects.values()) + t.n_feasible

    def test_best_is_the_lowest_governing_dc(self):
        t = SweepTally()
        t.record("kupfer_tensile", {"Kupfer biaxial": 6.45}, t_in=0.375)
        t.record("kupfer_tensile", {"Kupfer biaxial": 1.03}, t_in=0.500)
        t.record("kupfer_tensile", {"Kupfer biaxial": 3.10}, t_in=0.625)
        assert t.best["gov_dc"] == pytest.approx(1.03)
        assert t.best["t_in"] == pytest.approx(0.500)
        assert t.best["gov_gate"] == "Kupfer biaxial"

    def test_governing_gate_ignores_reported_components(self):
        """The standalone longitudinal/hoop numbers are reported, not gates —
        they must never be named as what governs."""
        t = SweepTally()
        t.record("kupfer_tensile", {"Kupfer biaxial": 3.28},
                 {"longitudinal (bending only)": 6.45, "transverse hoop": 0.71})
        assert t.best["gov_gate"] == "Kupfer biaxial"
        assert t.best["gov_dc"] == pytest.approx(3.28)

    def test_complete_record_beats_a_lower_partial_one(self):
        """A configuration an early gate cut short has incomplete D/Cs, so it
        cannot be compared like for like — it only surfaces when nothing in the
        space cleared every gate."""
        t = SweepTally()
        t.record("freeboard_flare", {"freeboard wall": 0.9}, partial=True)
        t.record("kupfer_tensile", {"Kupfer biaxial": 4.0})
        assert t.best["gov_dc"] == pytest.approx(4.0)
        assert t.best_partial["gov_dc"] == pytest.approx(0.9)
        assert "Kupfer biaxial" in t.failure_report()

    def test_partial_reported_when_nothing_cleared_every_gate(self):
        t = SweepTally()
        t.record("freeboard_flare", {"freeboard wall": 2.9}, partial=True)
        rep = t.failure_report()
        assert "2.9" in rep and "later gates never ran" in rep

    def test_stage2_moves_a_config_out_of_passed_stress(self):
        t = SweepTally()
        t.record(None, {"Kupfer biaxial": 0.8}, stage2_pending=True)
        assert (t.n_passed_stress, t.n_feasible, t.n_costed) == (1, 0, 1)
        t.record_stage2("freeboard_flare", {"freeboard wall": 1.7}, partial=True)
        assert (t.n_passed_stress, t.n_feasible) == (0, 0)
        assert t.n_costed == 1                      # resolved, not double-counted
        assert t.rejects["freeboard_flare"] == 1

    def test_skips_are_counted_apart_from_costed_configs(self):
        t = SweepTally()
        t.skip("N_mixes_max", 40)
        t.record("kupfer_tensile", {"Kupfer biaxial": 2.0})
        assert t.n_costed == 1 and t.skipped["N_mixes_max"] == 40
        assert "skipped before costing" in t.failure_report()


class TestFailureReport:
    def _tally(self, gov_dc):
        t = SweepTally({"mix density": "57.5 – 80.0 pcf, step 0.5 (46 densities)",
                        "wall thickness": "0.3750 – 0.6250 in (17 options)",
                        "mold depth": "12.78 – 17.28 in, step 0.5 (9 depths)"})
        t.record("kupfer_tensile", {"Kupfer biaxial": gov_dc,
                                    "freeboard wall": 0.4},
                 {"longitudinal (bending only)": 6.45, "transverse hoop": 0.71},
                 t_in=0.5, depth_in=12.78, mixes=[61.5, 59.5, 61.5],
                 fracs=[0.266, 0.399], weight=306.5)
        return t

    def test_reports_the_searched_ranges(self):
        """A too-narrow range is invisible in a failure that does not print it —
        exactly how a whole debugging session gets spent."""
        rep = self._tally(3.28).failure_report(total_configs=7038)
        assert "57.5" in rep and "80.0" in rep          # density range
        assert "0.3750" in rep and "0.6250" in rep      # thickness range
        assert "12.78" in rep                           # depth range
        assert "7,038" in rep

    def test_reports_the_histogram_and_every_gate_dc(self):
        rep = self._tally(3.28).failure_report()
        assert "kupfer_tensile" in rep and "GOVERNS" in rep
        assert "3.281" in rep or "3.280" in rep
        assert "6.45" in rep and "0.71" in rep          # reported components
        for gate in ("punching", "compression", "buckling"):
            assert gate in rep                          # named as NON-gates

    def test_marginal_and_gross_read_differently(self):
        assert "MARGINAL" in self._tally(1.03).failure_report()
        assert "GROSS" in self._tally(6.45).failure_report()

    def test_no_crash_without_any_record(self):
        assert "0 of" in SweepTally().failure_report()


# ══════════════════════════════════════════════════════════════════════════
#  Evaluator wiring — the tally must see what the gate saw
# ══════════════════════════════════════════════════════════════════════════

class TestEvaluatorWiring:
    def _ev(self, h, stack, tally, strong=True):
        return evaluate_multizone_weight(
            [3., 7.5, 12., 15.], [4., 14.], stack, (0.34, 0.67),
            h["x"], h["dx"], h["width_ft"], h["t_in"],
            h["ca"], h["cAz"], h["cIz"], h["zg"], h["sheer"], h["na"],
            _props(strong), coords_3=[6., 10., 14.],
            cum_Iy=h["cIy"], yhalf_grid=h["yh"], tally=tally, depth_in=13.0)

    def test_feasible_config_is_recorded_as_feasible(self):
        h, t = _hull(), SweepTally()
        w = self._ev(h, [80, 60, 80], t)
        import structural
        assert not structural.is_rejected(w) and w > 0
        assert t.n_feasible == 1 and not t.rejects
        assert t.best["mixes"] == [80.0, 60.0, 80.0]
        assert t.best["gov_dc"] < 1.0

    def test_rejected_config_records_the_gate_that_rejected_it(self):
        h, t = _hull(), SweepTally()
        import structural
        assert structural.is_rejected(
            self._ev(h, [80, 60, 80], t, strong=False))
        assert t.n_feasible == 0
        assert sum(t.rejects.values()) == 1
        assert t.best is not None and t.best["gov_dc"] > 1.0

    def test_n_mixes_max_rejection_is_tallied(self):
        h, t = _hull(), SweepTally()
        w = evaluate_multizone_weight(
            [3., 7.5, 12., 15.], [4., 14.], [80, 70, 60], (0.34, 0.67),
            h["x"], h["dx"], h["width_ft"], h["t_in"],
            h["ca"], h["cAz"], h["cIz"], h["zg"], h["sheer"], h["na"],
            _props(), coords_3=[6., 10., 14.], n_mixes_max=2, tally=t)
        import structural
        assert structural.is_rejected(w) and t.rejects["n_mixes_max"] == 1

    def test_tally_none_leaves_the_result_identical(self):
        """The collector is opt-in: passing one must not move a single number."""
        h = _hull()
        assert self._ev(h, [80, 60, 80], None) == self._ev(
            h, [80, 60, 80], SweepTally())


# ── An empty grid must not read as an infeasible design ──────────────────────

def test_inverted_depth_range_raises_instead_of_reporting_infeasible(tmp_path,
                                                                     monkeypatch):
    """
    depth_options = np.arange(floor, floor + span, step) returns [] for a
    non-positive span, so an inverted range yields ZERO tasks.  Zero tasks
    means no worker raises, so the all-tasks-failed guard cannot see it, and
    the sweep used to report "no feasible design" -- a search that never
    happened, presented as a design verdict.
    """
    import numpy as np
    import pytest
    from main import run_grand_optimization

    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="EMPTY"):
        run_grand_optimization(
            freeboard_min_in=6.0, thickness_options_in=[0.5],
            precision_level=1, flare_max_deg=20.0,
            multizone=False, num_zones=3, reports_dir=str(tmp_path),
            depth_step_in=0.5, depth_span_in=-1.5, density_step_pcf=4.0)


def test_the_empty_grid_error_names_the_offending_range(tmp_path, monkeypatch):
    """The message must say WHICH range was empty, or it is just another
    unhelpful catch-all."""
    import pytest
    from main import run_grand_optimization

    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError) as exc:
        run_grand_optimization(
            freeboard_min_in=6.0, thickness_options_in=[0.5],
            precision_level=1, flare_max_deg=20.0,
            multizone=False, num_zones=3, reports_dir=str(tmp_path),
            depth_step_in=0.5, depth_span_in=-1.5, density_step_pcf=4.0)
    msg = str(exc.value)
    assert "depth search range is EMPTY" in msg
    assert "NOT an infeasible design" in msg
    assert "freeboard-required minimum" in msg
    assert "span=" in msg and "step=" in msg


# ── Depth bounds: span -> absolute maximum (pure re-parameterisation) ────────

def _depth_grid(**kw):
    """The depth array a sweep would search, without running one."""
    import numpy as np
    import main
    floor = kw["floor"]
    step = kw["step"]
    if "max" in kw:
        span = kw["max"] - floor
    else:
        span = kw["span"]
    return np.arange(floor, floor + span, step)


def test_absolute_max_reproduces_the_old_span_grid_exactly():
    """
    THE acceptance test for the re-parameterisation.  Old floor 12.57 / span
    4.5 must search precisely the depths new min 12.57 / max 17.07 does -- if
    the arrays differ at all, the change moved the search space rather than
    relabelling it.
    """
    import numpy as np
    old = _depth_grid(floor=12.57, span=4.5, step=0.5)
    new = _depth_grid(floor=12.57, max=17.07, step=0.5)
    assert len(old) == len(new)
    assert np.array_equal(old, new), (
        f"re-parameterisation changed the searched depths:\n"
        f"  old (floor+span): {old}\n  new (floor+max):  {new}")


@pytest.mark.parametrize("step", [0.1, 0.25, 0.5, 1.5, 2.0])
def test_equivalence_holds_across_steps(step):
    import numpy as np
    old = _depth_grid(floor=12.57, span=4.5, step=step)
    new = _depth_grid(floor=12.57, max=12.57 + 4.5, step=step)
    assert np.array_equal(old, new)


def test_max_below_the_freeboard_floor_is_a_named_error(tmp_path, monkeypatch):
    """
    MIN_MOLD_DEPTH is computed from the freeboard rule and enforced regardless
    of input, so `min < max` can look valid while being unsatisfiable.  The
    error must name BOTH numbers, or the user cannot tell what to change.
    """
    from main import run_grand_optimization

    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="below the"):
        run_grand_optimization(
            freeboard_min_in=6.0, thickness_options_in=[0.5],
            precision_level=1, flare_max_deg=20.0,
            multizone=False, num_zones=3, reports_dir=str(tmp_path),
            depth_step_in=0.5, depth_max_in=10.0, density_step_pcf=4.0)


def test_both_depth_forms_together_are_refused(tmp_path, monkeypatch):
    """Silently preferring one would make the other a no-op.

    There are three forms now -- a freeboard ceiling, an absolute mold depth,
    and the deprecated span -- so the rule is exactly one, not "not both".
    """
    from main import run_grand_optimization

    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="exactly ONE depth bound"):
        run_grand_optimization(
            freeboard_min_in=6.0, thickness_options_in=[0.5],
            precision_level=1, flare_max_deg=20.0,
            multizone=False, num_zones=3, reports_dir=str(tmp_path),
            depth_step_in=0.5, depth_span_in=4.5, depth_max_in=17.07,
            density_step_pcf=4.0)


# ── The 4-person floor NARROWS an absolute-maximum search ────────────────────
#
# Under the old span semantics the window rose with the floor and the search
# stayed 4.5 in tall, so a raised floor was worth one line of output.  With an
# absolute maximum the floor rises INTO a fixed ceiling and the range shrinks.
# That is what "maximum" has to mean, but a user on a hull where the 4-person
# check binds would otherwise watch the depth count drop with nothing said --
# and fewer depths than you asked for reads as a bug, not as a rule.

def test_the_narrowing_note_names_both_ranges_and_the_floor_that_moved():
    from main import describe_depth_narrowing

    note = describe_depth_narrowing(floor_entered=12.57, floor_raised_to=14.0,
                                    depth_max=17.07, depth_step=0.5,
                                    n_searched=7)
    assert note is not None
    # What was asked for, what moved, and what is actually searched.
    assert "12.57" in note and "17.07" in note, "the entered range is missing"
    assert "14.00" in note, "the raised floor is missing"
    assert "9 depths" in note, "the entered depth count is missing"
    assert "7 depths" in note, "the narrowed depth count is missing"
    assert "NARROWED" in note
    # And what to do about it.
    assert "Raise the maximum" in note


def test_no_note_when_the_four_person_check_left_the_floor_alone():
    """The common case, and the current hull.  Silence is correct here."""
    from main import describe_depth_narrowing

    assert describe_depth_narrowing(12.57, None, 17.07, 0.5, 10) is None
    # Defensive: a "raise" that is not above the floor did not narrow anything.
    assert describe_depth_narrowing(12.57, 12.57, 17.07, 0.5, 10) is None
    assert describe_depth_narrowing(12.57, 12.0, 17.07, 0.5, 10) is None


def test_the_span_path_has_nothing_to_narrow():
    """
    A span re-measures from wherever the floor ends up, so the search keeps its
    height and slides.  Reporting a narrowing there would be false.
    """
    from main import describe_depth_narrowing

    assert describe_depth_narrowing(12.57, 14.0, None, 0.5, 9) is None


def test_a_raised_floor_is_not_blamed_on_the_freeboard_rule(tmp_path, monkeypatch):
    """
    When the 4-person check owns the floor, the empty-range error must say so.
    "draft + freeboard" sends the user to lower a number that is not the
    binding one -- the freeboard rule is satisfied; the crew is what does not
    fit.
    """
    import main
    from main import run_grand_optimization

    monkeypatch.chdir(tmp_path)
    # Force the raise: 4P needs far more depth than draft + freeboard gives.
    monkeypatch.setattr(main, "calculate_4_person_waterline",
                        lambda **kw: (28.0, 34.0))
    with pytest.raises(ValueError) as exc:
        run_grand_optimization(
            freeboard_min_in=6.0, thickness_options_in=[0.5],
            precision_level=1, flare_max_deg=20.0,
            multizone=False, num_zones=3, reports_dir=str(tmp_path),
            depth_step_in=0.5, depth_max_in=30.0, density_step_pcf=4.0)
    msg = str(exc.value)
    assert "34.0" in msg, "the error does not name the floor that actually bound"
    assert "4-person" in msg, (
        "a 4P-raised floor is still reported as 'draft + freeboard'; the user "
        "would go lower the freeboard requirement, which is not the constraint")


# ── Freeboard as the bound the user actually sets ────────────────────────────
#
# freeboard_min_in always meant "how much hull stands above the water".  The
# maximum was an absolute MOLD DEPTH, so the two columns of one row held
# different quantities with the draft silently between them.  They are the
# same quantity now, and the conversion to mold depth happens HERE, against
# the draft this run parsed -- not in the UI, against one it scraped itself.

def test_a_freeboard_band_is_measured_from_the_floor_not_from_the_draft():
    """
    THE bug this row shipped, pinned.

    freeboard_min_in sets MIN_MOLD_DEPTH, which the 4-person check may lift to
    the heaviest crew's waterline -- 6.57 in on the hull below, not the
    2-person design draft of 4.58 in.  Converting the MAXIMUM through
    true_draft_in put the two ends of one row on two different datums.

    It had a loud face and a silent one.  Loud: "min 6.0 / max 6.5" resolved
    to a ceiling of 11.08 in, below the 12.57 in floor, and the sweep refused
    to run at all.  Silent, and worse: the shipped default "min 6.0 / max
    10.5" resolved to 15.08 in, ABOVE the floor, so it ran and quietly
    searched 6 depths where the design searches 9.
    """
    import numpy as np

    draft_2p, wl_4p, fb_min, step = 4.58, 6.57, 6.0, 0.5
    floor = wl_4p + fb_min                     # MIN_MOLD_DEPTH, 4P-governed

    for fb_max, expect in ((6.5, 1), (8.0, 4), (10.5, 9)):
        ceiling = floor + (fb_max - fb_min)    # anchored to the floor
        assert len(np.arange(floor, ceiling, step)) == expect

        by_draft = draft_2p + fb_max           # the shipped bug
        assert len(np.arange(floor, by_draft, step)) < expect, (
            f"fb_max {fb_max}: converting through the 2-person draft no "
            f"longer truncates the search, so this test has stopped pinning "
            f"the bug it exists for")


def test_the_band_width_survives_a_raised_floor():
    """
    Both ends move together, so a raised floor SLIDES the band rather than
    narrowing it.  That is the difference between a freeboard ceiling and the
    fixed mold-depth ceiling it replaced, and it is why the narrowing note
    must not fire on this path.
    """
    import numpy as np

    fb_min, fb_max, step = 6.0, 10.5, 0.5
    counts = set()
    for floor in (10.58, 12.57, 14.0, 20.0):   # every plausible 4P outcome
        ceiling = floor + (fb_max - fb_min)
        counts.add(len(np.arange(floor, ceiling, step)))
    assert counts == {9}, (
        f"the searched depth count moves with the floor ({sorted(counts)}); "
        f"the band is not anchored to it")


def test_the_freeboard_error_answers_in_freeboard(tmp_path, monkeypatch):
    """
    A user who typed a freeboard ceiling and is told to "raise the maximum
    depth above 12.57 in" has to convert by hand, against a draft nothing on
    screen showed them.  Answer in the units they asked in.
    """
    from main import run_grand_optimization

    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError) as exc:
        run_grand_optimization(
            freeboard_min_in=6.0, thickness_options_in=[0.5],
            precision_level=1, flare_max_deg=20.0,
            multizone=False, num_zones=3, reports_dir=str(tmp_path),
            depth_step_in=0.5, freeboard_max_in=1.0, density_step_pcf=4.0)
    msg = str(exc.value)
    assert "freeboard" in msg.lower()
    assert "1.00" in msg, "the maximum the user actually typed is not named"
    assert "6.00" in msg, "the minimum it is being compared against is not named"
    assert "maximum depth" not in msg, (
        "the error still answers in mold depth, which the user never entered")
    # No draft, no derived floor -- naming a 7.99 in freeboard the user never
    # typed is what made the first version of this message unreadable.
    assert "draft" not in msg.lower(), (
        "the error reintroduces a draft the user neither entered nor saw")


# ── The team's density floor is a real bound now ─────────────────────────────
#
# config.MIN_DENSITY_PCF existed all along, documented as "the team's stated
# floor, NOT the sweep's -- nothing gates on it today": a setting the user
# could change that changed nothing.

def test_the_team_floor_raises_the_grid_but_never_lowers_it():
    import config

    db = config.get_mix_db()
    viable = float(db.tensile_viability_floor()[0])

    def _floor_of(min_density):
        grid = db.get_structural_sweep_grid(
            fos=1.5, max_density=80.0, n_mixes=2, step=0.5,
            min_density=min_density)
        return min(d for combo in grid for d in combo)

    above = viable + 8.0
    assert _floor_of(above) >= above - 1e-6, (
        "the team's floor did not bind; it is documentation again")
    # Below the viable floor it must change NOTHING -- those densities are
    # configs that get built, evaluated and rejected.
    assert _floor_of(viable - 10.0) == pytest.approx(_floor_of(None)), (
        "a floor below the regression's admitted densities it predicts no "
        "strength for")


def test_the_team_floor_filters_the_grid_and_never_re_phases_it():
    """
    The anchor note in get_structural_sweep_grid, applied to the new bound:
    np.arange(floor, ...) would re-phase every point, so a floor moved by
    1.8 pcf once turned 57.5/59.5/61.5 into 59.3/61.3/63.3 and a winning
    stack stopped EXISTING.  Surviving densities must keep their values.
    """
    import config

    db = config.get_mix_db()
    unbounded = {d for combo in db.get_structural_sweep_grid(
        fos=1.5, max_density=80.0, n_mixes=2, step=0.5) for d in combo}
    bounded = {d for combo in db.get_structural_sweep_grid(
        fos=1.5, max_density=80.0, n_mixes=2, step=0.5,
        min_density=65.3) for d in combo}
    assert bounded <= unbounded, (
        f"the floor re-phased the grid; {sorted(bounded - unbounded)} are new "
        f"densities that the unbounded search never contained")


def test_an_inverted_density_range_is_a_named_error(tmp_path, monkeypatch):
    """Zero mix combinations must never reach the user as 'no feasible
    design' -- the same rule the depth range already follows."""
    import config
    from main import run_grand_optimization

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "MIN_DENSITY_PCF", 95.0, raising=False)
    monkeypatch.setattr(config, "MAX_DENSITY_PCF", 80.0, raising=False)
    with pytest.raises(ValueError) as exc:
        run_grand_optimization(
            freeboard_min_in=6.0, thickness_options_in=[0.5],
            precision_level=1, flare_max_deg=20.0,
            multizone=False, num_zones=3, reports_dir=str(tmp_path),
            depth_step_in=0.5, freeboard_max_in=10.5, density_step_pcf=4.0)
    msg = str(exc.value)
    assert "EMPTY" in msg and "95.0" in msg and "80.0" in msg
    assert "NOT an infeasible design" in msg
