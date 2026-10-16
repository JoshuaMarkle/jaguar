//! Water resistance of a floating hull.
//!
//! Used only for the `drag` objective (pick the fastest hull that is strong
//! enough). Pure function, no I/O. Units at this boundary: hull geometry in
//! inches, speed in m/s, result in newtons (convert inside, see `units`).
//!
//! Model:
//! * wave resistance: Michell thin-ship integral over the SUBMERGED part of the
//!   hull at the loaded draft (half-breadths clipped at `draft_in`), fresh water
//!   998 kg/m^3, g = 9.80665, ntheta = 128 (or fewer if a test shows <1%
//!   change), theta excluding pi/2.
//! * friction: 0.5 rho V^2 S Cf with S the wetted surface (girth below the draft
//!   integrated along the length), Re = V L / nu, nu = 1.139e-6 m^2/s (15 C).
//!   Cf = the larger of ITTC-1957 (hydraulically smooth), 0.075 / (log10 Re - 2)^2, and,
//!   for a surface of equivalent sand roughness ks (`hull.roughness_um`; 0 = smooth), the
//!   fully rough flat plate (Schlichting), (1.89 + 1.62 log10(L / ks))^-2.5 ([`friction_coeff`]).
//!   No form factor (k = 0).
//!
//! Errors (Err, never 0.0): non-finite inputs, speed <= 0, draft <= 0, or a
//! hull with no submerged volume at `draft_in`.

use crate::hull::Hull;
use crate::hull::linspace;
use crate::hullgen::Turn;
use crate::params::Displacement;
use crate::units::{IN3_PER_FT3, M_TO_IN, WATER_PCF};
use anyhow::{Result, ensure};
use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
pub struct Resistance {
    pub wave_n: f64,
    pub friction_n: f64,
    pub total_n: f64,
    /// Wetted surface used for friction, m^2.
    pub wetted_m2: f64,
    /// Length used for Re and the roughness ratio, m.
    pub length_m: f64,
    pub speed_mps: f64,
    /// Froude number V / sqrt(g L).
    pub froude: f64,
    /// Reynolds number V L / nu.
    pub reynolds: f64,
    /// ITTC-1957 (smooth) friction coefficient.
    pub cf_smooth: f64,
    /// Fully rough plate coefficient at the given roughness (None: smooth, ks = 0).
    pub cf_rough: Option<f64>,
    /// The friction coefficient used: max(cf_smooth, cf_rough).
    pub cf: f64,
}

/// Kinematic viscosity of fresh water at 15 C, m^2/s, and density, kg/m^3 (for reports).
pub const NU_WATER: f64 = NU;
pub const RHO_WATER: f64 = RHO;

/// Skin-friction coefficient of a hull of length `l_m` at Reynolds number `re` with
/// equivalent sand roughness `ks_um` (micrometres; 0 = hydraulically smooth):
/// (smooth, rough, used). Smooth: ITTC-1957 correlation line. Rough: Schlichting's fully rough
/// flat plate, (1.89 + 1.62 log10(L / ks))^-2.5. Used: the larger, the usual envelope of the
/// two (White, Fluid Mechanics, flat-plate friction): a surface is "smooth" until its roughness
/// pokes through the viscous sublayer, then friction depends on L / ks, not on Re.
pub fn friction_coeff(re: f64, l_m: f64, ks_um: f64) -> (f64, Option<f64>, f64) {
    let smooth = 0.075 / (re.log10() - 2.0).powi(2);
    let rough = (ks_um > 0.0).then(|| (1.89 + 1.62 * (l_m / (ks_um * 1e-6)).log10()).powf(-2.5));
    (smooth, rough, rough.map_or(smooth, |r| r.max(smooth)))
}

/// Stations and waterlines used to sample the submerged hull.
const NX: usize = 41;
const NZ: usize = 21;
/// Angular quadrature for the Michell integral. Integrated in u with
/// sec(theta) = cosh(u), u in [0, U_MAX] (theta up to ~88 deg; pi/2 itself
/// excluded). Uniform theta steps alias the transverse short waves near pi/2
/// at low Froude number; uniform u steps follow them. NTHETA midpoints:
/// a test shows < 1% change against 4x as many.
const NTHETA: usize = 256;
const U_MAX: f64 = 3.5;
const RHO: f64 = 998.0;
const G: f64 = 9.80665;
const NU: f64 = 1.139e-6;

