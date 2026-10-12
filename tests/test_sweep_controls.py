# tests/test_sweep_controls.py
"""
Sweep usability + correctness fixes (2026-07-13):

  * buildable_thickness_grid — 1/32 in AND 0.5 mm constructible increments
  * multi-mix flare gate — 2- and 3-mix sweeps no longer reject every combo
  * stop_event — the Stop button actually aborts the sweep
  * progress — starts at 0, reports CONFIGURATION count + ETA, monotonic

Mix strengths are monkeypatched so the user's live concrete_mixes.db
regression never decides whether these tests pass.
"""
from __future__ import annotations

import threading

import numpy as np
import pytest

import config
from structural import (buildable_thickness_grid, describe_layer_breakdown,
                        _IN_PER_32ND, _IN_PER_HALF_MM)

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



def _stable_mixes(monkeypatch):
    monkeypatch.setattr(
        config, "get_mix_properties",
        lambda d, fos=1.5: {
            "density": float(d),
            "strength_t": max(20.0, 8.0 * (float(d) - 40.0) + 40.0) / fos,
            "strength_c": max(100.0, 40.0 * (float(d) - 40.0) + 200.0) / fos})
    # Pin EVERY run-level config the sweep's FEASIBILITY depends on, so these
    # tests are order-independent.  A prior test (any that runs a sweep --
    # StructuralService/run_grand write these onto the shared config singleton
    # and never restore them) could otherwise leak a heavy load case, a high
    # dynamic factor, a severe turn, or a GMT floor, and the unified Kupfer gate
    # (stricter than the old independent checks) would then reject the whole
    # worker grid ("0 configurations survived").  monkeypatch restores them all.
    # NEVER monkeypatch RACE_4/2/3_PERSON.  They are DERIVED on read via the
    # PEP 562 module __getattr__, and monkeypatch's restore is what breaks
    # them: it reads the old value THROUGH the hook (getting a derived list),
    # then on teardown writes that list back with setattr -- storing a derived
    # value in config.__dict__, where it permanently shadows the derivation.
    # The split-brain returns, and test_load_case_expansion catches it as
    # "RACE_4_PERSON is a STORED module attribute again" -- but only if it runs
    # AFTER this file, which is why the suite passed forward and failed
    # reversed.
    #
    # RACE_CATEGORIES is the single source of truth, it is in
    # RUN_LEVEL_CONFIG_ATTRS, and the aliases follow it automatically.
    _cats = {k: {**v, "weights_lb": list(w)} for (k, v), w in zip(
        sorted(config.RACE_CATEGORIES.items()),
        ([145.0, 135.0], [175.0, 165.0], [160.0, 160.0, 160.0],
         [175.0, 170.0, 165.0], [160.0, 150.0, 170.0, 160.0]))}
    monkeypatch.setattr(config, "RACE_CATEGORIES", _cats, raising=False)
    monkeypatch.setattr(config, "DYNAMIC_LOAD_FACTOR", 1.2, raising=False)
    monkeypatch.setattr(config, "MAX_DENSITY_PCF", 80.0, raising=False)
    monkeypatch.setattr(config, "ADVANCED_PADDLER_LOADING", False, raising=False)
    monkeypatch.setattr(config, "PADDLER_LOAD_ENVELOPE", True, raising=False)
    monkeypatch.setattr(config, "MANEUVER_SPEED_MPS", 2.5, raising=False)
    monkeypatch.setattr(config, "MANEUVER_TURN_RADIUS_M", 4.0, raising=False)
    monkeypatch.setattr(config, "MIN_GMT_IN", 0.0, raising=False)
    monkeypatch.setattr(config, "FACTOR_OF_SAFETY", 1.5, raising=False)
    monkeypatch.setattr(config, "LONGITUDINAL_STATIONS", None, raising=False)


# ══════════════════════════════════════════════════════════════════════════
#  Buildable thickness grid
# ══════════════════════════════════════════════════════════════════════════

