# np_compat.py
"""
NumPy 1.x / 2.x compatibility helpers.

The trapezoidal integrator was RENAMED in NumPy 2.0:
    NumPy 1.x:  np.trapz        (np.trapezoid does not exist)
    NumPy 2.x:  np.trapezoid    (np.trapz was removed)

Neither name works across both major versions, and the app must run on
both: the dev environment tracks NumPy 2.x while the frozen build's venv
resolves to 1.26 (requirements.txt pins numpy<2 for cadquery/pymoo
compatibility).  Import ``trapezoid`` from here instead of numpy.
"""
import numpy as np

trapezoid = getattr(np, "trapezoid", None) or np.trapz
