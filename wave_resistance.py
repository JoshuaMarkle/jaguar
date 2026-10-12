# wave_resistance.py
"""
Pure-Python wave resistance engine replacing Michlet's Fortran core.
Implements Michell's thin-ship integral, ITTC-1957 and Grigson skin
friction, and viscous form-factor estimation.

Coordinate convention for all functions
---------------------------------------
``offsets`` is an (n_stations × n_waterlines) array of half-breadths (m).
Row 0 = bow, row -1 = stern (same order as hull_generator.generate_hull).
Column 0 = keel (z = -T), column -1 = waterplane (z = 0).

Inside michell_resistance() the row index is remapped to a centred frame
x ∈ [-L/2, +L/2] for the Michell integral.  wetted_surface() uses
x ∈ [0, L].  Both conventions produce identical results because only
the relative row ordering and spacing matter for the integrals.
"""
import numpy as np
from physics.constants import WATER_RHO, NU, GRAVITY
from domain.units import physical_range

# ── Optional Numba acceleration ──────────────────────────────────────────────
# If numba is installed, the inner Michell kernel runs as compiled native code
# with automatic parallelisation across θ quadrature points.
# Falls back to the vectorised NumPy implementation transparently.
try:
    import numba as _numba
    import logging as _logging

    @_numba.jit(nopython=True, parallel=True, cache=True)
    def _michell_kernel_nb(dydx, x_centered, z, sec_t, sec2, sec3,
                           kappa0, dz_half, dx_half):
        """
        Numba-compiled Michell kernel.  Computes P(θ), Q(θ) for every θ in
        parallel and returns Σ (P²+Q²)·sec³(θ) · dθ  (the un-prefixed sum).
        """
        ntheta = sec_t.shape[0]
        ns = dydx.shape[0]
        nw = dydx.shape[1]
        result = 0.0
        for it in _numba.prange(ntheta):
            s2 = sec2[it]
            st = sec_t[it]
            # z-integral of dydx·exp(κ₀·sec²·z) for each station
            Iz = np.zeros(ns)
            for ix in range(ns):
                iz_sum = 0.0
                for jz in range(nw - 1):
                    f0 = dydx[ix, jz]     * np.exp(kappa0 * s2 * z[jz])
                    f1 = dydx[ix, jz + 1] * np.exp(kappa0 * s2 * z[jz + 1])
                    iz_sum += 0.5 * (f0 + f1) * dz_half[jz]
                Iz[ix] = iz_sum
            # x-integral (trapezoid) of Iz·cos(κ₀·sec·x) and Iz·sin(...)
            P_t = 0.0
            Q_t = 0.0
            for ix in range(ns - 1):
                cos0 = np.cos(kappa0 * st * x_centered[ix])
                cos1 = np.cos(kappa0 * st * x_centered[ix + 1])
                sin0 = np.sin(kappa0 * st * x_centered[ix])
                sin1 = np.sin(kappa0 * st * x_centered[ix + 1])
                P_t += 0.5 * (Iz[ix] * cos0 + Iz[ix + 1] * cos1) * dx_half[ix]
                Q_t += 0.5 * (Iz[ix] * sin0 + Iz[ix + 1] * sin1) * dx_half[ix]
            result += (P_t * P_t + Q_t * Q_t) * sec3[it]
        return result

    _USE_NUMBA = True
    _logging.getLogger(__name__).debug("Numba JIT enabled for Michell kernel")
except ImportError:
    _USE_NUMBA = False


