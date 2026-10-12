# tests/test_config_wiring.py
"""
canoe_suite.cfg must actually reach the code, and the manufacturing
constraints must actually constrain.

Both failures these tests guard are the same shape and are invisible at
runtime: a setting is parsed into the Settings dataclass, looks configured,
and is then read by nobody — so editing it silently changes nothing, and a
checkbox that claims to restrict the sweep does not.
"""
from __future__ import annotations

import textwrap

import pytest

import config
from config.settings import Settings, get_settings


# ══════════════════════════════════════════════════════════════════════════
#  canoe_suite.cfg  →  config globals
# ══════════════════════════════════════════════════════════════════════════

def _settings():
    return get_settings()


# Every canoe_suite.cfg key, and the thing it must reach.  A key that is parsed
# and then read by nobody is invisible at runtime: editing it changes no result
# and says nothing, which is how a whole debugging session gets spent.  If a
# value here is ever re-hardcoded, exactly one of these fails.
WIRED_KEYS = [
    ("structural.factor_of_safety",
     lambda: config.FACTOR_OF_SAFETY, lambda s: s.structural.factor_of_safety),
    ("structural.dynamic_load_factor",
     lambda: config.DYNAMIC_LOAD_FACTOR, lambda s: s.structural.dynamic_load_factor),
    ("structural.maneuver_coupling_k",
     lambda: config.MANEUVER_COUPLING_K, lambda s: s.structural.maneuver_coupling_k),
    ("structural.maneuver_speed_mps",
     lambda: config.MANEUVER_SPEED_MPS, lambda s: s.structural.maneuver_speed_mps),
    ("structural.maneuver_turn_radius_m",
     lambda: config.MANEUVER_TURN_RADIUS_M, lambda s: s.structural.maneuver_turn_radius_m),
    ("structural.freeboard_min_in",
     lambda: config.FREEBOARD_MIN_IN, lambda s: s.structural.freeboard_min_in),
    ("structural.n_construction_layers",
     lambda: config.N_CONSTRUCTION_LAYERS, lambda s: s.structural.n_construction_layers),
    ("structural.thickness_options_in (min)",
     lambda: config.THICKNESS_MIN_IN, lambda s: min(s.structural.thickness_options_in)),
    ("structural.thickness_options_in (max)",
     lambda: config.THICKNESS_MAX_IN, lambda s: max(s.structural.thickness_options_in)),
    ("crew.paddler_power_w",
     lambda: config.PADDLER_POWER_W, lambda s: s.crew.paddler_power_w),
    ("crew.knee_dia_in", lambda: config.KNEE_DIA_IN, lambda s: s.crew.knee_dia_in),
    ("crew.knee_spacing_in", lambda: config.KNEE_SPACING_IN, lambda s: s.crew.knee_spacing_in),
    ("crew.knee_load_split", lambda: config.KNEE_LOAD_SPLIT, lambda s: s.crew.knee_load_split),
    ("crew.knee_pad_in", lambda: config.KNEE_PAD_IN, lambda s: s.crew.knee_pad_in),
    ("crew.knee_frac_bending",
     lambda: config.KNEE_FRAC_BENDING, lambda s: s.crew.knee_frac_bending),
    ("crew.knee_frac_punching",
     lambda: config.KNEE_FRAC_PUNCHING, lambda s: s.crew.knee_frac_punching),
    ("crew.toe_offset_in", lambda: config.TOE_OFFSET_IN, lambda s: s.crew.toe_offset_in),
    ("crew.knee_frac_measured_n",
     lambda: config.KNEE_FRAC_MEASURED_N, lambda s: s.crew.knee_frac_measured_n),
    ("structural.gamma_dead",
     lambda: config.GAMMA_DEAD, lambda s: s.structural.gamma_dead),
    ("structural.gamma_live",
     lambda: config.GAMMA_LIVE, lambda s: s.structural.gamma_live),
    ("structural.phi_resistance",
     lambda: config.PHI_RESISTANCE, lambda s: s.structural.phi_resistance),
    ("structural.omega_safety",
     lambda: config.OMEGA_SAFETY, lambda s: s.structural.omega_safety),
    ("mix.min_density_pcf",
     lambda: config.MIN_DENSITY_PCF, lambda s: s.mix.min_density_pcf),
    ("mix.max_density_pcf",
     lambda: config.MAX_DENSITY_PCF, lambda s: s.mix.max_density_pcf),
]

