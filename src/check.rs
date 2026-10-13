//! Pass/fail checks on one analysed design.
//!
//! Every check produces a [`CheckResult`] with demand, capacity and D/C in the
//! same units, so the report and the web UI can list them uniformly. The
//! candidate's governing D/C (`Verdict::dc`) is the MAX over all checks.
//!
//! Checks (bending is the golden-tested one
//! and must keep matching `tests/golden.rs`):
//! * `bending_tension`: sigma = |M| c / I at the actual tension fibre (M > 0
//!   sagging: bottom, c = zbar; hogging: top, c = D - zbar), stations with
//!   I > 1e-3 max I. capacity = flexural strength / FS. Matches
//!   tests/golden/reference.py.
//! * `bending_compression`: sigma at the actual compression fibre (M > 0
//!   sagging: c = D - zbar; hogging: c = zbar) vs 0.85 f'c / FS,
//!   f'c from the lab fit (`mix::StrengthFit::fc_psi`).
//! * `shear`: tau = |V| Q / (I b) for the thin shell, b = 2t (two walls), Q =
//!   first moment of the shell below zbar (section.rs segment model), same
//!   station mask as bending, vs 2 lambda sqrt(f'c) / FS (ACI one-way).
//! * `punching`: kneeling paddler on the hull bottom (docs/PUNCHING.md).
//!   Round contact d_load = knee_patch_in + 2 pad_in, beta = 1. Two methods:
//!   `aci_22_6` (ACI 318-19 22.6.4 / Table 22.6.5.2): d = d/t * t, d/t from
//!   `Punching::d_over_t` (casting layers + mold side, or explicit override),
//!   b0 = pi (d_load + d), v_u = P / (b0 d), v_c = min(4, 2 + 4/beta,
//!   2 + 40 d / b0) lambda_s lambda sqrt(f'c). `plain_14_5` (Table 14.5.5.1):
//!   h = t, b0 = pi (d_load + h), v_u = P / (b0 h), v_n = min((4/3)(1 + 2/beta),
//!   8/3) lambda sqrt(f'c). P per knee: knees = 1 -> whole paddler (times the
//!   dynamic factor); knees = 2 -> knee_split share, unless the two critical
//!   circles overlap, then the whole paddler on the merged obround perimeter
//!   b0 = 2 spacing + pi (d_load + d). Capacity = v / FS (no phi on top).
//! * `biaxial`: Kupfer-Gerstle tension-compression envelope on the principal
//!   stresses of (bending fibre stress, transverse turning stress [`hoop_psi`],
//!   peak shear) per station, see [`kupfer_dc`]. Demand is the D/C itself,
//!   capacity 1. Without `[turning]` the transverse stress is 0.
//! * Freeboard: depth - max local draft (not a D/C; gated separately).
//!
//! f'c <= 0 or capacity <= 0 makes that check dc = +inf; optimize treats a
//! +inf verdict as a FAILED candidate (verdict kept), NaN as an error.
//! Capacities above are written for `method = "asd"` (/ FS). With "lrfd"
//! every capacity is x phi instead (0.60, or 0.75 for `aci_22_6` punching)
//! and the caller passes factored loads.
//! Order of `checks`: bending_tension, bending_compression, shear, punching, biaxial.
//! A check that cannot be computed FAILS. Never
//! default, never skip.

use crate::equilibrium::Loads;
use crate::hull::Grid;
use crate::mix::StrengthFit;
use crate::params::{DesignMethod, Material, Punching, PunchingMethod, Turning};
use crate::section::{self, Sections};
use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
pub struct CheckResult {
    /// Stable id: "bending_tension" | "bending_compression" | "shear" | "punching" | "biaxial".
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
        let p = section::perimeter(grid.row(i), &grid.z);
        (p > 1e-9).then(|| sec.area[i] / p)
    })
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
        let ds = section::segment_len(y, z, k);
        let top = z1.min(zb);
        let frac = (top - z0) / (z1 - z0);
        q += 2.0 * t * ds * frac * (zb - 0.5 * (z0 + top));
    }
    q
}