# ═══════════════════════  MICHELL'S INTEGRAL  ═══════════════════
def michell_resistance(offsets, L, T, V, rho=WATER_RHO, g=GRAVITY, ntheta=128):
    """
    Compute wave resistance via Michell's thin-ship integral.

    Parameters
    ----------
    offsets : ndarray (n_stations × n_waterlines)
        Half-breadth values in metres.  Row 0 = bow, row -1 = stern.
        Col 0 = keel (z = -T), col -1 = waterplane (z = 0).
        Internally remapped to a centred frame x_centered ∈ [-L/2, +L/2]
        for the Michell integral; the caller's coordinate origin does not
        matter as long as row ordering is bow → stern.
    L : float   – waterline length (m)
    T : float   – draft (m)
    V : float   – speed (m/s)
    rho : float – water density (kg/m³); default freshwater 998 kg/m³
    g : float   – gravitational acceleration (m/s²)
    ntheta : int – angular quadrature points

    Returns
    -------
    Rw : float – wave resistance (N)
    """
    assert offsets.ndim == 2, "offsets must be a 2-D (n_stations × n_waterlines) array"
    if V < 0.01:
        return 0.0

    ns, nw = offsets.shape
    kappa0 = g / (V * V)                       # fundamental wave number

    # Remap bow→stern rows to centred longitudinal frame for Michell integral.
    # Row 0 (bow) → x_centered = -L/2;  row -1 (stern) → x_centered = +L/2.
    x_centered = np.linspace(-L / 2, L / 2, ns)
    z = np.linspace(-T, 0.0, nw)               # vertical (keel → surface)
    dx = L / (ns - 1)

    # Compute ∂y/∂x  (hull slope) by central differences
    dydx = np.zeros_like(offsets)
    dydx[1:-1, :] = (offsets[2:, :] - offsets[:-2, :]) / (2 * dx)
    dydx[0, :]    = (offsets[1, :] - offsets[0, :]) / dx
    dydx[-1, :]   = (offsets[-1, :] - offsets[-2, :]) / dx

    # Angular quadrature  (avoid θ = π/2 where sec → ∞)
    theta = np.linspace(0, np.pi / 2 * (1 - 1e-6), ntheta, endpoint=False)
    dtheta = theta[1] - theta[0]
    sec_t  = 1.0 / np.cos(theta)              # sec(θ)
    sec2   = sec_t ** 2
    sec3   = sec_t ** 3

    dz_half = np.diff(z)
    dx_half = np.diff(x_centered)

    if _USE_NUMBA:
        # Numba parallel kernel: compiled native code with θ-loop parallelised
        raw = _michell_kernel_nb(
            np.ascontiguousarray(dydx, dtype=np.float64),
            np.ascontiguousarray(x_centered, dtype=np.float64),
            np.ascontiguousarray(z, dtype=np.float64),
            np.ascontiguousarray(sec_t, dtype=np.float64),
            np.ascontiguousarray(sec2, dtype=np.float64),
            np.ascontiguousarray(sec3, dtype=np.float64),
            float(kappa0),
            np.ascontiguousarray(dz_half, dtype=np.float64),
            np.ascontiguousarray(dx_half, dtype=np.float64),
        )
        Rw = raw * dtheta
    else:
        # ── Fully-vectorised NumPy fallback ─────────────────────────────────
        # exp_z_all  : (ntheta, nw)   — depth decay for each θ
        # cos_x_all  : (ntheta, ns)   — longitudinal cosine kernel
        # sin_x_all  : (ntheta, ns)   — longitudinal sine kernel
        exp_z_all = np.exp(kappa0 * sec2[:, np.newaxis] * z[np.newaxis, :])
        cos_x_all = np.cos(kappa0 * sec_t[:, np.newaxis] * x_centered[np.newaxis, :])
        sin_x_all = np.sin(kappa0 * sec_t[:, np.newaxis] * x_centered[np.newaxis, :])

        # integrand_all : (ntheta, ns, nw)
        integrand_all = dydx[np.newaxis, :, :] * exp_z_all[:, np.newaxis, :]

        # Trapezoid over z → Iz_all : (ntheta, ns)
        Iz_all = 0.5 * (np.sum(
            (integrand_all[:, :, :-1] + integrand_all[:, :, 1:])
            * dz_half[np.newaxis, np.newaxis, :],
            axis=2,
        ))

        # Trapezoid over x → P_all, Q_all : (ntheta,)
        P_all = 0.5 * np.sum(
            (Iz_all[:, :-1] * cos_x_all[:, :-1] + Iz_all[:, 1:] * cos_x_all[:, 1:])
            * dx_half[np.newaxis, :], axis=1,
        )
        Q_all = 0.5 * np.sum(
            (Iz_all[:, :-1] * sin_x_all[:, :-1] + Iz_all[:, 1:] * sin_x_all[:, 1:])
            * dx_half[np.newaxis, :], axis=1,
        )
        Rw = float(np.sum((P_all ** 2 + Q_all ** 2) * sec3)) * dtheta

    Rw *= 4.0 * rho * g * g / (np.pi * V * V)
    return float(max(Rw, 0.0))


