# tests/test_stability.py
"""
Transverse stability on the REAL pour.

The sweep's stability figure came from compute_hydrostatics, whose
``gmt = bmt - (T - vcb)`` ASSUMES KG equals the waterline.  That is a hull-FORM
estimate: it never sees where the concrete is, so an I-beam with heavy top
flanges scores identically to a uniform light hull -- unconservative in exactly
the case this suite optimises toward.  These tests pin the three conventions
apart, pin the crew-CG dominance that makes a bare GM_T uninterpretable, and
pin the direction of the top-flange strength/stability trade.
"""
from __future__ import annotations

import numpy as np
import pytest

import config
import structural
from structural import (hull_vertical_cg_in, transverse_gmt_design_in,
                        stability_conventions, transverse_hydro_terms_in,
                        hull_mass_and_cg)


# ── fixtures: a two-zone box whose KG is known in closed form ───────────────
def _box(n_st=21, n_z=41, half_b=10.0, depth=12.0):
    """Prismatic box: constant half-breadth, so every section quantity is exact."""
    x = np.linspace(0.0, 18.0, n_st)                    # ft
    z = np.linspace(0.0, depth, n_z)                    # in above keel
    # cum_area[i, k] = wall area of station i below z[k].  Uniform wall of
    # 1 in^2 per inch of height => area below z is simply z.
    cum_area = np.tile(z, (n_st, 1))                    # in^2
    # first moment of that area about the keel = INT_0^z (1)*zz dzz = z^2/2
    cum_Az = np.tile(0.5 * z ** 2, (n_st, 1))           # in^3
    return x, z, cum_area, cum_Az


def _zb(n_st, cuts, sheer):
    """Ascending zone-boundary list [0, ...cuts..., sheer], per station."""
    out = [np.zeros(n_st)]
    for c in cuts:
        out.append(np.full(n_st, float(c)))
    out.append(np.full(n_st, float(sheer)))
    return out


# ── KG of the zoned pour ────────────────────────────────────────────────────
def test_uniform_density_kg_is_the_geometric_centroid():
    """Equal densities must collapse to the shape's own centroid, d/2."""
    x, z, cA, cAz = _box(depth=12.0)
    kg = hull_vertical_cg_in(cA, cAz, z, _zb(len(x), [6.0], 12.0),
                             [70.0, 70.0], x)
    assert kg == pytest.approx(6.0, abs=1e-6)


def test_heavy_top_flange_raises_kg_above_the_centroid():
    """
    THE defect the form-only GM_T is blind to: mass moved up raises KG.
    Same geometry, same centroid -- only the distribution differs.
    """
    x, z, cA, cAz = _box(depth=12.0)
    bounds = _zb(len(x), [6.0], 12.0)
    light_top = hull_vertical_cg_in(cA, cAz, z, bounds, [80.0, 60.0], x)
    heavy_top = hull_vertical_cg_in(cA, cAz, z, bounds, [60.0, 80.0], x)
    assert heavy_top > 6.0 > light_top
    # exact: KG = (rho_b*Az_b + rho_t*Az_t)/(rho_b*A_b + rho_t*A_t)
    # bottom half A=6, Az=18; top half A=6, Az=54
    assert heavy_top == pytest.approx((60*18 + 80*54) / (60*6 + 80*6), abs=1e-6)


def test_kg_is_resolution_independent():
    """KG must be a property of the design, not of the offset grid."""
    vals = []
    for n_st, n_z in ((11, 21), (21, 41), (41, 161)):
        x, z, cA, cAz = _box(n_st=n_st, n_z=n_z, depth=12.0)
        vals.append(hull_vertical_cg_in(cA, cAz, z, _zb(n_st, [6.0], 12.0),
                                        [60.0, 80.0], x))
    assert max(vals) - min(vals) < 1e-6


def test_gunwale_rail_mass_raises_kg():
    """The rail is real concrete at the sheer -- the highest in the section."""
    from collections import namedtuple
    x, z, cA, cAz = _box(depth=12.0)
    bounds = _zb(len(x), [6.0], 12.0)
    n = len(x)
    R = namedtuple("R", "A_gw I_gw_own gw_cent A_fil fil_cent I_fil_own Kt")
    rail = R(np.full(n, 2.0), np.zeros(n), np.full(n, 12.0),
             np.zeros(n), np.full(n, 12.0), np.zeros(n), np.zeros(n))
    bare = hull_vertical_cg_in(cA, cAz, z, bounds, [70.0, 70.0], x)
    with_rail = hull_vertical_cg_in(cA, cAz, z, bounds, [70.0, 70.0], x,
                                    rail=rail, rail_density=70.0)
    assert with_rail > bare
    # 12 in^2 of shell at KG 6 + 2 in^2 of rail at 12 -> (12*6 + 2*12)/14
    assert with_rail == pytest.approx((12*6.0 + 2*12.0) / 14.0, abs=1e-6)


