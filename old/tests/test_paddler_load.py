# tests/test_paddler_load.py
"""
Paddler load application must CONSERVE total weight on any grid, and the two
knee/toe bases (bending vs punching) must stay distinct.

Guards a real bug: the advanced path spread the knee load as w_knee/0.25,
dividing by the NOMINAL 0.25 ft window while the caller integrates over the
ACTUAL grid.  The applied load scaled as covered_width/0.25 -- on the
production 41-station grid (dx = 0.5 ft) that applied 1216 lb of paddler for
640 lb of paddlers, a 90% overload, and the error swung with grid resolution
(+135%, 0%, +90%, +15%, +45% ...).
"""
from __future__ import annotations

import numpy as np
import pytest

import config
from np_compat import trapezoid
from structural import apply_realistic_paddler_loads

WEIGHTS = [160.0, 150.0, 170.0, 160.0]
COORDS = [3.0, 7.5, 10.5, 15.0]
L = 20.0
TOTAL = sum(WEIGHTS)

# Spacings chosen to straddle the 0.25 ft knee window: coarser than it, equal
# to it, and finer than it. The old code was only correct by luck near dx=0.25.
GRIDS = [21, 31, 41, 61, 81, 161, 401, 801]


@pytest.mark.parametrize("n", GRIDS)
@pytest.mark.parametrize("advanced", [False, True])
def test_paddler_load_conserves_weight_on_any_grid(n, advanced):
    x = np.linspace(0, L, n)
    dx = L / (n - 1)
    w = apply_realistic_paddler_loads(x, dx, COORDS, WEIGHTS, L,
                                      advanced_loading=advanced)
    assert trapezoid(w, x) == pytest.approx(TOTAL, rel=1e-9), (
        f"n={n} advanced={advanced}: applied "
        f"{trapezoid(w, x):.2f} lb for {TOTAL:.2f} lb of paddlers"
    )


def test_production_grid_is_not_overloaded():
    # The exact case the bug hit: 41 stations, dx = 0.5 ft.
    n = 41
    x = np.linspace(0, L, n)
    w = apply_realistic_paddler_loads(x, L / (n - 1), COORDS, WEIGHTS, L,
                                      advanced_loading=True)
    applied = trapezoid(w, x)
    assert applied == pytest.approx(TOTAL, rel=1e-9)
    assert applied < 1.01 * TOTAL, "regression: knee patch re-overloading"


def test_both_load_models_apply_the_same_total():
    # Apples-to-apples: the two models must differ in DISTRIBUTION only, never
    # in magnitude. Otherwise any winner comparison between them is invalid.
    x = np.linspace(0, L, 41)
    dx = L / 40
    off = apply_realistic_paddler_loads(x, dx, COORDS, WEIGHTS, L, advanced_loading=False)
    on = apply_realistic_paddler_loads(x, dx, COORDS, WEIGHTS, L, advanced_loading=True)
    assert trapezoid(off, x) == pytest.approx(trapezoid(on, x), rel=1e-9)


def test_advanced_moves_weight_aft_to_the_toes(monkeypatch):
    """
    Pins a 0.90 split EXPLICITLY rather than reading the shipped default.
    The default is now 1.00 (100/0), which puts nothing on the toes, so a
    test that leans on the default would silently stop exercising the toe
    path instead of failing.  The mechanism must keep working for anyone who
    lowers the fraction on the strength of a future measurement.
    """
    monkeypatch.setattr(config, "KNEE_FRAC_BENDING", 0.90)
    x = np.linspace(0, L, 41)
    dx = L / 40
    off = apply_realistic_paddler_loads(x, dx, COORDS, WEIGHTS, L, advanced_loading=False)
    on = apply_realistic_paddler_loads(x, dx, COORDS, WEIGHTS, L, advanced_loading=True)
    # advanced spreads onto more stations (knee + toe per paddler)
    assert np.count_nonzero(on) > np.count_nonzero(off)
    # and the load centroid moves AFT (toes are behind the knees)
    c_off = trapezoid(off * x, x) / trapezoid(off, x)
    c_on = trapezoid(on * x, x) / trapezoid(on, x)
    assert c_on > c_off


