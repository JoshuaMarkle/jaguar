#!/usr/bin/env python3
"""Golden reference for tests/golden.rs (needs numpy + scipy).

Generated tests/golden/*_v0.json. A minimal, independent implementation written
from docs/SPEC.md (not from the Rust), kept so the golden values can be regenerated
and audited (`python3 -I tests/golden/reference.py --regen`, from the repo root):

    bending_v0:   hull table -> shell section per station -> weight/buoyancy/crew
                  -> M(x) -> bending tension vs f_t/FS
    strength_v0:  bending tension + compression, shell shear, Kupfer-Gerstle
                  biaxial, under ASD (/FS) and LRFD (1.2D+1.6L, 0.9D+1.6L, phi 0.60)
    flotation_v0: swamped-flotation air-pocket length (SPEC Physics 6)

The CLI below runs the bending-only search (lightest passing design, recheck at
2x stations). Punching is not covered here (docs/PUNCHING.md has hand checks).

Units: inches, pounds, psi, pcf. The strength line below is a fixed test input,
not real mix data.
"""
import argparse
import re
import sys

import numpy as np
from scipy.optimize import brentq

trapz = getattr(np, "trapezoid", None) or np.trapz   # numpy 1.x / 2.x
M_TO_IN = 39.3701
WATER_PCF = 62.4
FT_SLOPE, FT_ICPT = 27.797, -1648.6      # f_t[psi] = slope*density[pcf] + icpt  (fixed test input)
FS = 1.5                                 # factor of safety
ACCEPT_SEARCH, ACCEPT_VERIFY = 0.93, 0.95   # dc_search, dc_verify
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
    """Thin-shell area, centroid height, I about centroid, per station (both sides).

    Only real hull girth counts: where a waterline segment has zero half-breadth at both
    ends it lies below the keel (rocker lifts the keel at the ends) or is the closed stem,
    so there is no wall there."""
    dz = np.diff(z)
    dy = np.diff(y, axis=1)
    hull = (y[:, :-1] > 0) | (y[:, 1:] > 0)
    ds = np.where(hull, np.hypot(dy, dz), 0.0)
    zm = (z[:-1] + z[1:]) / 2
    a = 2 * t * ds                                    # per segment, both sides
    a0 = 2 * y[:, 0] * t                              # flat strip at the keel
    A = a.sum(1) + a0
    zbar = np.where(A > 0, (a * zm).sum(1) / np.maximum(A, 1e-12), 0.0)
    zb = zbar[:, None]
    I = (a * ((zm - zb) ** 2 + dz ** 2 / 12)).sum(1) + a0 * zbar ** 2
    return A, zbar, I


def moments(x, z, y, A, crew_lb, seats, dyn, rho_pcf, dead=1.0, live=1.0):
    """Free-free beam: hull weight + crew vs buoyancy at the equilibrium waterline.

    dead, live: load factors on self weight and on crew (x dyn); buoyancy
    balances the factored weight (LRFD combinations, SPEC Physics 4).
    Returns (V, M, factored hull weight, draft)."""
    L = x[-1]
    w_hull = dead * A * rho_pcf / 1728.0              # lb/in
    cum = 2 * np.concatenate([np.zeros((len(x), 1)),
                              np.cumsum((y[:, 1:] + y[:, :-1]) / 2 * np.diff(z), axis=1)], axis=1)
    xc = L / 2
    W_hull = trapz(w_hull, x)
    crew_w = np.asarray(crew_lb) * dyn * live
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
    V = np.concatenate([[0], np.cumsum((q[1:] + q[:-1]) / 2 * np.diff(x))])
    M = np.concatenate([[0], np.cumsum((V[1:] + V[:-1]) / 2 * np.diff(x))])
    for xi, wi in zip(seats, crew_w):                 # knee patch, not a point
        # Uniform load wi/p on [xi - p/2, xi + p/2], integrated exactly from the bow
        # (sampling it at nodes catches 1-3 nodes depending on spacing and seat).
        u = np.clip(x - (xi - KNEE_PATCH_IN / 2), 0.0, KNEE_PATCH_IN)
        V = V - wi * u / KNEE_PATCH_IN
        M = M - wi * (u ** 2 / (2 * KNEE_PATCH_IN) + np.maximum(x - (xi + KNEE_PATCH_IN / 2), 0.0))
    if abs(V[-1]) > 1e-3 * W or abs(M[-1]) > 1e-3 * W * L:   # free-free beam must close
        raise RuntimeError(f"equilibrium broken: V_end={V[-1]:.2f} M_end={M[-1]:.1f}")
    return V, M, W_hull, p[0]