# ── mass + CG together ──────────────────────────────────────────────────────
def test_mass_and_cg_agree_with_the_cg_only_helper():
    """One implementation: hull_vertical_cg_in must be the same integral."""
    x, z, cA, cAz = _box(depth=12.0)
    b = _zb(len(x), [6.0], 12.0)
    w, kg = hull_mass_and_cg(cA, cAz, z, b, [60.0, 80.0], x)
    assert kg == pytest.approx(hull_vertical_cg_in(cA, cAz, z, b,
                                                  [60.0, 80.0], x))
    # closed form: 18 ft of hull, 6 in^2 per zone, /144 to reach pounds
    assert w == pytest.approx((60.0 * 6.0 + 80.0 * 6.0) * 18.0 / 144.0, rel=1e-9)


def test_mass_scales_linearly_with_density():
    x, z, cA, cAz = _box(depth=12.0)
    b = _zb(len(x), [6.0], 12.0)
    w1, _ = hull_mass_and_cg(cA, cAz, z, b, [60.0, 60.0], x)
    w2, _ = hull_mass_and_cg(cA, cAz, z, b, [120.0, 120.0], x)
    assert w2 == pytest.approx(2.0 * w1, rel=1e-12)


def test_lightening_the_top_zone_reduces_mass_and_kg():
    """
    The trade study's premise, pinned: a lighter top flange must come back
    LIGHTER and LOWER.  Regression for reading a rejected evaluation's sentinel
    weight (1e6) as a real mass -- that made the crew negligible and fabricated
    a large artificial stability gain.
    """
    x, z, cA, cAz = _box(depth=12.0)
    b = _zb(len(x), [6.0], 12.0)
    w_heavy, kg_heavy = hull_mass_and_cg(cA, cAz, z, b, [65.5, 65.5], x)
    w_light, kg_light = hull_mass_and_cg(cA, cAz, z, b, [65.5, 61.5], x)
    assert w_light < w_heavy
    assert kg_light < kg_heavy
    assert w_light < 1e5, "sentinel weight leaked into a geometric mass"


def test_a_light_hull_is_destabilising_when_the_crew_sits_high():
    """
    Counter-intuitive but exact, and it inverts once the crew CG is modelled
    rather than assumed: the hull is BALLAST.  KG_hull (~6 in) sits far below
    KG_crew (~20 in), so shedding hull weight raises the crew's share of the
    combined KG and LOWERS loaded GM_T.

    Pinned because the top-flange trade compares a lighter variant against the
    winner: reading the sign backwards (as is natural under the old flat 12 in
    assumption, where the crew sat lower) would report a stability gain that
    does not exist.
    """
    kb, bm, kg_hull, kg_crew, crew = 5.72, 8.78, 6.24, 20.02, 640.0
    heavy = transverse_gmt_design_in(kb, bm, kg_hull, 161.6,
                                     crew_lbs=crew, crew_kg_in=kg_crew)
    light = transverse_gmt_design_in(kb, bm, kg_hull, 157.9,
                                     crew_lbs=crew, crew_kg_in=kg_crew)
    assert light < heavy
    # ... and the effect reverses when the crew sits BELOW the hull's CG
    kg_low = 2.0
    heavy_lo = transverse_gmt_design_in(kb, bm, kg_hull, 161.6,
                                        crew_lbs=crew, crew_kg_in=kg_low)
    light_lo = transverse_gmt_design_in(kb, bm, kg_hull, 157.9,
                                        crew_lbs=crew, crew_kg_in=kg_low)
    assert light_lo > heavy_lo


def test_a_sentinel_weight_would_swamp_the_crew():
    """
    Demonstrates WHY the weight must come from geometry: at a 1e6 lb hull the
    crew contributes nothing and loaded GM_T collapses onto the bare-hull
    value, which looks like a huge stability win and is entirely artifact.
    """
    real = transverse_gmt_design_in(3.0, 10.0, 7.0, 160.0, crew_lbs=640.0,
                                    crew_kg_in=12.0)
    sentinel = transverse_gmt_design_in(3.0, 10.0, 7.0, 1e6, crew_lbs=640.0,
                                        crew_kg_in=12.0)
    bare = transverse_gmt_design_in(3.0, 10.0, 7.0, 160.0)
    assert sentinel == pytest.approx(bare, abs=1e-2)   # crew vanishes
    assert sentinel - real > 3.0                       # the fake "gain"


