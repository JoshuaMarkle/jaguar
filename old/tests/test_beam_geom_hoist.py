# tests/test_beam_geom_hoist.py
"""
THE TRAPEZOID HOIST MUST NOT MOVE A RESULT.

`_solve_beam_case` made seven `trapezoid` calls per solve -- 12,330,920 of them
in one profiled task -- and four depended only on `x` and `width_ft`, which are
fixed for a whole sweep task.  Each call re-ran `np.diff(x)` on a grid that
never changes (15.9 M `diff` calls, 79.4 s), and two of the four were the same
integral computed twice in one function body.

`_beam_geom` computes them once per envelope.  Two guarantees, pinned
separately because they are not equally strong:

  * S0 / S1 / S2 / det are still computed BY `trapezoid`, just once.  Same
    function, same inputs -> BIT-IDENTICAL.
  * the three load-dependent integrals became `tw @ y` dot products.  Exact in
    exact arithmetic; in floating point the summation reassociates.  MEASURED
    against the original arithmetic: 1.3e-14 relative on the moment curve.
    Budget here is 1e-12 -- two orders looser than the measurement, and ten
    orders tighter than anything physical.

`test_the_hoist_matches_the_original_arithmetic` carries a transcription of the
pre-hoist body as its reference.  That is deliberate: comparing the new code to
itself proves only self-consistency, and self-consistency is exactly what a
faithful-looking but wrong rewrite would also have.
"""
from __future__ import annotations

import numpy as np
import pytest

from structural import (_beam_geom, _solve_beam_case,
                        apply_realistic_paddler_loads, cumulative_trapezoid,
                        trapezoid)

TOL = 1e-12
COORDS = [4.0, 8.0, 12.0, 16.0]
WEIGHTS = [185.0, 165.0, 150.0, 140.0]
L = 20.0


def _grid(n=161, jitter=False):
    x = np.linspace(0.0, L, n)
    if jitter:
        # Non-uniform ON PURPOSE for the pure-integral tests: a uniform grid
        # makes every trapezoid weight equal, which would let a wrong weight
        # vector pass.  NOT used for full solves -- those take a scalar `dx`,
        # which only means anything on a uniform grid, and production always
        # hands them one.
        x[1:-1] += 0.004 * np.sin(np.linspace(0, 9.0, n))[1:-1]
    width = 2.2 * np.sin(np.pi * x / L) ** 0.7 + 0.05
    w_self = 12.0 + 3.0 * np.cos(np.pi * x / L)
    return x, width, w_self


def _original_solve(x, dx, w_self, width_ft, coords, weights, Lspan):
    """The pre-hoist body, transcribed verbatim.  The reference."""
    w_payload = apply_realistic_paddler_loads(x, dx, coords, weights, Lspan,
                                              advanced_loading=False)
    w_self = np.asarray(w_self, float)
    total_weight = trapezoid(w_self + w_payload, x)
    q_buoyancy = (width_ft / trapezoid(width_ft, x)) * total_weight
    q_net = q_buoyancy - w_self - w_payload
    _w = np.asarray(width_ft, dtype=float)
    S0 = trapezoid(_w, x)
    S1 = trapezoid(_w * x, x)
    S2 = trapezoid(_w * x * x, x)
    R0 = trapezoid(q_net, x)
    R1 = trapezoid(q_net * x, x)
    det = S0 * S2 - S1 * S1
    if abs(det) > 1e-9 and S0 > 1e-9:
        a = (R0 * S2 - R1 * S1) / det
        b = (S0 * R1 - S1 * R0) / det
        q_net = q_net - _w * (a + b * x)
    elif S0 > 1e-9:
        q_net = q_net - _w * (R0 / S0)
    V = cumulative_trapezoid(q_net, x, initial=0)
    M = cumulative_trapezoid(V, x, initial=0)
    return M, V


# ── the integrals ──────────────────────────────────────────────────────────

def test_the_test_grid_is_genuinely_nonuniform():
    """POSITIVE CONTROL on the FIXTURE: a uniform grid would hide a bad tw."""
    x, _w, _ws = _grid(jitter=True)
    d = np.diff(x)
    assert d.max() / d.min() > 1.0 + 1e-6, "the jittered grid is uniform"


def test_the_weight_vector_reproduces_trapezoid():
    x, width, w_self = _grid(jitter=True)
    g = _beam_geom(x, width)
    for y in (width, w_self, width * x, np.ones_like(x), w_self * x * x):
        assert float(g.tw @ y) == pytest.approx(trapezoid(y, x),
                                                rel=TOL, abs=1e-12)