/// Resistance of `hull` floating level at `draft_in` (inches above keel) at
/// `speed_mps`. Total = wave (Michell) + ITTC-1957 friction with form factor
/// k = 0 (no viscous form drag).
pub fn resistance(hull: &Hull, draft_in: f64, speed_mps: f64, ks_um: f64) -> Result<Resistance> {
    ensure!(draft_in.is_finite() && draft_in > 0.0, "draft must be finite and > 0, got {draft_in}");
    ensure!(speed_mps.is_finite() && speed_mps > 0.0, "speed must be finite and > 0, got {speed_mps}");
    ensure!(hull.length.is_finite() && hull.length > 0.0, "hull length must be > 0");
    let (x, z, y) = sample(hull, draft_in);
    resistance_offsets(&x, &z, &y, speed_mps, ks_um)
}

/// Displaced volume of `hull` floating level at `draft_in`, in^3 (same
/// sampling as the resistance integrals).
pub fn displaced_volume_in3(hull: &Hull, draft_in: f64) -> f64 {
    let (_, z, y) = sample(hull, draft_in);
    let dx = hull.length / (NX - 1) as f64;
    let dz = z[1] - z[0];
    let area: Vec<f64> = (0..NX).map(|i| 2.0 * trapz(&y[i * NZ..(i + 1) * NZ], dz)).collect();
    trapz(&area, dx)
}

/// Level draft at which `hull` displaces `weight_lb` of fresh water
/// (bisection on [`displaced_volume_in3`], monotone in draft). Err if it
/// would sink past the top of the hull table.
pub fn level_draft_in(hull: &Hull, weight_lb: f64) -> Result<f64> {
    ensure!(weight_lb.is_finite() && weight_lb > 0.0, "displacement must be > 0, got {weight_lb}");
    let need = weight_lb * IN3_PER_FT3 / WATER_PCF;
    let top = hull.z[hull.z.len() - 1];
    ensure!(displaced_volume_in3(hull, top) >= need, "{weight_lb:.0} lb sinks the hull past z = {top:.1} in");
    let (mut lo, mut hi) = (0.0, top);
    for _ in 0..60 {
        let mid = 0.5 * (lo + hi);
        if displaced_volume_in3(hull, mid) < need { lo = mid } else { hi = mid }
    }
    Ok(0.5 * (lo + hi))
}

/// Displacement-weighted drag, N: sum of weight x total resistance at the
/// level draft of (hull_lb + crew_lb), for each `(crew_lb, weight)`.
pub fn weighted_resistance(
    hull: &Hull,
    hull_lb: f64,
    displacements: &[Displacement],
    speed_mps: f64,
    ks_um: f64,
) -> Result<f64> {
    let mut sum = 0.0;
    for d in displacements {
        let draft = level_draft_in(hull, hull_lb + d.crew_lb)?;
        sum += d.weight * resistance(hull, draft, speed_mps, ks_um)?.total_n;
    }
    Ok(sum)
}

/// Cross-flow drag coefficient of a canoe section moving sideways (Hoerner,
/// Fluid-Dynamic Drag ch. 3: about 1.0 for a shallow surface-piercing section,
/// below the 2.0 of a flat plate in open water).
pub const CD_CROSSFLOW: f64 = 1.0;
/// lb (mass) -> kg.
const KG_PER_LB: f64 = 0.453_592_37;