# ═══════════════════════  SKIN FRICTION  ════════════════════════
def wetted_surface(offsets, L, T):
    """Approximate wetted surface area (m²) from the offset grid."""
    ns, nw = offsets.shape
    x = np.linspace(0, L, ns)
    z = np.linspace(0, T, nw)
    S = 0.0
    for j in range(nw - 1):
        dz = z[j + 1] - z[j]
        for i in range(ns - 1):
            dx_val = x[i + 1] - x[i]
            dy = offsets[i + 1, j] - offsets[i, j]
            # Arc length along the girth for half-hull, times 2
            ds_x = np.sqrt(dx_val ** 2 + dy ** 2)
            S += ds_x * dz * 2.0
    return S


def ittc1957_cf(Re):
    """ITTC-1957 friction coefficient."""
    if Re < 1e3:
        return 0.0
    return 0.075 / (np.log10(Re) - 2.0) ** 2


def grigson_cf(Re):
    """Grigson (1993) correction to ITTC-1957."""
    cf = ittc1957_cf(Re)
    if Re < 1e6:
        return cf
    log_Re = np.log10(Re)
    # Grigson's delta-Cf correlation
    delta = 0.0
    if 6 <= log_Re < 9:
        delta = (68.396 / log_Re - 21.244) * 1e-4
    return max(cf + delta, 0.0)


def friction_resistance(V, L, S, nu=NU, rho=WATER_RHO, method=1):
    """
    Frictional resistance.

    method: 0 = None, 1 = ITTC-1957, 2 = Grigson
    Returns Rf in Newtons.
    """
    if V < 0.01 or method == 0:
        return 0.0
    Re = V * L / nu
    if method == 2:
        Cf = grigson_cf(Re)
    else:
        Cf = ittc1957_cf(Re)
    return 0.5 * rho * V * V * S * Cf


# ═══════════════════════  FORM FACTOR  ══════════════════════════
def viscous_form_factor(L, B, T, disp, S):
    """
    Holtrop-Mennen style form factor  k  for (1+k)·Rf.
    Simplified for slender displacement hulls.
    """
    if S < 1e-6 or L < 1e-6:
        return 0.0
    Lr = L / (disp ** (1 / 3))  if disp > 0 else 10.0
    k = 0.93 + 0.487 * (1.0 / Lr) ** 1.06
    return max(k - 1.0, 0.0)


# ═══════════════════════  TOTAL RESISTANCE  ═════════════════════
@physical_range(L=(1.0, 15.0), B=(0.1, 3.0), T=(0.01, 1.0), V=(0.1, 10.0))
def total_resistance(offsets, L, B, T, V, disp,
                     rho=WATER_RHO, g=GRAVITY, nu=NU,
                     ntheta=128, friction_method=1, form_factor_on=True):
    """
    Total ship resistance  Rt = Rw + (1+k)·Rf.

    Units: L [m], B [m], T [m], V [m/s], disp [m³], rho [kg/m³], g [m/s²].

    Returns dict with component breakdown.
    """
    Rw = michell_resistance(offsets, L, T, V, rho, g, ntheta)
    S  = wetted_surface(offsets, L, T)
    Rf = friction_resistance(V, L, S, nu, rho, friction_method)

    k = 0.0
    if form_factor_on:
        k = viscous_form_factor(L, B, T, disp, S)

    Rv = (1.0 + k) * Rf
    Rt = Rw + Rv

    return {
        "Rt": Rt, "Rw": Rw, "Rf": Rf, "Rv": Rv,
        "k": k, "S": S, "V": V
    }
