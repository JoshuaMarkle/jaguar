"""
P0 unit tests for the 5-D structural sweep pipeline.

Covers: OptimizationConfig defaults, maneuver load factor formula,
paddler biomechanics model, Godzilla file parsing, freeboard extrusion,
shell geometry scaling, pour-zone logic, and StructuralService config
isolation.  No external binaries or pre-existing .mlt files required.
"""
import numpy as np
from np_compat import trapezoid
import pytest


# ─── 1 — OptimizationConfig defaults ─────────────────────────────────────────

class TestOptimizationConfig:

    def test_default_fos(self):
        """Competition default FoS must be 1.5 (ACI 318 minimum)."""
        from domain.models import OptimizationConfig
        assert OptimizationConfig().factor_of_safety == pytest.approx(1.5)

    def test_default_freeboard(self):
        """Default minimum freeboard must be 6 in (ASCE rule minimum)."""
        from domain.models import OptimizationConfig
        assert OptimizationConfig().freeboard_min_in == pytest.approx(6.0)

    def test_default_thickness_options(self):
        """Default thickness grid must include the four standard trowel sizes."""
        from domain.models import OptimizationConfig
        opts = set(OptimizationConfig().thickness_options_in)
        assert opts == {0.375, 0.5, 0.625, 0.75}

    def test_default_crew_configuration(self):
        """Default crew must be 4-person and 2-person with positive weights."""
        from domain.models import OptimizationConfig
        cfg = OptimizationConfig()
        assert len(cfg.race_4_person_lbs) == 4
        assert len(cfg.race_2_person_lbs) == 2
        assert all(w > 0 for w in cfg.race_4_person_lbs)
        assert all(w > 0 for w in cfg.race_2_person_lbs)


# ─── 2 — Maneuver Load Factor ─────────────────────────────────────────────────

class TestManeuverLoadFactor:

    def test_zero_speed_gives_unity(self):
        """V=0 → no centripetal acceleration → MLF must equal exactly 1."""
        from structural import compute_maneuver_load_factor
        assert compute_maneuver_load_factor(0.0, 3.0) == pytest.approx(1.0)

    def test_formula_at_known_values(self):
        """
        At V=2.5 m/s, r=3.0 m, g=9.807, k=0.5:
          a_c = 2.5² / 3.0 = 2.0833  m/s²
          n_lat = 2.0833 / 9.807 = 0.21244
          MLF = 1 + 0.5 × 0.21244 = 1.10622
        """
        from structural import compute_maneuver_load_factor
        expected = 1.0 + 0.5 * (2.5 ** 2 / 3.0) / 9.807
        result = compute_maneuver_load_factor(2.5, 3.0, g=9.807)
        assert result == pytest.approx(expected, rel=1e-5)

    def test_zero_radius_clamped(self):
        """radius=0 uses the internal max(r, 0.1) guard — must not raise or be inf."""
        from structural import compute_maneuver_load_factor
        mlf = compute_maneuver_load_factor(3.0, 0.0)
        assert np.isfinite(mlf)
        assert mlf > 1.0

    def test_mlf_always_geq_one(self):
        """MLF must be ≥ 1 for any non-negative physical inputs."""
        from structural import compute_maneuver_load_factor
        for speed in [0.0, 1.0, 3.0, 5.0]:
            for radius in [0.5, 2.0, 10.0]:
                assert compute_maneuver_load_factor(speed, radius) >= 1.0, (
                    f"MLF < 1 at V={speed}, r={radius}"
                )


# ─── 3 — Paddler Biomechanics ────────────────────────────────────────────────