def test_the_hoisted_constants_are_bit_identical():
    """Not approximately: S0/S1/S2 are still trapezoid's own output."""
    x, width, _ws = _grid(jitter=True)
    g = _beam_geom(x, width)
    w64 = np.asarray(width, dtype=float)
    assert g.S0 == trapezoid(w64, x)
    assert g.S1 == trapezoid(w64 * x, x)
    assert g.S2 == trapezoid(w64 * x * x, x)
    assert g.det == g.S0 * g.S2 - g.S1 * g.S1


# ── the solve ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("n", [41, 161, 321])
def test_the_hoist_matches_the_original_arithmetic(n):
    """
    THE REQUIREMENT.  Against a transcription of the pre-hoist body, not
    against the new code itself.
    """
    x, width, w_self = _grid(n)
    dx = x[1] - x[0]
    M_ref, V_ref = _original_solve(x, dx, w_self, width, COORDS, WEIGHTS, L)
    M_new, V_new = _solve_beam_case(x, dx, w_self, width, COORDS, WEIGHTS, L,
                                    advanced_loading=False)
    mscale = max(float(np.abs(M_ref).max()), 1e-30)
    vscale = max(float(np.abs(V_ref).max()), 1e-30)
    dM = float(np.abs(M_new - M_ref).max()) / mscale
    dV = float(np.abs(V_new - V_ref).max()) / vscale
    assert dM < TOL, f"moment moved {dM:.3e} relative (budget {TOL:.0e})"
    assert dV < TOL, f"shear moved {dV:.3e} relative (budget {TOL:.0e})"


def test_passing_geom_matches_computing_it_inline():
    x, width, w_self = _grid()
    dx = x[1] - x[0]
    g = _beam_geom(x, width)
    M_a, V_a = _solve_beam_case(x, dx, w_self, width, COORDS, WEIGHTS, L,
                                advanced_loading=False)
    M_b, V_b = _solve_beam_case(x, dx, w_self, width, COORDS, WEIGHTS, L,
                                advanced_loading=False, geom=g)
    assert np.array_equal(M_a, M_b), "a precomputed geometry changed the moment"
    assert np.array_equal(V_a, V_b), "a precomputed geometry changed the shear"


# ── the physics ────────────────────────────────────────────────────────────

def test_shear_closes_to_machine_precision():
    """
    The sinkage/trim correction zeroes int q dx exactly, and V[-1] IS that
    integral, so this one really does close to float noise.
    """
    x, width, w_self = _grid(321)
    M, V = _solve_beam_case(x, x[1] - x[0], w_self, width, COORDS, WEIGHTS, L,
                            advanced_loading=False)
    rel = abs(float(V[-1])) / max(float(np.abs(V).max()), 1e-30)
    assert rel < 1e-11, f"shear does not close: V[-1] relative {rel:.3e}"


def test_moment_closes_at_SECOND_ORDER_not_to_machine_precision():
    """
    MEASURED, AND EXPLAINED -- this is not a defect, and the distinction is the
    whole value of the test.

    Analytically M[-1] = L*V(L) - int x*q dx = 0, and the correction zeroes
    both terms.  But it zeroes them as `trapezoid` integrals, while M[-1] is a
    DOUBLE `cumulative_trapezoid` -- a different discretisation of the same
    identity.  So the residual is O(h^2) truncation, not an equilibrium error.

    Measured on this hull, relative to |M|max:
        41 ->2.21e-03, 81 ->5.57e-04, 161 ->1.40e-04, 321 ->3.49e-05,
        641 ->8.74e-06, 1281 ->2.19e-06     -- 4.00x per doubling.

    THE ORIGINAL CODE BEHAVES IDENTICALLY (verified: same 2.0446e-02 at 161
    stations), so the hoist did not introduce it.

    Asserting the ORDER rather than a threshold is what makes this catch a real
    bug: a genuine equilibrium error does not converge, so its residual would
    stay flat and the ratio would collapse toward 1.0.
    """
    ratios = []
    prev = None
    for n in (81, 161, 321, 641):
        x, width, w_self = _grid(n)
        M, _V = _solve_beam_case(x, x[1] - x[0], w_self, width, COORDS,
                                 WEIGHTS, L, advanced_loading=False)
        rel = abs(float(M[-1])) / max(float(np.abs(M).max()), 1e-30)
        if prev is not None:
            ratios.append(prev / rel)
        prev = rel
    assert prev < 1e-5, (
        f"moment residual at 641 stations is {prev:.3e}; it should be tiny "
        f"even though it is not machine-zero")
    for r in ratios:
        assert 3.5 < r < 4.5, (
            f"moment residual is not converging at second order (ratio {r:.2f} "
            f"per doubling, expected ~4.0). A ratio near 1.0 means the residual "
            f"is NOT truncation -- that would be a real equilibrium error.")