/// ACI 318-19 Table 22.6.5.2 two-way shear stress v_c (before FS), psi: the
/// least of (a) 4, (b) 2 + 4/beta, (c) 2 + alpha_s d / b0, each times
/// lambda_s lambda sqrt(f'c). lambda_s = sqrt(2 / (1 + d/10)) <= 1 (22.5.5.1.3).
pub fn punching_vc(fc: f64, lambda: f64, d: f64, b0: f64, beta: f64, alpha_s: f64) -> f64 {
    let lambda_s = (2.0 / (1.0 + d / 10.0)).sqrt().min(1.0);
    let sq = lambda_s * lambda * fc.sqrt();
    (4.0 * sq).min((2.0 + 4.0 / beta) * sq).min((2.0 + alpha_s * d / b0) * sq)
}

/// ACI 318-19 Table 14.5.5.1 plain-concrete two-way shear stress v_n = V_n /
/// (b0 h) (before FS), psi: least of (4/3)(1 + 2/beta) and 2 (4/3), times
/// lambda sqrt(f'c).
pub fn punching_plain_vn(fc: f64, lambda: f64, beta: f64) -> f64 {
    let c = (4.0 / 3.0 * (1.0 + 2.0 / beta)).min(2.0 * 4.0 / 3.0);
    c * lambda * fc.sqrt()
}

/// One knee punching calculation, every intermediate kept for the docs/tests.
#[derive(Debug, Clone, Serialize)]
pub struct PunchingCalc {
    /// Shear depth: d (aci_22_6) or h = t (plain_14_5), in.
    pub depth_in: f64,
    /// Contact diameter knee + 2 pad, in.
    pub d_load_in: f64,
    /// Critical perimeter, in.
    pub b0_in: f64,
    /// Load on that perimeter, lb.
    pub load_lb: f64,
    /// True when the two knees' critical circles overlap (merged perimeter).
    pub merged: bool,
    /// Demand v_u = load / (b0 depth), psi.
    pub v_u: f64,
    /// Nominal stress capacity before FS, psi.
    pub v_nominal: f64,
}

/// Punching under one kneeling paddler of weight `paddler_lb` (dynamic factor
/// already applied) on a wall of thickness `t`.
pub fn punching_calc(
    paddler_lb: f64,
    t: f64,
    fc: f64,
    lambda: f64,
    knee_in: f64,
    pu: &Punching,
) -> PunchingCalc {
    // A round knee (with pad) is a circular loaded area: beta = 1.
    let (beta, alpha_s) = (1.0, 40.0);
    let d_load = knee_in + 2.0 * pu.pad_in;
    let depth = match pu.method {
        // validate() guarantees Ok; if not, NaN depth makes the check fail (dc = inf).
        PunchingMethod::Aci22_6 => pu.d_over_t().unwrap_or(f64::NAN) * t,
        PunchingMethod::Plain14_5 => t,
    };
    // Critical circle: depth/2 outside the contact (22.6.4.1 / 14.5.5.2).
    let single = std::f64::consts::PI * (d_load + depth);
    let merged = pu.knees == 2 && pu.knee_spacing_in < d_load + depth;
    let (b0, load) = match (pu.knees, merged) {
        (1, _) => (single, paddler_lb),
        (_, false) => (single, pu.knee_split * paddler_lb),
        (_, true) => (2.0 * pu.knee_spacing_in + single, paddler_lb),
    };
    let v_nominal = if fc > 0.0 {
        match pu.method {
            PunchingMethod::Aci22_6 => punching_vc(fc, lambda, depth, b0, beta, alpha_s),
            PunchingMethod::Plain14_5 => punching_plain_vn(fc, lambda, beta),
        }
    } else {
        f64::NAN
    };
    PunchingCalc {
        depth_in: depth,
        d_load_in: d_load,
        b0_in: b0,
        load_lb: load,
        merged,
        v_u: load / (b0 * depth),
        v_nominal,
    }
}

/// (tension, compression) fibre distances from the axis for moment `m`.
/// Sagging (M >= 0): bottom in tension, top in compression; hogging: reverse.
pub fn fibres(m: f64, c_top: f64, c_bot: f64) -> (f64, f64) {
    if m >= 0.0 { (c_bot, c_top) } else { (c_top, c_bot) }
}

