# config/__init__.py
"""
Global configuration singleton for the ASCE Concrete Canoe Engineering Suite.

This package exposes the same flat namespace as the original config.py so that
all existing ``import config`` statements continue to work unchanged.
Sub-modules in this package (settings.py, constants.py) provide structured,
file-backed configuration — use those for new code.
"""
MAX_MIXES = 2
# Hard cap on the number of DISTINCT mix densities used anywhere on the canoe
# (across all vertical zones and, once longitudinal zoning lands, all
# longitudinal segments).  The team can realistically batch only this many
# unique mixes.  Enforced as a real feasibility gate in evaluate_multizone_
# weight (and in the sweep enumeration), NOT a post-hoc filter: a design using
# more distinct mixes than this can never win, even if it would be lighter.
# Distinct from MAX_MIXES, which sets the pour-layer / density-combo size.
N_MIXES_MAX = 2
EXISTING_MIXES_ONLY = False
ADVANCED_PADDLER_LOADING = False

# --- HULL TEAM LIMITS ---
# File-backed from canoe_suite.cfg (structural:).  These are the DEFAULTS the
# app starts from and that headless callers get; StructuralService.run_sweep
# overwrites FACTOR_OF_SAFETY / DYNAMIC_LOAD_FACTOR per run from the Structural
# tab, and the tab's own fields are seeded from the same settings, so the file
# is the single source for "what this suite assumes unless told otherwise".
from .settings import get_settings as _get_settings_hull

_hull = _get_settings_hull().structural
FACTOR_OF_SAFETY    = float(_hull.factor_of_safety)
DYNAMIC_LOAD_FACTOR = float(_hull.dynamic_load_factor)
# Kupfer utilisation a winner must stay under.  A margin against a NUMERICAL
# non-convergence (the gating utilisation drifts with station count and does not
# plateau), NOT against physics -- the factor of safety covers that separately.
# See structural.kupfer_acceptance_limit() for the measured drift and the
# derivation of 0.95.  1.0 disables it.
KUPFER_ACCEPTANCE_MAX = float(_hull.kupfer_acceptance_max)
# Measured per-doubling drift of that utilisation.  The sweep gates its SEARCH
# at KUPFER_ACCEPTANCE_MAX/(1+drift) so the winner clears acceptance at the
# verification resolution by construction, instead of being caught by hand
# afterwards.  See structural.kupfer_search_limit and canoe_suite.cfg.
KUPFER_DRIFT_PER_DOUBLING = float(_hull.kupfer_drift_per_doubling)

# How many of the lightest survivors the refinement gate re-evaluates at 2n/4n.
# A BUDGET, not a claim that candidate k+1 cannot win: weight itself moves
# ~0.72% under refinement, so the gate WARNS when the cap binds at a gap
# narrower than that rather than truncating silently.
GATE_TOP_K = int(getattr(_hull, "gate_top_k", 12) or 12)

# ── Design methodology ─────────────────────────────────────────────────────
# "LRFD" (default) or "ASD".  The FAILURE CRITERION is the Kupfer-Gerstle
# biaxial envelope under BOTH -- only the load combination and the capacity
# divisor differ.  See structural.design_method / load_combinations /
# capacity_divisor.
DESIGN_METHOD = str(_hull.design_method).upper()

# LRFD load factors (ASCE 7-22 Sec. 2.3).  DYNAMIC_LOAD_FACTOR is separate and
# multiplies the live load first: it is an impact amplification, not safety.
GAMMA_DEAD = float(_hull.gamma_dead)
GAMMA_LIVE = float(_hull.gamma_live)
# Dead COUNTERACTS live at this hull's governing station (measured: M_dead
# -16.4 lb*ft hogging vs M_live +242.9 lb*ft sagging), so the 0.9D alternative
# of ASCE 7-22 Sec. 2.3.1 is enveloped alongside 1.2D.  Without it the relieving
# dead load is factored UP and the demand is understated.
LRFD_COUNTERACTING_DEAD = True
GAMMA_DEAD_COUNTERACTING = 0.9
# Per-case overrides, e.g. {"race_4": (1.3, 1.7)}.  Unlisted cases use the
# globals above.  Case names: "race_4", "race_2", "race_3".
LOAD_FACTORS_BY_CASE = dict(_hull.load_factors_by_case or {})