class TestThicknessGrid:
    def test_every_value_is_constructible(self):
        g = buildable_thickness_grid(0.375, 0.75)
        assert len(g) > 8
        for v in g:
            on_32 = abs(round(v / _IN_PER_32ND) - v / _IN_PER_32ND) < 1e-6
            on_mm = abs(round(v / _IN_PER_HALF_MM) - v / _IN_PER_HALF_MM) < 1e-6
            assert on_32 or on_mm, f"{v} is on neither the 1/32in nor 0.5mm grid"

    def test_sorted_within_bounds_and_deduped(self):
        g = buildable_thickness_grid(0.375, 0.625)
        assert g == sorted(g)
        assert g[0] >= 0.375 - 1e-9 and g[-1] <= 0.625 + 1e-9
        assert all(b - a >= 0.005 - 1e-12 for a, b in zip(g, g[1:]))

    def test_contains_both_imperial_and_metric_marks(self):
        g = np.array(buildable_thickness_grid(0.375, 0.625))
        # 1/2 in = 0.5 (imperial) and 13 mm = 0.51181 in (metric) both present
        assert np.any(np.abs(g - 0.5) < 1e-6), "imperial 1/2in missing"
        assert np.any(np.abs(g - 13.0 / 25.4) < 1e-6), "metric 13mm missing"

    def test_reversed_bounds_and_degenerate(self):
        assert buildable_thickness_grid(0.625, 0.375) == \
            buildable_thickness_grid(0.375, 0.625)
        one = buildable_thickness_grid(0.5, 0.5)
        assert len(one) >= 1

    @pytest.mark.parametrize("n", [1, 2, 3])
    def test_every_total_divides_into_constructible_layers(self, n):
        """Option B: total ÷ n_layers must itself land on the 1/32in or
        0.5mm grid, so each equal troweled layer is buildable."""
        g = buildable_thickness_grid(0.375, 0.75, n_layers=n)
        assert len(g) >= 3
        for total in g:
            per = total / n
            on32 = abs(round(per / _IN_PER_32ND) - per / _IN_PER_32ND) < 1e-4
            onmm = abs(round(per / _IN_PER_HALF_MM) - per / _IN_PER_HALF_MM) < 1e-4
            assert on32 or onmm, \
                f"n={n}: total {total:.4f} -> layer {per:.4f} not constructible"

    def test_n_layers_1_equals_plain_grid(self):
        assert buildable_thickness_grid(0.375, 0.625, n_layers=1) == \
            buildable_thickness_grid(0.375, 0.625)

    def test_more_layers_is_coarser_or_equal(self):
        g1 = buildable_thickness_grid(0.375, 0.75, n_layers=1)
        g3 = buildable_thickness_grid(0.375, 0.75, n_layers=3)
        assert len(g3) <= len(g1)

    def test_narrow_range_fallback_not_below_min(self):
        g = buildable_thickness_grid(0.50, 0.51, n_layers=3)
        assert len(g) == 1 and g[0] >= 0.50 - 1e-9

    def test_describe_layer_breakdown_imperial_and_metric(self):
        assert describe_layer_breakdown(0.46875, 3) == '3 layers × 0.156" (5/32")'
        # 3 layers of 5.0 mm = 15 mm total; described in mm, not a clean 1/32
        s = describe_layer_breakdown(3 * 5.0 / 25.4, 3)
        assert "mm" in s and "3 layers" in s


# ══════════════════════════════════════════════════════════════════════════
#  Multi-mix flare gate (2-mix no longer fails where 1-mix passes)
# ══════════════════════════════════════════════════════════════════════════