/// Kupfer-Gerstle (1973) biaxial D/C at one station. Plane stress
/// (sigma_x, sigma_y, tau) -> principal sigma_1 >= sigma_2; tension-compression
/// branch sigma_1 <= f_t (1 - 0.8 |sigma_2| / f'c), tension-tension
/// (sigma_2 >= 0) sigma_1 <= f_t. Conservatively combines the extreme-fibre
/// bending stress with the section's peak shear stress (which really peaks at
/// the axis) and the transverse stress `sy` at both signs (it bends the wall,
/// so one face is in tension and the other in compression). Both longitudinal
/// fibres are tried: tension (sigma_x = +st) and compression (sigma_x = -sc).
/// `ft`, `fc` are already divided by FS. Non-finite or exhausted capacity, or
/// non-finite `sy` -> +inf.
pub fn kupfer_dc(st: f64, sc: f64, sy: f64, tau: f64, ft: f64, fc: f64) -> f64 {
    if !sy.is_finite() {
        return f64::INFINITY;
    }
    let one = |sx: f64, sy: f64| {
        let r = ((0.5 * (sx - sy)).powi(2) + tau * tau).sqrt();
        let c = 0.5 * (sx + sy);
        let (s1, s2) = (c + r, c - r);
        let cap = ft * (1.0 - 0.8 * s2.min(0.0).abs() / fc);
        if cap.is_nan() || cap <= 0.0 || !cap.is_finite() { f64::INFINITY } else { s1.max(0.0) / cap }
    };
    one(st, sy).max(one(-sc, sy)).max(one(st, -sy)).max(one(-sc, -sy))
}

/// Transverse bending stress in the wall from a steady turn, psi per station
/// (the legacy ASCE report's k = V^2 / (g R)). Assumptions:
/// * Everything floated (hull + crew, `loads.weight_lb`, factored like the
///   rest of `loads`) accelerates sideways at `lateral_g` g. The water reacts
///   it as a uniform lateral pressure on the underwater profile of one side:
///   p = k W / A_lat, A_lat = integral h(x) dx, h = local waterline - keel
///   (vertical submerged height). How load and reaction differ along the hull
///   (the yaw moment) is not modelled.
/// * Each side is a cantilever fixed at the keel, free at the waterline. Any
///   horizontal pressure p on a projected height h has moment p h^2 / 2 per
///   unit length about the keel whatever the section shape, so the root
///   stress is sigma_y = 6 m / t^2 = 3 p h^2 / t^2.
/// * The side's own inertia (it relieves the side) and the symmetric
///   hydrostatic pressure are left out. The root stress is combined with the
///   extreme longitudinal fibre in [`kupfer_dc`] (conservative, like the shear).
///
/// `turning = None` -> all 0. Uncomputable (no underwater profile, t <= 0) ->
/// +inf at every station, which fails the biaxial check.
pub fn hoop_psi(grid: &Grid, loads: &Loads, t: f64, turning: Option<&Turning>) -> Vec<f64> {
    let n = grid.nx();
    let Some(tu) = turning else { return vec![0.0; n] };
    let l = grid.x[n - 1];
    let h: Vec<f64> = (0..n)
        .map(|i| {
            let y = grid.row(i);
            let Some(j) = y.iter().position(|&v| v > 0.0) else { return 0.0 };
            let keel = grid.z[j.saturating_sub(1)];
            let wl = (loads.draft_in + loads.trim_in * (grid.x[i] - l / 2.0) / l).clamp(0.0, grid.depth);
            (wl - keel).max(0.0)
        })
        .collect();
    let a_lat: f64 = (1..n).map(|i| 0.5 * (h[i] + h[i - 1]) * (grid.x[i] - grid.x[i - 1])).sum();
    let p = tu.lateral_g() * loads.weight_lb / a_lat;
    if !(a_lat > 0.0 && t > 0.0 && p.is_finite()) {
        return vec![f64::INFINITY; n];
    }
    h.iter().map(|h| 3.0 * p * h * h / (t * t)).collect()
}

