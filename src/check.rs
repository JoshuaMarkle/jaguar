//! Pass/fail checks on one analysed design. Owner: strength agent (T9).
//!
//! Every check produces a [`CheckResult`] with demand, capacity and D/C in the
//! same units, so the report and the web UI can list them uniformly. The
//! candidate's governing D/C (`Verdict::dc`) is the MAX over all checks.
//!
//! Checks (bending is the golden-tested one
//! and must keep matching `tests/golden.rs`):
//! * `bending_tension`: sigma = |M| c / I, c = max(zbar, D - zbar), stations
//!   with I > 1e-3 max I. capacity = (ft_slope*rho + ft_intercept) / FS.
//!   The tension fibre is NOT sign-selected: the golden oracle uses max(c) and
//!   the signed fibre misses it by ~40%, so the conservative max stays.
//! * `bending_compression`: sigma at the actual compression fibre (M > 0
//!   sagging: c = D - zbar; hogging: c = zbar) vs 0.85 f'c / FS,
//!   f'c from the lab fit (`mix::StrengthFit::fc_psi`).
//! * `shear`: tau = |V| Q / (I b) for the thin shell, b = 2t (two walls), Q =
//!   first moment of the shell below zbar (section.rs segment model), same
//!   station mask as bending, vs 2 lambda sqrt(f'c) / FS (ACI one-way).
//! * `punching`: one knee on the hull bottom, ACI 318-19 22.6.5.2:
//!   b0 = 4 (patch + d), d = t/2, v_u = P/(b0 t) with P = heaviest paddler *
//!   dynamic factor; capacity = min of the three ACI v_c / FS. Port intent from
//!   old/structural.py::punching_shear_check (keep the min-of-three rule).
//! * Freeboard: depth - max local draft (not a D/C; gated separately).
//!
//! f'c <= 0 or capacity <= 0 makes that check dc = +inf; optimize treats a
//! +inf verdict as a FAILED candidate (verdict kept), NaN as an error.
//! Order of `checks`: bending_tension, bending_compression, shear, punching.
//! A check that cannot be computed FAILS. Never
//! default, never skip.