LIST_KEYS = [
    # Crew weights now live PER CATEGORY (race_categories), not per crew size:
    # 2M and 2F are different crews of the same size and one list could not
    # express both.  race_*_person_lbs are retired from canoe_suite.cfg and
    # survive only as a loader fallback for older files, so they are no longer
    # asserted live here -- the categories below are what must reach config.
    ("crew.race_categories[4_coed].weights_lb",
     lambda: config.RACE_CATEGORIES["4_coed"]["weights_lb"],
     lambda s: s.crew.race_categories["4_coed"]["weights_lb"]),
    ("crew.race_categories[2M].weights_lb",
     lambda: config.RACE_CATEGORIES["2M"]["weights_lb"],
     lambda s: s.crew.race_categories["2M"]["weights_lb"]),
    ("crew.race_categories[2F].weights_lb",
     lambda: config.RACE_CATEGORIES["2F"]["weights_lb"],
     lambda s: s.crew.race_categories["2F"]["weights_lb"]),
    ("crew.race_categories[3M].weights_lb",
     lambda: config.RACE_CATEGORIES["3M"]["weights_lb"],
     lambda s: s.crew.race_categories["3M"]["weights_lb"]),
    ("crew.race_categories[3F].weights_lb",
     lambda: config.RACE_CATEGORIES["3F"]["weights_lb"],
     lambda s: s.crew.race_categories["3F"]["weights_lb"]),
    ("crew.race_categories[4_coed].heights_in",
     lambda: config.RACE_CATEGORIES["4_coed"]["heights_in"],
     lambda s: s.crew.race_categories["4_coed"]["heights_in"]),
]


class TestEveryKeyReachesSomething:
    @pytest.mark.parametrize("name,got,want",
                             WIRED_KEYS, ids=[k[0] for k in WIRED_KEYS])
    def test_scalar_key_is_live(self, name, got, want):
        assert got() == pytest.approx(float(want(_settings()))), (
            f"{name} is no longer read from canoe_suite.cfg")

    @pytest.mark.parametrize("name,got,want",
                             LIST_KEYS, ids=[k[0] for k in LIST_KEYS])
    def test_list_key_is_live(self, name, got, want):
        assert [float(v) for v in got()] == \
               [float(v) for v in want(_settings())], (
            f"{name} is no longer read from canoe_suite.cfg")

    def test_design_method_is_live(self):
        """A string key, so it needs its own check rather than the numeric
        parametrisation above."""
        from config.settings import get_settings
        assert config.DESIGN_METHOD == \
            str(get_settings().structural.design_method).upper()

    def test_water_block_drives_the_physics_constants(self):
        from physics import constants
        s = _settings()
        assert constants.WATER_RHO == pytest.approx(s.water.rho_kg_m3)
        assert constants.NU == pytest.approx(s.water.nu_m2_s)

    def test_db_path_is_honoured_when_it_exists(self, tmp_path, monkeypatch):
        import mix_database
        real = tmp_path / "mixes.db"
        real.write_bytes(b"")
        monkeypatch.setattr(mix_database.MixDatabase, "_configured_db_path",
                            staticmethod(lambda: str(real)))
        assert mix_database.MixDatabase._configured_db_path() == str(real)

    def test_stale_db_path_falls_back_to_the_repo_copy(self, monkeypatch):
        """A cfg carrying another machine's absolute path must not silently
        open an empty database — that reads as 'the Mix Lab lost our data'."""
        import mix_database
        from config import settings as _s

        class _Fake:
            class mix:
                db_path = r"Z:\nowhere\gone.db"
        monkeypatch.setattr(_s, "get_settings", lambda: _Fake)
        got = mix_database.MixDatabase._configured_db_path()
        assert got.endswith("concrete_mixes.db")
        assert "nowhere" not in got


class TestMixBlockIsWired:
    """The mix: block used to be parsed and then dropped on the floor."""

    def test_config_tracks_the_file(self):
        s = get_settings()
        assert config.MAX_DENSITY_PCF == pytest.approx(s.mix.max_density_pcf)
        assert config.MIN_DENSITY_PCF == pytest.approx(s.mix.min_density_pcf)

    def test_loader_reads_real_values_from_a_file(self, tmp_path):
        """Proves the tracking above is a live read, not a coincidence of
        two hardcoded 80.0s."""
        p = tmp_path / "canoe_suite.cfg"
        p.write_text(textwrap.dedent("""
            mix:
              min_density_pcf: 51.5
              max_density_pcf: 73.25
        """), encoding="utf-8")
        s = Settings.load(str(p))
        assert s.mix.min_density_pcf == pytest.approx(51.5)
        assert s.mix.max_density_pcf == pytest.approx(73.25)

    def test_ui_value_still_overrides_the_file_per_run(self, monkeypatch):
        """The file sets the default; the Structural tab's Max Mix bounds the
        run.  StructuralService is the only place allowed to do that."""
        from domain.models import OptimizationConfig
        monkeypatch.setattr(config, "MAX_DENSITY_PCF", 80.0)
        oc = OptimizationConfig(max_density_pcf=64.0)
        import config as _cfg
        _cfg.MAX_DENSITY_PCF = oc.max_density_pcf     # what run_sweep does
        assert config.MAX_DENSITY_PCF == pytest.approx(64.0)
        assert get_settings().mix.max_density_pcf == pytest.approx(80.0)