class TestMultiMixSweep:
    @pytest.fixture(autouse=True)
    def _publish_overhang(self, monkeypatch):
        """
        These call _evaluate_geometry_worker DIRECTLY, and only
        run_grand_optimization publishes the stem overhang (main.py:2004/2077).
        They passed in a full run purely because an earlier test FILE had left
        it on the config module -- cross-file pollution, invisible until the
        file is run alone.

        _struct_rocker_ft() refuses to inherit a silent zero, which is exactly
        the guard for a directly-called evaluator; publish the hull's own value
        so the test states its own preconditions.
        """
        # Plain assignment, NOT monkeypatch: STEM_OVERHANG_IN is now in
        # RUN_LEVEL_CONFIG_ATTRS, so the autouse conftest snapshot owns its
        # cleanup. Letting monkeypatch manage it too means BOTH try to delete
        # it on teardown and the second raises AttributeError.
        config.STEM_OVERHANG_IN = 1.045163

    @pytest.mark.parametrize("n_mixes", [1, 2, 3])
    def test_worker_finds_a_config(self, monkeypatch, n_mixes):
        _stable_mixes(monkeypatch)
        monkeypatch.setattr(config, "FIXED_PADDLER_POSITIONS", None)
        monkeypatch.setattr(config, "PADDLER_POS_VARIANCE_FT", 0.5)
        monkeypatch.setattr(config, "MAX_MIXES", n_mixes)

        from main import _evaluate_geometry_worker
        from geometry import parse_godzilla_dimensions
        L_ft, draft_in = parse_godzilla_dimensions("out.mlt")
        grid = config.get_mix_db().get_structural_sweep_grid(
            fos=1.5, max_density=80.0, n_mixes=n_mixes)
        # draft + 9.5 in, not + 8.0: the loaded-freeboard gate is now
        # per-configuration, and at draft+8.0 (12.58 in mold depth) the
        # heaviest hull that clears the 6 in ASCE rule with a 640 lb crew
        # is 70 lb -- no real design exists there, whatever it is made of.
        # draft+9.5 admits 281 lb, so these fixtures test what they claim
        # to test (worker mechanics) rather than the freeboard rule.
        args = (0.5, draft_in + 9.5, grid, L_ft, draft_in, "off1.mlt",
                0.0, 0.0, False, 1.0, 6.0, 30.0, None, False, 3)
        weight, best_config, _n, _gate_cands = _evaluate_geometry_worker(args)
        assert best_config is not None, \
            f"{n_mixes}-mix task rejected every combo (flare-gate regression)"
        assert 0 < weight < 1e5
        assert len(best_config["mixes"]) == n_mixes

    def test_more_mixes_not_heavier(self, monkeypatch):
        """More mixes = more freedom, so the optimum cannot get heavier."""
        _stable_mixes(monkeypatch)
        monkeypatch.setattr(config, "FIXED_PADDLER_POSITIONS", None)
        monkeypatch.setattr(config, "PADDLER_POS_VARIANCE_FT", 0.5)
        from main import _evaluate_geometry_worker
        from geometry import parse_godzilla_dimensions
        L_ft, draft_in = parse_godzilla_dimensions("out.mlt")

        weights = {}
        for n in (1, 2):
            monkeypatch.setattr(config, "MAX_MIXES", n)
            grid = config.get_mix_db().get_structural_sweep_grid(
                fos=1.5, max_density=80.0, n_mixes=n)
            args = (0.5, draft_in + 9.5, grid, L_ft, draft_in, "off1.mlt",
                    0.0, 0.0, False, 1.0, 6.0, 30.0, None, False, 3)
            weights[n] = _evaluate_geometry_worker(args)[0]
        assert weights[2] <= weights[1] + 1e-6, \
            f"2-mix ({weights[2]:.1f}) heavier than 1-mix ({weights[1]:.1f})"


# ══════════════════════════════════════════════════════════════════════════
#  Stop event
# ══════════════════════════════════════════════════════════════════════════

class TestStopAndProgress:
    def _cfg(self, monkeypatch):
        _stable_mixes(monkeypatch)
        monkeypatch.setattr(config, "FIXED_PADDLER_POSITIONS", None)
        monkeypatch.setattr(config, "PADDLER_POS_VARIANCE_FT", 0.0)
        monkeypatch.setattr(config, "MAX_MIXES", 1)
        monkeypatch.setattr(config, "MAX_DENSITY_PCF", 80.0)

    def test_stop_before_start_returns_none(self, monkeypatch, repo_safe_cwd):
        self._cfg(monkeypatch)
        from main import run_grand_optimization
        stop = threading.Event()
        stop.set()                       # already stopped
        res = run_grand_optimization(
            freeboard_min_in=6.0, thickness_options_in=[0.4, 0.45, 0.5],
            flare_max_deg=20.0, stop_event=stop, reports_dir=str(repo_safe_cwd))
        assert res is None

    def test_progress_starts_at_zero_with_config_count_and_eta(
            self, monkeypatch, repo_safe_cwd):
        self._cfg(monkeypatch)
        from main import run_grand_optimization
        prog = []
        res = run_grand_optimization(
            progress_callback=lambda f, m: prog.append((f, m)),
            freeboard_min_in=6.0, thickness_options_in=[0.4, 0.45, 0.5],
            flare_max_deg=20.0, reports_dir=str(repo_safe_cwd))
        assert isinstance(res, dict)
        assert prog, "no progress callbacks fired"
        # starts low (not 60%), ends at 100%
        assert prog[0][0] < 0.5
        assert prog[-1][0] == pytest.approx(1.0)
        # measured in configurations, with an ETA, and monotonic
        assert "Configuration" in prog[0][1] and "ETA" in prog[0][1]
        assert all(a[0] <= b[0] + 1e-9 for a, b in zip(prog, prog[1:]))