use crate::equilibrium::Loads;
use crate::hull::Grid;
use crate::mix::StrengthFit;
use crate::params::Material;
use crate::section::Sections;
use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
pub struct CheckResult {
    /// Stable id: "bending_tension" | "bending_compression" | "shear" | "punching".
    pub name: &'static str,
    /// Demand and capacity in psi (stresses), capacity already divided by FS.
    pub demand: f64,
    pub capacity: f64,
    pub dc: f64,
    /// Station of the governing value, in from bow (None for local checks).
    pub x_in: Option<f64>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Verdict {
    /// Governing D/C = max over `checks`.
    pub dc: f64,
    /// Name of the governing check.
    pub governing_check: &'static str,
    /// Station of the governing bending D/C, in from bow.
    pub x_governing_in: f64,
    /// Minimum loaded freeboard, in.
    pub freeboard_in: f64,
    /// Every check that ran, in a fixed order.
    pub checks: Vec<CheckResult>,
}

/// Wall thickness recovered from the section: area = t * (2 y0 + sum 2 ds).
fn thickness(grid: &Grid, sec: &Sections) -> Option<f64> {
    (0..grid.nx()).find_map(|i| {
        let p = perimeter(grid.row(i), &grid.z);
        (p > 1e-9).then(|| sec.area[i] / p)
    })
}

/// Shell "perimeter" per unit thickness: 2 y0 (keel strip) + 2 sum ds.
fn perimeter(y: &[f64], z: &[f64]) -> f64 {
    2.0 * y[0] + (0..z.len() - 1).map(|k| 2.0 * (y[k + 1] - y[k]).hypot(z[k + 1] - z[k])).sum::<f64>()
}

/// First moment about `zb` of the shell area below `zb` (same segment model
/// as `section.rs`; segments straddling the axis are clipped linearly).
pub fn first_moment_below(y: &[f64], z: &[f64], t: f64, zb: f64) -> f64 {
    let mut q = if z[0] < zb { 2.0 * y[0] * t * (zb - z[0]) } else { 0.0 };
    for k in 0..z.len() - 1 {
        let (z0, z1) = (z[k], z[k + 1]);
        if z0 >= zb || z1 <= z0 {
            continue;
        }
        let ds = (y[k + 1] - y[k]).hypot(z1 - z0);
        let top = z1.min(zb);
        let frac = (top - z0) / (z1 - z0);
        q += 2.0 * t * ds * frac * (zb - 0.5 * (z0 + top));
    }
    q
}

/// ACI 318-19 22.6.5.2 two-way shear capacity (before FS): min of three v_c.
pub fn punching_vc(fc: f64, lambda: f64, d: f64, b0: f64) -> f64 {
    let sq = lambda * fc.sqrt();
    let (beta, alpha_s) = (1.0, 40.0);
    (4.0 * sq).min((2.0 + 4.0 / beta) * sq).min((2.0 + alpha_s * d / b0) * sq)
}

fn result(name: &'static str, demand: f64, capacity: f64, x_in: Option<f64>) -> CheckResult {
    let dc = if capacity.is_nan() || capacity <= 0.0 || !capacity.is_finite() || !demand.is_finite() {
        f64::INFINITY
    } else {
        demand / capacity
    };
    CheckResult { name, demand, capacity, dc, x_in }
}

pub fn check(
    grid: &Grid,
    sec: &Sections,
    loads: &Loads,
    rho: f64,
    mat: &Material,
    fit: &StrengthFit,
    max_paddler_lb: f64,
) -> Verdict {
    let fs = mat.factor_of_safety;
    let fc = fit.fc_psi(rho);
    // f'c comes from the team's own lightweight-mix breaks via f_ct = 6.7 sqrt(f'c),
    // so the lightweight reduction is already in the data: lambda = 1 here.
    let lambda = 1.0;
    let t = thickness(grid, sec).unwrap_or(f64::NAN);
    let imax = sec.inertia.iter().cloned().fold(0.0, f64::max);
    let (mut st, mut xt) = (f64::NEG_INFINITY, f64::NAN);
    let (mut sc, mut xc) = (f64::NEG_INFINITY, f64::NAN);
    let (mut tau, mut xs) = (f64::NEG_INFINITY, f64::NAN);
    for i in 0..grid.nx() {
        let ii = sec.inertia[i];
        if ii <= 1e-3 * imax {
            continue;
        }
        let m = loads.moment[i];
        let (c_top, c_bot) = (grid.depth - sec.zbar[i], sec.zbar[i]);
        // Sagging (M > 0): top in compression, bottom in tension.
        // Tension keeps the conservative max fibre: the signed tension fibre
        // misses the prototype golden (0.49 vs 0.84), so the oracle used max(c).
        let cc = if m >= 0.0 { c_top } else { c_bot };
        let s = m.abs() * c_top.max(c_bot) / ii;
        if s > st {
            (st, xt) = (s, grid.x[i]);
        }
        let s = m.abs() * cc / ii;
        if s > sc {
            (sc, xc) = (s, grid.x[i]);
        }
        let q = first_moment_below(grid.row(i), &grid.z, t, sec.zbar[i]);
        let s = loads.shear[i].abs() * q / (ii * 2.0 * t);
        if s > tau {
            (tau, xs) = (s, grid.x[i]);
        }
    }
    let ft_cap = fit.flexural_psi(rho) / fs;
    let (fc_cap, v_cap, p_cap, vu) = if fc > 0.0 {
        let d = t / 2.0;
        let b0 = 4.0 * (mat.knee_patch_in + d);
        (
            0.85 * fc / fs,
            2.0 * lambda * fc.sqrt() / fs,
            punching_vc(fc, lambda, d, b0) / fs,
            max_paddler_lb / (b0 * t),
        )
    } else {
        (f64::NAN, f64::NAN, f64::NAN, max_paddler_lb / (4.0 * (mat.knee_patch_in + t / 2.0) * t))
    };
    let checks = vec![
        result("bending_tension", st, ft_cap, Some(xt)),
        result("bending_compression", sc, fc_cap, Some(xc)),
        result("shear", tau, v_cap, Some(xs)),
        result("punching", vu, p_cap, None),
    ];
    let gov = checks.iter().fold(&checks[0], |g, c| if c.dc > g.dc { c } else { g });
    let l = grid.x[grid.nx() - 1];
    let fb = grid
        .x
        .iter()
        .map(|xi| grid.depth - (loads.draft_in + loads.trim_in * (xi - l / 2.0) / l))
        .fold(f64::INFINITY, f64::min);
    Verdict {
        dc: gov.dc,
        governing_check: gov.name,
        x_governing_in: gov.x_in.unwrap_or(xt),
        freeboard_in: fb,
        checks,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Rectangular box: flat keel half-width 6, vertical walls to z=10.
    fn box_grid(nx: usize) -> Grid {
        let z: Vec<f64> = (0..=10).map(|k| k as f64).collect();
        let x: Vec<f64> = (0..nx).map(|i| i as f64 * 10.0).collect();
        let y = vec![6.0; nx * z.len()];
        Grid { x, z, y, depth: 10.0 }
    }

    fn mat() -> Material {
        Material {
            mix_data: "unused".into(),
            flex_over_split: 1.0,
            density_extrapolation_pcf: 0.0,
            factor_of_safety: 2.0,
            dynamic_load_factor: 1.2,
            knee_patch_in: 3.0,
        }
    }

    /// Linear split fit chosen so f'c = 6000 psi at 60 pcf:
    /// f_sp = 6.7 sqrt(6000) at 60 pcf, through the origin.
    fn fit() -> StrengthFit {
        StrengthFit {
            points: vec![],
            split_slope_psi_per_pcf: 6.7 * 6000f64.sqrt() / 60.0,
            split_intercept_psi: 0.0,
            rms_residual_psi: 0.0,
            density_min_pcf: 50.0,
            density_max_pcf: 70.0,
            flex_over_split: 1.0,
        }
    }

    fn loads(nx: usize, m: f64, v: f64) -> Loads {
        Loads {
            hull_weight_lb: 0.0,
            draft_in: 2.0,
            trim_in: 0.0,
            shear: vec![v; nx],
            moment: vec![m; nx],
            gm_in: 10.0,
        }
    }

    fn get<'a>(v: &'a Verdict, n: &str) -> &'a CheckResult {
        v.checks.iter().find(|c| c.name == n).unwrap()
    }