def test_knee_fraction_comes_from_config_not_a_literal():
    # One source of truth: changing the config value must change the split.
    x = np.linspace(0, L, 41)
    dx = L / 40
    old = config.KNEE_FRAC_BENDING
    try:
        config.KNEE_FRAC_BENDING = 0.90
        w90 = apply_realistic_paddler_loads(x, dx, [10.0], [100.0], L, advanced_loading=True)
        config.KNEE_FRAC_BENDING = 0.50
        w50 = apply_realistic_paddler_loads(x, dx, [10.0], [100.0], L, advanced_loading=True)
    finally:
        config.KNEE_FRAC_BENDING = old
    assert not np.allclose(w90, w50)
    # both still conserve
    assert trapezoid(w90, x) == pytest.approx(100.0, rel=1e-9)
    assert trapezoid(w50, x) == pytest.approx(100.0, rel=1e-9)


def test_bending_basis_never_credits_more_relief_than_punching():
    """
    These two were once DISTINCT (bending 0.90 measured, punching 1.00).
    Adopting the 100/0 bending basis made them EQUAL, so "distinct" is no
    longer the property -- it never was the safety property anyway.

    What must hold is the ORDERING: punching credits no toe relief, and
    bending may never credit MORE relief than punching does.  Equal is fine
    (both 1.00 today); bending above punching would mean the global bending
    check assumed a lighter knee load than the local contact check, which is
    the one combination that is unsafe.
    """
    assert config.KNEE_FRAC_PUNCHING == 1.0
    assert config.KNEE_FRAC_BENDING <= config.KNEE_FRAC_PUNCHING


# ── continuity in seat position ──────────────────────────────────────────────
# The SLSQP seating solver finite-differences an objective built on this load
# vector.  When loads were SNAPPED to the nearest station the vector was
# byte-identical while a paddler moved within +-dx/2, so the objective was a
# STEP function and the FD gradient was EXACTLY zero -- the solver returned its
# initial guess and the "winner" was decided by the hardcoded x0.

@pytest.mark.parametrize("advanced", [False, True])
def test_load_vector_changes_continuously_with_seat_position(advanced):
    x = np.linspace(0, L, 41)
    dx = L / 40
    prev = None
    for s in np.arange(7.0, 8.0, 0.05):
        w = apply_realistic_paddler_loads(x, dx, [s], [170.0], L,
                                          advanced_loading=advanced)
        if prev is not None:
            assert not np.array_equal(w, prev), (
                f"load vector identical at s={s:.2f} vs previous step: "
                "seat position is not resolved (snapping regression)"
            )
        prev = w


@pytest.mark.parametrize("advanced", [False, True])
def test_first_moment_tracks_seat_position(advanced):
    # The load's centroid must move with the seat, monotonically -- this is the
    # property the seating gradient depends on.
    x = np.linspace(0, L, 41)
    dx = L / 40
    cents = []
    for s in np.arange(6.0, 12.0, 0.1):
        w = apply_realistic_paddler_loads(x, dx, [s], [170.0], L,
                                          advanced_loading=advanced)
        cents.append(trapezoid(w * x, x) / trapezoid(w, x))
    cents = np.array(cents)
    assert np.all(np.diff(cents) > 0), "load centroid does not track the seat"


def test_deposit_is_exact_for_a_seat_between_two_stations():
    # A load exactly midway between stations must split 50/50 and conserve.
    x = np.linspace(0, L, 41)
    dx = L / 40
    mid = 0.5 * (x[14] + x[15])
    w = apply_realistic_paddler_loads(x, dx, [mid], [100.0], L,
                                      advanced_loading=False)
    assert trapezoid(w, x) == pytest.approx(100.0, rel=1e-12)
    assert w[14] == pytest.approx(w[15], rel=1e-9)
    assert np.count_nonzero(w) == 2


# ── load-model envelope ──────────────────────────────────────────────────────
# Neither paddler load model bounds the other: splitting weight to the toes
# lowers the sagging moment and peak shear but RAISES the hogging moment.
# Designing to whichever one a global flag selects leaves the other limit state
# unchecked, so the envelope designs to the worst of both at every station.

