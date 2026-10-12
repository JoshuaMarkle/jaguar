# domain/models.py
"""
Canonical domain dataclasses for the ASCE Concrete Canoe Engineering Suite.

These replace all loose dicts, positional tuples, and return-value bundles
scattered across the codebase.  Import from here; never construct raw dicts
where one of these types is appropriate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Dict
import numpy as np


# ═══════════════════════════════════════════════════════════════════
#  HULL GEOMETRY
# ═══════════════════════════════════════════════════════════════════

@dataclass
class HullGeometry:
    """
    Parametric hull form plus the offset matrix it produced.

    offsets : (n_stations × n_waterlines) half-breadth array [m]
    L       : waterline length [m]
    B       : maximum beam [m]
    T       : draft [m]
    factors : 7-element Michlet Series 7 shape-factor vector (or empty)
    source  : "generated" | "godzilla" | "fallback"
    """
    offsets: np.ndarray
    L: float
    B: float
    T: float
    factors: List[float] = field(default_factory=list)
    source: str = "generated"


# ═══════════════════════════════════════════════════════════════════
#  HYDROSTATICS
# ═══════════════════════════════════════════════════════════════════

@dataclass
class HullHydrostatics:
    """
    Complete hydrostatic property set for a hull at its design waterline.
    All SI units unless field name carries a unit suffix.

    Backward compatibility
    ---------------------
    dict-style access (``h["volume"]``, ``h.get("gmt", 0)``) is supported so
    that callers still using the old compute_hydrostatics() dict format work
    without immediate changes.
    """
    volume: float       # m³ — displaced volume
    displacement: float # kg — mass displacement
    beam: float         # m
    lcb: float          # fractional LCB = (lcb_abs/L) - 0.5  (0 = midship)
    vcb: float          # m — vertical centre of buoyancy above keel
    awp: float          # m² — waterplane area
    S: float            # m² — wetted surface
    Am: float           # m² — midship section area
    Cb: float           # block coefficient
    Cm: float           # midship coefficient
    Cp: float           # prismatic coefficient
    Cwp: float          # waterplane area coefficient
    L_B: float          # length-to-beam ratio
    L_T: float          # length-to-draft ratio
    B_T: float          # beam-to-draft ratio
    bmt: float          # m — transverse BM (metacentric radius)
    gmt: float          # m — transverse GM (metacentric height)
    Iwp_yaw: float      # m⁴ — yaw stability moment of inertia
    L: float = 0.0      # m — waterline length
    B: float = 0.0      # m — beam (same as .beam; matches legacy dict key "B")
    T: float = 0.0      # m — draft (matches legacy dict key "T")

    # ------------------------------------------------------------------
    # Legacy dict-style access — lets callers use hydro["key"] or
    # hydro.get("key", default) without any code changes.
    # All legacy dict keys map directly to same-named attributes.
    # ------------------------------------------------------------------
    def __getitem__(self, key: str):
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)

    def get(self, key: str, default=None):
        return getattr(self, key, default)

    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, d: dict) -> "HullHydrostatics":
        """Construct from the dict returned by hull_generator.compute_hydrostatics()."""
        b = d.get("beam", d.get("B", 0.0))
        t = d.get("T", 0.0)
        l = d.get("L", 0.0)
        inst = cls(
            volume=d["volume"], displacement=d["displacement"],
            beam=b, lcb=d["lcb"], vcb=d["vcb"],
            awp=d["awp"], S=d["S"], Am=d["Am"],
            Cb=d["Cb"], Cm=d["Cm"], Cp=d["Cp"], Cwp=d["Cwp"],
            L_B=d["L_B"], L_T=d["L_T"], B_T=d["B_T"],
            bmt=d["bmt"], gmt=d["gmt"], Iwp_yaw=d["Iwp_yaw"],
            L=l, B=b, T=t,
        )
        return inst

    def to_dict(self) -> dict:
        """Convert back to a plain dict (for code that explicitly needs a dict)."""
        return {
            "volume": self.volume, "displacement": self.displacement,
            "beam": self.beam, "lcb": self.lcb, "vcb": self.vcb,
            "awp": self.awp, "S": self.S, "Am": self.Am,
            "Cb": self.Cb, "Cm": self.Cm, "Cp": self.Cp, "Cwp": self.Cwp,
            "L_B": self.L_B, "L_T": self.L_T, "B_T": self.B_T,
            "bmt": self.bmt, "gmt": self.gmt, "Iwp_yaw": self.Iwp_yaw,
            "L": self.L, "B": self.B, "T": self.T,
        }


# ═══════════════════════════════════════════════════════════════════
#  STRUCTURAL CROSS-SECTION PROPERTIES
# ═══════════════════════════════════════════════════════════════════

@dataclass
class ShellSection:
    """Per-station cross-section properties for a single longitudinal station."""
    x_ft: float       # station longitudinal position [ft]
    A_in2: float      # total shell cross-sectional area [in²]
    A_top_in2: float  # area above neutral axis [in²]
    A_bot_in2: float  # area below neutral axis [in²]
    I_in4: float      # second moment of area about NA [in⁴]
    Q_in3: float      # first moment of area at NA [in³]
    c_top: float      # distance from NA to top fibre [in]
    c_bot: float      # distance from NA to bottom fibre [in]
    width_ft: float   # half-breadth at waterplane [ft]


@dataclass
class ShellProperties:
    """
    Vectorised cross-section properties for all stations, as returned by
    geometry.calculate_shell_properties().

    All arrays have length n_stations and are parallel to x_stations.

    Backward compatibility
    ---------------------
    ``__iter__`` yields the 8 arrays in the same order as the old tuple
    return, so existing destructuring still works::

        A_in2, A_top_in2, A_bot_in2, I_in4, Q_in3, c_top, c_bot, width_ft = props
    """
    A_in2: np.ndarray
    A_top_in2: np.ndarray
    A_bot_in2: np.ndarray
    I_in4: np.ndarray
    Q_in3: np.ndarray
    c_top: np.ndarray
    c_bot: np.ndarray
    width_ft: np.ndarray
    # (n_stations × n_z) cumulative shell area below each z_grid_in height
    # [in²] — lets the evaluator split the weight at an arbitrary pour
    # height (fix plan C3).  Not yielded by __iter__ (legacy 8-tuple).
    cum_area_in2: Optional[np.ndarray] = None
    z_grid_in: Optional[np.ndarray] = None
    # Cumulative first and second moments of shell area about the keel
    # baseline (z=0), below each z_grid_in height — the raw material for
    # composite (transformed-section) properties of arbitrary horizontal
    # pour zones (multi-zone I-beam strategy).  cum_Az = Σ z_c·dA,
    # cum_Iz = Σ (z_c²·dA + dI_own), both ×2 for port+starboard.
    cum_Az_in3: Optional[np.ndarray] = None
    cum_Iz_in4: Optional[np.ndarray] = None
    # Cumulative second moment of shell area about the VERTICAL centerplane
    # (y=0), below each z_grid_in height [in⁴] — the transformed-section raw
    # material for the transverse (hoop / lateral) bending check.  cum_Iy =
    # Σ (y_c²·dA + dI_own_horizontal), ×2 for port+starboard.  yhalf_in is the
    # per-station half-breadth [in] at each z_grid_in height, so the evaluator
    # can find each pour zone's outboard lateral fibre.
    cum_Iy_in4: Optional[np.ndarray] = None
    yhalf_in: Optional[np.ndarray] = None

    def __iter__(self):
        yield self.A_in2
        yield self.A_top_in2
        yield self.A_bot_in2
        yield self.I_in4
        yield self.Q_in3
        yield self.c_top
        yield self.c_bot
        yield self.width_ft

    def to_sections(self, x_ft: np.ndarray) -> "List[ShellSection]":
        """Convert to a list of per-station ShellSection objects."""
        return [
            ShellSection(
                x_ft=float(x_ft[i]),
                A_in2=float(self.A_in2[i]),
                A_top_in2=float(self.A_top_in2[i]),
                A_bot_in2=float(self.A_bot_in2[i]),
                I_in4=float(self.I_in4[i]),
                Q_in3=float(self.Q_in3[i]),
                c_top=float(self.c_top[i]),
                c_bot=float(self.c_bot[i]),
                width_ft=float(self.width_ft[i]),
            )
            for i in range(len(x_ft))
        ]


# ═══════════════════════════════════════════════════════════════════
#  CONCRETE MIX FORMULATIONS
# ═══════════════════════════════════════════════════════════════════

@dataclass
class ConcreteMix:
    """A concrete mix design as stored in the SQLite database."""
    id: int
    name: str
    density_pcf: float
    fc_psi: float
    ft_psi: float
    fiber_type: str = "PVA"
    fiber_vol_pct: float = 2.0
    description: str = ""


@dataclass
class AllowableMix:
    """
    Structural allowables for a mix at a specific density after applying FoS.
    Used by evaluate_canoe_weight; replaces the plain dicts it currently returns.
    """
    density_pcf: float
    strength_c_psi: float   # f'c / FoS
    strength_t_psi: float   # f_t / FoS

    @classmethod
    def from_dict(cls, d: dict) -> "AllowableMix":
        return cls(
            density_pcf=d["density"],
            strength_c_psi=d["strength_c"],
            strength_t_psi=d["strength_t"],
        )

    def to_dict(self) -> dict:
        return {
            "density": self.density_pcf,
            "strength_c": self.strength_c_psi,
            "strength_t": self.strength_t_psi,
        }


# ═══════════════════════════════════════════════════════════════════
#  STRUCTURAL ANALYSIS RESULT
# ═══════════════════════════════════════════════════════════════════

@dataclass
class StructuralResult:
    """
    Complete output of a single evaluate_canoe_weight(return_arrays=True) call.
    Replaces the (weight, struct_data_dict, layout_array) 3-tuple.
    """
    weight_lbs: float
    is_feasible: bool
    thickness_in: float
    depth_in: float
    mix_densities: List[float]      # [heavy_pcf, mid_pcf, light_pcf]

    x_ft: np.ndarray                # station positions [ft]
    M_envelope: np.ndarray          # absolute moment envelope [ft·lb]
    M_race_4: np.ndarray            # signed moment for 4-person race [ft·lb]
    M_race_2: np.ndarray            # signed moment for 2-person race [ft·lb]
    V_max_pos: np.ndarray           # positive shear envelope [lb]
    V_max_neg: np.ndarray           # negative shear envelope [lb]
    V_race_4: np.ndarray            # signed shear for 4-person race [lb]
    V_race_2: np.ndarray            # signed shear for 2-person race [lb]
    na_line: np.ndarray             # neutral axis height above keel at each station [in]
    pour_layout: np.ndarray         # (n_stations × 2) int array: [mix_bot, mix_top]
    M_race_3: Optional[np.ndarray] = None   # signed moment for 3-person race [ft·lb]
    V_race_3: Optional[np.ndarray] = None   # signed shear for 3-person race [lb]

    # ── Signed envelopes + governing-case trace (2026-07 fix plan C2) ──
    # Sign convention: sagging (M>0) → bottom fiber tension; hogging (M<0)
    # → top fiber / gunwale tension.  Envelopes stored as ≥0 magnitudes.
    M_sag_env: Optional[np.ndarray] = None  # sagging moment envelope [ft·lb]
    M_hog_env: Optional[np.ndarray] = None  # hogging moment envelope [ft·lb]
    gov_sag: Optional[np.ndarray] = None    # per-station case index (into case_names)
    gov_hog: Optional[np.ndarray] = None    # per-station case index (into case_names)
    case_names: Optional[List[str]] = None  # e.g. ["race_4", "race_2", "race_3"]

    # ── Utilization = demand / allowable (FOS already in the allowable) ──
    util_bot_t: Optional[np.ndarray] = None  # bottom fiber, tension (sagging)
    util_top_t: Optional[np.ndarray] = None  # top fiber, tension (hogging)
    util_bot_c: Optional[np.ndarray] = None  # bottom fiber, compression (hogging)
    util_top_c: Optional[np.ndarray] = None  # top fiber, compression (sagging)

    # ── Depth-based pour zoning (fix plan C3) ──
    # Height above keel [in] where the bottom-zone mix hands off to the
    # top-zone mix at each station (NaN = single mix through the depth).
    pour_line_in: Optional[np.ndarray] = None
    sheer_line_in: Optional[np.ndarray] = None   # section top (c_bot + c_top) [in]


# ═══════════════════════════════════════════════════════════════════
#  TORSION + LATERAL LOAD RESULT
# ═══════════════════════════════════════════════════════════════════

@dataclass
class TorsionLateralResult:
    """
    Output of structural.run_torsion_lateral_checks().

    Torsion uses the St. Venant open-section formula (J = t²A/3).
    Lateral bending uses the linearised hull-profile approximation
    (I_lat = 2tB²D/3).  Both are documented "hand-calc" methods.
    """
    # ── Design loads ────────────────────────────────────────────────
    T_design_lb_in: float           # torsional design torque [lb·in]
    M_lat_lb_in: float              # lateral bending design moment [lb·in]
    F_lat_lb: float                 # lateral centripetal force [lb]

    # ── Critical torsion ─────────────────────────────────────────
    J_min_in4: float                # minimum J across all stations [in⁴]
    tau_max_psi: float              # maximum torsional shear stress [psi]
    tau_allowable_psi: float        # allowable shear stress [psi]
    torsion_utilization: float      # tau_max / tau_allowable  (1.0 = limit)
    torsion_status: str             # "OK" or "ADVISORY" (never gates)
    critical_torsion_x_ft: float    # station [ft] where τ is worst

    # ── Critical lateral bending ──────────────────────────────────
    I_lat_min_in4: float            # minimum I_lat across all stations [in⁴]
    c_lat_max_in: float             # half-breadth at critical station [in]
    sigma_lat_psi: float            # maximum lateral bending stress [psi]
    sigma_lat_allowable_psi: float  # allowable bending stress [psi]
    lateral_utilization: float      # sigma_lat / sigma_lat_allowable
    lateral_status: str             # "OK" or "ADVISORY" (never gates)
    critical_lateral_x_ft: float    # station [ft] where σ_lat is worst

    # ── Per-station arrays ────────────────────────────────────────
    x_ft: np.ndarray
    J_in4: np.ndarray               # St. Venant J at every station [in⁴]
    tau_psi: np.ndarray             # torsional shear stress [psi]
    I_lat_in4: np.ndarray           # lateral I at every station [in⁴]
    sigma_lat_psi_arr: np.ndarray   # lateral bending stress [psi]

    @property
    def is_feasible(self) -> bool:
        return self.torsion_status == "OK" and self.lateral_status == "OK"

    def summary_lines(self) -> "List[str]":
        """
        BOTH LINES ARE ADVISORY and neither gates the sweep.  Each overstates
        by construction, for a different reason, and a reader who cannot tell
        that will chase a number that needs no action:

          * torsion -- J = t^2*A/3 is the OPEN-section St. Venant bound and
            ignores the warping restraint from the closed stems and gunwale
            flanges, so tau is high by roughly an order of magnitude.
          * lateral -- I_lat comes from a hand-calc that models the section as
            a straight V and is documented valid only for B_half < D/3.  A
            beamy hull is well outside that (this one by ~2.2x), and both the
            V idealisation and the dropped arc-length term push I_lat DOWN,
            so sigma_lat comes out high.

        The transverse stress that DOES gate is the Kupfer biaxial check, on
        the properly transformed composite section.
        """
        return [
            f"Torsion : tau_max = {self.tau_max_psi:6.1f} psi  "
            f"allow = {self.tau_allowable_psi:6.1f} psi  "
            f"util = {self.torsion_utilization:.3f}  [{self.torsion_status}]",
            f"Lateral : sig_lat = {self.sigma_lat_psi:6.1f} psi  "
            f"allow = {self.sigma_lat_allowable_psi:6.1f} psi  "
            f"util = {self.lateral_utilization:.3f}  [{self.lateral_status}]",
            "  (both ADVISORY: overstated by construction, neither gates the "
            "sweep; the gating transverse check is Kupfer biaxial)",
        ]


# ═══════════════════════════════════════════════════════════════════
#  RACE SIMULATION RESULT
# ═══════════════════════════════════════════════════════════════════

@dataclass
class RaceResult:
    """
    Output of simulate_race_with_telemetry() for a single course.

    telemetry : (N × 5) float array, columns = [t, x, y, heading_rad, speed_m_s]
    """
    course_name: str
    total_time_s: float
    telemetry: np.ndarray


# ═══════════════════════════════════════════════════════════════════
#  OPTIMIZATION CONFIG + SESSION
# ═══════════════════════════════════════════════════════════════════

@dataclass
class OptimizationConfig:
    """
    All parameters that govern a single structural sweep run.
    Passed from the UI to the service layer; no more direct config.py writes.
    """
    factor_of_safety: float = 1.5
    # Kupfer utilisation a winner must stay under (numerical-convergence
    # margin, separate from the factor of safety).  1.0 disables it.
    kupfer_acceptance_max: float = 0.95
    # Design methodology: "LRFD" (default) or "ASD".  Same Kupfer-Gerstle
    # failure criterion under both; only load/capacity factoring differs.
    design_method: str = "LRFD"
    gamma_dead: float = 1.2               # LRFD, ASCE 7-22 Sec. 2.3.1
    gamma_live: float = 1.6
    # {case_name: (gamma_dead, gamma_live)} overrides; empty = use the globals.
    load_factors_by_case: dict = field(default_factory=dict)
    phi_resistance: float = 0.60          # LRFD; ACI 318-19 plain concrete
    omega_safety: float = 1.5             # ASD factor of safety
    dynamic_load_factor: float = 1.2
    max_density_pcf: float = 80.0
    # The team's density FLOOR [pcf] -- a manufacturing constraint ("we will
    # not batch below this"), not a claim about strength.  It can only RAISE
    # the Mix Lab regression's viability floor, never lower it: densities the
    # regression predicts no strength for are configs that get built,
    # evaluated and rejected, so admitting them costs time and buys nothing.
    min_density_pcf: float = 60.0
    thickness_options_in: List[float] = field(default_factory=lambda: [0.375, 0.5, 0.625, 0.75])
    freeboard_min_in: float = 6.0
    # Legacy per-SIZE crew weights.  Kept for older callers; when
    # race_categories is None they SEED every category of the matching size.
    race_4_person_lbs: List[float] = field(default_factory=lambda: [160.0, 150.0, 170.0, 160.0])
    race_2_person_lbs: List[float] = field(default_factory=lambda: [170.0, 160.0])
    race_3_person_lbs: List[float] = field(default_factory=lambda: [160.0, 160.0, 160.0])
    # THE per-run crew definition: {category: {sexes, heights_in, weights_lb,
    # seat_pins}}.  Authoritative when present.  It exists because the per-size
    # lists above cannot express 2M and 2F as different crews, and because a run
    # must have exactly ONE crew definition -- writing the legacy aliases while
    # leaving the categories at their file defaults gave a sweep two.
    race_categories: Optional[Dict] = None
    # User-specified seating {"4": [ft], "2": [ft], "3": [ft]} — bypasses the
    # SLSQP placement when set. None = optimizer places the paddlers.
    fixed_paddler_positions: Optional[Dict] = None
    # Expected drift of real paddlers from the optimized station [ft];
    # only meaningful in optimizer mode (ignored with fixed positions).
    paddler_pos_variance_ft: float = 0.0
    n_mixes: int = 2
    # Through-thickness construction layers the wall is troweled up from (equal
    # passes of the same mix).  A DERIVED manufacturing descriptor: it drives the
    # report's layer schedule, trowel-ability check and punching mesh-depth, but
    # is NEVER read by the sweep, so the structural winner is independent of it.
    n_construction_layers: int = 3
    enable_gunwales: bool = False
    enable_mesh: bool = False
    # NOTE: price_mix_development is deliberately NOT here.  It is a
    # run-time reporting choice, passed to StructuralService.run_sweep
    # alongside existing_mixes_only and advanced_loading.  On this
    # dataclass it would be saved into a design preset and could turn a
    # phase measured at 93% of a run's time on for whoever loaded it.
    gw_min_width_in: float = 0.5
    gw_max_width_in: float = 2.5
    gw_min_height_in: float = 0.5
    gw_max_height_in: float = 3.0
    gw_steps: int = 5
    gw_fillet_ratio: float = 1.0
    flare_max_deg: float = 15.0
    # Multi-zone (I-beam) pour strategy: sweep N-1 pour-line elevations as
    # independent variables and analyse the section as a composite.  Active
    # only when multizone is True and num_zones >= 2.
    multizone: bool = False
    # Number of horizontal pour ZONES, DECOUPLED from the mix count.  e.g.
    # 2 physical mixes poured in 3 zones (Struct/Light/Struct I-beam).
    # None → default to n_mixes.
    num_zones: Optional[int] = None
    # Design turn for the transverse-hoop / lateral checks (M_lat ~ v^2 / R).
    # A tighter radius raises the centripetal load the hull must survive.
    maneuver_speed_mps: float = 2.5
    maneuver_turn_radius_m: float = 4.0
    # Longitudinal integration resolution (station count) — a NUMERICAL-ACCURACY
    # control, not a design variable.  None = the offset file's native rows; the
    # production default oversamples (~161) because native (~41) under-reads both
    # hull weight and bending demand (verified: winner 107.6 lb @native vs
    # 113.33 lb converged).  Values above native interpolate between real
    # offsets rather than adding hull data.
    longitudinal_stations: Optional[int] = 161
    # Advanced sweep-precision overrides (None → the precision-level preset).
    # Steps are the design-grid increments; depth_span_in is the depth search
    # range measured ABOVE the 4-person freeboard floor.  These are DESIGN-space
    # controls (which candidates are tried), unlike longitudinal_stations.
    depth_step_in: Optional[float] = None
    # Absolute maximum mold depth.  depth_span_in is the DEPRECATED form (a
    # span above the computed freeboard floor); pass one or the other, never
    # both -- main.py raises on the pair.
    # Maximum FREEBOARD [in] -- the most hull the design may carry above the
    # water, bounding from above the same quantity freeboard_min_in bounds
    # from below.  Mold depth is derived from it (draft + freeboard) inside
    # the sweep, against the draft that run parsed.  depth_max_in (an absolute
    # mold depth) and depth_span_in (a span above the floor) are the older
    # forms; exactly one of the three may be set.
    freeboard_max_in: Optional[float] = None
    depth_max_in: Optional[float] = None
    depth_span_in: Optional[float] = None
    density_step_pcf: Optional[float] = None
    # True when the UI replaced the buildable thickness grid with a custom
    # uniform step.  The grid is BUILT in the UI, so this is the only way the
    # sweep can know its thicknesses are not buildable ones and say so.
    thickness_grid_overridden: bool = False
    # Bow/stern stem-rocker overhang length [in], a SINGLE user input (not swept
    # — a structural optimizer would just drive it to 0 to shed weight). None =
    # auto (derived from the hull's degenerate tip span, matching the CAD export).
    # Its cantilevered concrete mass IS included in the 1-D beam self-weight, so
    # a longer rocker raises the midship hogging moment.
    bow_stern_rocker_in: Optional[float] = None


@dataclass
class SweepSession:
    """
    Serializable snapshot of an optimization run — config + best result.
    Save to / load from JSON so a 30-minute sweep does not need to be re-run.
    """
    config: OptimizationConfig
    best_result: Optional[StructuralResult] = None
    progress: float = 0.0          # 0.0–1.0
    status_message: str = ""
    completed: bool = False


# ═══════════════════════════════════════════════════════════════════
#  PIPELINE DESIGN STATE  (seed → godzilla → structural → export)
# ═══════════════════════════════════════════════════════════════════

@dataclass
class CanoeDesignState:
    """
    Persistent record of one canoe design as it moves through the pipeline.

    Each step section is a plain JSON-friendly dict with a "timestamp" key
    (ISO-8601 string) so downstream steps can be flagged stale when an
    upstream step re-runs.  The seed is always user-authored in the Hull
    Configurator; ``race_guideline`` is reference-only and never consumed
    by downstream steps.

    seed       : {factors, L_m, B_m, T_m, timestamp}
    godzilla   : {L_ft, draft_in, off1_path, out_path, timestamp}
    structural : {t_in, depth_in, flare_deg, gw_width_in, gw_height_in,
                  gw_fillet_ratio, mixes_pcf, weight_lbs,
                  regression_version, timestamp}
    exports    : {label: filepath}
    """
    seed: Optional[Dict] = None
    race_guideline: Optional[Dict] = None
    godzilla: Optional[Dict] = None
    structural: Optional[Dict] = None
    exports: Dict[str, str] = field(default_factory=dict)

    _STEP_ORDER = ("seed", "godzilla", "structural")

    # ── timestamps / staleness ─────────────────────────────────────
    @staticmethod
    def now() -> str:
        # Microsecond resolution: consecutive pipeline steps can run within
        # the same second and staleness compares these strings lexically.
        from datetime import datetime
        return datetime.now().isoformat(timespec="microseconds")

    def _ts(self, step: str) -> Optional[str]:
        section = getattr(self, step, None)
        return section.get("timestamp") if isinstance(section, dict) else None

    def is_stale(self, step: str) -> bool:
        """
        True when *step* ran before an earlier pipeline step's latest run
        (its inputs have changed since it produced its result).
        Steps that have not run at all are 'pending', not stale — False.
        """
        if step not in self._STEP_ORDER:
            return False
        own_ts = self._ts(step)
        if own_ts is None:
            return False
        for upstream in self._STEP_ORDER[: self._STEP_ORDER.index(step)]:
            up_ts = self._ts(upstream)
            if up_ts is not None and up_ts > own_ts:
                return True
        return False

    def step_status(self, step: str) -> str:
        """'pending' | 'done' | 'stale' for the pipeline stepper UI."""
        if self._ts(step) is None:
            return "pending"
        return "stale" if self.is_stale(step) else "done"

    # ── step recorders ─────────────────────────────────────────────
    def mark_seed(self, factors: List[float], L_m: float, B_m: float, T_m: float):
        self.seed = {
            "factors": [float(f) for f in factors],
            "L_m": float(L_m), "B_m": float(B_m), "T_m": float(T_m),
            "timestamp": self.now(),
        }

    def mark_godzilla(self, L_ft: float, draft_in: float,
                      off1_path: str, out_path: str):
        self.godzilla = {
            "L_ft": float(L_ft), "draft_in": float(draft_in),
            "off1_path": off1_path, "out_path": out_path,
            "timestamp": self.now(),
        }

    def mark_structural(self, result: Dict):
        """Record the winning sweep configuration from a result dict."""
        self.structural = {
            "t_in":            float(result.get("t_in", 0.0)),
            "depth_in":        float(result.get("depth_in", 0.0)),
            # flare_angle_deg (tab-level optimum) wins over the sweep's
            # minimum-feasible flare_req_deg fallback
            "flare_deg":       float(result.get("flare_angle_deg",
                                     result.get("flare_req_deg", 0.0))),
            "gw_width_in":     float(result.get("gw_width_in", 0.0)),
            "gw_height_in":    float(result.get("gw_height_in", 0.0)),
            "gw_fillet_ratio": float(result.get("gw_fillet_ratio", 1.0)),
            "mixes_pcf":       [float(m) for m in result.get("best_mixes", [])],
            "weight_lbs":      float(result.get("weight_lbs", 0.0)),
            "L_ft":            float(result.get("L_ft", 0.0)),
            "draft_in":        float(result.get("draft_in", 0.0)),
            "regression_version": str(result.get("regression_version", "unknown")),
            # Provenance for a design gated on a strength fit the sanity guard
            # rejected: None on a clean fit, otherwise who released it and what
            # was flagged.  A saved design must carry this — otherwise it is
            # indistinguishable from one built on sound data.
            "fit_override":    result.get("fit_override"),
            "timestamp":       self.now(),
        }

    def mark_export(self, label: str, path: str):
        self.exports[label] = path

    # ── (de)serialisation ──────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "seed": self.seed,
            "race_guideline": self.race_guideline,
            "godzilla": self.godzilla,
            "structural": self.structural,
            "exports": dict(self.exports),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CanoeDesignState":
        return cls(
            seed=d.get("seed"),
            race_guideline=d.get("race_guideline"),
            godzilla=d.get("godzilla"),
            structural=d.get("structural"),
            exports=dict(d.get("exports") or {}),
        )
