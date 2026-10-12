# tests/test_multizone.py
"""
Multi-zone (I-beam) pour strategy — composite transformed-section core.

Verifies the physics that the swept multi-zone evaluator is built on:
  * cumulative first/second moments from calculate_shell_properties
  * composite_section_properties reduces EXACTLY to the homogeneous section
    when all zones share a modulus (the acceptance test)
  * ACI elastic modulus derivation
  * constrained pour-line generator (explosion control)
  * evaluate_multizone_weight feasibility + I-beam material placement

Uses a synthetic elliptical hull so it is independent of the live
off1.mlt / mix database.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import config

from geometry import calculate_shell_properties
from structural import (
    composite_section_properties, aci_elastic_modulus,
    multizone_pour_grids, evaluate_multizone_weight, ibeam_zone_stack,
)

# ── Service-level load calibration ──────────────────────────────────────────
@pytest.fixture(autouse=True)
def _service_level_loads(monkeypatch):
    """Pin these tests to ASD (service-level) loads.

    The suite's default methodology is LRFD, whose factored demand on this hull
    is ~1.7x the service-level demand these fixtures were sized against -- the
    stub mixes and geometries below were chosen so a design merely *clears*, and
    clearing service loads does not imply clearing factored ones.

    The tests in this file characterise evaluator MECHANICS (a feasible stack
    returns a positive weight, the distinct-mix cap gates, the sweep worker
    finds a configuration), which are orthogonal to how loads and capacities are
    factored.  Pinning ASD keeps that orthogonality EXPLICIT rather than an
    accident of whatever the default happens to be -- the failure mode that
    made six of these break when the default moved to LRFD.

    LRFD/ASD behaviour itself is covered by tests/test_lrfd_asd.py.
    """
    monkeypatch.setattr(config, "DESIGN_METHOD", "ASD", raising=False)
    monkeypatch.setattr(config, "OMEGA_SAFETY", 1.5, raising=False)



def _hull(n_st=41, n_wl=17, L_ft=20.0, depth_in=13.0, half_beam_in=13.0,
          t_in=0.5):
    x = np.linspace(0.0, L_ft, n_st)
    z = np.linspace(0.0, depth_in, n_wl)
    w = half_beam_in * np.sin(np.pi * x / L_ft)
    prof = np.sqrt(z / depth_in)
    y = np.outer(w, prof)
    p = calculate_shell_properties(x, z, y, t_in)
    v = p.A_in2 > 0.001
    return dict(
        x=x[v], dx=L_ft / (n_st - 1), width_ft=p.width_ft[v], t_in=t_in,
        ca=p.cum_area_in2[v], cAz=p.cum_Az_in3[v], cIz=p.cum_Iz_in4[v],
        zg=p.z_grid_in, sheer=p.c_bot[v] + p.c_top[v], na=p.c_bot[v],
        cIy=p.cum_Iy_in4[v], yh=p.yhalf_in[v],
        I0=p.I_in4[v], ct0=p.c_top[v], cb0=p.c_bot[v], depth=depth_in)


# ══════════════════════════════════════════════════════════════════════════
#  Composite section properties
# ══════════════════════════════════════════════════════════════════════════

class TestCompositeSection:
    def test_single_zone_equals_homogeneous(self):
        h = _hull()
        z0 = np.zeros_like(h["sheer"])
        yb, I, ct, cb = composite_section_properties(
            [z0, h["sheer"]], [3.0e6], h["ca"], h["cAz"], h["cIz"], h["zg"])
        assert np.allclose(I, h["I0"], rtol=1e-9, atol=1e-9)
        assert np.allclose(ct, h["ct0"], atol=1e-9)
        assert np.allclose(cb, h["cb0"], atol=1e-9)

    def test_equal_modulus_partition_invariance(self):
        """Splitting into N zones of the SAME modulus must not change I."""
        h = _hull()
        z0 = np.zeros_like(h["sheer"])
        yb, I3, _, _ = composite_section_properties(
            [z0, 0.3 * h["sheer"], 0.65 * h["sheer"], h["sheer"]],
            [2.5e6, 2.5e6, 2.5e6], h["ca"], h["cAz"], h["cIz"], h["zg"])
        assert np.allclose(I3, h["I0"], rtol=1e-8, atol=1e-8)

    def test_stiffer_material_at_extremes_raises_rigidity(self):
        """I-beam (stiff extremes) must have higher flexural rigidity EI than
        a uniform section of the compliant core material."""
        h = _hull()
        z0 = np.zeros_like(h["sheer"])
        E_hi, E_lo = 1.0e6, 0.3e6
        _, I_ibeam, _, _ = composite_section_properties(
            [z0, 0.25 * h["sheer"], 0.75 * h["sheer"], h["sheer"]],
            [E_hi, E_lo, E_hi], h["ca"], h["cAz"], h["cIz"], h["zg"])
        _, I_soft, _, _ = composite_section_properties(
            [z0, h["sheer"]], [E_lo], h["ca"], h["cAz"], h["cIz"], h["zg"])
        # EI in absolute terms (I_trans is in reference-modulus units)
        EI_ibeam = E_hi * I_ibeam          # ref = extreme mix
        EI_soft = E_lo * I_soft
        i = int(np.argmax(h["sheer"]))
        assert EI_ibeam[i] > EI_soft[i] * 1.3

    def test_aci_modulus_monotonic_and_reasonable(self):
        # Lightweight concrete: a few hundred ksi to ~1000 ksi
        e60 = aci_elastic_modulus(60, 600)
        e80 = aci_elastic_modulus(80, 1800)
        assert 2e5 < e60 < 1.5e6 and e80 > e60


# ══════════════════════════════════════════════════════════════════════════
#  Pour-line generator (explosion control)
# ══════════════════════════════════════════════════════════════════════════

class TestPourGrids:
    def test_one_zone_has_no_pour_lines(self):
        assert multizone_pour_grids(14.0, 1) == [()]

    def test_min_separation_and_count(self):
        grids = multizone_pour_grids(14.0, 3, step_in=2.0, min_sep_in=2.0)
        assert len(grids) >= 3
        for fr in grids:
            assert len(fr) == 2
            z = [f * 14.0 for f in fr]
            # (fracs are 4-decimal-rounded, so allow a hair under 2")
            assert z[1] - z[0] >= 2.0 - 0.02          # min separation
            assert all(a < b for a, b in zip(fr, fr[1:]))  # ascending

    def test_margins_keep_extreme_zones_real(self):
        for fr in multizone_pour_grids(14.0, 3, margin_frac=0.15):
            assert fr[0] >= 0.15 - 1e-6 and fr[-1] <= 0.85 + 1e-6

    def test_shallow_depth_falls_back_to_even_split(self):
        g = multizone_pour_grids(3.0, 3, step_in=2.0, min_sep_in=2.0)
        assert len(g) == 1 and len(g[0]) == 2


class TestIBeamStack:
    def test_three_zone_two_mix_is_true_ibeam(self):
        # heavy at bottom AND top, light in the core
        assert ibeam_zone_stack([80.0, 62.0], 3) == (80.0, 62.0, 80.0)

    def test_two_zone(self):
        assert ibeam_zone_stack([80.0, 62.0], 2) == (80.0, 62.0)

    def test_four_zone(self):
        assert ibeam_zone_stack([80.0, 62.0], 4) == (80.0, 62.0, 62.0, 80.0)

    def test_never_inverted_or_uniform(self):
        s = ibeam_zone_stack([75.0, 60.0], 3)
        assert s[0] == s[-1] == 75.0        # heavy extremes (not inverted)
        assert s[1] == 60.0                 # light core (not uniform)

    def test_three_mixes_grade_from_extremes(self):
        # 5 zones, 3 mixes: heaviest extremes → medium → lightest core
        assert ibeam_zone_stack([80.0, 70.0, 60.0], 5) == \
            (80.0, 70.0, 60.0, 70.0, 80.0)


# ══════════════════════════════════════════════════════════════════════════
#  Multi-zone evaluator
# ══════════════════════════════════════════════════════════════════════════

def _gp(d, fos=1.5):
    d = float(d)
    return {"density": d,
            "strength_t": max(10.0, 20.0 * (d - 55.0) + 20.0),
            "strength_c": max(80.0, 100.0 * (d - 55.0) + 120.0)}


class TestMultizoneEvaluator:
    def _ev(self, h, zone_stack, fr):
        # zone_stack is EXPLICIT bottom→top density per zone
        return evaluate_multizone_weight(
            [3., 7.5, 12., 15.], [4., 14.], zone_stack, fr,
            h["x"], h["dx"], h["width_ft"], h["t_in"],
            h["ca"], h["cAz"], h["cIz"], h["zg"], h["sheer"], h["na"], _gp,
            coords_3=[6., 10., 14.], position_variance_ft=0.0)

    def test_feasible_weight_positive(self):
        h = _hull()
        w = self._ev(h, [80, 70, 62], (0.34, 0.67))
        assert 0 < w < 1e5

    def test_ibeam_from_two_mixes_lighter_than_uniform_heavy(self):
        """Decoupled zones: pour 2 physical mixes (82 heavy, 60 light) in a
        3-zone I-beam [82, 60, 82].  It must be feasible and lighter than a
        uniform-heavy wall when the light mix alone is insufficient."""
        h = _hull(depth_in=11.0, t_in=0.375)
        w_uni_heavy = self._ev(h, [82, 82, 82], (0.34, 0.67))
        assert w_uni_heavy < 1e5, "uniform heavy should be feasible"
        # I-beam stacks from ONLY 2 available densities (82, 60)
        best = min(self._ev(h, [82, 60, 82], fr)
                   for fr in multizone_pour_grids(h["depth"], 3))
        assert best < 1e5, "no feasible I-beam found"
        assert best < w_uni_heavy, \
            f"I-beam ({best:.1f}) not lighter than uniform heavy ({w_uni_heavy:.1f})"

    def test_explicit_stack_is_honoured(self):
        """The passed stack IS the assignment (no stress re-ordering): the
        returned zone densities equal the input, bottom→top."""
        h = _hull()
        d = evaluate_multizone_weight(
            [3., 7.5, 12., 15.], [4., 14.], [70, 62, 80], (0.34, 0.67),
            h["x"], h["dx"], h["width_ft"], h["t_in"],
            h["ca"], h["cAz"], h["cIz"], h["zg"], h["sheer"], h["na"], _gp,
            coords_3=[6., 10., 14.], return_details=True)
        assert d["feasible"]
        assert list(np.round(d["zone_dens"], 1)) == [70.0, 62.0, 80.0]

    def test_overstress_returns_rejected_not_a_number(self):
        """
        An infeasible design has NO MASS.  It used to come back as 1e6, a float
        indistinguishable from a very heavy hull, which downstream code then
        did arithmetic on -- producing a -999842.82 lb weight delta and a
        "swamped" canoe.  The marker must not be usable as a number.
        """
        import structural
        thin = _hull(t_in=0.25, depth_in=16.0)
        w = self._ev(thin, [56, 56, 56], (0.34, 0.67))
        assert w is structural.REJECTED
        assert structural.is_rejected(w)
        assert not w                                   # falsy
        for op in (lambda: w + 1.0, lambda: 1.0 + w, lambda: w - 1.0,
                   lambda: w * 2, lambda: float(w), lambda: w < 100.0,
                   lambda: w > 100.0):
            with pytest.raises(TypeError, match="REJECTED"):
                op()


class TestTransverseHoopGate:
    """The corrected transverse (hoop) gate: per-zone allowable on a
    transformed (modular-ratio) lateral section, loaded by the turn-moment
    DISTRIBUTION M_lat(x) = k_lat·|M_vert_env(x)|  (k_lat = V²/gR), NOT a single
    scalar.  The governing fibre is the soft light web at the hull's widest
    point — checked against the WEB's own f_t, not the top-zone strength.  The
    load scales with turn severity and vanishes at the degenerate tips, so a
    realistic turn no longer spuriously rejects a light web (the earlier scalar
    F_lat·H_CG, applied identically at every station, diverged as 1/b² at the
    bow/stern taper and was resolution-dependent)."""

    def _ev(self, h, stack, gate=True, details=False):
        return evaluate_multizone_weight(
            [3., 7.5, 12., 15.], [4., 14.], stack, (0.34, 0.67),
            h["x"], h["dx"], h["width_ft"], h["t_in"],
            h["ca"], h["cAz"], h["cIz"], h["zg"], h["sheer"], h["na"], _gp,
            coords_3=[6., 10., 14.], cum_Iy=h["cIy"], yhalf_grid=h["yh"],
            enable_hoop_gate=gate, return_details=details)

    def test_unified_kupfer_gate_rejects_web_each_independent_check_passes(self, monkeypatch):
        # The transverse hoop is now a REPORTING quantity; the binding gate is
        # the unified Kupfer biaxial criterion.  This pins the whole point of
        # Kupfer over Rankine: at a realistic turn a soft 54-pcf web passes each
        # INDEPENDENT check (longitudinal bending AND transverse hoop, both far
        # below 1.0) yet is REJECTED by the combined biaxial+shear state.
        import config
        h = _hull()
        monkeypatch.setattr(config, "MANEUVER_SPEED_MPS", 2.5, raising=False)
        monkeypatch.setattr(config, "MANEUVER_TURN_RADIUS_M", 4.0, raising=False)
        d = self._ev(h, [80, 54, 80], gate=True, details=True)
        assert float(np.max(d["hoop_util"])) < 0.3          # transverse alone: fine
        assert float(np.max(d["worst_util"])) < 1.0         # longitudinal alone: fine
        assert d["kupfer_max"] > d["rankine_max"]           # T-C interaction adds demand
        assert d["kupfer_max"] > 1.0 and d["feasible"] is False   # unified gate rejects
        # Densifying the web clears the unified gate.
        assert 0 < self._ev(h, [80, 80, 80], gate=True) < 1e5
        # Hoop demand still scales with turn severity (resolution-safe M_lat(x)).
        monkeypatch.setattr(config, "MANEUVER_SPEED_MPS", 3.0, raising=False)
        monkeypatch.setattr(config, "MANEUVER_TURN_RADIUS_M", 0.4, raising=False)
        d_sev = self._ev(h, [80, 80, 80], gate=False, details=True)
        monkeypatch.setattr(config, "MANEUVER_SPEED_MPS", 2.5, raising=False)
        monkeypatch.setattr(config, "MANEUVER_TURN_RADIUS_M", 4.0, raising=False)
        d_mild = self._ev(h, [80, 80, 80], gate=False, details=True)
        assert float(np.max(d_sev["hoop_util"])) > float(np.max(d_mild["hoop_util"]))

    def test_web_zone_governs_not_top(self):
        h = _hull()
        d = self._ev(h, [80, 54, 80], gate=False, details=True)
        i = int(np.argmax(d["hoop_util"]))
        assert d["hoop_gov_zone"][i] == 1                    # the web, not top(2)
        z = d["hoop_util_zone"][:, i]
        assert z[1] > z[0] and z[1] > z[2]                   # web has peak D/C

    def test_transformed_below_homogeneous_for_soft_web(self):
        # The soft web sheds lateral load in the transformed model, so its
        # transformed D/C is well below the homogeneous overstate.
        h = _hull()
        d = self._ev(h, [80, 54, 80], gate=False, details=True)
        i = int(np.argmax(d["hoop_util"]))
        assert d["hoop_util_zone"][1, i] < d["hoop_dc_homog"][1, i]

    def test_gate_skipped_without_lateral_geometry(self):
        # Legacy callers that omit cum_Iy/yhalf are not hoop-gated.
        h = _hull()
        w = evaluate_multizone_weight(
            [3., 7.5, 12., 15.], [4., 14.], [80, 56, 80], (0.34, 0.67),
            h["x"], h["dx"], h["width_ft"], h["t_in"],
            h["ca"], h["cAz"], h["cIz"], h["zg"], h["sheer"], h["na"], _gp,
            coords_3=[6., 10., 14.])
        assert 0 < w < 1e5


class TestNMixesCap:
    """N_mixes_max: a hard cap on the number of DISTINCT mix densities.  A
    design over the cap is rejected by the evaluator itself (a real gate), so
    it can never win even when it would be lighter."""

    def _ev(self, h, stack, n_mixes_max):
        return evaluate_multizone_weight(
            [3., 7.5, 12., 15.], [4., 14.], stack, (0.34, 0.67),
            h["x"], h["dx"], h["width_ft"], h["t_in"],
            h["ca"], h["cAz"], h["cIz"], h["zg"], h["sheer"], h["na"], _gp,
            coords_3=[6., 10., 14.], n_mixes_max=n_mixes_max)

    def test_three_distinct_rejected_under_cap_two(self):
        h = _hull()
        assert 0 < self._ev(h, [80, 70, 60], 3) < 1e5   # allowed at cap 3
        import structural
        assert structural.is_rejected(self._ev(h, [80, 70, 60], 2))  # cap 2

    def test_two_distinct_allowed_under_cap_two(self):
        h = _hull()
        assert 0 < self._ev(h, [80, 60, 80], 2) < 1e5    # I-beam, 2 distinct

    def test_no_cap_allows_three_distinct(self):
        h = _hull()
        assert 0 < self._ev(h, [80, 70, 60], None) < 1e5


# ══════════════════════════════════════════════════════════════════════════
#  End-to-end sweep wiring (worker → run_grand_optimization result dict)
# ══════════════════════════════════════════════════════════════════════════

def _mz_env(monkeypatch, max_mixes):
    import config
    monkeypatch.setattr(config, "FIXED_PADDLER_POSITIONS", None)
    monkeypatch.setattr(config, "PADDLER_POS_VARIANCE_FT", 0.0)
    monkeypatch.setattr(config, "MAX_MIXES", max_mixes)
    monkeypatch.setattr(config, "MAX_DENSITY_PCF", 80.0)
    monkeypatch.setattr(config, "FACTOR_OF_SAFETY", 1.5)
    monkeypatch.setattr(
        config, "get_mix_properties",
        lambda d, fos=1.5: {"density": float(d),
                            "strength_t": max(10.0, 18.0 * (float(d) - 55.0) + 15.0),
                            "strength_c": max(80.0, 90.0 * (float(d) - 55.0) + 120.0)})


def test_multizone_sweep_end_to_end(monkeypatch, repo_safe_cwd):
    """A multizone=True sweep must produce a result dict carrying the N-zone
    geometry (pour-line curves + zone densities) so the pour plot/report can
    draw the I-beam bands.  Uses the common 2-mix case."""
    _mz_env(monkeypatch, max_mixes=2)
    from main import run_grand_optimization
    res = run_grand_optimization(
        freeboard_min_in=6.0, thickness_options_in=[0.5],
        precision_level=1, flare_max_deg=20.0, multizone=True, num_zones=3,
        reports_dir=str(repo_safe_cwd))
    assert isinstance(res, dict), "multizone sweep found no feasible design"
    assert res["multizone"] is True
    assert res["zone_densities"] is not None and len(res["zone_densities"]) == 3
    assert res["zone_pour_lines"] is not None and len(res["zone_pour_lines"]) == 2
    assert len(res["pour_fracs"]) == 2
    assert np.all(np.isfinite(res["na_line"]))
    assert 0 < res["best_weight"] < 1e5


def test_two_mixes_three_zones_decoupled(monkeypatch, repo_safe_cwd):
    """ISSUE 2: 2 physical mixes must be pourable in 3 zones — the winning
    stack draws from only the 2 available densities but fills 3 zones (an
    I-beam repeats a density at both extremes)."""
    _mz_env(monkeypatch, max_mixes=2)          # only 2 mixes batched
    from main import run_grand_optimization
    res = run_grand_optimization(
        freeboard_min_in=6.0, thickness_options_in=[0.5],
        precision_level=1, flare_max_deg=20.0, multizone=True, num_zones=3,
        reports_dir=str(repo_safe_cwd))
    assert isinstance(res, dict)
    zd = res["zone_densities"]
    assert len(zd) == 3, "expected 3 zones"
    # at most 2 DISTINCT densities were used (only 2 mixes available)
    assert len(set(np.round(zd, 1))) <= 2


def _run_capture(monkeypatch, repo_safe_cwd, tiny_grid):
    """Run a small multizone sweep and return (total_configs, evaluated) parsed
    from the console.  Uses a tiny mix grid so the brute-force variant is fast."""
    import io
    import re
    import contextlib
    import config
    import main
    _mz_env(monkeypatch, max_mixes=2)
    monkeypatch.setattr(config.get_mix_db(), "get_structural_sweep_grid",
                        lambda **kw: [list(c) for c in tiny_grid])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        main.run_grand_optimization(
            freeboard_min_in=6.0, thickness_options_in=[0.5], precision_level=1,
            flare_max_deg=20.0, multizone=True, num_zones=3,
            reports_dir=str(repo_safe_cwd))
    out = buf.getvalue()
    total = int(re.search(r"across ~(\d+)", out).group(1))
    evaluated = int(re.search(r"([\d,]+) configurations evaluated", out)
                    .group(1).replace(",", ""))
    return total, evaluated


class TestPruningCount:
    """The bound-pruned percentage is only meaningful if total_configs is the
    TRUE un-pruned total.  The multi-zone pour-line count grows with depth, so
    a single sample at the shallowest depth undercounts it (the bug this
    guards)."""

    _GRID = [[80.0, 60.0], [75.0, 62.0], [70.0, 65.0]]

    def test_total_equals_brute_force_count(self, monkeypatch, repo_safe_cwd):
        # Disable the bound -> the sweep evaluates EVERY combo, so the actual
        # evaluated count must exactly equal the reported denominator.  This is
        # the invariant the bound-pruned % depends on; the old estimate sampled
        # the pour-line count at one depth and undercounted it.
        import main
        monkeypatch.setattr(main, "min_density_weight_bound",
                            lambda combo, vf: float("-inf"))
        total, evaluated = _run_capture(monkeypatch, repo_safe_cwd, self._GRID)
        assert evaluated == total, (
            f"total_configs {total} != brute-force evaluated {evaluated}; "
            "the un-pruned denominator is wrong")


class TestGunwaleModeling:
    """Both the single-zone and (now) multi-zone evaluators model the gunwale
    rails through the shared gunwale_rail_section, so the gw_w × gw_h grid is a
    REAL design axis on either path — it must be swept, and different grid
    points must give genuinely different sections/weights."""

    _GRID = [[80.0, 60.0], [75.0, 62.0], [70.0, 65.0]]

    def _sweep(self, monkeypatch, repo_safe_cwd, multizone, enable_gw, steps=5):
        import io
        import re
        import contextlib
        import config
        import main
        _mz_env(monkeypatch, max_mixes=2)
        monkeypatch.setattr(config.get_mix_db(), "get_structural_sweep_grid",
                            lambda **kw: [list(c) for c in self._GRID])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            res = main.run_grand_optimization(
                freeboard_min_in=6.0, thickness_options_in=[0.5],
                precision_level=1, flare_max_deg=20.0,
                multizone=multizone, num_zones=(3 if multizone else None),
                enable_gunwales=enable_gw, gw_steps=steps,
                reports_dir=str(repo_safe_cwd))
        total = int(re.search(r"across ~(\d+)", buf.getvalue()).group(1))
        return total, res

    def test_multizone_sweeps_gunwale_grid(self, monkeypatch, repo_safe_cwd):
        steps = 5
        on_total, _ = self._sweep(monkeypatch, repo_safe_cwd, True, True, steps)
        off_total, _ = self._sweep(monkeypatch, repo_safe_cwd, True, False, steps)
        # Gunwales are modeled on the multi-zone path now, so the grid is REAL:
        # gw_steps² more configs (no longer collapsed).
        assert on_total == steps * steps * off_total, (
            f"multi-zone gunwale grid not swept: {on_total} != "
            f"{steps*steps} × {off_total}")

    def test_singlezone_still_sweeps_gunwale_grid(self, monkeypatch, repo_safe_cwd):
        steps = 5
        on_total, _ = self._sweep(monkeypatch, repo_safe_cwd, False, True, steps)
        off_total, _ = self._sweep(monkeypatch, repo_safe_cwd, False, False, steps)
        assert on_total == steps * steps * off_total, (
            f"single-zone gunwale grid wrong: {on_total} != {steps*steps} × {off_total}")

    def test_gunwale_size_changes_multizone_weight(self):
        """A genuinely-modeled rail must change the multi-zone section/weight:
        at least two grid points give different weights."""
        from geometry import (parse_godzilla_dimensions, parse_godzilla_offsets,
                              calculate_shell_properties)
        from structural import evaluate_multizone_weight, multizone_pour_grids
        root = Path(__file__).resolve().parent.parent
        if not ((root / "off1.mlt").exists() and (root / "out.mlt").exists()):
            pytest.skip("reference hull not present")
        L_ft, draft_in = parse_godzilla_dimensions(str(root / "out.mlt"))
        # draft + 9.5 in, not + 8.0: the loaded-freeboard gate is now
        # per-configuration, and at draft+8.0 (12.58 in mold depth) the
        # heaviest hull that clears the 6 in ASCE rule with a 640 lb crew
        # is 70 lb -- no real design exists there, whatever it is made of.
        # draft+9.5 admits 281 lb, so these fixtures test what they claim
        # to test (worker mechanics) rather than the freeboard rule.
        t_in, d_in = 0.5, draft_in + 9.5
        xr, zr, y = parse_godzilla_offsets(str(root / "off1.mlt"), L_ft=L_ft,
                                           draft_in=draft_in, mold_depth_in=d_in)
        p = calculate_shell_properties(xr, zr, y, t_in)
        A, At, Ab, I, Q, ct, cb, wf = p
        v = A > 0
        x = xr[v]

        def props(dd, fos=1.5):
            dd = float(dd)
            return {"density": dd, "strength_t": max(10.0, 18.0*(dd-55)+15),
                    "strength_c": max(80.0, 90.0*(dd-55)+120)}

        def w(gw_w, gw_h):
            return evaluate_multizone_weight(
                np.array([3., 7.5, 12., 15.]), np.array([4., 14.]),
                (80.0, 70.0, 80.0), multizone_pour_grids(d_in, 3)[0],
                x, np.max(x)/(len(xr)-1), wf[v], t_in, p.cum_area_in2[v],
                p.cum_Az_in3[v], p.cum_Iz_in4[v], p.z_grid_in, cb[v]+ct[v], cb[v],
                props, coords_3=np.array([6., 10., 14.]), position_variance_ft=0.0,
                gw_width_mid=gw_w, gw_height_mid=gw_h, gw_fillet_ratio=1.0)

        w_none, w_small, w_big = w(0.0, 0.0), w(0.5, 0.5), w(2.5, 3.0)
        assert w_none < w_small < w_big, (
            f"rail did not add monotone weight: {w_none} {w_small} {w_big}")