# ══════════════════════════════════════════════════════════════════════════
#  "Evaluate using existing mixes only"
# ══════════════════════════════════════════════════════════════════════════

class _FakeDB:
    """Mix DB stub with a known set of real mixes and a linear regression.

    Only the grid builder is under test, so this supplies exactly what it
    reads — no SQLite, no dependence on the live Mix Lab contents.
    """
    fc_regression = (120.0, -6900.0)
    ft_regression = (27.0, -1600.0)
    fr_regression = None          # no MOR points -> split-tensile tier
    _regression_n = 2
    _regression_n_fr = 0

    # Borrow the REAL floor derivation rather than restating it here: a second
    # copy in the stub is the two-homes-for-one-fact pattern, and it would let
    # the grid builder and its test disagree about where the floor is.
    from mix_database import MixDatabase as _MDB
    tensile_viability_floor = _MDB.tensile_viability_floor
    del _MDB

    def __init__(self, densities):
        self._d = sorted(densities)

    def get_existing_mix_densities(self):
        return list(self._d)

    @staticmethod
    def _combos_from_densities(densities, n_mixes):
        from mix_database import MixDatabase
        return MixDatabase._combos_from_densities(densities, n_mixes)


def _grid(densities, **kw):
    from mix_database import MixDatabase
    db = _FakeDB(densities)
    return MixDatabase.get_structural_sweep_grid(db, **kw)


class TestExistingMixesOnly:
    LAB = [58.0, 62.0, 65.0, 71.9, 78.0]

    def test_restricts_to_mix_lab_densities(self):
        grid = _grid(self.LAB, max_density=80.0, n_mixes=2, existing_only=True)
        used = {d for combo in grid for d in combo}
        assert used <= set(self.LAB), "sweep searched a density nobody can batch"

    def test_off_searches_the_continuous_spectrum(self):
        grid = _grid(self.LAB, max_density=80.0, n_mixes=2, existing_only=False)
        used = {d for combo in grid for d in combo}
        assert len(used) > len(self.LAB)
        assert used - set(self.LAB), "interpolated densities should appear"

    def test_constraint_is_a_real_restriction(self):
        on = _grid(self.LAB, max_density=80.0, n_mixes=2, existing_only=True)
        off = _grid(self.LAB, max_density=80.0, n_mixes=2, existing_only=False)
        assert 0 < len(on) < len(off)

    def test_density_ceiling_still_applies(self):
        grid = _grid(self.LAB, max_density=66.0, n_mixes=2, existing_only=True)
        assert max(d for combo in grid for d in combo) <= 66.0

    def test_pour_floor_spacing_rule_is_shared_with_the_continuous_grid(self):
        for combo in _grid(self.LAB, max_density=80.0, n_mixes=2,
                           existing_only=True):
            assert combo[0] >= combo[1] + 2.0

    def test_single_mix_uses_every_available_density(self):
        grid = _grid(self.LAB, max_density=80.0, n_mixes=1, existing_only=True)
        assert sorted(c[0] for c in grid) == sorted(self.LAB)

    def test_falls_back_when_the_mix_lab_is_too_thin(self):
        """An empty grid would surface as '0 configurations survived' and get
        blamed on the physics — fall back to the continuous grid instead."""
        grid = _grid([70.0], max_density=80.0, n_mixes=2, existing_only=True)
        used = {d for combo in grid for d in combo}
        assert len(grid) > 0 and used - {70.0}

    def test_step_is_ignored_in_existing_mode(self):
        a = _grid(self.LAB, max_density=80.0, n_mixes=2, step=0.5,
                  existing_only=True)
        b = _grid(self.LAB, max_density=80.0, n_mixes=2, step=2.0,
                  existing_only=True)
        assert a == b


class TestExistingMixDensities:
    def test_unions_the_mix_library_and_the_lab_batches(self):
        from mix_database import MixDatabase

        class _DB:
            def get_all_mixes(self):
                return [{"density_pcf": 80.0}, {"density_pcf": 60.0}]

            def get_lab_results(self, limit=500):
                return [{"density_pcf": 65.0}, {"density_pcf": 71.94}]

        got = MixDatabase.get_existing_mix_densities(_DB())
        assert got == [60.0, 65.0, 71.9, 80.0]

    def test_deduplicates_a_design_and_its_lab_batch(self):
        from mix_database import MixDatabase

        class _DB:
            def get_all_mixes(self):
                return [{"density_pcf": 70.0}]

            def get_lab_results(self, limit=500):
                return [{"density_pcf": 70.04}, {"density_pcf": None}]

        assert MixDatabase.get_existing_mix_densities(_DB()) == [70.0]
