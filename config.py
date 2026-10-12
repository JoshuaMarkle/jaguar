# config.py
MAX_MIXES = 3

# --- HULL TEAM LIMITS ---
THICKNESS_IN = 0.5 
FACTOR_OF_SAFETY = 1.5
DYNAMIC_LOAD_FACTOR = 1.2
MANEUVER_COUPLING_K = 0.5    # Empirical coupling constant for slalom-induced lateral loads

RACE_4_PERSON = [160, 150, 170, 160]
RACE_2_PERSON = [170, 160]
RACE_3_PERSON = [160, 160, 160]

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
# Based on the empirical y-intercept of 59.309 pcf
MIN_DENSITY_PCF = 60.0
# The absolute heaviest/strongest mix the team is willing to pour
MAX_DENSITY_PCF = 80.0

# ─── SQLite Mix Database Singleton ────────────────────────────────
_mix_db = None

def get_mix_db():
    """
    Lazy singleton accessor for the concrete mix database.
    Creates the database and seeds defaults on first access.
    """
    global _mix_db
    if _mix_db is None:
        from mix_database import MixDatabase
        _mix_db = MixDatabase()
    return _mix_db

def get_mix_properties(density_pcf):
    """
    Returns allowable structural properties for a concrete mix at the
    given unit weight.

    Lookup strategy (via MixDatabase):
      1. Exact match (±0.5 pcf) in the SQLite database → use lab values.
      2. No match → fall back to the team's empirical regression:
             f'c [psi] = (density_pcf − 59.309) / 0.0062
             f_t [psi] = 0.20 × f'c   (assuming ~2 % PVA fiber content)

    Allowable stresses are divided by FACTOR_OF_SAFETY (ACI 318 practice).
    """
    return get_mix_db().get_mix_properties(density_pcf, fos=FACTOR_OF_SAFETY)