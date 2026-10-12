"""
Parametric hull shape generator and hydrostatic calculator.
Produces competitive ASCE canoe hull forms from 7 shape factors.
"""
import numpy as np
from np_compat import trapezoid
from domain.models import HullHydrostatics


def generate_hull(factors, L, B, T, n_stations=41, n_waterlines=21):
    """
    Michlet Series 7 parametric hull generator.
    Produces non-dimensional offsets scaled to dimensions L, B, T.
    Factors map 1:1 to Michlet Shape Function 7 (Kayak).

    Output coordinate convention
    ----------------------------
    offsets[row, col] = half-breadth (m) at station ``row``, waterline ``col``.
    Row 0 = bow (x = 0), row -1 = stern (x = L).
    Col 0 = keel (z = 0), col -1 = waterplane (z = T).

    wave_resistance.michell_resistance() internally remaps these rows to a
    centred frame x ∈ [-L/2, +L/2]; the two coordinate origins are consistent
    because only row ordering (bow → stern) matters for the integrals.
    """
    f = np.array(factors, dtype=float)
    # f0: Fore WP, f1: Section, f2: Fore Rocker, f3: Aft WP, f4: Aft Rocker, f5: Lf, f6: La
    f0, f1, f2, f3, f4, f5, f6 = f
    
    # Ensure Lf + La <= 1.0 (Parallel middle body if < 1.0)
    total_f = f5 + f6
    if total_f > 1.0:
        f5 /= total_f
        f6 /= total_f
        
    L_fore = L * f5
    L_aft  = L * f6
    
    # xi: 0 (bow) to L (stern)
    xi = np.linspace(0, L, n_stations)
    # zi: 0 (keel baseline) to T (waterline)
    zi = np.linspace(0, T, n_waterlines)
    
    offsets = np.zeros((n_stations, n_waterlines))
    
    for i, x_global in enumerate(xi):
        # 1. Determine Longitudinal Position and Base X
        if x_global < L_fore:
            # Forebody taper
            x_norm = x_global / L_fore if L_fore > 0 else 1.0
            u = 0.5 * x_norm
            X = 4.0 * u * (1.0 - u) # Parabolic taper from 0 to 1
            pw = f0
            pr = f2
        elif x_global > (L - L_aft):
            # Afterbody taper
            x_norm = (L - x_global) / L_aft if L_aft > 0 else 1.0
            u = 0.5 * x_norm
            X = 4.0 * u * (1.0 - u) # Parabolic taper from 0 to 1
            pw = f3
            pr = f4
        else:
            # Parallel middle section
            X = 1.0
            pw = 0.0
            pr = 0.0
            
        if X <= 0:
            offsets[i, :] = 0.0
            continue
            
        # 2. Waterplane Shape Factor
        F = X**pw
        
        for j, z_height in enumerate(zi):
            # 3. Vertical Position (Normalized Depth from Waterline)
            # z_height goes from 0 (keel) to T (WL). Michlet uses z=0 at WL.
            z_depth = T - z_height
            z_norm = z_depth / T if T > 0 else 0.0
            
            # 4. Rocker Check (Local Draft Boundary)
            # If X^pr < z_norm, we are below the keel line.
            if X**pr < z_norm:
                offsets[i, j] = 0.0
            else:
                # 5. Non-dimensional Cross-Section (f1)
                # Formula: Y = 0.5 * F * (1 - z^2 / X^(2*pr))^f1
                if X > 1e-9:
                    G = 1.0 - (z_norm**2) / (X**(2.0 * pr))
                    G = max(0.0, G) # Numerical stability
                    y_nd = 0.5 * F * (G**f1)
                else:
                    y_nd = 0.0
                
                # 6. Scale by Beam (offsets are half-breadths, max y_nd is 0.5)
                offsets[i, j] = y_nd * B
                
    return offsets