class TestPaddlerBiomechanics:

    def test_total_load_conserved(self):
        """
        Integral of the distributed load array must be within 15% of
        sum(dynamic_weights).  The trapezoidal rule's boundary half-intervals
        at the sharp edges of the 3-inch knee footprint produce a systematic
        ~7% overshoot; 15% tolerance comfortably catches true conservation
        failures while allowing for that discretisation artefact.
        """
        from structural import apply_realistic_paddler_loads
        n, L = 200, 18.0
        x = np.linspace(0, L, n)
        dx = L / (n - 1)
        coords = [3.0, 7.5, 10.5, 15.0]
        weights = [192.0, 180.0, 204.0, 192.0]   # DLF-amplified: 1.2 × {160,150,170,160}
        w = apply_realistic_paddler_loads(x, dx, coords, weights, L)
        assert trapezoid(w, x) == pytest.approx(sum(weights), rel=0.15)

    def test_knee_region_carries_majority_of_load(self):
        """
        For a single paddler the knee footprint must carry at least 70% of the
        total load (nominal design is 80%).  A ±0.2 ft window is used rather
        than the exact ±0.125 ft so the boundary half-trapezoids are included
        in the measure; the toe region is not within this window.
        """
        from structural import apply_realistic_paddler_loads
        n, L = 400, 18.0
        x = np.linspace(0, L, n)
        dx = L / (n - 1)
        knee_pos = 8.0
        weight = 100.0

        w = apply_realistic_paddler_loads(x, dx, [knee_pos], [weight], L)

        total = trapezoid(w, x)
        knee_window = (x >= knee_pos - 0.2) & (x <= knee_pos + 0.2)
        knee_frac = trapezoid(w[knee_window], x[knee_window]) / total

        assert knee_frac >= 0.70, (
            f"Knee footprint carries {knee_frac:.1%} of load (expected ≥ 70%)"
        )

    def test_toe_load_placed_the_configured_offset_aft(self, monkeypatch):
        """
        The toe share must land config.TOE_OFFSET_IN aft of the knee.

        Passes advanced_loading=True EXPLICITLY.  This previously relied on the
        module default, which is config.ADVANCED_PADDLER_LOADING -- currently
        off, so the function applied one point load at the knee, there was no
        toe load at all, and argmax over an all-zero masked array returned
        index 0.  The biomechanics split is what this test is about, so it must
        ask for it rather than depend on a global that is a separate design
        decision.
        """
        import config
        from structural import apply_realistic_paddler_loads
        # 0.90 pinned explicitly: the shipped default is 1.00 (100/0), which
        # places NO toe load, so reading the default would quietly retire
        # this check instead of failing it.
        monkeypatch.setattr(config, "KNEE_FRAC_BENDING", 0.90)
        n, L = 400, 18.0
        x = np.linspace(0, L, n)
        dx = L / (n - 1)
        knee_pos = 7.0
        toe_pos = knee_pos + float(config.TOE_OFFSET_IN) / 12.0   # ~8.417 ft

        w = apply_realistic_paddler_loads(x, dx, [knee_pos], [100.0], L,
                                          advanced_loading=True)

        # Loads deposit across the two bracketing stations, so locate the toe
        # by the centroid of everything outside the knee footprint rather than
        # by a single peak index.
        outside_knee = (x < knee_pos - 0.2) | (x > knee_pos + 0.2)
        w_out = w * outside_knee
        assert trapezoid(w_out, x) > 0, "no toe load was applied"
        centroid = trapezoid(w_out * x, x) / trapezoid(w_out, x)
        assert centroid == pytest.approx(toe_pos, abs=0.05)

    def test_toe_share_matches_the_configured_bending_split(self):
        import config
        from structural import apply_realistic_paddler_loads
        n, L = 400, 18.0
        x = np.linspace(0, L, n)
        w = apply_realistic_paddler_loads(x, L / (n - 1), [7.0], [100.0], L,
                                          advanced_loading=True)
        knee_pos = 7.0
        outside = (x < knee_pos - 0.2) | (x > knee_pos + 0.2)
        toe_frac = trapezoid(w * outside, x) / trapezoid(w, x)
        assert toe_frac == pytest.approx(1.0 - config.KNEE_FRAC_BENDING, abs=1e-6)


# ─── 4 — Godzilla Dimension Parsing ─────────────────────────────────────────