    #[test]
    fn punching_hand_calc() {
        let g = box_grid(3);
        let t = 0.5;
        let sec = crate::section::sections(&g, t);
        let v = check(&g, &sec, &loads(3, 0.0, 0.0), 60.0, &mat(), &fit(), 210.0);
        let p = get(&v, "punching");
        // fc = 6000, d = .25, b0 = 13, (c): 2 + 40*.25/13 = 2.769 < 4.
        let b0 = 13.0;
        assert!((p.demand - 210.0 / (b0 * t)).abs() < 1e-9);
        let cap = (2.0 + 10.0 / 13.0) * 6000f64.sqrt() / 2.0;
        assert!((p.capacity - cap).abs() < 1e-9);
    }

    #[test]
    fn shear_box() {
        let g = box_grid(3);
        let t = 0.5;
        let sec = crate::section::sections(&g, t);
        let zb = sec.zbar[1];
        // Keel 12*t at z=0 plus two walls; Q below zbar by hand.
        let q = 12.0 * t * zb + 2.0 * t * zb * zb / 2.0;
        assert!((first_moment_below(g.row(1), &g.z, t, zb) - q).abs() < 1e-9);
        let v = check(&g, &sec, &loads(3, 0.0, 100.0), 60.0, &mat(), &fit(), 0.0);
        let s = get(&v, "shear");
        assert!((s.demand - 100.0 * q / (sec.inertia[1] * 2.0 * t)).abs() < 1e-9);
        assert!((s.capacity - 2.0 * 6000f64.sqrt() / 2.0).abs() < 1e-9);
    }

    #[test]
    fn compression_fibre_follows_sign() {
        let g = box_grid(3);
        let sec = crate::section::sections(&g, 0.5);
        let (zb, i) = (sec.zbar[1], sec.inertia[1]);
        let sag = check(&g, &sec, &loads(3, 1000.0, 0.0), 60.0, &mat(), &fit(), 0.0);
        assert!((get(&sag, "bending_compression").demand - 1000.0 * (10.0 - zb) / i).abs() < 1e-9);
        let hog = check(&g, &sec, &loads(3, -1000.0, 0.0), 60.0, &mat(), &fit(), 0.0);
        assert!((get(&hog, "bending_compression").demand - 1000.0 * zb / i).abs() < 1e-9);
        let names: Vec<_> = sag.checks.iter().map(|c| c.name).collect();
        assert_eq!(names, ["bending_tension", "bending_compression", "shear", "punching"]);
    }

    #[test]
    fn nonpositive_fc_fails() {
        let g = box_grid(3);
        let sec = crate::section::sections(&g, 0.5);
        let mut f = fit();
        f.split_intercept_psi = -1e6;
        let v = check(&g, &sec, &loads(3, 10.0, 10.0), 60.0, &mat(), &f, 100.0);
        assert!(v.dc.is_infinite());
        assert!(get(&v, "punching").dc.is_infinite());
        assert!(get(&v, "shear").dc.is_infinite());
        assert!(get(&v, "bending_tension").dc.is_infinite());
    }
}
