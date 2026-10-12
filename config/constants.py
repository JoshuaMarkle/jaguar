# config/constants.py
"""
Compile-time structural and physical constants.
These values are derived from ASCE rules, biomechanics literature, and
the team's empirical regression — not user-editable preferences.
"""

# ── Hull zone boundaries (fraction of LOA) ──────────────────────────
# Used by evaluate_canoe_weight to assign cells to the 8-zone grid.
# Layout: |  Tip-Bow  |  Q-Bow  |      Midbody      |  Q-Stern  |  Tip-Stern  |
HULL_ZONE_FRACTIONS = (0.15, 0.30, 0.70, 0.85)

# Fraction of maximum absolute moment below which a station is
# considered a "zero-moment end" (assigned the lightest mix).
MOMENT_THRESHOLD_FRAC = 0.05

# ── Paddler biomechanics ─────────────────────────────────────────────
# PADDLER_KNEE_FRACTION / PADDLER_TOE_FRACTION (the old 80/20) lived here and
# were DEAD -- nothing imported them, while the live split came from
# canoe_suite.cfg via config.KNEE_FRAC_BENDING / _PUNCHING.  They were the
# last thing still asserting 80/20, which was unsourced from the initial
# commit.  Removed 2026-08-28; the cfg carries the split and its provenance.

# Distance (inches) from the knee contact point to the toe contact point.
PADDLER_TOE_OFFSET_IN = 17.0

# Knee pad footprint modelled as a 3-inch square (0.25 ft) for load spreading.
PADDLER_KNEE_FOOTPRINT_FT = 0.25

# Minimum centre-to-centre spacing between adjacent paddler stations (ft).
MIN_PADDLER_SPACING_FT = 3.5

# ── Concrete mix regression (team empirical data) ────────────────────
# Governs the structural sweep's lower bound and the MixDatabase fallback.
# Formula: f'c [psi] = (density_pcf - INTERCEPT) / SLOPE
REGRESSION_SLOPE     = 0.0062   # Δpsi per Δpcf
REGRESSION_INTERCEPT = 59.309   # x-intercept density [pcf]

# Tensile strength at ~2 % PVA fiber content:  f_t ≈ FRAC × f'c
EMPIRICAL_FT_FRACTION = 0.20

# Minimum density gap between adjacent pour zones (prevents near-identical mixes).
MIN_MIX_DENSITY_GAP_PCF = 2.0