def compute_hydrostatics(offsets, L, T, rho=1025.0, g=9.807):
    """
    Compute hydrostatic properties from an offset matrix.
    """
    offsets = np.asarray(offsets, dtype=float)
    # NON-FINITE INPUT IS NOT A SMALL HULL.  Every `if volume > 1e-12` guard
    # below is written for a genuinely empty section -- but `NaN > 1e-12` is
    # FALSE, so a NaN volume takes the SAME else-branch and the function
    # returns fabricated numbers instead of failing:
    #
    #     vcb = T/2,  bmt = 0,  gmt = bmt - (T - vcb) = -T/2
    #
    # On this hull that is GM_T = -2.2880 in: a plausible marginal-stability
    # figure, right order of magnitude, indistinguishable from a real result.
    #
    # AND THE RETURN IS MIXED, which is the instructive part.  Only the GUARDED
    # quantities fabricate (lcb, vcb, bmt, Cm, Cp, Cwp).  The unguarded ones
    # propagate NaN (beam, awp, Cb, S, volume, displacement, Iwp_yaw).  So one
    # call hands back a believable gmt sitting next to a NaN Cb, and which one
    # a caller notices depends entirely on which field it happens to read.
    # That is why this check belongs HERE and not in any one caller's wrapper.
    #
    # The split is deliberate: non-finite has no continuum and gets a hard
    # check, while small-but-valid geometry (the shallow sections near the
    # stems are real) keeps the 1e-12 fallbacks below untouched.
    if offsets.size and not np.all(np.isfinite(offsets)):
        raise ValueError(
            "offset matrix contains non-finite values (NaN/Inf); hydrostatics "
            "from it would be fabricated, not small")
    if not (np.isfinite(L) and np.isfinite(T)):
        raise ValueError("L and T must be finite, got L=%r T=%r" % (L, T))
    ns, nw = offsets.shape
    x  = np.linspace(0, L, ns)
    z  = np.linspace(0, T, nw)
    dx = L / max(ns - 1, 1)
    dz = T / max(nw - 1, 1)

    # ── Section areas & displacement ──
    section_areas = np.array([2.0 * trapezoid(offsets[i, :], z) for i in range(ns)])
    volume = trapezoid(section_areas, x)
    if not np.isfinite(volume):
        raise ValueError("computed displaced volume is non-finite; the "
                         "fallbacks below would fabricate a hull from it")
    displacement = volume * rho

    # ── Max Widths for Waterplane ──
    max_widths = np.max(offsets, axis=1)

    # ── Beam ──
    beam = 2.0 * np.max(max_widths)

    # ── LCB ──
    if volume > 1e-12:
        lcb_abs = trapezoid(section_areas * x, x) / volume
        lcb = (lcb_abs / L) - 0.5
    else:
        lcb_abs = L / 2.0
        lcb = 0.0

    # ── VCB ──
    moment_z = 0.0
    for i in range(ns):
        for j in range(nw - 1):
            strip_a = (offsets[i, j] + offsets[i, j + 1]) * dz * 2.0
            strip_z = (z[j] + z[j + 1]) / 2.0
            moment_z += strip_a * strip_z * dx
    vcb = moment_z / volume if volume > 1e-12 else T / 2.0

    # ── Waterplane area ──
    awp = 2.0 * trapezoid(max_widths, x)

    # ── Wetted surface ──
    S = 0.0
    for j in range(nw - 1):
        for i in range(ns - 1):
            dy = offsets[i + 1, j] - offsets[i, j]
            ds_x = np.sqrt(dx ** 2 + dy ** 2)
            S += ds_x * dz * 2.0
    S = max(S, 1e-6)

    # ── Midship section area ──
    mid_idx = ns // 2
    Am = 2.0 * trapezoid(offsets[mid_idx, :], z)

    # ── Coefficients ──
    LBT = L * beam * T if (beam * T) > 0 else 1e-12
    Cb  = volume / LBT
    Cm  = Am / (beam * T) if (beam * T) > 0 else 0
    Cp  = volume / (Am * L) if (Am * L) > 0 else 0
    Cwp = awp / (L * beam) if (L * beam) > 0 else 0

    # ── Ratios ──
    lb = L / beam if beam > 1e-6 else 100.0
    lt = L / T    if T     > 1e-6 else 100.0
    bt = beam / T if T     > 1e-6 else 100.0

    # ── Stability (BMT / GMT) ──
    Iwp = (2.0 / 3.0) * trapezoid(max_widths ** 3, x)
    bmt = Iwp / volume if volume > 1e-12 else 0
    gmt = bmt - (T - vcb)

    # ── Yaw Stability (Iwp_yaw) ──
    Iwp_yaw = 2.0 * trapezoid((x - lcb_abs)**2 * max_widths, x)

    return HullHydrostatics(
        volume=volume, displacement=displacement,
        beam=beam, lcb=lcb, vcb=vcb,
        awp=awp, S=S, Am=Am,
        Cb=Cb, Cm=Cm, Cp=Cp, Cwp=Cwp,
        L_B=lb, L_T=lt, B_T=bt,
        bmt=bmt, gmt=gmt,
        Iwp_yaw=Iwp_yaw,
        L=L, B=beam, T=T,
    )

def check_constraints(hydro, bounds):
    """
    Returns list of constraint violation values (≤ 0 = feasible).
    """
    violations = []
    checks = [
        ("displacement",  "disp_min",  "disp_max"),
        ("beam",    "beam_min",  "beam_max"),
        ("lcb",     "lcb_min",   "lcb_max"),
        ("vcb",     "vcb_min",   "vcb_max"),
        ("awp",     "awp_min",   "awp_max"),
        ("S",       "sa_min",    "sa_max"),
        ("gmt",     "gmt_min",   "gmt_max"),
        ("bmt",     "bmt_min",   "bmt_max"),
        ("Cb",      "cb_min",    "cb_max"),
        ("Cm",      "cm_min",    "cm_max"),
        ("Cp",      "cp_min",    "cp_max"),
        ("Cwp",     "cwp_min",   "cwp_max"),
        ("L_B",     "lb_min",    "lb_max"),
        ("L_T",     "lt_min",    "lt_max"),
        ("B_T",     "bt_min",    "bt_max"),
    ]
    for key, lo_key, hi_key in checks:
        val = hydro.get(key, 0)
        lo  = bounds.get(lo_key)
        hi  = bounds.get(hi_key)
        if lo is not None:
            violations.append(lo - val)
        if hi is not None:
            violations.append(val - hi)
    return violations