def evaluate(L, T, y0, depth, n, t, rho, crew, seats, dyn):
    x, z, y = build(L, T, y0, depth, n)
    A, zbar, I = section(z, y, t)
    _, M, W_hull, draft = moments(x, z, y, A, crew, seats, dyn, rho)
    ok = I > 1e-3 * I.max()                           # skip the pointed tips
    c = np.where(M >= 0, zbar, depth - zbar)       # tension fibre: bottom when sagging (M > 0)
    sigma = np.abs(M[ok]) * c[ok] / I[ok]
    cap = (FT_SLOPE * rho + FT_ICPT) / FS
    dc = np.inf if cap <= 0 else sigma.max() / cap
    return dict(t=t, rho=rho, weight=W_hull, dc=dc, draft=draft,
                x_gov=x[ok][sigma.argmax()], Mmax=np.abs(M).max())


def shear_q(z, y, t, zb):
    """First moment about zb of the shell area below zb.

    The keel strip 2*y0*t sits at z0. Side segment k carries 2*t*ds of area spread
    evenly over its height, i.e. 2*t*ds/dz per unit z, so the part below zb adds
    2*t*(ds/dz) * integral_{z_k}^{min(z_k+1, zb)} (zb - z) dz."""
    q = 2 * y[0] * t * max(zb - z[0], 0.0)
    for k in range(len(z) - 1):
        z0, z1 = z[k], z[k + 1]
        if z0 >= zb:
            break
        if y[k] <= 0 and y[k + 1] <= 0:              # below the keel: no wall (see section)
            continue
        top = min(z1, zb)
        per_z = 2 * t * np.hypot(y[k + 1] - y[k], z1 - z0) / (z1 - z0)
        q += per_z * ((zb - z0) ** 2 - (zb - top) ** 2) / 2
    return q


def kupfer(sigma, tau, ft, fc):
    """Kupfer-Gerstle tension-compression branch: D/C of one plane-stress state.

    sigma: normal stress (tension +), tau: shear. Principal stresses s1 >= s2;
    allowable s1 = ft * (1 - 0.8 |s2| / fc)."""
    r = np.sqrt((sigma / 2) ** 2 + tau ** 2)
    s1, s2 = sigma / 2 + r, sigma / 2 - r
    cap = ft * (1 - 0.8 * abs(s2) / fc)
    return np.inf if cap <= 0 else s1 / cap