/// Steady turn of radius `radius_in` at `speed_mps`, cross-flow (strip) model.
/// `x_in` stations from the bow (strictly increasing),
/// `depth_in` underwater lateral-profile depth at each (draft - keel height),
/// `displacement_lb` the floating weight.
///
/// Each strip moves sideways at w(x) = U (x/R - beta), x from midships toward
/// the bow, beta the drift angle (small angles). It carries the cross-flow
/// force f = 0.5 rho Cd T(x) |w| w against w (Allen-Perkins cross-flow
/// analogy, as in ship manoeuvring models). beta is solved so the net
/// lateral force supplies the centripetal force m U^2 / R. Then
/// moment = integral of x f dx, and drag = integral of 0.5 rho Cd T |w|^3 dx / U,
/// the power the paddlers spend to hold the turn (= moment / R + beta m U^2 / R:
/// steering plus induced drag). U cancels from beta, so drag scales as U^2.
/// Neglected: the linear (potential-lift) term, added mass, wave effects.
pub fn turn(
    x_in: &[f64],
    depth_in: &[f64],
    displacement_lb: f64,
    speed_mps: f64,
    radius_in: f64,
) -> Result<Turn> {
    let n = x_in.len();
    ensure!(n >= 3 && depth_in.len() == n, "turn: need >= 3 stations with a depth each");
    ensure!(x_in.iter().chain(depth_in).all(|v| v.is_finite()), "turn: non-finite profile");
    ensure!(depth_in.iter().all(|d| *d >= 0.0), "turn: negative profile depth");
    ensure!(speed_mps.is_finite() && speed_mps > 0.0, "turn: speed must be > 0, got {speed_mps}");
    ensure!(radius_in.is_finite() && radius_in > 0.0, "turn: radius must be > 0, got {radius_in}");
    ensure!(displacement_lb.is_finite() && displacement_lb > 0.0, "turn: displacement must be > 0");
    let s = 1.0 / M_TO_IN;
    ensure!(x_in.windows(2).all(|p| p[1] > p[0]), "turn: stations must increase from the bow");
    let mid = 0.5 * (x_in[0] + x_in[n - 1]);
    // x from midships toward the bow, m.
    let x: Vec<f64> = x_in.iter().map(|v| (mid - v) * s).collect();
    let t: Vec<f64> = depth_in.iter().map(|v| v * s).collect();
    let integrate = |f: &dyn Fn(f64) -> f64| -> f64 {
        (1..n).map(|i| 0.5 * (t[i - 1] * f(x[i - 1]) + t[i] * f(x[i])) * (x[i - 1] - x[i])).sum()
    };
    ensure!(integrate(&|_| 1.0) > 0.0, "turn: no underwater lateral profile");
    let (r, u) = (radius_in * s, speed_mps);
    let k = 0.5 * RHO * CD_CROSSFLOW;
    let m = displacement_lb * KG_PER_LB;
    // Inward lateral force per U^2 minus the centripetal need; increasing in beta.
    let excess = |b: f64| k * integrate(&|xx| (b - xx / r) * (xx / r - b).abs()) - m / r;
    let (mut a, mut c) = (-1.0, 1.0);
    while excess(a) > 0.0 {
        a *= 2.0;
    }
    while excess(c) < 0.0 {
        c *= 2.0;
    }
    for _ in 0..100 {
        let b = 0.5 * (a + c);
        if excess(b) < 0.0 { a = b } else { c = b }
    }
    let beta = 0.5 * (a + c);
    let w = |xx: f64| u * (xx / r - beta);
    let moment = k * integrate(&|xx| xx * w(xx) * w(xx).abs());
    let drag = k * integrate(&|xx| w(xx).abs().powi(3)) / u;
    ensure!(drag.is_finite() && moment.is_finite(), "turn: non-finite result");
    Ok(Turn { drag_n: drag, moment_nm: moment, drift_deg: beta.to_degrees() })
}

/// Resample `hull` on the NX x NZ grid from keel to `draft_in` (bilinear).
fn sample(hull: &Hull, draft_in: f64) -> (Vec<f64>, Vec<f64>, Vec<f64>) {
    let x = linspace(0.0, hull.length, NX);
    let z = linspace(0.0, draft_in, NZ);
    let mut y = vec![0.0; NX * NZ];
    for (i, &xi) in x.iter().enumerate() {
        // half-breadth column at this station, then along z (top value extruded)
        let j = hull.x.partition_point(|&v| v <= xi).clamp(1, hull.x.len() - 1);
        let t = ((xi - hull.x[j - 1]) / (hull.x[j] - hull.x[j - 1])).clamp(0.0, 1.0);
        let col: Vec<f64> = (0..hull.z.len())
            .map(|k| hull.half_breadth[j - 1][k] * (1.0 - t) + hull.half_breadth[j][k] * t)
            .collect();
        for (k, &zk) in z.iter().enumerate() {
            let m = hull.z.partition_point(|&v| v <= zk).clamp(1, hull.z.len() - 1);
            let u = ((zk - hull.z[m - 1]) / (hull.z[m] - hull.z[m - 1])).clamp(0.0, 1.0);
            y[i * NZ + k] = col[m - 1] * (1.0 - u) + col[m] * u;
        }
    }
    (x, z, y)
}

fn trapz(f: &[f64], dx: f64) -> f64 {
    let n = f.len();
    dx * (f.iter().sum::<f64>() - 0.5 * (f[0] + f[n - 1]))
}

/// Core on a uniform grid: `x` stations (in, bow to stern), `z` waterlines
/// (in, keel 0 up to the waterline at `z[last]`), `y[i * nz + k]` half-breadth, in.
pub(crate) fn resistance_offsets(x: &[f64], z: &[f64], y: &[f64], v: f64, ks_um: f64) -> Result<Resistance> {
    resistance_n(x, z, y, v, NTHETA, ks_um)
}

