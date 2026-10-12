#!/usr/bin/env python3
"""TEMPORARY minimal canoe optimizer (terminal only).

Question answered: lightest uniform wall (thickness, mix density) for a given
hull that does not crack under the crew, in longitudinal bending.

    hull table -> shell section per station -> weight/buoyancy/crew -> M(x)
    -> bending tension vs f_t/FS -> lightest passing design -> recheck at 2x stations

Units: inches, pounds, psi, pcf.  Needs numpy + scipy.

DELIBERATELY NOT HERE: biaxial Kupfer-Gerstle check, multizone mixes,
5 race categories, seating search, torsion, stability/GM_T, freeboard gate, PDF.
Strength constants below are PLACEHOLDERS from the old Mix Lab fit (4 points, not
a real fit): weights are relative comparisons only.
"""
import argparse
import re
import sys

import numpy as np
from scipy.optimize import brentq

trapz = getattr(np, "trapezoid", None) or np.trapz   # numpy 1.x / 2.x
M_TO_IN = 39.3701
WATER_PCF = 62.4
FT_SLOPE, FT_ICPT = 27.797, -1648.6      # f_t[psi] = slope*density[pcf] + icpt  (old Mix Lab)
FS = 1.5                                 # old structural.factor_of_safety (ASD)
ACCEPT_SEARCH, ACCEPT_VERIFY = 0.93, 0.95   # old refinement-gate margins
KNEE_PATCH_IN = 3.0


def read_hull(off1, out):
    """Return (x, z, y) in inches; y = half-breadth, extruded straight up to depth later."""
    rows = []
    for line in open(off1):
        s = line.split("#")[0].split("!")[0].replace(",", " ").split()
        if s:
            rows.append([float(v) for v in s])
    y = np.array(rows) * M_TO_IN              # (stations bow->stern, waterlines keel->DWL)
    txt = open(out).read()

    def field(name):
        m = re.search(rf"^{re.escape(name)}\s*,\s*([-\d.eE+]+)", txt, re.M)
        if not m:
            sys.exit(f"{out}: no '{name}' line - refusing to guess a hull size")
        return float(m.group(1)) * M_TO_IN
    L, T = field("LPP (m)"), field("T (m)")
    return L, T, y


def build(L, T, y, depth, n):
    """Resample to n stations and extrude the DWL offsets up to the gunwale."""
    x0 = np.linspace(0, L, y.shape[0])
    x = np.linspace(0, L, n)
    y = np.column_stack([np.interp(x, x0, y[:, k]) for k in range(y.shape[1])])
    z = np.linspace(0, T, y.shape[1])
    if depth > T:
        zf = np.linspace(T + 0.1, depth, 10)
        y = np.hstack([y, np.tile(y[:, -1:], (1, len(zf)))])
        z = np.concatenate([z, zf])
    return x, z, y


def section(z, y, t):
    """Thin-shell area, centroid height, I about centroid, per station (both sides)."""
    dz = np.diff(z)
    dy = np.diff(y, axis=1)
    ds = np.hypot(dy, dz)
    zm = (z[:-1] + z[1:]) / 2
    a = 2 * t * ds                                    # per segment, both sides
    a0 = 2 * y[:, 0] * t                              # flat strip at the keel
    A = a.sum(1) + a0
    zbar = np.where(A > 0, (a * zm).sum(1) / np.maximum(A, 1e-12), 0.0)
    zb = zbar[:, None]
    I = (a * ((zm - zb) ** 2 + dz ** 2 / 12)).sum(1) + a0 * zbar ** 2
    return A, zbar, I


def moments(x, z, y, A, crew_lb, seats, dyn, rho_pcf):
    """Free-free beam: hull weight + crew vs buoyancy at the equilibrium waterline."""
    L = x[-1]
    w_hull = A * rho_pcf / 1728.0                     # lb/in
    cum = 2 * np.concatenate([np.zeros((len(x), 1)),
                              np.cumsum((y[:, 1:] + y[:, :-1]) / 2 * np.diff(z), axis=1)], axis=1)
    xc = L / 2
    W_hull = trapz(w_hull, x)
    crew_w = np.asarray(crew_lb) * dyn
    W = W_hull + crew_w.sum()
    M_w = trapz(w_hull * (x - xc), x) + (crew_w * (np.asarray(seats) - xc)).sum()

    def buoy(p):
        d = np.clip(p[0] + p[1] * (x - xc) / L, 0, z[-1])   # p[1] = draft change over a length
        sub = np.array([np.interp(d[i], z, cum[i]) for i in range(len(x))])
        return sub * WATER_PCF / 1728.0

    def draft_for(theta):                              # force balance: monotone in d0
        f = lambda d0: (trapz(buoy([d0, theta]), x) - W) / W
        if f(z[-1]) < 0:
            raise RuntimeError("hull sinks: total weight exceeds buoyancy at full depth")
        return brentq(f, 0.0, z[-1])

    def moment_res(theta):                             # trim balance: monotone in theta
        return (trapz(buoy([draft_for(theta), theta]) * (x - xc), x) - M_w) / (W * L)

    TH = 0.5 * z[-1]                                   # max draft change over the length
    if moment_res(-TH) > 0 or moment_res(TH) < 0:
        raise RuntimeError("trim balance not bracketed")
    theta = brentq(moment_res, -TH, TH, xtol=1e-9)
    p = [draft_for(theta), theta]
    q = buoy(p) - w_hull
    for xi, wi in zip(seats, crew_w):                 # knee patch, not a point
        q = q - np.where(np.abs(x - xi) <= KNEE_PATCH_IN / 2, wi / KNEE_PATCH_IN, 0.0)
    V = np.concatenate([[0], np.cumsum((q[1:] + q[:-1]) / 2 * np.diff(x))])
    M = np.concatenate([[0], np.cumsum((V[1:] + V[:-1]) / 2 * np.diff(x))])
    if abs(V[-1]) > 1e-3 * W or abs(M[-1]) > 1e-3 * W * L:   # free-free beam must close
        raise RuntimeError(f"equilibrium broken: V_end={V[-1]:.2f} M_end={M[-1]:.1f}")
    return M, W_hull, p[0]