# ── the three conventions ───────────────────────────────────────────────────
def test_form_only_ignores_where_the_concrete_is():
    """
    The form-only figure is identical for a top-heavy and a bottom-heavy hull.
    That is precisely why it may not be quoted as the canoe's stability.
    """
    x, z, cA, cAz = _box(depth=12.0)
    bounds = _zb(len(x), [6.0], 12.0)
    kb, bm, draft = 3.0, 10.0, 4.0
    top = hull_vertical_cg_in(cA, cAz, z, bounds, [60.0, 80.0], x)
    bot = hull_vertical_cg_in(cA, cAz, z, bounds, [80.0, 60.0], x)
    a = stability_conventions(kb, bm, top, 120.0, 640.0, gmt_form_in=kb+bm-draft)
    b = stability_conventions(kb, bm, bot, 120.0, 640.0, gmt_form_in=kb+bm-draft)
    assert a["gmt_form_in"] == b["gmt_form_in"]          # blind
    assert a["gmt_bare_hull_in"] < b["gmt_bare_hull_in"]  # not blind
    assert a["gmt_loaded_in"] < b["gmt_loaded_in"]


def test_the_three_conventions_are_distinct_and_ordered_by_kg():
    """Higher KG => lower GM_T, always. The three must not be interchanged."""
    s = stability_conventions(kb_in=3.0, bm_t_in=10.0, kg_hull_in=7.0,
                              hull_lbs=120.0, crew_lbs=640.0,
                              gmt_form_in=9.0, crew_kg_in=12.0)
    assert s["gmt_bare_hull_in"] == pytest.approx(3.0 + 10.0 - 7.0)
    kg_c = (120.0 * 7.0 + 640.0 * 12.0) / 760.0
    assert s["gmt_loaded_in"] == pytest.approx(3.0 + 10.0 - kg_c)
    assert s["gmt_loaded_in"] < s["gmt_bare_hull_in"]
    assert s["kg_combined_in"] == pytest.approx(kg_c)


def test_crew_cg_dominates_the_loaded_figure():
    """
    640 lb of crew against ~120 lb of hull: the crew sets KG.  A 4 in swing in
    the ASSUMED crew CG must move loaded GM_T by more than swapping the entire
    hull between its lightest and heaviest plausible KG.
    """
    base = dict(kb_in=3.0, bm_t_in=10.0, hull_lbs=120.0, crew_lbs=640.0)
    crew_swing = abs(
        transverse_gmt_design_in(kg_hull_in=7.0, crew_kg_in=10.0, **base)
        - transverse_gmt_design_in(kg_hull_in=7.0, crew_kg_in=14.0, **base))
    hull_swing = abs(
        transverse_gmt_design_in(kg_hull_in=5.0, crew_kg_in=12.0, **base)
        - transverse_gmt_design_in(kg_hull_in=9.0, crew_kg_in=12.0, **base))
    assert crew_swing > hull_swing
    # mass-ratio exact: swings scale as each mass / total
    assert crew_swing / hull_swing == pytest.approx(640.0 / 120.0, rel=1e-9)


def test_sensitivity_band_spans_the_reported_crew_cgs():
    s = stability_conventions(3.0, 10.0, 7.0, 120.0, 640.0, crew_kg_in=12.0)
    got = [c for c, _ in s["crew_kg_sensitivity"]]
    assert got == [10.0, 12.0, 14.0]
    gms = [g for _, g in s["crew_kg_sensitivity"]]
    assert gms[0] > gms[1] > gms[2]          # higher crew CG => less stable
    # the middle entry must agree with the headline loaded value
    assert gms[1] == pytest.approx(s["gmt_loaded_in"])


def test_zero_crew_reduces_loaded_to_bare_hull():
    s = stability_conventions(3.0, 10.0, 7.0, 120.0, 0.0)
    assert s["gmt_loaded_in"] == pytest.approx(s["gmt_bare_hull_in"])


def test_bm_t_is_the_ceiling_no_distribution_can_beat_it():
    """
    GM_T = KB + BM_T - KG with KG > 0 always, so KB + BM_T bounds every
    convention.  Mix distribution moves KG only; the ceiling is hull form.
    """
    x, z, cA, cAz = _box(depth=12.0)
    bounds = _zb(len(x), [6.0], 12.0)
    kb, bm = 3.0, 10.0
    for stack in ([60.0, 60.0], [60.0, 80.0], [80.0, 60.0], [57.5, 80.0]):
        kg = hull_vertical_cg_in(cA, cAz, z, bounds, stack, x)
        s = stability_conventions(kb, bm, kg, 120.0, 640.0)
        assert s["gmt_bare_hull_in"] < kb + bm
        assert s["gmt_loaded_in"] < kb + bm