fn resistance_n(x: &[f64], z: &[f64], y: &[f64], v: f64, nth: usize, ks_um: f64) -> Result<Resistance> {
    ensure!(ks_um.is_finite() && ks_um >= 0.0, "roughness must be >= 0 um, got {ks_um}");
    let (nx, nz) = (x.len(), z.len());
    ensure!(nx >= 3 && nz >= 2 && y.len() == nx * nz, "bad offset grid");
    ensure!(y.iter().all(|a| a.is_finite() && *a >= 0.0), "non-finite or negative half-breadth");
    let s = 1.0 / M_TO_IN;
    let l = (x[nx - 1] - x[0]) * s;
    let t = (z[nz - 1] - z[0]) * s;
    ensure!(l > 0.0 && t > 0.0, "zero length or draft");
    let (dx, dz) = (l / (nx - 1) as f64, t / (nz - 1) as f64);
    let ym: Vec<f64> = y.iter().map(|a| a * s).collect();
    // displaced volume
    let area: Vec<f64> = (0..nx).map(|i| 2.0 * trapz(&ym[i * nz..(i + 1) * nz], dz)).collect();
    let vol = trapz(&area, dx);
    ensure!(vol > 0.0, "no submerged volume at this draft");

    // dy/dx by central differences
    let mut dydx = vec![0.0; nx * nz];
    for i in 0..nx {
        let (a, b, h) = match i {
            0 => (0, 1, dx),
            _ if i == nx - 1 => (nx - 2, nx - 1, dx),
            _ => (i - 1, i + 1, 2.0 * dx),
        };
        for k in 0..nz {
            dydx[i * nz + k] = (ym[b * nz + k] - ym[a * nz + k]) / h;
        }
    }
    let k0 = G / (v * v);
    let xc: Vec<f64> = (0..nx).map(|i| -l / 2.0 + i as f64 * dx).collect();
    let zc: Vec<f64> = (0..nz).map(|k| -t + k as f64 * dz).collect();
    let mut ez = vec![0.0; nz];
    let mut iz = vec![0.0; nx];
    let du = U_MAX / nth as f64;
    let mut sum = 0.0;
    for it in 0..nth {
        // sec(theta) = cosh(u), d(theta) = du / cosh(u); midpoint rule in u
        let sec = ((it as f64 + 0.5) * du).cosh();
        let s2 = sec * sec;
        let (wlo, whi) = exp_weights(k0 * s2, dz);
        for (e, &zk) in ez.iter_mut().zip(&zc) {
            *e = (k0 * s2 * zk).exp();
        }
        for (i, out) in iz.iter_mut().enumerate() {
            let row = &dydx[i * nz..(i + 1) * nz];
            let mut acc = 0.0;
            for k in 0..nz - 1 {
                acc += (row[k] * wlo + row[k + 1] * whi) * ez[k + 1];
            }
            *out = acc;
        }
        let kx = k0 * sec;
        let ((ar, ai), (br, bi)) = osc_weights(kx, dx);
        let (mut p, mut q) = (0.0, 0.0);
        for i in 0..nx - 1 {
            let (sn, cs) = (kx * xc[i]).sin_cos();
            let (fr, fi) = (iz[i] * ar + iz[i + 1] * br, iz[i] * ai + iz[i + 1] * bi);
            p += cs * fr - sn * fi;
            q += cs * fi + sn * fr;
        }
        sum += (p * p + q * q) * s2;
    }
    let sum = sum * du;
    let wave = sum * 4.0 * RHO * G * G / (std::f64::consts::PI * v * v);

    // wetted surface: girth below the waterline (both sides + flat bottom); a segment with zero
    // half-breadth at both ends is below the rockered keel, not hull (as section::segment_len)
    let girth: Vec<f64> = (0..nx)
        .map(|i| {
            let r = &ym[i * nz..(i + 1) * nz];
            let seg = |w: &[f64]| if w[0] <= 0.0 && w[1] <= 0.0 { 0.0 } else { (w[1] - w[0]).hypot(dz) };
            2.0 * (r[0] + r.windows(2).map(seg).sum::<f64>())
        })
        .collect();
    let wetted = trapz(&girth, dx);
    let re = v * l / NU;
    let (cf_smooth, cf_rough, cf) = friction_coeff(re, l, ks_um);
    let friction = 0.5 * RHO * v * v * wetted * cf;
    let total = wave + friction;
    ensure!(total.is_finite() && wave >= 0.0, "non-finite resistance");
    Ok(Resistance {
        wave_n: wave,
        friction_n: friction,
        total_n: total,
        wetted_m2: wetted,
        length_m: l,
        speed_mps: v,
        froude: v / (G * l).sqrt(),
        reynolds: re,
        cf_smooth,
        cf_rough,
        cf,
    })
}