# LRFD resistance factor.  NOT ACI's 0.90 tension-controlled flexure value:
# that applies to REINFORCED sections where steel yields in tension and the
# concrete's tensile strength is neglected entirely.  This gate is a TENSILE
# criterion on the concrete itself, which is the plain-concrete limit state --
# ACI 318-19 Table 21.2.1(g) / Ch. 14 give phi = 0.60 for plain concrete
# (ACI 318-11 Sec. 9.3.5 used 0.55).  The hull's mesh is not conventional
# reinforcement and does not act before first cracking, which is what this
# check gates on.  See the report's methodology section.
PHI_RESISTANCE = float(_hull.phi_resistance)

# ASD safety factor.  Defaults to FACTOR_OF_SAFETY so the ASD path reproduces
# the pre-refactor result exactly.
OMEGA_SAFETY = float(_hull.omega_safety or FACTOR_OF_SAFETY)
MANEUVER_COUPLING_K = float(_hull.maneuver_coupling_k)
FREEBOARD_MIN_IN    = float(_hull.freeboard_min_in)
# Wall-thickness search bounds.  The sweep never uses a fixed thickness: it
# builds the buildable 1/32" + 0.5 mm grid between these two, so the file
# supplies the RANGE and the Structural tab's min/max fields (seeded from it)
# override per run.  THICKNESS_IN was a scalar nothing read; it is the min now.
THICKNESS_MIN_IN = float(min(_hull.thickness_options_in))
THICKNESS_MAX_IN = float(max(_hull.thickness_options_in))
THICKNESS_IN     = THICKNESS_MIN_IN      # legacy alias
_CREW_VCG_CFG    = float(_hull.crew_vertical_cg_in)
# Read before `del _hull`; published further down with the prose that
# explains them.
_SWEEP_EXECUTOR_CFG = str(_hull.sweep_executor)
_SWEEP_MAX_WORKERS_CFG = (int(_hull.sweep_max_workers)
                          if _hull.sweep_max_workers else None)

del _hull

# Minimum transverse metacentric height GM_T [in] the hull must clear for the
# structural sweep to accept it -- transverse (roll) stability as a measurable
# constraint.  Set per-run from the Hull Configurator's "Min GMT" input.  0.0 =
# no stability gate (the default, so sweeps/tests that never set it are
# unaffected).  The GM_T is computed with the same definition as the Hull
# Configurator (compute_hydrostatics), so the input and the check agree.
MIN_GMT_IN = 0.0

# ── Sweep executor ───────────────────────────────────────────────────────────
# 'thread' (default) or 'process'.  Threads are the safe default: they are what
# every historical result was produced with, and spawn has only been validated
# on the UNFROZEN interpreter -- a PyInstaller build is untested.  Processes
# escape the GIL for the parts of the sweep that do not release it, at the cost
# of re-importing `config` in each child, which is why every task carries a
# fingerprinted RunState (see config/run_state.py).
#
# NOT a run-level attribute: it selects HOW the sweep runs, never WHAT is
# computed, so it deliberately does not appear in RUN_LEVEL_CONFIG_ATTRS and
# cannot change a result.  If it ever could, that is a bug in the RunState.
SWEEP_EXECUTOR = _SWEEP_EXECUTOR_CFG

# Worker count.  None = automatic: physical cores for processes (dense float
# math -- the two hyperthreads of a core share one FPU), 8 for threads.
SWEEP_MAX_WORKERS = _SWEEP_MAX_WORKERS_CFG

# Kneeling crew CG height above the keel [in] -- an ASSUMPTION, and the one that
# dominates transverse stability: the crew outweighs the hull about five to one,
# so their CG largely sets the combined KG that GM_T is measured from.  Stated
# here rather than buried so it can be challenged.  12 in is a kneeling paddler's
# torso/hip centroid over a shallow hull bottom; measure it if GM_T ever gates.
# LIVE: canoe_suite.cfg -> hull.crew_vertical_cg_in.
CREW_VERTICAL_CG_IN = _CREW_VCG_CFG
del _CREW_VCG_CFG

# Construction application layers (through-thickness troweled passes) the wall
# is built up from -- a DERIVED manufacturing descriptor, set per-run by
# StructuralService from the UI.  Orthogonal to the vertical pour zones and to
# the mix count; NEVER an input to the sweep (the winner is independent of it).
# Drives the report's layer schedule, the trowel-ability check, and the
# punching effective-depth (mesh sits on a real layer interface, not t/2).
# The file-backed default is assigned from canoe_suite.cfg (structural:) below,
# once the settings singleton is available.

# Design turn for the transverse-hoop / lateral structural checks (M_lat ~
# v^2 / R).  File-backed via canoe_suite.cfg (structural:).  4.0 m is a
# realistic minimum slalom radius for a ~6.1 m hull; 3.0 m was effectively a
# pivot-turn at full speed and inflated the hoop demand at the bow/stern tips.
from .settings import get_settings as _get_settings_mnv