def strength(L, T, y0, i):
    """SPEC Physics 4-5: every stress check but punching, per strength combination.

    ASD: one combination at service loads, capacities / FS. LRFD: 1.2D+1.6L and
    0.9D+1.6L (L = crew x dyn), capacities x 0.60; the higher D/C governs."""
    depth, t, rho = i["depth"], i["thickness"], i["density"]
    x, z, y = build(L, T, y0, depth, i["stations"])
    A, zbar, I = section(z, y, t)
    f_sp = i["ft_slope"] * rho + i["ft_icpt"]           # split tensile, psi
    f_r = i["flex_over_split"] * f_sp                   # flexural
    fc = (f_sp / 6.7) ** 2                              # ACI f_ct = 6.7 sqrt(f'c)
    if i["method"] == "asd":
        combos, k = [(1.0, 1.0)], 1 / i["fs"]
    else:
        combos, k = [(1.2, 1.6), (0.9, 1.6)], 0.60
    ok = I > 1e-3 * I.max()
    out = []
    for dead, live in combos:
        V, M, W, draft = moments(x, z, y, A, i["crew_lb"], i["seats_in"], i["dyn"], rho, dead, live)
        st, sc, tau, kg = [], [], [], []
        for j in np.flatnonzero(ok):
            top, bot = depth - zbar[j], zbar[j]
            ct, cc = (bot, top) if M[j] > 0 else (top, bot)   # sagging: bottom in tension
            s_t, s_c = abs(M[j]) * ct / I[j], abs(M[j]) * cc / I[j]
            ta = abs(V[j]) * shear_q(z, y[j], t, zbar[j]) / (I[j] * 2 * t)
            st.append(s_t)
            sc.append(s_c)
            tau.append(ta)
            kg.append(max(kupfer(s_t, ta, f_r * k, fc * k), kupfer(-s_c, ta, f_r * k, fc * k)))
        dc = {"bending_tension": max(st) / (f_r * k),
              "bending_compression": max(sc) / (0.85 * fc * k),
              "shear": max(tau) / (2 * np.sqrt(fc) * k),
              "biaxial": max(kg)}
        out.append(dict(dead=dead, live=live, weight=float(W), draft=float(draft),
                        Mmax=float(np.abs(M).max()), dc={n: float(v) for n, v in dc.items()}))
    gov = max(range(len(out)), key=lambda c: max(out[c]["dc"].values()))
    return {"combos": out, "governing": gov, "dc": max(out[gov]["dc"].values())}


def flotation(L, T, y0, i):
    """SPEC Physics 6: swamped, the wall volume plus two sealed end pockets (length
    l from each tip, full hull interior up to D) displace >= swamp_factor x self
    weight at 62.4 pcf.

    Interior area per station = area enclosed by the outer mold line up to D minus
    the shell (wall) area, so pocket air + pocket wall = the enclosed volume."""
    depth, t, rho = i["depth"], i["thickness"], i["density"]
    x, z, y = build(L, T, y0, depth, i["stations"])
    A, _, _ = section(z, y, t)
    wall = trapz(A, x)
    W = wall * rho / 1728.0
    need = max(i["swamp_factor"] * W * 1728.0 / WATER_PCF - wall, 0.0)
    interior = np.maximum(2 * trapz(y, z, axis=1) - A, 0.0)

    def end_volume(a, b):                  # integral of the linear interior area over [a, b]
        xs = np.concatenate([[a], x[(x > a) & (x < b)], [b]])
        return trapz(np.interp(xs, x, interior), xs)

    vol = lambda l: end_volume(0.0, l) + end_volume(L - l, L)
    if vol(L / 2) < need:
        raise RuntimeError("pockets cannot float the canoe even filling the whole hull")
    l = 0.0 if need == 0 else brentq(lambda l: vol(l) - need, 0.0, L / 2, xtol=1e-10)
    return dict(wall_in3=float(wall), weight=float(W), pocket_in3=float(need), pocket_len_in=float(l))


def regen(off1="tests/golden/michlet/off1.mlt", out="tests/golden/michlet/out.mlt"):
    """Recompute the outputs of every case in every golden JSON from its inputs."""
    import json
    L, T, y0 = read_hull(off1, out)

    def bending(i):
        r = evaluate(L, T, y0, i["depth"], i["stations"], i["thickness"], i["density"],
                     i["crew_lb"], i["seats_in"], i["dyn"])
        return {k: float(r[k]) for k in ("weight", "dc", "draft", "x_gov", "Mmax")}

    for name, fn in (("bending", bending), ("strength", lambda i: strength(L, T, y0, i)),
                     ("flotation", lambda i: flotation(L, T, y0, i))):
        path = f"tests/golden/{name}_v0.json"
        d = json.load(open(path))
        for c in d["cases"]:
            c["outputs"] = fn(c["inputs"])
        json.dump(d, open(path, "w"), indent=1)
        open(path, "a").write("\n")


def main():
    if sys.argv[1:] == ["--regen"]:
        return regen()
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
