//! Water resistance of a floating hull. Owner: T12 (physics agent).
//!
//! Used only for the `drag` objective (pick the fastest hull that is strong
//! enough). Pure function, no I/O. Units at this boundary: hull geometry in
//! inches, speed in m/s, result in newtons (convert inside, see `units`).
//!
//! Model (port intent from old/wave_resistance.py; do not port its numba code):
//! * wave resistance: Michell thin-ship integral over the SUBMERGED part of the
//!   hull at the loaded draft (half-breadths clipped at `draft_in`), fresh water
//!   998 kg/m^3, g = 9.80665, ntheta = 128 (or fewer if a test shows <1%
//!   change), theta excluding pi/2.
//! * friction: ITTC-1957, Cf = 0.075 / (log10 Re - 2)^2, Re = V L / nu,
//!   nu = 1.139e-6 m^2/s, times 0.5 rho V^2 S with S the wetted surface
//!   (girth below the draft integrated along the length).
//!   No form factor (k = 0); say so in the result doc.
//!
//! Errors (Err, never 0.0): non-finite inputs, speed <= 0, draft <= 0, or a
//! hull with no submerged volume at `draft_in`.

use crate::hull::Hull;
use crate::hull::linspace;
use crate::units::M_TO_IN;
use anyhow::{Result, ensure};
use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
pub struct Resistance {
    pub wave_n: f64,
    pub friction_n: f64,
    pub total_n: f64,
    /// Wetted surface used for friction, m^2.
    pub wetted_m2: f64,
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
pub fn resistance(hull: &Hull, draft_in: f64, speed_mps: f64) -> Result<Resistance> {
    ensure!(draft_in.is_finite() && draft_in > 0.0, "draft must be finite and > 0, got {draft_in}");
    ensure!(speed_mps.is_finite() && speed_mps > 0.0, "speed must be finite and > 0, got {speed_mps}");
    ensure!(hull.length.is_finite() && hull.length > 0.0, "hull length must be > 0");
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
    resistance_offsets(&x, &z, &y, speed_mps)
}

fn trapz(f: &[f64], dx: f64) -> f64 {
    let n = f.len();
    dx * (f.iter().sum::<f64>() - 0.5 * (f[0] + f[n - 1]))
}

/// Core on a uniform grid: `x` stations (in, bow to stern), `z` waterlines
/// (in, keel 0 up to the waterline at `z[last]`), `y[i * nz + k]` half-breadth, in.
pub(crate) fn resistance_offsets(x: &[f64], z: &[f64], y: &[f64], v: f64) -> Result<Resistance> {
    resistance_n(x, z, y, v, NTHETA)
}

fn resistance_n(x: &[f64], z: &[f64], y: &[f64], v: f64, nth: usize) -> Result<Resistance> {
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

    // wetted surface: girth below the waterline (both sides + flat bottom)
    let girth: Vec<f64> = (0..nx)
        .map(|i| {
            let r = &ym[i * nz..(i + 1) * nz];
            2.0 * (r[0] + r.windows(2).map(|w| (w[1] - w[0]).hypot(dz)).sum::<f64>())
        })
        .collect();
    let wetted = trapz(&girth, dx);
    let re = v * l / NU;
    let cf = 0.075 / (re.log10() - 2.0).powi(2);
    let friction = 0.5 * RHO * v * v * wetted * cf;
    let total = wave + friction;
    ensure!(total.is_finite() && wave >= 0.0, "non-finite resistance");
    Ok(Resistance { wave_n: wave, friction_n: friction, total_n: total, wetted_m2: wetted })
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

    #[test]
    fn thin_plate_is_friction_only() {
        let (l, t) = (200.0, 6.0);
        let x = linspace(0.0, l, NX);
        let z = linspace(0.0, t, NZ);
        let y = vec![0.001; NX * NZ];
        let r = resistance_offsets(&x, &z, &y, 2.5).unwrap();
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
    fn wigley_wave_curve() {
        let (l, b, t) = (240.0, 24.0, 15.0); // L/B 10, B/T 1.6 (standard Wigley)
        let lm = l / M_TO_IN;
        let cw = |fn_: f64| {
            let v = fn_ * (G * lm).sqrt();
            let (x, z, y) = wigley(l, b, t);
            let r = resistance_offsets(&x, &z, &y, v).unwrap();
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
            let a = resistance_n(&x, &z, &y, v, NTHETA).unwrap().wave_n;
            let b = resistance_n(&x, &z, &y, v, 4 * NTHETA).unwrap().wave_n;
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
                let r = resistance_offsets(&x, &z, &y, v).unwrap();
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
        assert!(resistance_offsets(&x, &z, &y, 0.0).is_err());
        assert!(resistance_offsets(&x, &z, &vec![0.0; y.len()], 2.0).is_err());
    }

    #[test]
    fn fast_and_hull_entry() {
        let hull = Hull::load(std::path::Path::new("tests/golden/off1_hull.json")).unwrap();
        let t0 = Instant::now();
        let n = 50;
        let mut r = None;
        for _ in 0..n {
            r = Some(resistance(&hull, 5.0, 2.5).unwrap());
        }
        let dt = t0.elapsed() / n;
        eprintln!("resistance(): {dt:?} per call, {:?}", r.unwrap());
        assert!(dt.as_secs_f64() < 2e-3 || cfg!(debug_assertions), "{dt:?}");
        assert!(resistance(&hull, 0.0, 2.5).is_err());
    }
}