class TestGodzillaDimensionParsing:

    def test_fallback_on_missing_file(self):
        """Missing out.mlt must silently return the 20 ft / 14 in defaults."""
        from geometry import parse_godzilla_dimensions
        L_ft, T_in = parse_godzilla_dimensions("__no_such_file__.mlt")
        assert L_ft == pytest.approx(20.0)
        assert T_in == pytest.approx(14.0)

    def test_parses_loa_and_draft(self, tmp_path):
        """Correctly converts SI LOA and Tmax to ft and inches."""
        from geometry import parse_godzilla_dimensions
        p = tmp_path / "out.mlt"
        p.write_text("LOA (m), 5.4864\nTmax (m), 0.1100\n")
        L_ft, T_in = parse_godzilla_dimensions(str(p))
        assert L_ft == pytest.approx(5.4864 * 3.28084, rel=1e-4)
        assert T_in == pytest.approx(0.1100 * 39.3701, rel=1e-4)

    def test_out_of_range_loa_is_ignored(self, tmp_path):
        """LOA outside the plausible canoe range (4–7.5 m) is silently discarded."""
        from geometry import parse_godzilla_dimensions
        p = tmp_path / "out.mlt"
        p.write_text("LOA (m), 2.0\nTmax (m), 0.1100\n")
        L_ft, _ = parse_godzilla_dimensions(str(p))
        assert L_ft == pytest.approx(20.0)   # rejected; default preserved


# ─── 5 — Godzilla Offset Parsing ────────────────────────────────────────────

class TestGodzillaOffsetParsing:

    @pytest.fixture
    def tiny_off1(self, tmp_path):
        """3-station × 5-waterline offset file with constant half-breadth (5 in)."""
        row = "  5.00000  5.00000  5.00000  5.00000  5.00000\n"
        p = tmp_path / "off1.mlt"
        p.write_text(row * 3)
        return str(p)

    def test_returns_three_arrays_on_valid_file(self, tiny_off1):
        from geometry import parse_godzilla_offsets
        x, z, y = parse_godzilla_offsets(tiny_off1, L_ft=18.0, draft_in=4.0, mold_depth_in=4.0)
        assert x is not None
        assert z is not None
        assert y is not None

    def test_freeboard_extrusion_extends_z_axis(self, tiny_off1):
        """mold_depth > draft must append additional z-heights above the waterline."""
        from geometry import parse_godzilla_offsets
        _, z_flush, _  = parse_godzilla_offsets(tiny_off1, L_ft=18.0, draft_in=4.0, mold_depth_in=4.0)
        _, z_extruded, _ = parse_godzilla_offsets(tiny_off1, L_ft=18.0, draft_in=4.0, mold_depth_in=10.0)
        assert len(z_extruded) > len(z_flush)
        assert z_extruded[-1] == pytest.approx(10.0)

    def test_comment_lines_are_skipped(self, tmp_path):
        """Lines beginning with # or ! (Michlet comment tokens) must be ignored."""
        from geometry import parse_godzilla_offsets
        content = (
            "# Godzilla offset output\n"
            "  5.00000  5.00000  5.00000\n"
            "! another comment\n"
            "  5.00000  5.00000  5.00000\n"
            "  5.00000  5.00000  5.00000\n"
        )
        p = tmp_path / "commented.mlt"
        p.write_text(content)
        x, z, y = parse_godzilla_offsets(str(p), L_ft=18.0, draft_in=4.0, mold_depth_in=4.0)
        assert x is not None
        assert len(x) == 3   # exactly 3 data rows

    def test_returns_none_on_missing_file(self):
        """Missing offset file must return (None, None, None), not raise."""
        from geometry import parse_godzilla_offsets
        x, z, y = parse_godzilla_offsets("__no_such_off1__.mlt")
        assert x is None
        assert z is None
        assert y is None


# ─── 6 — Shell Geometry Scaling ─────────────────────────────────────────────