_struct = _get_settings_mnv().structural
MANEUVER_SPEED_MPS     = _struct.maneuver_speed_mps
MANEUVER_TURN_RADIUS_M = _struct.maneuver_turn_radius_m
N_CONSTRUCTION_LAYERS  = max(int(_struct.n_construction_layers), 1)
del _struct

# Crew weights — file-backed from canoe_suite.cfg (crew:); the Structural tab's
# per-case weight fields are seeded from these and override per run.
from .settings import get_settings as _get_settings_race

_race = _get_settings_race().crew
# Sustained watts per paddler for the race model.  The Race tab seeds its
# "Watts/paddler" field from this and sets canoe_physics.PADDLER_POWER_W per run.
PADDLER_POWER_W = float(_race.paddler_power_w)

# --- RACE CATEGORIES: the five ASCE 2026 races (SINGLE SOURCE OF TRUTH) ---
# LIVE: canoe_suite.cfg -> crew.race_categories.  Carries per-seat sex, stature
# AND weight for every category.  Sex picks the sex-specific segment mass
# fractions and stature scales the segment lengths (physics.biomechanics);
# weights drive the bending envelope and the freeboard ceiling.
#
# Weights live HERE, not in a per-size list: 2M and 2F are different crews of
# the same size, and one list per size cannot express that.
from .settings import RACE_CATEGORY_ORDER, RACE_CATEGORY_SIZES  # noqa: E402

RACE_CATEGORIES = {k: {"sexes": list(v["sexes"]),
                       "heights_in": list(v["heights_in"]),
                       "weights_lb": list(v["weights_lb"]),
                       # 1-based seat each paddler is pinned to, None = any.
                       # Pins SHRINK the enumerated arrangement set and so make
                       # the envelope less conservative -- disclosed, never
                       # silent.
                       "seat_pins": list(v.get("seat_pins")
                                         or [None] * len(v["weights_lb"]))}
                   for k, v in _race.race_categories.items()}

# Legacy per-size aliases -- COMPUTED ON READ, never stored.
#
# These deliberately have NO module-level binding.  They are resolved by the
# module __getattr__ below, every time, from RACE_CATEGORIES.  That is the whole
# point: while they were stored values, StructuralService assigned them per run
# without touching RACE_CATEGORIES, so a sweep ran with the UI's crew weights in
# the punching check and the cfg's weights in the bending envelope -- two
# different crews in one analysis.  A stored alias can drift from its source; a
# derived one cannot.
#
# RACE_2/3_PERSON resolve to the HEAVIER of the two same-size crews: anything
# reading a single per-size list is asking "what does a crew of this size weigh"
# and the conservative answer is the heavy one.  New code should use
# race_category_crew() / crew_load_cases() and name the race it means.
_DERIVED_CREW_ALIASES = ("RACE_4_PERSON", "RACE_2_PERSON", "RACE_3_PERSON")


def _derive_crew_alias(name):
    if name == "RACE_4_PERSON":
        return list(RACE_CATEGORIES["4_coed"]["weights_lb"])
    size = 2 if name == "RACE_2_PERSON" else 3
    same = [c for k, c in RACE_CATEGORIES.items()
            if RACE_CATEGORY_SIZES[k] == size]
    return list(max(same, key=lambda c: sum(c["weights_lb"]))["weights_lb"])