def evaluate(L, T, y0, depth, n, t, rho, crew, seats, dyn):
    x, z, y = build(L, T, y0, depth, n)
    A, zbar, I = section(z, y, t)
    M, W_hull, draft = moments(x, z, y, A, crew, seats, dyn, rho)
    ok = I > 1e-3 * I.max()                           # skip the pointed tips
    c = np.maximum(zbar, depth - zbar)
    sigma = np.abs(M[ok]) * c[ok] / I[ok]
    cap = (FT_SLOPE * rho + FT_ICPT) / FS
    dc = np.inf if cap <= 0 else sigma.max() / cap
    return dict(t=t, rho=rho, weight=W_hull, dc=dc, draft=draft,
                x_gov=x[ok][sigma.argmax()], Mmax=np.abs(M).max())


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--off1", default="tests/golden/michlet/off1.mlt")
    ap.add_argument("--out", default="tests/golden/michlet/out.mlt")
    ap.add_argument("--depth", type=float, default=14.0, help="mold depth, in")
    ap.add_argument("--crew", default="175,165", help="paddler weights, lb")
    ap.add_argument("--seats", help="seat x positions, in from bow (default: evenly spaced)")
    ap.add_argument("--dyn", type=float, default=1.2, help="dynamic load factor on crew")
    ap.add_argument("--thickness", default="0.375,0.5,0.625,0.75")
    ap.add_argument("--density", default="60,80,2", help="min,max,step pcf")
    ap.add_argument("--stations", type=int, default=161)
    a = ap.parse_args()

    L, T, y0 = read_hull(a.off1, a.out)
    crew = [float(v) for v in a.crew.split(",")]
    seats = ([float(v) for v in a.seats.split(",")] if a.seats
             else [L * (i + 1) / (len(crew) + 1) for i in range(len(crew))])
    lo, hi, st = (float(v) for v in a.density.split(","))
    print(f"hull L={L/12:.2f} ft  T={T:.2f} in  depth={a.depth} in  crew={crew} lb  seats={[round(s) for s in seats]} in")
    print("WARNING: placeholder strength fit (4 lab points) - weights are relative only\n")
    print(f"{'t(in)':>6} {'rho':>5} {'wt(lb)':>8} {'D/C':>6}  pass")

    best = None
    for t in (float(v) for v in a.thickness.split(",")):
        for rho in np.arange(lo, hi + 1e-9, st):
            r = evaluate(L, T, y0, a.depth, a.stations, t, rho, crew, seats, a.dyn)
            ok = r["dc"] <= ACCEPT_SEARCH
            print(f"{t:6.3f} {rho:5.1f} {r['weight']:8.1f} {r['dc']:6.3f}  {'yes' if ok else ''}")
            if ok and (best is None or r["weight"] < best["weight"]):
                best = r
    if best is None:
        sys.exit("\nNO DESIGN PASSES in this grid - widen thickness/density or reduce load.")
    v = evaluate(L, T, y0, a.depth, 2 * a.stations - 1, best["t"], best["rho"], crew, seats, a.dyn)
    status = "VERIFIED" if v["dc"] <= ACCEPT_VERIFY else "NOT VERIFIED (fails at 2x stations)"
    print(f"\nBEST: t={best['t']} in, {best['rho']} pcf, {best['weight']:.1f} lb, D/C {best['dc']:.3f} "
          f"-> {v['dc']:.3f} at {2*a.stations-1} stations: {status}")
    ts = [float(v) for v in a.thickness.split(",")]
    if best["t"] in (min(ts), max(ts)) or best["rho"] in (lo, hi):
        print("WARNING: winner is on the edge of the search grid - widen it before trusting 'best'.")
    sys.exit(0 if v["dc"] <= ACCEPT_VERIFY else 2)


if __name__ == "__main__":
    main()