# ── biomechanical crew CG through the SAME entry point ─────────────────────
def test_modelled_crew_cg_flows_through_stability_conventions():
    """
    One implementation: the model feeds the existing function, it does not get
    a parallel path.  All three conventions must come out of one call.
    """
    from physics.biomechanics import crew_kg_in as bio_kg
    kg_c = bio_kg([70.0, 70.0, 65.0, 65.0], ["M", "M", "F", "F"],
                  [160.0, 150.0, 170.0, 160.0], 70.0, datum_offset_in=0.5)
    s = stability_conventions(5.72, 8.78, 6.24, 161.6, 640.0,
                              gmt_form_in=9.92, crew_kg_in=kg_c,
                              crew_kg_basis="biomechanical model")
    assert s["kg_crew_in"] == pytest.approx(kg_c)
    assert s["crew_kg_basis"] == "biomechanical model"
    assert {"gmt_form_in", "gmt_bare_hull_in", "gmt_loaded_in"} <= set(s)


def test_legacy_loaded_value_is_always_reported_for_comparison():
    """The delta the model introduces must never be silently absorbed."""
    s = stability_conventions(5.72, 8.78, 6.24, 161.6, 640.0, crew_kg_in=20.02)
    assert s["crew_kg_legacy_in"] == pytest.approx(config.CREW_VERTICAL_CG_IN)
    assert s["gmt_loaded_legacy_in"] > s["gmt_loaded_in"]
    # the old assumption was OPTIMISTIC — that is the whole point
    assert s["gmt_loaded_legacy_in"] - s["gmt_loaded_in"] > 4.0


def test_posture_band_replaces_the_round_number_sweep():
    band = [("A_heels_sitting", 45.0, 11.04), ("B_upright_kneel", 90.0, 20.22)]
    s = stability_conventions(5.72, 8.78, 6.24, 161.6, 640.0,
                              crew_kg_in=20.02, crew_kg_band=band)
    pb = s["crew_kg_posture_band"]
    assert [p for p, *_ in pb] == ["A_heels_sitting", "B_upright_kneel"]
    # each entry carries its own loaded GM_T, and a higher crew CG scores lower
    assert pb[0][3] > pb[1][3]
    assert pb[1][3] < 0.0                      # the negative result surfaces


def test_basis_defaults_to_calling_itself_an_assumption():
    s = stability_conventions(5.72, 8.78, 6.24, 161.6, 640.0)
    assert "assum" in s["crew_kg_basis"].lower()
    assert s["crew_kg_posture_band"] is None


# ── the form-only convention's own definition ──────────────────────────────
def test_hydro_terms_reproduce_the_form_only_gmt():
    """
    transverse_hydro_terms_in must decompose the SAME number transverse_gmt_in
    returns: gmt_form == kb + bmt - draft (KG = waterline).
    """
    n_st, n_wl = 21, 15
    draft = 5.0
    y = np.tile(np.linspace(2.0, 9.0, n_wl), (n_st, 1))   # flaring sides
    kb, bmt, gmt = transverse_hydro_terms_in(y, 216.0, draft)
    if bmt <= 1e-9:
        pytest.skip("degenerate synthetic hull")
    assert gmt == pytest.approx(structural.transverse_gmt_in(y, 216.0, draft))
    assert gmt == pytest.approx(kb + bmt - draft, abs=1e-6)


def test_hydro_terms_raise_rather_than_degrade_to_zeros():
    """
    CONTRACT REVERSED, deliberately.  This test used to assert that degenerate
    geometry "degrades to zeros, not exceptions".  Zeros are not an absence
    here -- KB = 0 and BM_T = 0 describe a hull with no buoyancy and no
    waterplane stiffness, and GM_T = 0 is the specific claim "neutrally
    stable".  Worse, the sweep's gate reads `if gmt > 1e-9`, so a zero SKIPPED
    the Min-GMT check instead of failing it: a hull whose stability could not
    be computed passed with no stability check at all.

    There is no value that can stand in for "could not be computed", so the
    functions raise and callers handle absence explicitly.
    """
    with pytest.raises(ValueError, match="entirely zero|empty|2x2"):
        transverse_hydro_terms_in(np.zeros((3, 3)), 0.0, 0.0)