/// Minimum freeboard over the stations, in: depth - local draft.
pub fn freeboard(grid: &Grid, loads: &Loads) -> f64 {
    let l = grid.x[grid.nx() - 1];
    grid.x
        .iter()
        .map(|xi| grid.depth - (loads.draft_in + loads.trim_in * (xi - l / 2.0) / l))
        .fold(f64::INFINITY, f64::min)
}

fn result(name: &'static str, demand: f64, capacity: f64, x_in: Option<f64>) -> CheckResult {
    let dc = if capacity.is_nan() || capacity <= 0.0 || !capacity.is_finite() || !demand.is_finite() {
        f64::INFINITY
    } else {
        demand / capacity
    };
    CheckResult { name, demand, capacity, dc, x_in }
}

#[allow(clippy::too_many_arguments)] // each input is a distinct physical quantity
pub fn check(
    grid: &Grid,
    sec: &Sections,
    loads: &Loads,
    rho: f64,
    mat: &Material,
    fit: &StrengthFit,
    max_paddler_lb: f64,
    pu: &Punching,
    turning: Option<&Turning>,
) -> Verdict {
    let fs = mat.factor_of_safety;
    // Capacity multiplier: 1/FS (asd) or phi (lrfd, ACI 318-19 21.2.1:
    // 0.60 plain concrete, 0.75 two-way shear of a reinforced slab).
    let (k, k_punch) = match mat.method {
        DesignMethod::Asd => (1.0 / fs, 1.0 / fs),
        DesignMethod::Lrfd => (0.60, if pu.method == PunchingMethod::Aci22_6 { 0.75 } else { 0.60 }),
    };
    let fc = fit.fc_psi(rho);
    // f'c comes from the team's own lightweight-mix breaks via f_ct = 6.7 sqrt(f'c),
    // so the lightweight reduction is already in the data: lambda = 1 here.
    let lambda = 1.0;
    let t = thickness(grid, sec).unwrap_or(f64::NAN);
    let imax = sec.inertia.iter().cloned().fold(0.0, f64::max);
    let (mut st, mut xt) = (f64::NEG_INFINITY, f64::NAN);
    let (mut sc, mut xc) = (f64::NEG_INFINITY, f64::NAN);
    let (mut tau, mut xs) = (f64::NEG_INFINITY, f64::NAN);
    let ft_cap = fit.flexural_psi(rho) * k;
    let fc_kupfer = if fc > 0.0 { fc * k } else { f64::NAN };
    let (mut kz, mut xk) = (f64::NEG_INFINITY, f64::NAN);
    let sy = hoop_psi(grid, loads, t, turning);
    for (i, &sy) in sy.iter().enumerate() {
        let ii = sec.inertia[i];
        if ii <= 1e-3 * imax {
            continue;
        }
        let m = loads.moment[i];
        let (c_top, c_bot) = (grid.depth - sec.zbar[i], sec.zbar[i]);
        let (ct, cc) = fibres(m, c_top, c_bot);
        let s = m.abs() * ct / ii;
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
        let k = kupfer_dc(m.abs() * ct / ii, m.abs() * cc / ii, sy, s, ft_cap, fc_kupfer);
        if k.is_nan() || k > kz {
            (kz, xk) = (k, grid.x[i]);
        }
    }
    let (fc_cap, v_cap) =
        if fc > 0.0 { (0.85 * fc * k, 2.0 * lambda * fc.sqrt() * k) } else { (f64::NAN, f64::NAN) };
    let pc = punching_calc(max_paddler_lb, t, fc, lambda, mat.knee_patch_in, pu);
    let (vu, p_cap) = (pc.v_u, pc.v_nominal * k_punch);
    let checks = vec![
        result("bending_tension", st, ft_cap, Some(xt)),
        result("bending_compression", sc, fc_cap, Some(xc)),
        result("shear", tau, v_cap, Some(xs)),
        result("punching", vu, p_cap, None),
        // Kupfer is already a ratio: demand = D/C, capacity = 1.
        result("biaxial", kz, 1.0, Some(xk)),
    ];
    let gov = checks.iter().fold(&checks[0], |g, c| if c.dc > g.dc { c } else { g });
    let fb = freeboard(grid, loads);
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
            strength_model: crate::params::StrengthModel::Fit,
            mix_data: "unused".into(),
            flex_over_split: 1.0,
            density_extrapolation_pcf: 0.0,
            method: DesignMethod::Asd,
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
            flexural_fit: None,
            compressive_fit: None,
        }
    }

    fn loads(nx: usize, m: f64, v: f64) -> Loads {
        Loads {
            hull_weight_lb: 0.0,
            weight_lb: 0.0,
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

    fn pu1() -> Punching {
        Punching {
            method: PunchingMethod::Aci22_6,
            knees: 1,
            knee_split: 0.5,
            knee_spacing_in: 8.0,
            pad_in: 0.0,
            d_over_t: Some(0.5),
            layers: None,
            mesh_after_layer: None,
            mold: None,
        }
    }

    /// f'c = 6000 psi (fit()), t = 0.5, knee 3 in, one knee, ACI 22.6.5.2.
    #[test]
    fn punching_aci_one_knee_hand_calc() {
        let g = box_grid(3);
        let t = 0.5;
        let sec = crate::section::sections(&g, t);
        let v = check(&g, &sec, &loads(3, 0.0, 0.0), 60.0, &mat(), &fit(), 210.0, &pu1(), None);
        let p = get(&v, "punching");
        let d = 0.25;
        let b0 = std::f64::consts::PI * (3.0 + d);
        // demand on b0 * d (22.6.1.2), not b0 * t
        assert!((p.demand - 210.0 / (b0 * d)).abs() < 1e-9);
        // (c) governs for a thin wall: 2 + 40 d / b0; lambda_s = 1 for d << 10 in
        let cap = (2.0 + 40.0 * d / b0) * 6000f64.sqrt() / 2.0;
        assert!((p.capacity - cap).abs() < 1e-9, "{} vs {cap}", p.capacity);
    }

    #[test]
    fn punching_two_knees_split_and_merge() {
        let mut pu = pu1();
        pu.knees = 2;
        pu.knee_split = 0.6;
        let one = punching_calc(210.0, 0.5, 6000.0, 1.0, 3.0, &pu1());
        let two = punching_calc(210.0, 0.5, 6000.0, 1.0, 3.0, &pu);
        assert!(!two.merged);
        assert!((two.load_lb - 126.0).abs() < 1e-9);
        assert!((two.v_u / one.v_u - 0.6).abs() < 1e-9);
        // knees closer than d_load + d: critical circles overlap -> full load on obround
        pu.knee_spacing_in = 3.0;
        let m = punching_calc(210.0, 0.5, 6000.0, 1.0, 3.0, &pu);
        assert!(m.merged && (m.load_lb - 210.0).abs() < 1e-9);
        assert!((m.b0_in - (6.0 + std::f64::consts::PI * 3.25)).abs() < 1e-9);
    }

    #[test]
    fn punching_plain_14_5_hand_calc_and_pad() {
        let mut pu = pu1();
        pu.method = PunchingMethod::Plain14_5;
        let c = punching_calc(210.0, 0.5, 6000.0, 1.0, 3.0, &pu);
        let b0 = std::f64::consts::PI * 3.5; // h/2 outside a 3 in circle, h = t
        assert!((c.v_u - 210.0 / (b0 * 0.5)).abs() < 1e-9);
        // beta = 1: (4/3)(1 + 2) = 4 vs 2 (4/3) = 2.667 -> 2.667
        assert!((c.v_nominal - 8.0 / 3.0 * 6000f64.sqrt()).abs() < 1e-9);
        pu.pad_in = 0.5;
        let padded = punching_calc(210.0, 0.5, 6000.0, 1.0, 3.0, &pu);
        assert!(padded.v_u < c.v_u && padded.d_load_in == 4.0);
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
        let v = check(&g, &sec, &loads(3, 0.0, 100.0), 60.0, &mat(), &fit(), 0.0, &pu1(), None);
        let s = get(&v, "shear");
        assert!((s.demand - 100.0 * q / (sec.inertia[1] * 2.0 * t)).abs() < 1e-9);
        assert!((s.capacity - 2.0 * 6000f64.sqrt() / 2.0).abs() < 1e-9);
    }

    #[test]
    fn compression_fibre_follows_sign() {
        let g = box_grid(3);
        let sec = crate::section::sections(&g, 0.5);
        let (zb, i) = (sec.zbar[1], sec.inertia[1]);
        let sag = check(&g, &sec, &loads(3, 1000.0, 0.0), 60.0, &mat(), &fit(), 0.0, &pu1(), None);
        assert!((get(&sag, "bending_compression").demand - 1000.0 * (10.0 - zb) / i).abs() < 1e-9);
        let hog = check(&g, &sec, &loads(3, -1000.0, 0.0), 60.0, &mat(), &fit(), 0.0, &pu1(), None);
        assert!((get(&hog, "bending_compression").demand - 1000.0 * zb / i).abs() < 1e-9);
        // B5: tension at the opposite fibre (bottom when sagging, top when hogging).
        assert!((get(&sag, "bending_tension").demand - 1000.0 * zb / i).abs() < 1e-9);
        assert!((get(&hog, "bending_tension").demand - 1000.0 * (10.0 - zb) / i).abs() < 1e-9);
        let names: Vec<_> = sag.checks.iter().map(|c| c.name).collect();
        assert_eq!(names, ["bending_tension", "bending_compression", "shear", "punching", "biaxial"]);
    }

    #[test]
    fn kupfer_hand_calc() {
        // Pure bending: sigma_1 = st, sigma_2 = 0 -> D/C = st / ft.
        assert!((kupfer_dc(100.0, 0.0, 0.0, 0.0, 200.0, 1000.0) - 0.5).abs() < 1e-12);
        // Pure shear tau: sigma_1 = tau, sigma_2 = -tau -> tau / (ft (1 - 0.8 tau / fc)).
        let want = 50.0 / (200.0 * (1.0 - 0.8 * 50.0 / 1000.0));
        assert!((kupfer_dc(0.0, 0.0, 0.0, 50.0, 200.0, 1000.0) - want).abs() < 1e-12);
        // Shear raises the D/C above uniaxial bending alone.
        assert!(kupfer_dc(100.0, 100.0, 0.0, 30.0, 200.0, 1000.0) > 0.5);
        // Compression past 1.25 fc exhausts the tension capacity.
        assert!(kupfer_dc(0.0, 1300.0, 0.0, 1.0, 200.0, 1000.0).is_infinite());
        assert!(kupfer_dc(1.0, 1.0, 0.0, 1.0, 200.0, f64::NAN).is_infinite());
    }

    #[test]
    fn kupfer_transverse_stress_hand_calc() {
        // sigma_y alone acts like sigma_x alone.
        assert!((kupfer_dc(0.0, 0.0, 100.0, 0.0, 200.0, 1000.0) - 0.5).abs() < 1e-12);
        // sx = 100 with sy = -50 (the other face): s1 = 100, s2 = -50 -> 100 / (200 x 0.96).
        // sy = +50 would be tension-tension (D/C 0.5), the worse face governs.
        assert!((kupfer_dc(100.0, 0.0, 50.0, 0.0, 200.0, 1000.0) - 100.0 / 192.0).abs() < 1e-12);
        assert!(kupfer_dc(100.0, 0.0, f64::NAN, 0.0, 200.0, 1000.0).is_infinite());
        assert!(kupfer_dc(100.0, 0.0, f64::INFINITY, 0.0, 200.0, 1000.0).is_infinite());
    }

    fn turn(speed_mps: f64, radius_in: f64) -> Turning {
        Turning { speed_mps, radius_in }
    }

    #[test]
    fn hoop_stress_hand_calc() {
        // Box L = 20, keel at z = 0, draft 2: h = 2 everywhere, A_lat = 40 in^2.
        let mut g = box_grid(3);
        let mut l = loads(3, 0.0, 0.0);
        l.weight_lb = 400.0;
        let tu = turn(2.5, 10.0 * crate::units::M_TO_IN);
        let k = 6.25 / (9.80665 * 10.0);
        // p = k W / A_lat = 10 k psi; sigma_y = 3 p h^2 / t^2 = 120 k / 0.25.
        let s = hoop_psi(&g, &l, 0.5, Some(&tu));
        assert!(s.iter().all(|&v| (v - 480.0 * k).abs() < 1e-9), "{s:?}");
        assert_eq!(hoop_psi(&g, &l, 0.5, None), vec![0.0; 3]);
        // Station 0 rockered: no wall below z = 4, keel at z = 3 is above the
        // waterline -> h = 0 there, A_lat = 10 + 20 = 30.
        for k in 0..4 {
            g.y[k] = 0.0;
        }
        let s = hoop_psi(&g, &l, 0.5, Some(&tu));
        assert_eq!(s[0], 0.0);
        let p = k * 400.0 / 30.0;
        assert!((s[2] - 3.0 * p * 4.0 / 0.25).abs() < 1e-9);
        // Nothing under water: the stress cannot be computed -> fails, not 0.
        l.draft_in = 0.0;
        assert!(hoop_psi(&g, &l, 0.5, Some(&tu)).iter().all(|v| v.is_infinite()));
    }

    #[test]
    fn turning_raises_biaxial_monotonically_and_vanishes_on_a_straight() {
        let g = box_grid(3);
        let sec = crate::section::sections(&g, 0.5);
        let mut l = loads(3, 1000.0, 50.0);
        l.weight_lb = 400.0;
        let bi = |tu: Option<&Turning>| {
            get(&check(&g, &sec, &l, 60.0, &mat(), &fit(), 0.0, &pu1(), tu), "biaxial").dc
        };
        let straight = bi(None);
        assert_eq!(bi(Some(&turn(2.5, f64::INFINITY))), straight);
        assert!((bi(Some(&turn(2.5, 1e12))) - straight).abs() < 1e-9);
        let mut prev = straight;
        for v in [0.5, 1.0, 2.0, 3.0, 4.0] {
            let d = bi(Some(&turn(v, 400.0)));
            assert!(d > prev, "v = {v}: {d} <= {prev}");
            prev = d;
        }
        // Only V^2 / R matters.
        assert_eq!(bi(Some(&turn(2.0, 400.0))), bi(Some(&turn(4.0, 1600.0))));
    }

    #[test]
    fn female_mold_mesh_raises_punching_dc() {
        // Same 3-pass wall, mesh after pass 2: male d = 2t/3, female d = t/3.
        let g = box_grid(3);
        let sec = crate::section::sections(&g, 0.375);
        let mut pu = pu1();
        (pu.d_over_t, pu.layers, pu.mesh_after_layer) =
            (None, Some(3), Some(crate::params::MeshAfter::One(2)));
        let dc = |m| {
            let mut q = pu.clone();
            q.mold = Some(m);
            get(&check(&g, &sec, &loads(3, 0.0, 0.0), 60.0, &mat(), &fit(), 210.0, &q, None), "punching").dc
        };
        let (male, female) = (dc(crate::params::Mold::Male), dc(crate::params::Mold::Female));
        assert!(female > 1.5 * male, "male {male} female {female}");
        let c = punching_calc(210.0, 0.375, 6000.0, 1.0, 3.0, &{
            let mut q = pu.clone();
            q.mold = Some(crate::params::Mold::Female);
            q
        });
        assert!((c.depth_in - 0.125).abs() < 1e-12);
    }

    #[test]
    fn invalid_mesh_depth_fails_the_check() {
        let g = box_grid(3);
        let sec = crate::section::sections(&g, 0.5);
        let mut pu = pu1();
        pu.layers = Some(3); // conflicts with d_over_t: never validated, must not pass
        let v = check(&g, &sec, &loads(3, 0.0, 0.0), 60.0, &mat(), &fit(), 210.0, &pu, None);
        assert!(get(&v, "punching").dc.is_infinite());
    }

    #[test]
    fn nonpositive_fc_fails() {
        let g = box_grid(3);
        let sec = crate::section::sections(&g, 0.5);
        let mut f = fit();
        f.split_intercept_psi = -1e6;
        let v = check(&g, &sec, &loads(3, 10.0, 10.0), 60.0, &mat(), &f, 100.0, &pu1(), None);
        assert!(v.dc.is_infinite());
        assert!(get(&v, "punching").dc.is_infinite());
        assert!(get(&v, "shear").dc.is_infinite());
        assert!(get(&v, "bending_tension").dc.is_infinite());
        assert!(get(&v, "biaxial").dc.is_infinite());
    }
}
