# config/settings.py
"""
Loads canoe_suite.cfg (YAML) into validated dataclasses.

Usage
-----
    from config.settings import Settings
    cfg = Settings.load()          # reads canoe_suite.cfg next to this repo
    cfg = Settings.load(path)      # explicit path
    cfg = Settings.defaults()      # hardcoded fallback (no file needed)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

_CFG_NAME = "canoe_suite.cfg"

#: How the live Settings were obtained, for the sweep banner to report.
#: A silent fallback to defaults makes every cfg edit a no-op while the file on
#: disk still reads what the user typed -- which is indistinguishable, from the
#: outside, from the wiring being broken.  So the outcome is RECORDED.
CONFIG_SOURCE = {"path": None, "loaded": False, "reason": "not yet loaded"}


@dataclass
class StructuralSettings:
    factor_of_safety: float = 1.5
    dynamic_load_factor: float = 1.2
    # Kupfer utilisation a design must stay under to be ACCEPTED.  Not 1.0:
    # the gating utilisation drifts ~1-2% per station-count doubling with no
    # observed plateau, so a winner accepted at 1.0 on the search grid fails
    # one refinement step.  See structural.kupfer_acceptance_limit().
    kupfer_acceptance_max: float = 0.95
    # Sweep executor: "thread" or "process".  Processes escape the GIL, worth
    # >2x at equal worker count; each spawned child re-imports config fresh, so
    # the run's settings reach it only via the fingerprinted RunState.
    sweep_executor: str = "thread"
    # Sweep worker count.  null/None = automatic: physical cores for processes
    # (dense float math -- the two hyperthreads of a core share one FPU), 8 for
    # threads.
    sweep_max_workers: int | None = None
    # Measured growth of the governing Kupfer utilisation per doubling of the
    # station count.  The SEARCH limit is acceptance/(1+drift), so the winner
    # clears acceptance at the verification resolution by construction.
    kupfer_drift_per_doubling: float = 0.0219
    # Kneeling paddler's centre of gravity above the keel [in].  An ASSUMPTION,
    # not a measurement, and it dominates the loaded GM_T: the crew outweighs
    # the hull ~5:1, so their CG largely sets the combined KG.
    crew_vertical_cg_in: float = 12.0
    # Design methodology: "LRFD" (default) or "ASD".  The Kupfer-Gerstle
    # biaxial failure criterion is used under BOTH; only the load combination
    # and the capacity divisor change.
    design_method: str = "LRFD"
    gamma_dead: float = 1.2               # ASCE 7-22 Sec. 2.3.1
    gamma_live: float = 1.6
    # Per-case (gamma_dead, gamma_live) overrides keyed by case name
    # ("race_4" / "race_2" / "race_3"); unlisted cases use the globals.
    load_factors_by_case: dict = field(default_factory=dict)
    # LRFD resistance factor.  0.60 = ACI 318-19 Table 21.2.1(g), PLAIN
    # concrete -- not the 0.90 tension-controlled value, which presumes a
    # reinforced section carrying tension in steel.  See config/__init__.py.
    phi_resistance: float = 0.60
    omega_safety: float = 1.5             # ASD; None -> factor_of_safety
    maneuver_coupling_k: float = 0.5
    # Design turn for the transverse-hoop / lateral checks.  M_lat ~ v^2 / R,
    # so these set the centripetal load the hull is designed to survive in a
    # slalom turn.  4.0 m is a realistic minimum radius for a ~6.1 m hull; a
    # tighter radius approaches a pivot-turn at speed, which is not the ASCE
    # slalom geometry.
    maneuver_speed_mps: float = 2.5
    maneuver_turn_radius_m: float = 4.0
    thickness_options_in: List[float] = field(default_factory=lambda: [0.375, 0.5, 0.625, 0.75])
    freeboard_min_in: float = 6.0
    # Number of successive troweled application layers (through-thickness) the
    # wall is built up from.  Each layer is one pass of the SAME mix at a given
    # height; N equal layers sum to the local wall thickness.  This is a
    # CONSTRUCTION descriptor (a derived manufacturing output), ORTHOGONAL to
    # the vertical pour zones and to the mix count -- it never enters the sweep.
    n_construction_layers: int = 3
    # Bow/stern stem-rocker sweep for CAD export [in] (side-view rounding;
    # plan view stays pointed).  None → "auto"; 0.0 → vertical stem ends.
    bow_stern_rocker_in: float | None = None


@dataclass
class CrewSettings:
    race_4_person_lbs: List[float] = field(default_factory=lambda: [160.0, 150.0, 170.0, 160.0])
    race_2_person_lbs: List[float] = field(default_factory=lambda: [170.0, 160.0])
    race_3_person_lbs: List[float] = field(default_factory=lambda: [160.0, 160.0, 160.0])
    paddler_power_w: float = 75.0
    # Kneeling-contact geometry for the punching check.  Paddlers are lifted
    # and placed onto BOTH knees (no heel-first step-in), so the contact is two
    # circular knee patches, not one square patch.
    knee_dia_in: float = 3.0
    knee_spacing_in: float = 8.0          # centre-to-centre, hips-width kneel
    knee_load_split: float = 0.5          # fraction of total on the worst knee
    knee_pad_in: float = 0.0              # design pad thickness (ASCE cap 0.5)
    # --- knee/toe load split (TWO BASES, one source of truth) ---
    # Fraction of a paddler's weight carried on the KNEES; the remainder goes
    # to the toes, toe_offset_in aft.  Two different bases on purpose:
    #   bending  : team-MEASURED value, used for envelopes/load distribution
    #   punching : 1.0 -- no toe relief, the conservative local-contact case
    # Only applied when ADVANCED_PADDLER_LOADING is on.
    knee_frac_bending: float = 1.00
    knee_frac_punching: float = 1.00
    toe_offset_in: float = 17.0
    knee_frac_measured_n: int = 1         # sample size behind knee_frac_bending
    # --- ASCE race categories: per-seat sex and stature ---
    # The FIVE 2026 races.  Sex selects the sex-specific segment mass fractions
    # in physics.biomechanics; stature scales every segment length.  Both are
    # needed for the crew-CG model and neither existed before.
    #
    # THE SINGLE SOURCE OF TRUTH for crew composition.  Weights live here too,
    # per CATEGORY -- the old race_*_person_lbs lists were keyed by crew SIZE and
    # so could not express 2M and 2F as different crews.  Those lists are still
    # accepted as a legacy seed (see _parse_race_categories) and config exposes
    # RACE_*_PERSON as derived aliases, but nothing should read them directly.
    race_categories: Dict[str, Dict[str, list]] = field(default_factory=lambda: {
        "2M":     {"sexes": ["M", "M"], "heights_in": [70.0, 70.0],
                   "weights_lb": [175.0, 165.0], "seat_pins": [None, None]},
        "2F":     {"sexes": ["F", "F"], "heights_in": [65.0, 65.0],
                   "weights_lb": [145.0, 135.0], "seat_pins": [None, None]},
        "3M":     {"sexes": ["M", "M", "M"], "heights_in": [70.0, 70.0, 70.0],
                   "weights_lb": [175.0, 170.0, 165.0],
                   "seat_pins": [None, None, None]},
        "3F":     {"sexes": ["F", "F", "F"], "heights_in": [65.0, 65.0, 65.0],
                   "weights_lb": [145.0, 140.0, 135.0],
                   "seat_pins": [None, None, None]},
        "4_coed": {"sexes": ["M", "M", "F", "F"],
                   "heights_in": [70.0, 70.0, 65.0, 65.0],
                   "weights_lb": [175.0, 165.0, 145.0, 135.0],
                   "seat_pins": [None, None, None, None]},
    })
    # Kneeling posture driving the crew CG.  hip height DOMINATES the result --
    # the two postures differ by ~8 in of hip datum while the whole torso-angle
    # range moves the CG only ~2 in.  Measure it and set kneel_hip_height_in.
    kneel_posture: str = "B_upright_kneel"
    kneel_hip_height_in: Optional[float] = None   # measured; None = preset
    torso_angle_deg: float = 70.0                 # from HORIZONTAL, 90 = upright


#: The five ASCE 2026 race categories, with the crew size each implies.  Named
#: here so the category -> per-seat mapping is EXPLICIT and testable rather than
#: positional-by-convention: "4_coed" is 2 male + 2 female, and reading that off
#: a list index would be exactly the silent-convention bug worth avoiding.
RACE_CATEGORY_SIZES = {"2M": 2, "2F": 2, "3M": 3, "3F": 3, "4_coed": 4}

#: Deterministic ordering of the five races.  EVERY consumer that flattens the
#: crews into one vector -- the seating solve's variable layout, the stored seat
#: sets, the envelope's case list -- must use this order, or a design verified
#: against one ordering would be re-evaluated against another.
RACE_CATEGORY_ORDER = ("2M", "2F", "3M", "3F", "4_coed")

#: Legacy per-SIZE weight keys, accepted as a seed for categories that do not
#: state their own weights.  Kept so an existing canoe_suite.cfg still loads;
#: the categories are authoritative.
_LEGACY_WEIGHT_KEY = {2: "race_2_person_lbs", 3: "race_3_person_lbs",
                      4: "race_4_person_lbs"}


def _parse_race_categories(raw, crew_block=None):
    """
    Validate the race-category block: every category present, each with as many
    sexes, heights and weights as its name implies, and 4_coed genuinely 2M+2F.

    Fails LOUDLY.  A silently-defaulted crew composition would feed the
    stability model a wrong sex mix and the envelope a wrong load, both
    producing plausible numbers -- worse than a crash.

    A category may omit ``weights_lb``, in which case the legacy per-size list
    (race_2/3/4_person_lbs) seeds it.  That fallback is what lets 2M and 2F
    share weights, which is exactly the limitation the split removes, so it
    warns rather than passing silently.
    """
    defaults = CrewSettings().race_categories
    raw = raw or {}
    crew_block = crew_block or {}
    out = {}
    for name in RACE_CATEGORY_ORDER:
        size = RACE_CATEGORY_SIZES[name]
        if raw and name not in raw:
            raise ValueError(
                f"canoe_suite.cfg crew.race_categories is missing {name!r}; "
                f"all five ASCE races are required: "
                f"{list(RACE_CATEGORY_ORDER)}")
        blk = (raw.get(name) if raw else None) or {}
        d = defaults[name]
        sexes = ([str(s).strip().upper()[:1] for s in blk["sexes"]]
                 if blk.get("sexes") else list(d["sexes"]))
        heights = ([float(h) for h in blk["heights_in"]]
                   if blk.get("heights_in") else list(d["heights_in"]))
        if blk.get("weights_lb"):
            weights = [float(w) for w in blk["weights_lb"]]
        else:
            legacy = crew_block.get(_LEGACY_WEIGHT_KEY[size])
            weights = ([float(w) for w in legacy] if legacy
                       else list(d["weights_lb"]))
        if not (len(sexes) == len(heights) == len(weights) == size):
            raise ValueError(
                f"race category {name!r} implies {size} paddlers but carries "
                f"{len(sexes)} sexes, {len(heights)} heights and "
                f"{len(weights)} weights")
        bad = [s for s in sexes if s not in ("M", "F")]
        if bad:
            raise ValueError(f"race category {name!r} has invalid sexes {bad}; "
                             f"expected 'M' or 'F'")
        if any(h <= 0.0 for h in heights):
            raise ValueError(f"race category {name!r} has a non-positive height")
        if any(w <= 0.0 for w in weights):
            raise ValueError(f"race category {name!r} has a non-positive weight")
        # Seat pins: element i is the 1-based SEAT (bow->stern) that paddler i
        # must occupy, or null for "any".  Pinning REDUCES the arrangement set
        # and so makes the envelope less conservative -- a bad pin that is
        # silently dropped would quietly enumerate the wrong set, so every
        # failure mode below raises.
        pins_raw = blk.get("seat_pins")
        if pins_raw is None:
            pins = [None] * size
        else:
            if len(pins_raw) != size:
                raise ValueError(
                    f"race category {name!r} has {len(pins_raw)} seat_pins but "
                    f"{size} paddlers; give one entry per paddler (null = any)")
            pins = []
            for i, q in enumerate(pins_raw):
                if q is None or (isinstance(q, str)
                                 and q.strip().lower() in ("", "any", "null")):
                    pins.append(None)
                    continue
                s = int(q)
                if not (1 <= s <= size):
                    raise ValueError(
                        f"race category {name!r}: paddler {i + 1} is pinned to "
                        f"seat {s}, outside 1..{size}")
                pins.append(s)
            used = [s for s in pins if s is not None]
            if len(set(used)) != len(used):
                dupes = sorted({s for s in used if used.count(s) > 1})
                raise ValueError(
                    f"race category {name!r}: seats {dupes} are pinned to more "
                    f"than one paddler; a seat holds exactly one paddler")
        # the coed composition is a RULE, not a preference
        if name == "4_coed" and sorted(sexes) != ["F", "F", "M", "M"]:
            raise ValueError(
                f"race category '4_coed' must be 2 male + 2 female, got {sexes}")
        if name.endswith("M") and set(sexes) != {"M"}:
            raise ValueError(f"race category {name!r} must be all male, got {sexes}")
        if name.endswith("F") and set(sexes) != {"F"}:
            raise ValueError(f"race category {name!r} must be all female, got {sexes}")
        out[name] = {"sexes": sexes, "heights_in": heights,
                     "weights_lb": weights, "seat_pins": pins}
    return out


@dataclass
class MixSettings:
    min_density_pcf: float = 60.0
    max_density_pcf: float = 80.0
    # Explicit path to the SQLite mix database.  Empty/None = the repo's own
    # concrete_mixes.db, which is what a fresh clone should use.  A stale
    # absolute path from another machine must NOT silently redirect the mix
    # data, so the consumer ignores this when the file does not exist.
    db_path: str | None = None
    # Age at which a break counts as the mix's strength.  Earlier breaks
    # (1-day, 7-day) are still recorded, but they are held out of the
    # density -> strength regression: a 7-day cylinder is the same mix at a
    # fraction of its strength, and averaging it in drags the whole fit down.
    design_age_days: int = 28
    # Which measured density feeds the regression when a specimen has both:
    # "dry" or "wet".  The other is stored alongside either way.
    density_basis: str = "dry"
    # Lab-specimen geometry, used to fill in the columns the team's CSV
    # leaves to a spreadsheet formula: density = weight / volume, and
    # f_r = P*L/(b*d^2).  A labelled constant inside an imported CSV wins
    # over these, since it records the specimen actually broken.
    cylinder_volume_ft3: float = 0.058148148     # 4 x 8 in cylinder
    cube_volume_ft3: float = 0.00462963          # 2 in cube
    beam_volume_ft3: float = 0.008463542         # 6.5 x 1.5 x 1.5 in prism
    flexure_span_in: float = 4.75
    flexure_base_in: float = 1.5
    flexure_depth_in: float = 1.5


@dataclass
class WaterSettings:
    rho_kg_m3: float = 998.0
    nu_m2_s: float = 1.139e-6


def config_search_paths():
    """
    Where ``canoe_suite.cfg`` is looked for, in priority order.

    THE FROZEN BUILD NEVER LOADED IT.  The old rule was "next to this source
    file", which under PyInstaller resolves inside ``_internal/`` -- a
    directory the cfg was not shipped to and that no user would think to edit.
    Every frozen run therefore fell back to built-in defaults: factor of
    safety, design method, density bounds, race categories, station count, all
    of them.  The `[!! CONFIG DEFAULTED]` banner said so on every run, which is
    the only reason it was findable.

    Order matters, and it is chosen so the file stays EDITABLE:

      1. beside the executable -- for a frozen build this is the folder the
         team actually has, so it is the copy they can change.  In the
         interpreter it is wherever python.exe lives, which is harmless: the
         repo copy below wins in practice because that path will not exist.
      2. the current working directory -- how a CLI run picks up a cfg sitting
         next to the offsets it is being pointed at.
      3. next to this file (the repo root, or the bundled copy inside
         ``_internal``) -- the LAST resort, so a fresh install still works
         before anyone has edited anything.

    A bundled default that silently outranked a user's own edits would be the
    same defect in a new costume, which is why it is last rather than first.
    """
    import sys as _sys
    out = []
    exe_dir = os.path.dirname(os.path.abspath(_sys.executable))
    if getattr(_sys, "frozen", False):
        out.append(os.path.join(exe_dir, _CFG_NAME))
    out.append(os.path.join(os.path.abspath(os.getcwd()), _CFG_NAME))
    out.append(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), _CFG_NAME))
    seen, uniq = set(), []
    for c in out:
        k = os.path.normcase(c)
        if k not in seen:
            seen.add(k); uniq.append(c)
    return uniq


@dataclass
class Settings:
    structural: StructuralSettings = field(default_factory=StructuralSettings)
    crew: CrewSettings = field(default_factory=CrewSettings)
    mix: MixSettings = field(default_factory=MixSettings)
    water: WaterSettings = field(default_factory=WaterSettings)

    # ------------------------------------------------------------------
    @classmethod
    def defaults(cls) -> "Settings":
        """Return default settings without reading any file."""
        return cls()

    @classmethod
    def load(cls, path: str | None = None) -> "Settings":
        """
        Load settings from a YAML file.

        If ``path`` is None the loader SEARCHES; see ``config_search_paths``.
        Falls back to defaults if the file is absent or PyYAML is missing --
        loudly, via CONFIG_SOURCE, never silently.
        """
        tried = []
        if path is None:
            tried = config_search_paths()
            path = next((c for c in tried if os.path.exists(c)), tried[-1])

        CONFIG_SOURCE["path"] = path
        CONFIG_SOURCE["tried"] = tried

        try:
            import yaml
        except ImportError:
            CONFIG_SOURCE.update(
                loaded=False,
                reason="PyYAML is not installed in this interpreter, so EVERY "
                       "setting fell back to its built-in default")
            return cls.defaults()

        if not os.path.exists(path):
            _where = (path if not tried else
                      " or ".join(tried))
            CONFIG_SOURCE.update(
                loaded=False,
                reason=f"no config file at {_where}, so EVERY setting fell "
                       f"back to its built-in default")
            return cls.defaults()

        CONFIG_SOURCE.update(loaded=True, reason="ok")

        with open(path, "r") as fh:
            raw = yaml.safe_load(fh) or {}

        s = raw.get("structural", {})
        _exec = str(s.get("sweep_executor", "thread") or "thread").lower().strip()
        if _exec not in ("thread", "process"):
            raise ValueError(
                f"canoe_suite.cfg structural.sweep_executor must be 'thread' or "
                f"'process', got {_exec!r}")
        _mw = s.get("sweep_max_workers", None)
        _mw = int(_mw) if _mw else None
        c = raw.get("crew", {})
        m = raw.get("mix", {})
        w = raw.get("water", {})

        # "stem_overhang_in" is the current key; "bow_stern_rocker_in" is the
        # former name, still accepted so existing canoe_suite.cfg files load.
        rocker_raw = s.get("stem_overhang_in",
                           s.get("bow_stern_rocker_in", "auto"))
        if rocker_raw is None or (isinstance(rocker_raw, str)
                                  and rocker_raw.strip().lower() == "auto"):
            rocker_in = None                      # auto mode
        else:
            rocker_in = float(rocker_raw)

        return cls(
            structural=StructuralSettings(
                factor_of_safety=float(s.get("factor_of_safety", 1.5)),
                dynamic_load_factor=float(s.get("dynamic_load_factor", 1.2)),
                kupfer_acceptance_max=float(s.get("kupfer_acceptance_max", 0.95)),
                sweep_executor=_exec,
                sweep_max_workers=_mw,
                kupfer_drift_per_doubling=float(
                    s.get("kupfer_drift_per_doubling", 0.0219)),
                crew_vertical_cg_in=float(s.get("crew_vertical_cg_in", 12.0)),
                design_method=str(s.get("design_method", "LRFD")).upper().strip(),
                gamma_dead=float(s.get("gamma_dead", 1.2)),
                gamma_live=float(s.get("gamma_live", 1.6)),
                load_factors_by_case={
                    str(k): (float(v[0]), float(v[1]))
                    for k, v in (s.get("load_factors_by_case") or {}).items()
                    if isinstance(v, (list, tuple)) and len(v) == 2},
                phi_resistance=float(s.get("phi_resistance", 0.60)),
                omega_safety=float(s.get("omega_safety",
                                         s.get("factor_of_safety", 1.5))),
                maneuver_coupling_k=float(s.get("maneuver_coupling_k", 0.5)),
                maneuver_speed_mps=float(s.get("maneuver_speed_mps", 2.5)),
                maneuver_turn_radius_m=float(s.get("maneuver_turn_radius_m", 4.0)),
                thickness_options_in=[float(x) for x in s.get("thickness_options_in", [0.375, 0.5, 0.625, 0.75])],
                freeboard_min_in=float(s.get("freeboard_min_in", 6.0)),
                n_construction_layers=max(int(s.get("n_construction_layers", 3)), 1),
                bow_stern_rocker_in=rocker_in,
            ),
            crew=CrewSettings(
                race_4_person_lbs=[float(x) for x in c.get("race_4_person_lbs", [160, 150, 170, 160])],
                race_2_person_lbs=[float(x) for x in c.get("race_2_person_lbs", [170, 160])],
                race_3_person_lbs=[float(x) for x in c.get("race_3_person_lbs", [160, 160, 160])],
                paddler_power_w=float(c.get("paddler_power_w", 75.0)),
                knee_dia_in=float(c.get("knee_dia_in", 3.0)),
                knee_spacing_in=float(c.get("knee_spacing_in", 8.0)),
                knee_load_split=float(c.get("knee_load_split", 0.5)),
                knee_pad_in=float(c.get("knee_pad_in", 0.0)),
                knee_frac_bending=float(c.get("knee_frac_bending", 1.00)),
                knee_frac_punching=float(c.get("knee_frac_punching", 1.00)),
                toe_offset_in=float(c.get("toe_offset_in", 17.0)),
                knee_frac_measured_n=int(c.get("knee_frac_measured_n", 1)),
                race_categories=_parse_race_categories(
                    c.get("race_categories"), c),
                kneel_posture=str(c.get("kneel_posture",
                                        "B_upright_kneel")).strip(),
                kneel_hip_height_in=(None if c.get("kneel_hip_height_in") is None
                                     else float(c["kneel_hip_height_in"])),
                torso_angle_deg=float(c.get("torso_angle_deg", 70.0)),
            ),
            mix=MixSettings(
                min_density_pcf=float(m.get("min_density_pcf", 60.0)),
                max_density_pcf=float(m.get("max_density_pcf", 80.0)),
                db_path=(str(m["db_path"]).strip() or None
                         if m.get("db_path") else None),
                design_age_days=max(int(m.get("design_age_days", 28)), 0),
                density_basis=("wet" if str(
                    m.get("density_basis", "dry")).lower().startswith("w")
                    else "dry"),
                cylinder_volume_ft3=float(
                    m.get("cylinder_volume_ft3", 0.058148148)),
                cube_volume_ft3=float(m.get("cube_volume_ft3", 0.00462963)),
                beam_volume_ft3=float(m.get("beam_volume_ft3", 0.008463542)),
                flexure_span_in=float(m.get("flexure_span_in", 4.75)),
                flexure_base_in=float(m.get("flexure_base_in", 1.5)),
                flexure_depth_in=float(m.get("flexure_depth_in", 1.5)),
            ),
            water=WaterSettings(
                rho_kg_m3=float(w.get("rho_kg_m3", 998.0)),
                nu_m2_s=float(w.get("nu_m2_s", 1.139e-6)),
            ),
        )


# Module-level singleton — import this where you need current settings.
# The first import triggers the file read; subsequent imports reuse the object.
_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the module-level Settings singleton (loaded from canoe_suite.cfg)."""
    global _settings
    if _settings is None:
        _settings = Settings.load()
    return _settings


def reload_settings(path: str | None = None) -> Settings:
    """Force a reload from disk and return the new Settings object."""
    global _settings
    _settings = Settings.load(path)
    return _settings