/// Weights (lo, hi) such that the integral over [-h, 0] of the linear
/// interpolant of (f_lo, f_hi) times exp(k s) is f_lo*lo + f_hi*hi (k >= 0).
fn exp_weights(k: f64, h: f64) -> (f64, f64) {
    let u = k * h;
    let (j0, j1) = if u < 1e-4 {
        (h * (1.0 - u / 2.0 + u * u / 6.0), h * h * (0.5 - u / 6.0 + u * u / 24.0))
    } else {
        let e = (-u).exp();
        let j0 = (1.0 - e) / k;
        (j0, h * e / k - j0 / k + h * j0)
    };
    (j0 - j1 / h, j1 / h)
}

/// Complex weights (a, b) such that the integral over [0, h] of the linear
/// interpolant of (f_a, f_b) times exp(i k s) is f_a*a + f_b*b.
#[allow(clippy::type_complexity)]
fn osc_weights(k: f64, h: f64) -> ((f64, f64), (f64, f64)) {
    let u = k * h;
    // I0 = int e^{iks}, I1 = int s e^{iks}, as (re, im)
    let (i0, i1) = if u.abs() < 1e-3 {
        ((h * (1.0 - u * u / 6.0), h * u / 2.0), (h * h * (0.5 - u * u / 8.0), h * h * u / 3.0))
    } else {
        let (sn, cs) = u.sin_cos();
        // (e^{iu} - 1)/(ik) = (sin u + i(1 - cos u))/k
        let i0 = (sn / k, (1.0 - cs) / k);
        // h e^{iu}/(ik) + (e^{iu}-1)/k^2
        let i1 = (h * sn / k + (cs - 1.0) / (k * k), -h * cs / k + sn / (k * k));
        (i0, i1)
    };
    ((i0.0 - i1.0 / h, i0.1 - i1.1 / h), (i1.0 / h, i1.1 / h))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Instant;

    fn wigley(l: f64, b: f64, t: f64) -> (Vec<f64>, Vec<f64>, Vec<f64>) {
        let x = linspace(0.0, l, NX);
        let z = linspace(0.0, t, NZ);
        let mut y = vec![0.0; NX * NZ];
        for i in 0..NX {
            for k in 0..NZ {
                let xi = 2.0 * (x[i] - l / 2.0) / l;
                let zt = (t - z[k]) / t; // depth below waterline / T
                y[i * NZ + k] = b / 2.0 * (1.0 - xi * xi) * (1.0 - zt * zt);
            }
        }
        (x, z, y)
    }

    fn box_hull() -> Hull {
        // 200 x 20 in box, 12 in deep: volume = 4000 in^2 x draft.
        Hull {
            format: crate::hull::FORMAT.into(),
            units: "in".into(),
            name: "box".into(),
            source: String::new(),
            length: 200.0,
            draft_design: 12.0,
            x: linspace(0.0, 200.0, 11),
            z: linspace(0.0, 12.0, 7),
            half_breadth: vec![vec![10.0; 7]; 11],
            stem: None,
        }
    }

    fn rect(l: f64, t: f64, n: usize) -> (Vec<f64>, Vec<f64>) {
        (linspace(0.0, l, n), vec![t; n])
    }

    #[test]
    fn turn_of_a_rectangular_profile_by_hand() {
        // Rectangle L x T, pivot a = beta R ahead of midships, h = L/2, all SI:
        //   balance  k T (6 h^2 a + 2 a^3) / (3 R^2) = m / R
        //   drag     k T U^2 ((h + a)^4 + (h - a)^4) / (4 R^3)
        //   moment   k T U^2 / R^2 [((h - a)^4 + (h + a)^4) / 4 + a ((h - a)^3 - (h + a)^3) / 3]
        let (l, t, r, u, lb) = (240.0, 6.0, 600.0, 2.5, 500.0);
        let (x, d) = rect(l, t, 4001);
        let tr = turn(&x, &d, lb, u, r).unwrap();
        let (h, tm, rm) = (0.5 * l / M_TO_IN, t / M_TO_IN, r / M_TO_IN);
        let (k, m) = (0.5 * RHO * CD_CROSSFLOW, lb * KG_PER_LB);
        let rhs = 3.0 * m * rm / (k * tm);
        let mut a: f64 = 1.0;
        for _ in 0..50 {
            a -= (6.0 * h * h * a + 2.0 * a.powi(3) - rhs) / (6.0 * h * h + 6.0 * a * a);
        }
        assert!(a > 0.0 && a < h, "pivot inside the hull: a = {a}");
        let drag = k * tm * u * u * ((h + a).powi(4) + (h - a).powi(4)) / (4.0 * rm.powi(3));
        let mom = k * tm * u * u / (rm * rm)
            * (((h - a).powi(4) + (h + a).powi(4)) / 4.0 + a * ((h - a).powi(3) - (h + a).powi(3)) / 3.0);
        let rel = |p: f64, q: f64| (p / q - 1.0).abs();
        assert!(rel(tr.drift_deg, (a / rm).to_degrees()) < 1e-5, "{tr:?} a {a}");
        assert!(rel(tr.drag_n, drag) < 1e-5 && rel(tr.moment_nm, mom) < 1e-5, "{tr:?} {drag} {mom}");
        // Energy: drag = moment / R + beta m U^2 / R (steering + induced drag).
        let beta = tr.drift_deg.to_radians();
        assert!(rel(tr.drag_n, tr.moment_nm / rm + beta * m * u * u / rm) < 1e-9);
    }

    #[test]
    fn turn_scales_and_fails_loudly() {
        // Rockered profile: depth falls to zero at both ends.
        let x = linspace(0.0, 240.0, 401);
        let d: Vec<f64> = x.iter().map(|v| 6.0 * (1.0 - (v / 120.0 - 1.0).powi(4))).collect();
        let a = turn(&x, &d, 500.0, 2.5, 600.0).unwrap();
        // drag ~ U^2, drift independent of U.
        let b = turn(&x, &d, 500.0, 5.0, 600.0).unwrap();
        assert!((b.drag_n / a.drag_n - 4.0).abs() < 1e-9 && (b.drift_deg - a.drift_deg).abs() < 1e-9);
        // Geometric similarity x2 (mass x8): drift same, drag x4, moment x8.
        let x2: Vec<f64> = x.iter().map(|v| 2.0 * v).collect();
        let d2: Vec<f64> = d.iter().map(|v| 2.0 * v).collect();
        let c = turn(&x2, &d2, 4000.0, 2.5, 1200.0).unwrap();
        assert!((c.drift_deg - a.drift_deg).abs() < 1e-9);
        assert!((c.drag_n / a.drag_n - 4.0).abs() < 1e-9 && (c.moment_nm / a.moment_nm - 8.0).abs() < 1e-9);
        // Tighter turn costs more; deterministic.
        assert!(turn(&x, &d, 500.0, 2.5, 300.0).unwrap().drag_n > a.drag_n);
        assert_eq!(turn(&x, &d, 500.0, 2.5, 600.0).unwrap(), a);
        assert!(turn(&x, &vec![0.0; 401], 500.0, 2.5, 600.0).is_err());
        assert!(turn(&x, &d, 500.0, 2.5, 0.0).is_err());
        assert!(turn(&x, &d, 0.0, 2.5, 600.0).is_err());
    }

    #[test]
    fn level_draft_of_a_box() {
        let h = box_hull();
        let w = 4000.0 * 5.0 * WATER_PCF / IN3_PER_FT3;
        assert!((level_draft_in(&h, w).unwrap() - 5.0).abs() < 1e-9);
        assert!(level_draft_in(&h, 1e6).is_err());
    }

    #[test]
    fn weighted_drag_with_one_displacement_equals_single_drag() {
        let h = box_hull();
        let one = [Displacement { crew_lb: 350.0, weight: 1.0 }];
        let d = level_draft_in(&h, 100.0 + 350.0).unwrap();
        let single = resistance(&h, d, 2.5, 0.0).unwrap().total_n;
        assert_eq!(weighted_resistance(&h, 100.0, &one, 2.5, 0.0).unwrap(), single);
        let two =
            [Displacement { crew_lb: 350.0, weight: 0.75 }, Displacement { crew_lb: 700.0, weight: 0.25 }];
        let heavy = resistance(&h, level_draft_in(&h, 800.0).unwrap(), 2.5, 0.0).unwrap().total_n;
        let wsum = weighted_resistance(&h, 100.0, &two, 2.5, 0.0).unwrap();
        assert!((wsum - (0.75 * single + 0.25 * heavy)).abs() < 1e-9 && heavy > single);
    }

    #[test]
    fn roughness_raises_friction_once_it_pokes_through() {
        // 5.5 m canoe at 3 m/s: Re = 1.45e7, ITTC Cf = 0.00282. Fully rough Cf passes it near
        // ks = 28 um; 100 um is about 25% more friction, 300 um about 56% more.
        let re = 3.0 * 5.5 / NU;
        let (s, r0, c0) = friction_coeff(re, 5.5, 0.0);
        assert!((s - 0.00282).abs() < 2e-5 && r0.is_none() && c0 == s, "{s}");
        let (_, r20, c20) = friction_coeff(re, 5.5, 20.0);
        assert!(r20.unwrap() < s && c20 == s, "20 um is still smooth");
        let (_, _, c100) = friction_coeff(re, 5.5, 100.0);
        let (_, _, c300) = friction_coeff(re, 5.5, 300.0);
        assert!((c100 / s - 1.25).abs() < 0.03 && (c300 / s - 1.56).abs() < 0.04, "{c100} {c300}");
        // Only friction moves; the wave part does not see the surface.
        let h = box_hull();
        let (a, b) = (resistance(&h, 5.0, 2.5, 0.0).unwrap(), resistance(&h, 5.0, 2.5, 300.0).unwrap());
        assert_eq!(a.wave_n, b.wave_n);
        assert!(b.friction_n > 1.4 * a.friction_n && b.cf_rough.is_some() && a.cf_rough.is_none());
        assert!((b.friction_n - 0.5 * RHO * 2.5 * 2.5 * b.wetted_m2 * b.cf).abs() < 1e-9);
        assert!(resistance(&h, 5.0, 2.5, -1.0).is_err());
    }

    #[test]
    fn thin_plate_is_friction_only() {
        let (l, t) = (200.0, 6.0);
        let x = linspace(0.0, l, NX);
        let z = linspace(0.0, t, NZ);
        let y = vec![0.001; NX * NZ];
        let r = resistance_offsets(&x, &z, &y, 2.5, 0.0).unwrap();
        assert!(r.wave_n < 1e-3 * r.friction_n, "{r:?}");
        // ITTC by hand: S = 2 L T (sides) + bottom, both sides of the plate
        let (lm, tm) = (l / M_TO_IN, t / M_TO_IN);
        let s = 2.0 * lm * tm + 2.0 * 0.001 / M_TO_IN * lm;
        let re = 2.5 * lm / 1.139e-6;
        let rf = 0.5 * 998.0 * 6.25 * s * 0.075 / (re.log10() - 2.0).powi(2);
        assert!((r.friction_n / rf - 1.0).abs() < 1e-9, "{} {rf}", r.friction_n);
        assert!((r.wetted_m2 / s - 1.0).abs() < 1e-9);
    }

    #[test]
    fn waterlines_below_a_lifted_keel_are_not_wetted() {
        let (l, t) = (200.0, 6.0);
        let x = linspace(0.0, l, NX);
        let z = linspace(0.0, t, NZ);
        let full = vec![0.001; NX * NZ];
        // Same plate with the bottom half of every station empty (keel lifted to mid-draft).
        let lifted: Vec<f64> = (0..NX * NZ).map(|j| if j % NZ < NZ / 2 { 0.0 } else { 0.001 }).collect();
        let (a, b) = (
            resistance_offsets(&x, &z, &full, 2.5, 0.0).unwrap().wetted_m2,
            resistance_offsets(&x, &z, &lifted, 2.5, 0.0).unwrap().wetted_m2,
        );
        // Sides: NZ - NZ/2 of the NZ - 1 waterline segments remain (bottom strip negligible).
        let want = (NZ - NZ / 2) as f64 / (NZ - 1) as f64;
        assert!((b / a / want - 1.0).abs() < 1e-3, "{b} vs {a}");
    }

    #[test]
    fn wigley_wave_curve() {
        let (l, b, t) = (240.0, 24.0, 15.0); // L/B 10, B/T 1.6 (standard Wigley)
        let lm = l / M_TO_IN;
        let cw = |fn_: f64| {
            let v = fn_ * (G * lm).sqrt();
            let (x, z, y) = wigley(l, b, t);
            let r = resistance_offsets(&x, &z, &y, v, 0.0).unwrap();
            r.wave_n / (0.5 * RHO * v * v * r.wetted_m2)
        };
        let c: Vec<f64> = [0.15, 0.2, 0.3, 0.35, 0.5].iter().map(|&f| cw(f)).collect();
        eprintln!("Wigley Cw at Fn .15 .2 .3 .35 .5: {c:?}");
        assert!(c.iter().all(|v| v.is_finite() && *v > 0.0));
        // Classic Michell curve for Wigley: rises steeply, hump near Fn 0.3,
        // hollow near 0.35, main hump near 0.5 of O(4e-3).
        assert!(c[1] > 2.0 * c[0] && c[2] > 2.0 * c[1], "{c:?}");
        assert!(c[3] < c[2] && c[3] > c[1], "hollow at 0.35: {c:?}");
        assert!(c[4] > c[2] && c[4] > 2e-3 && c[4] < 8e-3, "{c:?}");
    }

    #[test]
    fn quadrature_weights_exact_for_linear() {
        let (h, k) = (0.3, 2.0);
        // f = 1 on [-h,0]: (1 - e^{-kh})/k
        let (lo, hi) = exp_weights(k, h);
        assert!((lo + hi - (1.0 - (-k * h).exp()) / k).abs() < 1e-12);
        let (lo2, hi2) = exp_weights(1e-9, h);
        assert!((lo2 - h / 2.0).abs() < 1e-9 && (hi2 - h / 2.0).abs() < 1e-9);
        let ((ar, ai), (br, bi)) = osc_weights(k, h);
        assert!((ar + br - (k * h).sin() / k).abs() < 1e-12);
        assert!((ai + bi - (1.0 - (k * h).cos()) / k).abs() < 1e-12);
        let ((ar, _), (br, _)) = osc_weights(1e-9, h);
        assert!((ar - h / 2.0).abs() < 1e-9 && (br - h / 2.0).abs() < 1e-9);
    }

    #[test]
    fn angular_quadrature_converged() {
        let (x, z, y) = wigley(240.0, 24.0, 15.0);
        for fn_ in [0.15, 0.2, 0.3, 0.5] {
            let v = fn_ * (G * 240.0 / M_TO_IN).sqrt();
            let a = resistance_n(&x, &z, &y, v, NTHETA, 0.0).unwrap().wave_n;
            let b = resistance_n(&x, &z, &y, v, 4 * NTHETA, 0.0).unwrap().wave_n;
            assert!((a / b - 1.0).abs() < 0.01, "Fn {fn_}: {a} vs {b}");
        }
    }

    #[test]
    #[ignore]
    fn print_curve() {
        for (nx, nz) in [(41, 21), (161, 61)] {
            let (l, b, t) = (240.0, 24.0, 15.0);
            let x = linspace(0.0, l, nx);
            let z = linspace(0.0, t, nz);
            let mut y = vec![0.0; nx * nz];
            for i in 0..nx {
                for k in 0..nz {
                    let xi = 2.0 * (x[i] - l / 2.0) / l;
                    let zt = (t - z[k]) / t;
                    y[i * nz + k] = b / 2.0 * (1.0 - xi * xi) * (1.0 - zt * zt);
                }
            }
            for f in [0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.5, 0.6] {
                let v = f * (G * l / M_TO_IN).sqrt();
                let r = resistance_offsets(&x, &z, &y, v, 0.0).unwrap();
                eprintln!(
                    "{nx}x{nz} Fn {f}: Cw*1e3 {:.3}",
                    1e3 * r.wave_n / (0.5 * RHO * v * v * r.wetted_m2)
                );
            }
        }
    }

    #[test]
    fn errors_not_zero() {
        let (x, z, y) = wigley(240.0, 24.0, 15.0);
        assert!(resistance_offsets(&x, &z, &y, 0.0, 0.0).is_err());
        assert!(resistance_offsets(&x, &z, &vec![0.0; y.len()], 2.0, 0.0).is_err());
    }

    #[test]
    fn fast_and_hull_entry() {
        let hull = Hull::load(std::path::Path::new("tests/golden/off1_hull.json")).unwrap();
        let t0 = Instant::now();
        let n = 50;
        let mut r = None;
        for _ in 0..n {
            r = Some(resistance(&hull, 5.0, 2.5, 0.0).unwrap());
        }
        let dt = t0.elapsed() / n;
        eprintln!("resistance(): {dt:?} per call, {:?}", r.unwrap());
        assert!(dt.as_secs_f64() < 2e-3 || cfg!(debug_assertions), "{dt:?}");
        assert!(resistance(&hull, 0.0, 2.5, 0.0).is_err());
    }
}