def test_a_real_hull_still_computes(off1_like_geometry=None):
    """The raise must not have swallowed the working path."""
    n_st, n_wl = 21, 15
    y = np.tile(np.linspace(2.0, 9.0, n_wl), (n_st, 1))
    kb, bmt, gmt = transverse_hydro_terms_in(y, 216.0, 5.0)
    assert np.isfinite(kb) and np.isfinite(bmt) and np.isfinite(gmt)
    assert bmt > 0.0, "a flaring synthetic hull must have real waterplane stiffness"


def test_non_finite_geometry_raises_rather_than_fabricating():
    """
    The -2.2880 case.  compute_hydrostatics guarded its fallbacks with
    `if volume > 1e-12`, and `NaN > 1e-12` is FALSE, so NaN geometry took the
    empty-hull branch and returned vcb = T/2, bmt = 0, gmt = -T/2 -- a
    plausible marginal-stability figure rather than a failure.
    """
    with pytest.raises(ValueError, match="non-finite"):
        transverse_hydro_terms_in(np.full((21, 11), np.nan), 216.0, 5.0)


# ── config wiring ───────────────────────────────────────────────────────────
def test_crew_vertical_cg_is_live_from_cfg(tmp_path):
    """
    The assumption must be editable in canoe_suite.cfg, not buried in code --
    the 'parsed but never read' pattern this suite has been cleaning out.
    """
    from config.settings import Settings
    p = tmp_path / "canoe_suite.cfg"
    p.write_text("structural:\n  crew_vertical_cg_in: 13.5\n", encoding="utf-8")
    s = Settings.load(str(p))
    assert float(s.structural.crew_vertical_cg_in) == pytest.approx(13.5)


def test_crew_vertical_cg_defaults_when_absent(tmp_path):
    from config.settings import Settings
    p = tmp_path / "canoe_suite.cfg"
    p.write_text("structural:\n  factor_of_safety: 1.5\n", encoding="utf-8")
    assert float(Settings.load(str(p)).structural.crew_vertical_cg_in) == 12.0


def test_shipped_cfg_carries_the_key():
    """The shipped cfg must actually expose it, or 'LIVE' is a false claim."""
    from config.settings import get_settings
    assert float(get_settings().structural.crew_vertical_cg_in) > 0.0
    assert float(config.CREW_VERTICAL_CG_IN) == pytest.approx(
        float(get_settings().structural.crew_vertical_cg_in))


def test_crew_cg_default_is_used_when_not_passed(monkeypatch):
    monkeypatch.setattr(config, "CREW_VERTICAL_CG_IN", 14.0)
    explicit = transverse_gmt_design_in(3.0, 10.0, 7.0, 120.0,
                                        crew_lbs=640.0, crew_kg_in=14.0)
    implied = transverse_gmt_design_in(3.0, 10.0, 7.0, 120.0, crew_lbs=640.0)
    assert implied == pytest.approx(explicit)


# ── peer comparison (third-party data) ──────────────────────────────────────
def test_peer_gmt_constants_carry_their_source():
    """
    The competitor figures must be named constants with provenance, not bare
    numbers inlined in prose -- they are third-party data and the claim built
    on them has to be auditable.
    """
    from reporting import latex_report as lr
    names = dict(lr.PEER_GMT_IN)
    assert names["Reptilia"] == pytest.approx(10.4)
    assert names["Tempest"] == pytest.approx(10.9)
    # the provenance comment lives with the constant; assert the module states
    # the reading is unverified rather than presenting it as established
    src = open(lr.__file__, encoding="utf-8").read()
    assert "not a verified apples-to-apples" in src
    assert "loading basis and KG assumption are NOT stated" in src


def test_peer_gap_is_computed_against_the_form_only_value():
    """
    The published figures are read as hull-FORM values, so the gap must be taken
    against our form-only GM_T -- comparing them to the loaded value would be
    the exact category error the section warns about.
    """
    from reporting.latex_report import PEER_GMT_IN
    ours_form, ours_loaded = 9.92, 3.66
    pv = [v for _, v in PEER_GMT_IN]
    gap_form = [100.0 * (v - ours_form) / v for v in pv]
    assert min(gap_form) == pytest.approx(4.6, abs=0.1)
    assert max(gap_form) == pytest.approx(9.0, abs=0.1)
    # against the loaded value the "gap" would be ~65%, which is meaningless
    assert 100.0 * (min(pv) - ours_loaded) / min(pv) > 60.0


def test_min_gmt_gate_stays_disabled():
    """
    Deliberate: the gate remains inert until the real-KG numbers are trusted.
    Last season's hull was accepted with B below G in every racing load case,
    so a live gate could reject the whole feasible set.  Changing this default
    is a decision, not a tweak -- this test is the tripwire.
    """
    assert float(getattr(config, "MIN_GMT_IN", 0.0)) == 0.0
