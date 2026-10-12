# services/structural_service.py
"""
Orchestrates the 5-D structural sweep.

No Qt imports — callable from tests or a background thread.
Accepts an OptimizationConfig so the caller never touches config.py
directly; config mutation is confined to this boundary.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

from domain.models import OptimizationConfig


class StructuralService:
    """
    Thin orchestration layer around ``main.run_grand_optimization``.

    Passes ``OptimizationConfig`` values into the module-level ``config``
    singleton so downstream code can read them without any UI coupling.
    """

    def _apply_run_config(self, opt_config, existing_mixes_only=False,
                          advanced_loading=False):
        """
        Push one OptimizationConfig onto the module-level config singleton.

        Extracted from run_sweep so the crew state a run actually uses can be
        exercised directly.  It was inline, which meant the only way to test
        that a UI crew weight reaches the load cases was to re-implement the
        mapping in the test -- and a test that re-implements the code under
        test cannot catch the code being wrong.
        """
        import config as _cfg

        # Apply per-run parameters to the module-level config singleton.
        # This is temporary; a future refactor will pass OptimizationConfig
        # all the way through to evaluate_canoe_weight.
        _cfg.FACTOR_OF_SAFETY    = opt_config.factor_of_safety
        _cfg.DYNAMIC_LOAD_FACTOR = opt_config.dynamic_load_factor
        _cfg.KUPFER_ACCEPTANCE_MAX = opt_config.kupfer_acceptance_max
        # Design methodology.  Same failure criterion either way -- these only
        # set how loads are combined and what divides the nominal strength.
        _cfg.DESIGN_METHOD   = str(opt_config.design_method).upper()
        _cfg.GAMMA_DEAD      = float(opt_config.gamma_dead)
        _cfg.GAMMA_LIVE      = float(opt_config.gamma_live)
        _cfg.LOAD_FACTORS_BY_CASE = dict(opt_config.load_factors_by_case or {})
        _cfg.PHI_RESISTANCE  = float(opt_config.phi_resistance)
        _cfg.OMEGA_SAFETY    = float(opt_config.omega_safety
                                     or opt_config.factor_of_safety)
        _cfg.MAX_DENSITY_PCF     = opt_config.max_density_pcf
        _cfg.MIN_DENSITY_PCF     = opt_config.min_density_pcf
        _cfg.MAX_MIXES           = opt_config.n_mixes
        # ONE crew definition per run.  Assigning RACE_*_PERSON here is what
        # produced the split-brain: the aliases changed, RACE_CATEGORIES did
        # not, and the sweep ran the punching check on the UI's crew while the
        # bending envelope used the file's.  The aliases are now DERIVED, so the
        # only supported write is through the categories.
        _cats = opt_config.race_categories
        if _cats is None:
            # Legacy per-SIZE input: seed every category of the matching size,
            # keeping each category's own sexes/heights/pins.
            _by_size = {4: list(opt_config.race_4_person_lbs),
                        2: list(opt_config.race_2_person_lbs),
                        3: list(opt_config.race_3_person_lbs)}
            _cats = {}
            for _name, _c in _cfg.RACE_CATEGORIES.items():
                _n = len(_c["weights_lb"])
                _w = _by_size.get(_n)
                _cats[_name] = dict(_c)
                if _w and len(_w) == _n:
                    _cats[_name] = {**_c, "weights_lb": list(_w)}
        _cfg.set_race_categories(_cats)
        _cfg.FIXED_PADDLER_POSITIONS = opt_config.fixed_paddler_positions
        _cfg.PADDLER_POS_VARIANCE_FT = float(opt_config.paddler_pos_variance_ft)
        _cfg.EXISTING_MIXES_ONLY     = existing_mixes_only
        _cfg.ADVANCED_PADDLER_LOADING = advanced_loading
        _cfg.MANEUVER_SPEED_MPS      = float(opt_config.maneuver_speed_mps)
        _cfg.MANEUVER_TURN_RADIUS_M  = float(opt_config.maneuver_turn_radius_m)
        # Construction layer count -- a DERIVED report/manufacturing descriptor,
        # not a sweep input (run_grand_optimization never reads it), so the
        # winner stays bit-identical regardless of this value.
        _cfg.N_CONSTRUCTION_LAYERS   = max(int(opt_config.n_construction_layers), 1)
        # Bow/stern stem-rocker overhang (single input, not swept).  Raw value:
        # None = auto.  run_grand_optimization resolves it against the hull and
        # publishes the numeric per-end length as _cfg.STRUCT_ROCKER_IN, which the
        # beam self-weight reads so the rocker's cantilever mass raises hogging.
        _cfg.BOW_STERN_ROCKER_IN     = opt_config.bow_stern_rocker_in
        # Longitudinal integration resolution (numerical accuracy, not a design
        # variable).  None = offset-file native rows; the OptimizationConfig
        # default oversamples to the converged grid.
        _cfg.LONGITUDINAL_STATIONS   = opt_config.longitudinal_stations
        _cfg._mix_db                 = None   # force re-init so new FoS takes effect

    def run_sweep(
        self,
        opt_config: OptimizationConfig,
        progress_callback: Optional[Callable[[float, str], None]] = None,
        stop_event: Optional[threading.Event] = None,
        precision_level: int = 3,
        existing_mixes_only: bool = False,
        advanced_loading: bool = False,
        # A RUN-TIME choice like the two above, not a design parameter: it
        # changes what the run REPORTS, never what it computes.  It therefore
        # arrives the same way they do -- as a kwarg -- and not on
        # OptimizationConfig, which is saved and shared as a design preset.
        price_mix_development: bool = False,
        reports_dir: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Run the full structural sweep and return a result dict, or None if
        no feasible configuration was found.

        Parameters
        ----------
        opt_config       : sweep parameters (FoS, DLF, densities, etc.)
        progress_callback: called with (fraction 0–1, label str) periodically
        stop_event       : set this Event to request a clean abort
        reports_dir      : explicit directory for the ASCE report PDFs; None
                           writes them to the project root (git-ignored). Pass
                           a scratch dir from tests so the tree stays clean.

        Returns
        -------
        dict with keys ``x``, ``M_envelope``, ``V_envelope``, ``layout``,
        ``na_line``; or None on failure / abort.
        """
        self._apply_run_config(opt_config, existing_mixes_only,
                               advanced_loading)
        import config as _cfg

        from main import run_grand_optimization
        result = run_grand_optimization(
            # The UI writes its own audit record from the widgets after this
            # returns; logging here as well would double-count every GUI sweep.
            audit_log=False,
            progress_callback=progress_callback,
            freeboard_min_in=opt_config.freeboard_min_in,
            thickness_options_in=list(opt_config.thickness_options_in),
            precision_level=precision_level,
            enable_gunwales=opt_config.enable_gunwales,
            enable_mesh=opt_config.enable_mesh,
            price_mix_development=price_mix_development,
            gw_min_width_in=opt_config.gw_min_width_in,
            gw_max_width_in=opt_config.gw_max_width_in,
            gw_min_height_in=opt_config.gw_min_height_in,
            gw_max_height_in=opt_config.gw_max_height_in,
            gw_steps=opt_config.gw_steps,
            gw_fillet_ratio=opt_config.gw_fillet_ratio,
            flare_max_deg=opt_config.flare_max_deg,
            stop_event=stop_event,
            multizone=opt_config.multizone,
            num_zones=opt_config.num_zones,
            reports_dir=reports_dir,
            depth_step_in=opt_config.depth_step_in,
            depth_max_in=opt_config.depth_max_in,
            freeboard_max_in=opt_config.freeboard_max_in,
            depth_span_in=opt_config.depth_span_in,
            density_step_pcf=opt_config.density_step_pcf,
            thickness_grid_overridden=opt_config.thickness_grid_overridden,
        )

        # Tag the result with the active regression version for PDF provenance,
        # and with any strength-fit override in force — a design gated on a
        # rejected fit has to carry that wherever it travels.
        if isinstance(result, dict):
            try:
                from config import get_mix_db
                _db = get_mix_db()
                result["regression_version"] = _db.get_active_version_tag()
                result["fit_override"] = _db.fit_override_record()
            except Exception as _tag_exc:
                # DELIBERATELY BROAD, same rule.  `result` is the finished sweep
                # -- this block only STAMPS it with provenance.  A database that
                # will not answer must not destroy the design it was asked
                # about.  "unknown" is a safe sentinel (nothing downstream acts
                # on it as a version), and it is not silent.
                #
                # `fit_override = None` is NOT safe in the same way -- None is
                # the legitimate value for "no override in force", so a failed
                # read here claims clean provenance.  Recorded in KNOWN_ISSUES
                # ("A failed fit-override read is indistinguishable from no
                # override"); it degrades a warning, not a number.
                result["regression_version"] = "unknown"
                result["fit_override"] = None
                print(f"[Sweep] provenance tag unavailable, result kept: "
                      f"{type(_tag_exc).__name__}: {_tag_exc}")

        return result

    @staticmethod
    def config_from_settings() -> OptimizationConfig:
        """Build an OptimizationConfig from the canoe_suite.cfg file."""
        from config.settings import get_settings
        s = get_settings()
        return OptimizationConfig(
            factor_of_safety=s.structural.factor_of_safety,
            dynamic_load_factor=s.structural.dynamic_load_factor,
            max_density_pcf=s.mix.max_density_pcf,
            min_density_pcf=s.mix.min_density_pcf,
            thickness_options_in=s.structural.thickness_options_in,
            freeboard_min_in=s.structural.freeboard_min_in,
            race_4_person_lbs=s.crew.race_4_person_lbs,
            race_2_person_lbs=s.crew.race_2_person_lbs,
            race_3_person_lbs=s.crew.race_3_person_lbs,
            maneuver_speed_mps=s.structural.maneuver_speed_mps,
            maneuver_turn_radius_m=s.structural.maneuver_turn_radius_m,
        )
