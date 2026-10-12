# domain/units.py
"""
Unit conversion helpers.  All functions follow the pattern:
    source_unit_to_target_unit(value) -> converted_value

Scalar or numpy array inputs are both supported (numpy-safe arithmetic).

Also provides ``physical_range`` — a lightweight decorator for asserting
that a function's numeric arguments are within physically meaningful bounds.
Applied at system boundaries (not internal hot paths) to catch unit-confusion
bugs without measurement overhead.

Usage
-----
    @physical_range(L_m=(1.0, 15.0), T_m=(0.01, 1.0))
    def my_fn(L_m, T_m, ...):
        ...

The decorator only activates when the keyword argument names match the
range dict; positional arguments are checked by name via inspect.
"""
from __future__ import annotations

import functools
import inspect
from typing import Tuple


# ── Physical range decorator ─────────────────────────────────────────────────

def physical_range(**ranges: Tuple[float, float]):
    """
    Validate that named numeric parameters fall within (lo, hi).

    Parameters
    ----------
    **ranges : param_name=(lo, hi)
        Inclusive bounds for each parameter.  Only scalar (non-array)
        arguments are checked; array values are skipped silently.

    Raises
    ------
    ValueError
        If any named parameter is outside its declared range.
    """
    def decorator(fn):
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            # Build {name: value} for positional args
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            for pname, (lo, hi) in ranges.items():
                if pname not in bound.arguments:
                    continue
                val = bound.arguments[pname]
                try:
                    fval = float(val)
                except (TypeError, ValueError):
                    continue   # non-scalar or non-numeric: skip
                if not (lo <= fval <= hi):
                    raise ValueError(
                        f"{fn.__qualname__}: argument '{pname}' = {fval} "
                        f"is outside physical bounds [{lo}, {hi}]."
                    )
            return fn(*args, **kwargs)
        wrapper._physical_ranges = ranges   # introspectable for tests
        return wrapper
    return decorator


# ── Length ──────────────────────────────────────────────────────────
def ft_to_m(ft):    return ft * 0.3048
def m_to_ft(m):     return m / 0.3048

def in_to_m(inch):  return inch * 0.0254
def m_to_in(m):     return m / 0.0254

def in_to_ft(inch): return inch / 12.0
def ft_to_in(ft):   return ft * 12.0


# ── Density ─────────────────────────────────────────────────────────
_LB_PER_FT3_TO_KG_PER_M3 = 16.0185

def pcf_to_kg_m3(pcf): return pcf * _LB_PER_FT3_TO_KG_PER_M3
def kg_m3_to_pcf(rho): return rho / _LB_PER_FT3_TO_KG_PER_M3


# ── Force / weight ──────────────────────────────────────────────────
def lbs_to_N(lbs): return lbs * 4.44822
def N_to_lbs(N):   return N / 4.44822


# ── Pressure / stress ───────────────────────────────────────────────
def psi_to_Pa(psi): return psi * 6894.76
def Pa_to_psi(Pa):  return Pa / 6894.76