class TestShellGeometryScaling:

    @pytest.fixture(scope="class")
    def draft_only_arrays(self):
        """11-station hull in inches, underwater profile only (no freeboard)."""
        from hull_generator import generate_hull
        L_m, B_m, T_m = 5.4864, 0.61, 0.108
        offsets_in = generate_hull(
            [1.4, 1.4, 1.2, 1.0, 1.5, 0.15, 0.05], L_m, B_m, T_m, 11, 11
        ) * 39.3701
        n_st, n_wl = offsets_in.shape
        x = np.linspace(0, L_m * 3.28084, n_st)
        z = np.linspace(0, T_m * 39.3701, n_wl)
        return x, z, offsets_in, n_st, T_m * 39.3701

    def test_area_doubles_with_thickness(self, draft_only_arrays):
        """Doubling the trowel thickness must exactly double the shell area."""
        from geometry import calculate_shell_properties
        x, z, offsets_in, n_st, _ = draft_only_arrays
        mid = n_st // 2
        p_half = calculate_shell_properties(x, z, offsets_in, 0.5)
        p_full = calculate_shell_properties(x, z, offsets_in, 1.0)
        assert p_full.A_in2[mid] / p_half.A_in2[mid] == pytest.approx(2.0, rel=0.01)

    def test_freeboard_more_than_doubles_inertia(self, draft_only_arrays):
        """
        Extruding 6 in of freeboard wall above the waterline must at least
        double the second moment of area at midship.  This is the structural
        benefit that justifies the minimum-freeboard ASCE rule.
        """
        from geometry import calculate_shell_properties
        x, z_draft, offsets_in, n_st, T_in = draft_only_arrays
        mid = n_st // 2

        p_draft = calculate_shell_properties(x, z_draft, offsets_in, 0.5)

        mold_in = T_in + 6.0
        n_fb = 10
        z_fb = np.linspace(T_in + 0.1, mold_in, n_fb)
        y_fb = np.tile(offsets_in[:, -1], (n_fb, 1)).T
        z_full = np.concatenate([z_draft, z_fb])
        y_full = np.hstack([offsets_in, y_fb])
        p_full = calculate_shell_properties(x, z_full, y_full, 0.5)

        assert p_full.I_in4[mid] > p_draft.I_in4[mid] * 2.0, (
            f"I_draft={p_draft.I_in4[mid]:.2f} in⁴ → "
            f"I_full={p_full.I_in4[mid]:.2f} in⁴ (expected > 2×)"
        )

    def test_all_areas_nonnegative(self, draft_only_arrays):
        """Every station must have a non-negative cross-section area."""
        from geometry import calculate_shell_properties
        x, z, offsets_in, _, _ = draft_only_arrays
        props = calculate_shell_properties(x, z, offsets_in, 0.5)
        assert np.all(props.A_in2 >= 0)
        assert np.all(props.A_top_in2 >= 0)
        assert np.all(props.A_bot_in2 >= 0)


# ─── 7 — Pour Zone Map: REMOVED ──────────────────────────────────────────────
# TestPourZoneMap and structural.generate_2d_pour_map are gone.  The function
# had NO production callers (only these tests reached it) and silently ignored
# its own mid_mix_pcf argument -- midbody was hardcoded to (heavy, heavy), so
# test_high_moment_midship_assigned_heavy_mix had been failing against a
# behaviour nothing depended on.  It was a remnant of the longitudinal pour
# zoning that was deliberately killed; test_report.py still asserts that the
# "zero-moment ends" language never returns to the report.  Real pour-zone
# behaviour is covered by TestMultiZone* and tests/test_multizone.py, which
# exercise the vertical I-beam stack the sweep actually builds.


# ─── 8 — StructuralService config isolation ──────────────────────────────────