def _envelope_probe(env_on, advanced):
    import numpy as np
    import config
    from structural import calculate_moment_envelope
    n = 41
    x = np.linspace(0.0, 20.0, n)
    width = np.full(n, 2.5)
    width[0] = width[-1] = 0.0          # degenerate tips, as the real hull has
    old_e, old_a = config.PADDLER_LOAD_ENVELOPE, config.ADVANCED_PADDLER_LOADING
    try:
        config.PADDLER_LOAD_ENVELOPE = env_on
        config.ADVANCED_PADDLER_LOADING = advanced
        r = calculate_moment_envelope(x, x[1] - x[0], np.full(n, 2.0), width,
                                      [3.0, 7.5, 10.5, 15.0], [7.0, 11.0],
                                      return_all=True)
    finally:
        config.PADDLER_LOAD_ENVELOPE, config.ADVANCED_PADDLER_LOADING = old_e, old_a
    return (float(np.max(r["moment"]["sag"])),
            float(np.max(r["moment"]["hog"])),
            float(max(np.max(r["shear"]["max_pos"]), -np.min(r["shear"]["max_neg"]))))


def test_envelope_bounds_both_load_models():
    point = _envelope_probe(False, False)
    split = _envelope_probe(False, True)
    env = _envelope_probe(True, False)
    for i, name in enumerate(("M_sag", "M_hog", "|V|")):
        assert env[i] >= point[i] - 1e-9, f"envelope below point-load {name}"
        assert env[i] >= split[i] - 1e-9, f"envelope below split-model {name}"


def test_envelope_is_independent_of_the_advanced_flag():
    # With the envelope on, ADVANCED_PADDLER_LOADING must not change the result
    # -- both models are evaluated either way.
    assert _envelope_probe(True, False) == pytest.approx(_envelope_probe(True, True))


def test_envelope_is_not_merely_one_of_the_models(monkeypatch):
    """Same reason as above: pin a split so the probe still has two models."""
    monkeypatch.setattr(config, "KNEE_FRAC_BENDING", 0.90)
    point = _envelope_probe(False, False)
    split = _envelope_probe(False, True)
    assert point != pytest.approx(split), (
        "the two load models produced identical envelopes; this probe can no "
        "longer detect a collapsed envelope"
    )


def test_the_envelope_collapses_at_1_00_and_only_at_1_00():
    """
    The real property, pinned in BOTH directions.

    An earlier version pinned only the collapse at the 100/0 default, which
    reads as though the two-model envelope were redundant machinery.  It is
    not.  Measured per-station at 161 stations, the SPLIT branch wins hogging
    at 142/161 stations and shear at 32-37, for every split from 0.70 to 0.90
    -- the envelope earns its second beam solve there.

    What the no-relief branch governs unconditionally is the SAGGING PEAK,
    because spreading load to the toes only ever lowers it, and on the current
    design sagging is what sets D/C.  That is the whole reason
    knee_frac_bending cannot move the shipped D/C while the envelope is on --
    and it is a property of THIS design's hogging being negligible, not a
    structural fact, so it must not be relied on.

    So: identical at exactly 1.00, genuinely different below it.  If the
    second half ever fails, the envelope really has become one model twice.
    """
    x = np.linspace(0, L, 41)
    dx = L / 40

    def _both(frac):
        old = config.KNEE_FRAC_BENDING
        try:
            config.KNEE_FRAC_BENDING = frac
            off = apply_realistic_paddler_loads(x, dx, COORDS, WEIGHTS, L,
                                                advanced_loading=False)
            on = apply_realistic_paddler_loads(x, dx, COORDS, WEIGHTS, L,
                                               advanced_loading=True)
        finally:
            config.KNEE_FRAC_BENDING = old
        return off, on

    off, on = _both(1.00)
    assert np.allclose(off, on), (
        "at 100/0 the split model must reduce EXACTLY to the no-relief model")

    for frac in (0.70, 0.80, 0.90):
        off, on = _both(frac)
        assert not np.allclose(off, on), (
            f"at knee_frac={frac} the two load models are identical; the "
            "envelope has collapsed to one model and its second beam solve "
            "per case is buying nothing")
