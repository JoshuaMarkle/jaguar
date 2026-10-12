# tests/test_paddler_jacobian.py
"""
Analytic constraint Jacobians for the paddler-placement SLSQP problem.

The spacing and trim constraints are affine in the 9 seat positions, so their
Jacobians are exact constant vectors.  Passing them via each constraint's
``jac`` removes SLSQP's finite-difference sensitivity on the CONSTRAINTS (the
eps=0.5 bug that stalled placement at the 3.5 ft spacing margin and discarded
whole thicknesses).

Two groups:
1. Jacobian correctness — every constraint's analytic ``jac`` matches a
   finite-difference gradient of its ``fun`` at random feasible points.
2. Placement convergence — the previously-stalling t=0.375" config now solves
   with the spacing rule satisfied.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from scipy.optimize import check_grad, minimize

from main import (SEATING_SLSQP_OPTS, paddler_placement_constraints,
                  seating_start_points)

_ROOT = Path(__file__).resolve().parent.parent
_RNG = np.random.default_rng(0xC0FFEE)

# Representative race weights (the config defaults); the trim gradients depend
# on them, so the test pins them rather than reading the live config.
_R4 = [160, 150, 170, 160]
_R2 = [170, 160]
_R3 = [160, 160, 160]
_L = 20.0131

# The seating vector spans all FIVE ASCE races.  Everything below is DERIVED
# from config.crew_seat_layout() rather than hardcoded to a 4+2+3 shape, so a
# future layout change fails these tests loudly instead of leaving them
# asserting a geometry the solver no longer uses.
import config as _cfg

_LAYOUT = _cfg.crew_seat_layout()
_NSEAT = _cfg.crew_seat_vector_len()


def _rand_point(L=_L):
    return _RNG.uniform(2.0, L - 3.5, size=_NSEAT)


def _within_crew_gaps(v):
    """
    Adjacent-seat gaps WITHIN each crew.  Never across a crew boundary: seats
    of different races are independent, and a cross-boundary difference is
    negative for any sensible seating.
    """
    return [v[j + 1] - v[j]
            for _n, sl in _LAYOUT for j in range(sl.start, sl.stop - 1)]


def _expected_constraints():
    """(count, [(kind, category, indices touched), ...]) in emission order."""
    out = []
    for name, sl in _LAYOUT:
        idx = list(range(sl.start, sl.stop))
        for j in range(sl.start, sl.stop - 1):
            out.append(("gap", name, [j, j + 1]))
        out.append(("trim_upper", name, idx))
        out.append(("trim_lower", name, idx))
    return out


class TestConstraintJacobians:
    """Every analytic Jacobian must match a finite-difference gradient."""

    def test_all_jacobians_match_finite_difference(self):
        cons = paddler_placement_constraints(_L)
        assert len(cons) == len(_expected_constraints())
        worst = 0.0
        for ci, c in enumerate(cons):
            for _ in range(8):
                err = check_grad(c["fun"], c["jac"], _rand_point())
                worst = max(worst, err)
                assert err < 1e-6, f"cons[{ci}] jac error {err:.2e}"
        # Affine constraints -> FD of a linear function is exact to roundoff.
        assert worst < 1e-6

    def test_jacobians_are_constant(self):
        """Affine constraints -> the Jacobian does not depend on the point."""
        cons = paddler_placement_constraints(_L)
        for c in cons:
            j0 = np.asarray(c["jac"](_rand_point()), float)
            j1 = np.asarray(c["jac"](_rand_point()), float)
            np.testing.assert_allclose(j0, j1, atol=0.0)

    def test_spacing_jacobian_values(self):
        """First constraint is v[1]-v[0]-3.5 -> grad = -1 at 0, +1 at 1."""
        cons = paddler_placement_constraints(_L)
        g = np.asarray(cons[0]["jac"](_rand_point()), float)
        expect = np.zeros(_NSEAT); expect[0] = -1.0; expect[1] = 1.0
        np.testing.assert_allclose(g, expect)
        # and the fun value is the gap minus 3.5
        v = _rand_point()
        assert cons[0]["fun"](v) == pytest.approx(v[1] - v[0] - 3.5)

    def test_trim_jacobian_equals_weight_over_total(self):
        """
        Every crew's trim gradient is its OWN wk/sum(w) on its OWN slice and
        zero elsewhere.  Checked for all five races: sharing one weight list
        across same-size crews would show up here as 2F carrying 2M's gradient.
        """
        cons = paddler_placement_constraints(_L)
        spec = _expected_constraints()
        for ci, (kind, name, idx) in enumerate(spec):
            if not kind.startswith("trim"):
                continue
            _s, _h, w = _cfg.race_category_crew(name)
            W = float(sum(w))
            want = np.zeros(_NSEAT)
            want[idx] = np.asarray(w, float) / W
            if kind == "trim_upper":
                want = -want
            np.testing.assert_allclose(
                np.asarray(cons[ci]["jac"](_rand_point()), float), want,
                atol=1e-12, err_msg=f"{name} {kind}")

    def test_constraint_count_and_nonzero_structure(self):
        """
        Spacing constraints touch exactly 2 adjacent seats, trim constraints
        touch exactly one whole crew -- and NEVER span two crews, which would
        couple races that have nothing to do with each other.
        """
        cons = paddler_placement_constraints(_L)
        spec = _expected_constraints()
        assert len(cons) == len(spec)
        nz = [sorted(int(i) for i in np.nonzero(
            np.asarray(c["jac"](_rand_point()), float))[0]) for c in cons]
        for ci, (kind, name, idx) in enumerate(spec):
            assert nz[ci] == sorted(idx), f"{name} {kind} at {ci}"
        # no constraint may straddle a crew boundary
        bounds = {sl.start for _n, sl in _LAYOUT}
        for ci, touched in enumerate(nz):
            owner = [n for n, sl in _LAYOUT
                     if sl.start <= min(touched) and max(touched) < sl.stop]
            assert len(owner) == 1, f"constraint {ci} spans crews: {touched}"


def _placement_geometry(t_in, d_in):
    """Build the sweep's placement objective for a (t, d) on the reference
    hull, or skip if the committed off1.mlt/out.mlt are unavailable."""
    off1, out = _ROOT / "off1.mlt", _ROOT / "out.mlt"
    if not (off1.exists() and out.exists()):
        pytest.skip("reference hull off1.mlt/out.mlt not present")
    from geometry import (parse_godzilla_dimensions, parse_godzilla_offsets,
                          calculate_shell_properties)
    from structural import evaluate_canoe_weight, as_optimizer_penalty

    L_ft, draft_in = parse_godzilla_dimensions(str(out))
    x_raw, z_raw, y = parse_godzilla_offsets(
        str(off1), L_ft=L_ft, draft_in=draft_in, mold_depth_in=d_in)
    p = calculate_shell_properties(x_raw, z_raw, y, t_in)
    A, At, Ab, I, Q, ct, cb, wf = p
    v = A > 0
    x = x_raw[v]; L = float(np.max(x)); dx = L / (len(x_raw) - 1)
    mix = [80.0, 60.0]

    def obj(pv):
        # The seating vector spans all five races; the mix densities are the
        # TAIL, after every seat.  Slicing at a fixed 6 here is the same bug
        # that made evaluate_canoe_weight read seat coordinates as densities.
        #
        # as_optimizer_penalty is THE sanctioned conversion of the REJECTED
        # sentinel into a number, and it exists precisely for this boundary:
        # SLSQP needs a float, and an infeasible probe has no weight.  Handing
        # the raw sentinel to minimize() is what production stopped doing --
        # this objective must be the one production actually solves, or the
        # test asserts a contract nobody ships.
        full = np.concatenate((pv, mix))
        return as_optimizer_penalty(evaluate_canoe_weight(
            full, x, dx, A[v] / 144, I[v], Q[v], ct[v], cb[v], wf[v],
            At[v] / 144, Ab[v] / 144, t_in,
            crew_cases=_cfg.crew_load_cases(
                {n: list(pv[sl]) for n, sl in _LAYOUT}),
            # Mirrors the production seating objective, which does NOT envelope
            # arrangements: it runs once per SLSQP probe and only has to rank
            # seat positions.
            enumerate_arrangements=False,
            position_variance_ft=0.0,
            cum_area_in2=p.cum_area_in2[v], z_grid_in=p.z_grid_in))

    return obj, L, draft_in


class TestPlacementConvergence:
    """
    The placement SOLVE must produce a converged, improved, spacing-legal
    seating at the configs that stalled under the old FD-constraint setup.

    These assert the BEST-OF-N contract, not single-start convergence, because
    best-of-N is what the sweep actually relies on (main.seating_start_points +
    SEATING_N_STARTS).  Individual starts are allowed to fail -- SLSQP returns
    status 4 "inequality constraints incompatible" from some starts on this
    geometry -- and the multi-start exists precisely so one bad start cannot
    decide the design.  Asserting a single hardcoded x0 converges would be
    testing a contract production abandoned when the landscape was shown to be
    multimodal.
    """

    @staticmethod
    def _best_of_n(obj, L):
        cons = paddler_placement_constraints(L)
        bnds = tuple((2.0, L - 3.5) for _ in range(_NSEAT))
        best = None
        for x0 in seating_start_points(L):
            r = minimize(obj, x0, method="SLSQP", bounds=bnds,
                         constraints=cons, options=dict(SEATING_SLSQP_OPTS))
            if r.status != 0 or not np.isfinite(r.fun):
                continue
            # Gaps WITHIN each crew only.  v[2]-v[1] under the five-race layout
            # is 2F's bow seat minus 2M's stern seat -- negative for any sane
            # seating, so the old form rejected every start and reported "no
            # seating start converged legally".
            if min(_within_crew_gaps(r.x)) < 3.4:
                continue
            if best is None or r.fun < best[0].fun:
                best = (r, x0)
        return best

    def test_problematic_thickness_0_375_converges(self):
        # t=0.375" is exactly the thickness the eps=0.5 bug discarded.
        obj, L, draft_in = _placement_geometry(0.375, 13.578)
        best = self._best_of_n(obj, L)
        assert best is not None, "no seating start converged legally"
        r, x0 = best
        gaps = _within_crew_gaps(r.x)
        assert min(gaps) >= 3.4, f"spacing violated: gaps={[round(g,3) for g in gaps]}"
        # status==0 alone is NOT enough: a zero gradient also reports success.
        # Require the solve to have actually improved on its own start.
        assert r.fun < obj(np.asarray(x0)) - 1e-9, (
            "SLSQP 'converged' without improving the objective -- the flat-"
            "landscape false-convergence regression"
        )

    @pytest.mark.parametrize("d_in", [13.578, 15.578, 17.578])
    def test_converges_across_depths_at_thin_wall(self, d_in):
        obj, L, _ = _placement_geometry(0.375, d_in)
        best = self._best_of_n(obj, L)
        assert best is not None, f"no seating start converged legally at d={d_in}"
        assert min(_within_crew_gaps(best[0].x)) >= 3.4