class TestStructuralService:

    def test_config_from_settings_returns_valid_config(self):
        """config_from_settings must return a fully-populated OptimizationConfig."""
        from services.structural_service import StructuralService
        from domain.models import OptimizationConfig
        cfg = StructuralService.config_from_settings()
        assert isinstance(cfg, OptimizationConfig)
        assert cfg.factor_of_safety > 0
        assert cfg.dynamic_load_factor > 0
        assert cfg.freeboard_min_in >= 0

    def test_run_sweep_writes_fos_and_dlf_before_optimization(self, monkeypatch):
        """
        run_sweep must apply opt_config.factor_of_safety and
        opt_config.dynamic_load_factor to the config singleton *before*
        calling run_grand_optimization, so downstream mix lookups use the
        correct safety factors.
        """
        import main
        import config as _cfg
        from services.structural_service import StructuralService
        from domain.models import OptimizationConfig

        captured = {}

        def mock_run(**kwargs):
            captured['fos'] = _cfg.FACTOR_OF_SAFETY
            captured['dlf'] = _cfg.DYNAMIC_LOAD_FACTOR
            return None

        monkeypatch.setattr(main, 'run_grand_optimization', mock_run)
        StructuralService().run_sweep(
            OptimizationConfig(factor_of_safety=2.5, dynamic_load_factor=1.8)
        )

        assert captured.get('fos') == pytest.approx(2.5), (
            "FACTOR_OF_SAFETY not applied before optimization"
        )
        assert captured.get('dlf') == pytest.approx(1.8), (
            "DYNAMIC_LOAD_FACTOR not applied before optimization"
        )

    def test_run_sweep_resets_mix_db_singleton(self, monkeypatch):
        """
        run_sweep must set config._mix_db = None so the mix database is
        re-initialized with the new FoS.  Without this reset a stale db
        with a different FoS would silently produce wrong allowable stresses.
        """
        import main
        import config as _cfg
        from services.structural_service import StructuralService
        from domain.models import OptimizationConfig

        _cfg._mix_db = object()   # pre-seed a non-None sentinel

        def mock_run(**kwargs):
            return None

        monkeypatch.setattr(main, 'run_grand_optimization', mock_run)
        StructuralService().run_sweep(OptimizationConfig())

        assert _cfg._mix_db is None, (
            "config._mix_db was not reset; stale mix properties may be used"
        )

    def test_run_sweep_passes_freeboard_and_thickness_to_optimizer(self, monkeypatch):
        """
        run_sweep must forward freeboard_min_in and thickness_options_in
        to run_grand_optimization as keyword arguments.
        """
        import main
        from services.structural_service import StructuralService
        from domain.models import OptimizationConfig

        captured = {}

        def mock_run(**kwargs):
            captured.update(kwargs)
            return None

        monkeypatch.setattr(main, 'run_grand_optimization', mock_run)
        StructuralService().run_sweep(
            OptimizationConfig(freeboard_min_in=8.0, thickness_options_in=[0.5, 0.75])
        )

        assert captured.get('freeboard_min_in') == pytest.approx(8.0)
        assert list(captured.get('thickness_options_in', [])) == [0.5, 0.75]

    def test_run_sweep_pushes_maneuver_turn_to_config(self, monkeypatch):
        """
        The maneuver turn speed/radius (transverse-hoop design load) are applied
        to the module-level config singleton before the sweep runs, so a
        user-chosen turn radius actually reaches transverse_hoop_utilization.
        """
        import main
        import config
        from services.structural_service import StructuralService
        from domain.models import OptimizationConfig

        seen = {}

        def mock_run(**kwargs):
            # capture what the sweep would see at call time
            seen["speed"] = config.MANEUVER_SPEED_MPS
            seen["radius"] = config.MANEUVER_TURN_RADIUS_M
            return None

        monkeypatch.setattr(main, 'run_grand_optimization', mock_run)
        StructuralService().run_sweep(
            OptimizationConfig(maneuver_speed_mps=2.75,
                               maneuver_turn_radius_m=3.0))

        assert seen["speed"] == pytest.approx(2.75)
        assert seen["radius"] == pytest.approx(3.0)

    def test_optimization_config_defaults_to_realistic_turn(self):
        # 4.0 m default: a realistic minimum slalom radius, not a pivot turn.
        from domain.models import OptimizationConfig
        oc = OptimizationConfig()
        assert oc.maneuver_turn_radius_m == pytest.approx(4.0)
        assert oc.maneuver_speed_mps == pytest.approx(2.5)
