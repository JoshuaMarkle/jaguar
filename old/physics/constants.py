# physics/constants.py
"""
Single authoritative source for all physical constants used across
the ASCE Concrete Canoe engineering suite.

All simulations model freshwater conditions (lakes/rivers) unless
explicitly overridden.  ITTC-1957 standard reference condition is
15 °C freshwater.
"""

# ── Water properties (freshwater, 15 °C) ─────────────────────────────
WATER_RHO_FRESH = 998.0      # density             [kg/m³]
WATER_RHO_SALT  = 1025.0     # density             [kg/m³]  (reference only)
NU_FRESH        = 1.139e-6   # kinematic viscosity [m²/s]
NU_SALT         = 1.188e-6   # kinematic viscosity [m²/s]   (reference only)

# Competition defaults — freshwater lake/river, overridable from
# canoe_suite.cfg (water:) so a race venue's measured conditions can be set
# without editing Python.  This module stays the single place the rest of the
# suite reads them from; the file only supplies the values.
try:                                    # settings has no heavy deps, but keep
    from config.settings import get_settings as _get_settings_water  # this
    _w = _get_settings_water().water    # import defensive: physics must stay
    WATER_RHO = float(_w.rho_kg_m3)     # importable with no config present.
    NU        = float(_w.nu_m2_s)
    del _w
except ImportError:                       # pragma: no cover - fallback path
    WATER_RHO = WATER_RHO_FRESH
    NU        = NU_FRESH

# ── Gravitational acceleration ────────────────────────────────────────
GRAVITY = 9.807  # [m/s²]

# ── Angular quadrature for Michell integral ───────────────────────────
NTHETA_DEFAULT = 64    # standard resolution  (race simulation)
NTHETA_FINE    = 128   # higher resolution    (validation suite)