def __getattr__(name):
    """PEP 562 module-level attribute hook -- see _DERIVED_CREW_ALIASES above."""
    if name in _DERIVED_CREW_ALIASES:
        return _derive_crew_alias(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def set_race_categories(cats):
    """
    Replace the per-run crew definition.  THE ONLY supported way to change crew
    weights for a run: the legacy aliases follow automatically because they are
    derived, so no caller can update one and forget the other.
    """
    global RACE_CATEGORIES
    RACE_CATEGORIES = {k: {"sexes": list(v["sexes"]),
                           "heights_in": list(v["heights_in"]),
                           "weights_lb": [float(w) for w in v["weights_lb"]],
                           "seat_pins": list(v.get("seat_pins")
                                             or [None] * len(v["weights_lb"]))}
                       for k, v in cats.items()}
    return RACE_CATEGORIES
# Kneeling posture. LIVE: crew.kneel_posture / kneel_hip_height_in /
# torso_angle_deg.  KNEEL_HIP_HEIGHT_IN is None until someone measures a
# paddler -- it is the input that dominates loaded stability, and a measurement
# supersedes the posture preset entirely.
KNEEL_POSTURE = str(_race.kneel_posture)
KNEEL_HIP_HEIGHT_IN = _race.kneel_hip_height_in
TORSO_ANGLE_DEG = float(_race.torso_angle_deg)
del _race


def race_category_crew(category):
    """``(sexes, heights_in, weights_lb)`` for one ASCE race category."""
    cat = RACE_CATEGORIES.get(str(category))
    if cat is None:
        raise KeyError(f"unknown race category {category!r}; "
                       f"expected one of {list(RACE_CATEGORY_ORDER)}")
    return (list(cat["sexes"]), list(cat["heights_in"]),
            list(cat["weights_lb"]))


def crew_seat_layout():
    """
    ``[(category, slice), ...]`` mapping each race into the flat seating vector.

    ONE definition of the layout, in RACE_CATEGORY_ORDER, so the seating solve,
    the stored seat sets and the refinement verifier cannot drift apart.  A
    design verified against a different ordering than it was optimised under
    would be verifying a different boat.
    """
    out, i = [], 0
    for name in RACE_CATEGORY_ORDER:
        n = RACE_CATEGORY_SIZES[name]
        out.append((name, slice(i, i + n)))
        i += n
    return out


def crew_seat_vector_len():
    """Total length of the flat seating vector (14 for the five ASCE races)."""
    return sum(RACE_CATEGORY_SIZES[n] for n in RACE_CATEGORY_ORDER)


def crew_load_cases(seats, with_pins=True):
    """
    ``[(category, coords_ft, weights_lb, seat_pins), ...]`` -- the load cases
    the bending envelope runs, in RACE_CATEGORY_ORDER.

    ``seats`` is either the flat seating vector or a {category: coords} mapping.
    Every case carries its OWN weights AND its own pins, so no caller can apply
    one crew's weights (or one crew's pinning) to another's seats.

    ``with_pins=False`` returns the same cases with every pin cleared -- the
    FULLY ENUMERATED comparison basis.  It exists so the report can price what
    the pins cost: same weights, same seats, same crew CG, pins the only
    difference.
    """
    def _entry(name, coords):
        cat = RACE_CATEGORIES[name]
        pins = (list(cat["seat_pins"]) if with_pins
                else [None] * len(cat["weights_lb"]))
        return (name, list(coords), list(cat["weights_lb"]), pins)

    cases = []
    if isinstance(seats, dict):
        for name in RACE_CATEGORY_ORDER:
            c = seats.get(name)
            if c is not None:
                cases.append(_entry(name, c))
        return cases
    n_need = crew_seat_vector_len()
    if len(seats) != n_need:
        raise ValueError(f"seating vector has {len(seats)} entries, "
                         f"expected {n_need} for "
                         f"{list(RACE_CATEGORY_ORDER)}")
    for name, sl in crew_seat_layout():
        cases.append(_entry(name, seats[sl]))
    return cases


def any_seats_pinned():
    """True when at least one paddler in any race is pinned to a seat."""
    return any(p is not None
               for c in RACE_CATEGORIES.values() for p in c["seat_pins"])


def heaviest_crew_lbs():
    """
    Total weight of the HEAVIEST race crew [lb].

    COMPUTED, never assumed.  This feeds the freeboard ceiling, and the old code
    took ``sum(RACE_4_PERSON)`` on the assumption that the 4-person crew is
    always the heaviest.  With five races that is a guess: a 3M crew of large
    paddlers can rival a coed four.  Guessing here would silently admit hulls
    that swamp under the real worst case.
    """
    return max(float(sum(c["weights_lb"])) for c in RACE_CATEGORIES.values())


def heaviest_crew_category():
    """Name of the crew behind :func:`heaviest_crew_lbs`, for reporting."""
    return max(RACE_CATEGORY_ORDER,
               key=lambda n: sum(RACE_CATEGORIES[n]["weights_lb"]))

# --- KNEELING CONTACT (punching check) ---
# Paddlers are lifted and placed onto BOTH knees rather than stepping in
# heel-first, so the bottom sees two circular knee patches, not one square
# patch and not a single concentrated heel.  File-backed via canoe_suite.cfg
# (crew:) so these are tunable without editing Python.
from .settings import get_settings as _get_settings

_crew = _get_settings().crew
KNEE_DIA_IN      = _crew.knee_dia_in
KNEE_SPACING_IN  = _crew.knee_spacing_in
KNEE_LOAD_SPLIT  = _crew.knee_load_split
KNEE_PAD_IN      = _crew.knee_pad_in
KNEE_PAD_MAX_IN  = 0.5          # ASCE rules cap on knee-pad thickness

# Knee/toe split — the ONLY definition of these numbers in the codebase.
# Two bases, deliberately different, each matched to its limit state:
#   BENDING  (envelopes, load distribution) — team-measured 90/10.  Splitting
#     weight to the toes spreads the load and LOWERS peak moment, so this is
#     an accuracy gain, not a conservatism gain; see docs/DESIGN_ROADMAP.md.
#   PUNCHING (local two-way shear) — 100/0.  No toe relief is credited: the
#     full paddler weight stays on the knees, which is the conservative case
#     for a local contact check and keeps the report's stated punching basis.
# Both apply only when ADVANCED_PADDLER_LOADING is enabled; with it off the
# model uses a single point load per paddler (equivalent to 100/0, unspread).
# PADDLER_LOAD_ENVELOPE: design the bending/shear envelopes to the WORST of
# both paddler load models (single point load at the knee, and the measured
# knee/toe split) rather than to whichever one ADVANCED_PADDLER_LOADING selects.
# Neither model bounds the other -- the toe split lowers sagging and shear but
# raises hogging -- so picking one leaves the other limit state unchecked. The
# envelope is a bound by construction and stays valid if the measured split is
# revised. Costs one extra beam solve per case; ADVANCED_PADDLER_LOADING then
# only matters when this is off.
PADDLER_LOAD_ENVELOPE = True

KNEE_FRAC_BENDING   = _crew.knee_frac_bending
KNEE_FRAC_PUNCHING  = _crew.knee_frac_punching
TOE_OFFSET_IN       = _crew.toe_offset_in
KNEE_FRAC_MEASURED_N = _crew.knee_frac_measured_n
del _crew

# Paddler seating control (set per-run by StructuralService from the UI):
# FIXED_PADDLER_POSITIONS — dict {"4": [ft x4], "2": [ft x2], "3": [ft x3]}
#   measured from the bow. When set, the sweep uses these positions verbatim
#   instead of SLSQP-optimizing the seating arrangement.
# PADDLER_POS_VARIANCE_FT — expected deviation of real paddlers from the
#   optimized position [ft]; moment/shear envelopes include perturbed
#   seatings so the design stays feasible when humans drift off-station.
FIXED_PADDLER_POSITIONS = None
PADDLER_POS_VARIANCE_FT = 0.0

# --- MIX TEAM LIMITS ---
# File-backed from canoe_suite.cfg (mix:), same pattern as the maneuver and
# knee blocks above.  These are the DEFAULTS; StructuralService.run_sweep
# overwrites MAX_DENSITY_PCF per run from the Structural tab's "Max Mix (pcf)"
# input, so the UI wins for a run and the file sets what the UI starts from.
#
# MIN_DENSITY_PCF is the team's MANUFACTURING floor, and it now BOUNDS the
# sweep (it was documentation only until the Min Mix field existed).  It is
# applied ON TOP of the Mix Lab regression's viability floor -- the lightest
# mix it still predicts any strength for -- and whichever is HIGHER wins, so
# setting it below that floor changes nothing.  The sweep's search-space
# banner names which one bound the range, so the two cannot be confused.
from .settings import get_settings as _get_settings_mix

_mix = _get_settings_mix().mix
MIN_DENSITY_PCF = float(_mix.min_density_pcf)
MAX_DENSITY_PCF = float(_mix.max_density_pcf)
del _mix

# Print the per-gate diagnostics (rejection histogram, searched ranges, the
# lowest-D/C configuration) after a sweep that SUCCEEDS as well.  A failing
# sweep always prints them.
SWEEP_DIAGNOSTICS = False

# ─── SQLite Mix Database Singleton ────────────────────────────────
_mix_db = None


def get_mix_db():
    """Lazy singleton accessor for the concrete mix database."""
    global _mix_db
    if _mix_db is None:
        from mix_database import MixDatabase
        _mix_db = MixDatabase()
    return _mix_db


def get_mix_properties(density_pcf):
    """
    DESIGN structural properties for a concrete mix at the given density.

    The divisor applied to the nominal strengths is set by the active design
    methodology (structural.capacity_divisor), NOT by FACTOR_OF_SAFETY
    directly:

        ASD   -> Omega            allowable      = f_n / Omega
        LRFD  -> 1/phi            design capacity = phi * f_n

    so a single ``strength_t`` / ``strength_c`` flows downstream and no
    consumer needs to know which method is active.  Under ASD with
    Omega = FACTOR_OF_SAFETY this is exactly the historical behaviour.

    Falls back to the empirical regression when no lab data exists.
    """
    try:
        from structural import capacity_divisor
        div = capacity_divisor()
    except ImportError:           # pragma: no cover - import cycle safety net
        div = FACTOR_OF_SAFETY
    return get_mix_db().get_mix_properties(density_pcf, fos=div)
