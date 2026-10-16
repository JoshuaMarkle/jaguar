//! The empty hull resting on two stands (docs/SPEC.md, "Stands"). Run once on the
//! winner after the search (`optimize::verify_stands`), never inside it.
//!
//! * Load: the hull's own weight per station, w(x) = A(x) rho / 1728 x dead factor
//!   (asd 1; lrfd `lrfd_dead_only`, ASCE 7 1.4D: there is no live load). No crew, no water.
//! * Supports: two pads of width `pad_in`, centred `x_s` in from each end (symmetric about
//!   midship). The reactions come from force and moment balance of the real w(x), so an
//!   asymmetric hull loads its stands unequally. Each reaction is spread uniformly over its pad
//!   and added in closed form (`equilibrium::patch_vm`), like a knee.
//! * V(x) = -int w + sum R_j F_V, M(x) = int V_w + sum R_j F_M, + = sagging (gravity frame).
//!   Upright, a sagging moment puts the keel in tension; inverted (hull on its gunwales) the
//!   gunwale, so the checks see -M.
//! * Checks: bending tension and compression, shear and biaxial from `check::check` (same
//!   formulas and capacity factors as every load case, no turning), plus a local check at the
//!   heavier pad: upright, ACI 318-19 22.6 punching of a pad x pad square (b0 = 4 (c + d),
//!   beta = 1, alpha_s = 40; `plain_14_5` uses h = t and Table 14.5.5.1); inverted, bearing on
//!   the two gunwale edges, A1 = 2 t pad, 0.85 f'c A1 (22.8.3.2), phi 0.65 (21.2.1).
//! * `x_s` is swept from pad/2 to L/2 - pad/2 in `step_in` steps. The position kept has the
//!   lowest beam D/C (max of bending, shear, biaxial: what the position controls); the local
//!   pad check depends on position only through the reaction split (none on a symmetric hull),
//!   so where it governs every position ties on the governing D/C and the first would be the
//!   ends, the worst place for bending. Ties: the first, so the result is deterministic. The
//!   governing D/C (local check included) is reported at that position.

use crate::check::{
    self, CheckResult, Verdict, capacity_factors, punching_calc, punching_plain_vn, punching_vc,
};
use crate::equilibrium::{Loads, patch_vm};
use crate::hull::Grid;
use crate::mix::StrengthFit;
use crate::params::{DesignMethod, Material, Punching, PunchingMethod, StandOrientation, Stands};
use crate::section::Sections;
use crate::units::IN3_PER_FT3;
use anyhow::{Result, bail};
use serde::Serialize;

/// Shear, moment and reactions of the hull on its two stands.
#[derive(Debug, Clone)]
pub struct Diagram {
    pub shear: Vec<f64>,
    pub moment: Vec<f64>,
    /// Bow and stern reaction, lb.
    pub reactions: [f64; 2],
}

fn trapz(f: &[f64], x: &[f64]) -> f64 {
    x.windows(2).zip(f.windows(2)).map(|(x, f)| 0.5 * (f[0] + f[1]) * (x[1] - x[0])).sum()
}

/// V and M of weight `w` (lb/in per station) on pads of width `pad` centred `xs` in from
/// each end of `x`.
pub fn stand_diagram(x: &[f64], w: &[f64], xs: f64, pad: f64) -> Result<Diagram> {
    let n = x.len();
    let (x0, xl) = (x[0], x[n - 1]);
    let (a, b) = (x0 + xs, xl - xs);
    if !(xs >= 0.5 * pad && b - a >= pad) {
        bail!("stands at {xs:.2} in from the ends do not fit a {pad} in pad");
    }
    let wt = trapz(w, x);
    if !(wt > 0.0 && wt.is_finite()) {
        bail!("hull self weight is {wt}");
    }
    let xg = trapz(&w.iter().zip(x).map(|(w, x)| w * x).collect::<Vec<_>>(), x) / wt;
    let reactions = [wt * (b - xg) / (b - a), wt * (xg - a) / (b - a)];
    let (mut vw, mut mw) = (vec![0.0; n], vec![0.0; n]);
    for i in 1..n {
        let dx = x[i] - x[i - 1];
        vw[i] = vw[i - 1] - 0.5 * (w[i] + w[i - 1]) * dx;
        mw[i] = mw[i - 1] + 0.5 * (vw[i] + vw[i - 1]) * dx;
    }
    let (mut shear, mut moment) = (vw, mw);
    for (r, xc) in reactions.iter().zip([a, b]) {
        for i in 0..n {
            let (fv, fm) = patch_vm(x[i], xc, pad);
            shear[i] += r * fv;
            moment[i] += r * fm;
        }
    }
    if shear[n - 1].abs() > 1e-6 * wt || moment[n - 1].abs() > 1e-3 * wt * (xl - x0) {
        bail!("stands equilibrium broken: V_end={:.3} M_end={:.2}", shear[n - 1], moment[n - 1]);
    }
    Ok(Diagram { shear, moment, reactions })
}

/// Local check at the heavier stand pad.
#[derive(Debug, Clone, Serialize)]
pub struct LocalCalc {
    /// "stand_punching" (upright) or "stand_bearing" (inverted).
    pub name: &'static str,
    /// Heavier factored reaction, lb.
    pub load_lb: f64,
    pub pad_in: f64,
    /// Punching: effective depth d (aci_22_6) or h = t (plain_14_5); bearing: t.
    pub depth_in: f64,
    /// Punching: critical perimeter 4 (pad + depth); bearing: none.
    pub b0_in: Option<f64>,
    /// Loaded area: b0 x depth (punching) or 2 t pad (bearing), in^2.
    pub area_in2: f64,
    pub demand_psi: f64,
    /// Nominal strength before the capacity factor, psi.
    pub nominal_psi: f64,
    pub factor: f64,
    pub capacity_psi: f64,
    pub dc: f64,
}

/// One stand position on the winner, every number behind its checks.
#[derive(Debug, Clone, Serialize)]
pub struct StandsCalc {
    pub orientation: StandOrientation,
    pub pad_in: f64,
    pub step_in: f64,
    /// Load factor on self weight (asd 1, lrfd `lrfd_dead_only`).
    pub dead_factor: f64,
    /// Capacity factor of bending, compression, shear, biaxial (1/FS or phi).
    pub capacity_factor: f64,
    /// Self weight, lb (unfactored) and x dead factor.
    pub hull_weight_lb: f64,
    pub factored_weight_lb: f64,
    pub length_in: f64,
    /// Pad centre from each end, in, and as a fraction of the length.
    pub x_stand_in: f64,
    pub x_over_l: f64,
    /// Centre-to-centre stand spacing, in.
    pub spacing_in: f64,
    /// Bow and stern reactions, factored, lb.
    pub reactions_lb: [f64; 2],
    /// Largest sagging (M > 0) and hogging (M < 0) moment and where, lb*in, in from bow.
    /// None when the moment never takes that sign.
    pub sag_max_lbin: Option<f64>,
    pub x_sag_in: Option<f64>,
    pub hog_max_lbin: Option<f64>,
    pub x_hog_in: Option<f64>,
    pub x_in: Vec<f64>,
    pub shear_lb: Vec<f64>,
    /// + = sagging in the gravity frame (keel in tension upright, gunwale inverted).
    pub moment_lbin: Vec<f64>,
    /// bending_tension, bending_compression, shear, stand_punching | stand_bearing, biaxial.
    pub verdict: Verdict,
    pub local: LocalCalc,
    /// Max of the beam checks (bending, shear, biaxial) at this position: what the sweep minimizes.
    pub beam_dc: f64,
    /// The sweep: every stand position tried, its governing D/C and its beam D/C.
    pub sweep_x_in: Vec<f64>,
    pub sweep_dc: Vec<f64>,
    pub sweep_beam_dc: Vec<f64>,
}

/// Stands result on the winner: a calculation, or why there is none.
#[derive(Debug, Clone, Serialize, Default)]
pub struct StandsCase {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    #[serde(flatten)]
    pub calc: Option<StandsCalc>,
}

fn local_check(
    reaction: f64,
    t: f64,
    fc: f64,
    mat: &Material,
    pu: &Punching,
    sp: &Stands,
    k_punch: f64,
) -> Result<LocalCalc> {
    let c = sp.pad_in;
    let lc = |name, depth, b0: Option<f64>, area: f64, nominal: f64, factor: f64| {
        let demand = reaction / area;
        let capacity = nominal * factor;
        let dc = if capacity > 0.0 && capacity.is_finite() && demand.is_finite() {
            demand / capacity
        } else {
            f64::INFINITY
        };
        LocalCalc {
            name,
            load_lb: reaction,
            pad_in: c,
            depth_in: depth,
            b0_in: b0,
            area_in2: area,
            demand_psi: demand,
            nominal_psi: nominal,
            factor,
            capacity_psi: capacity,
            dc,
        }
    };
    let fc_ok = |v: f64| if fc > 0.0 { v } else { f64::NAN };
    Ok(match sp.orientation {
        StandOrientation::Upright => {
            let (depth, nominal) = match pu.method {
                PunchingMethod::Aci22_6 => {
                    let d = pu.d_over_t()? * t;
                    (d, fc_ok(punching_vc(fc.max(0.0), 1.0, d, 4.0 * (c + d), 1.0, 40.0)))
                }
                PunchingMethod::Plain14_5 => (t, fc_ok(punching_plain_vn(fc.max(0.0), 1.0, 1.0))),
            };
            let b0 = 4.0 * (c + depth);
            lc("stand_punching", depth, Some(b0), b0 * depth, nominal, k_punch)
        }
        StandOrientation::Inverted => {
            // ACI 318-19 21.2.1: bearing phi = 0.65.
            let factor = match mat.method {
                DesignMethod::Asd => 1.0 / mat.factor_of_safety,
                DesignMethod::Lrfd => 0.65,
            };
            lc("stand_bearing", t, None, 2.0 * t * c, fc_ok(0.85 * fc), factor)
        }
    })
}

/// The stands case at one position `xs` (pad centre from each end).
#[allow(clippy::too_many_arguments)]
pub fn evaluate(
    grid: &Grid,
    sec: &Sections,
    rho: f64,
    mat: &Material,
    fit: &StrengthFit,
    pu: &Punching,
    sp: &Stands,
    xs: f64,
) -> Result<(Diagram, Verdict, LocalCalc)> {
    let dead = dead_factor(mat);
    let w: Vec<f64> = sec.area.iter().map(|a| a * rho / IN3_PER_FT3 * dead).collect();
    let d = stand_diagram(&grid.x, &w, xs, sp.pad_in)?;
    let sign = if sp.orientation == StandOrientation::Inverted { -1.0 } else { 1.0 };
    let wt = trapz(&w, &grid.x);
    // Not a floating case: no waterline, no GM (NaN, never read: no turning, no gates).
    let loads = Loads {
        hull_weight_lb: wt,
        weight_lb: wt,
        draft_in: f64::NAN,
        trim_in: 0.0,
        shear: d.shear.clone(),
        moment: d.moment.iter().map(|m| sign * m).collect(),
        gm_in: f64::NAN,
        kb_in: f64::NAN,
        bm_in: f64::NAN,
        kg_in: f64::NAN,
        buoyancy: Vec::new(),
        self_weight: w.clone(),
    };
    let mut v = check::check(grid, sec, &loads, rho, mat, fit, 0.0, pu, None);
    let t = check::thickness(grid, sec).unwrap_or(f64::NAN);
    let (_, k_punch) = capacity_factors(mat, pu);
    let local = local_check(d.reactions[0].max(d.reactions[1]), t, fit.fc_psi(rho), mat, pu, sp, k_punch)?;
    let i = v.checks.iter().position(|c| c.name == "punching").expect("check() always runs punching");
    v.checks[i] = CheckResult {
        name: local.name,
        demand: local.demand_psi,
        capacity: local.capacity_psi,
        dc: local.dc,
        x_in: None,
    };
    let gov = v.checks.iter().fold(&v.checks[0], |g, c| if c.dc > g.dc { c } else { g });
    (v.dc, v.governing_check) = (gov.dc, gov.name);
    v.x_governing_in = gov.x_in.unwrap_or(v.x_governing_in);
    v.freeboard_in = f64::NAN;
    Ok((d, v, local))
}

/// asd: 1; lrfd: the dead-only combination factor (ASCE 7 1.4D).
pub fn dead_factor(mat: &Material) -> f64 {
    match mat.method {
        DesignMethod::Asd => 1.0,
        DesignMethod::Lrfd => mat.lrfd_dead_only,
    }
}

/// Max D/C of the beam checks (everything but the local pad check).
fn beam_dc(v: &Verdict) -> f64 {
    v.checks.iter().filter(|c| !c.name.starts_with("stand_")).map(|c| c.dc).fold(f64::NEG_INFINITY, f64::max)
}

/// Sweep the stand position and keep the one with the lowest beam D/C (module docs).
pub fn optimize_position(
    grid: &Grid,
    sec: &Sections,
    rho: f64,
    mat: &Material,
    fit: &StrengthFit,
    pu: &Punching,
    sp: &Stands,
) -> Result<StandsCalc> {
    let n = grid.nx();
    let l = grid.x[n - 1] - grid.x[0];
    let (lo, hi) = (0.5 * sp.pad_in, 0.5 * l - 0.5 * sp.pad_in);
    if hi < lo {
        bail!("a {} in pad does not fit twice on a {l:.1} in hull", sp.pad_in);
    }
    let steps = ((hi - lo) / sp.step_in).floor() as usize;
    let xs: Vec<f64> = (0..=steps).map(|k| lo + k as f64 * sp.step_in).collect();
    let (mut sweep, mut sweep_beam) = (Vec::with_capacity(xs.len()), Vec::with_capacity(xs.len()));
    let mut best: Option<(f64, f64, Diagram, Verdict, LocalCalc)> = None;
    for &x in &xs {
        let (d, v, loc) = evaluate(grid, sec, rho, mat, fit, pu, sp, x)?;
        let b = beam_dc(&v);
        if v.dc.is_nan() || b.is_nan() {
            bail!("stands at {x:.1} in: D/C is not a number");
        }
        sweep.push(v.dc);
        sweep_beam.push(b);
        if best.as_ref().is_none_or(|k| b < k.1) {
            best = Some((x, b, d, v, loc));
        }
    }
    let (x, beam, d, verdict, local) = best.expect("at least one stand position");
    // (moment, x) of the largest sagging and hogging moment.
    let mut sag: Option<(f64, f64)> = None;
    let mut hog: Option<(f64, f64)> = None;
    for (&m, &xi) in d.moment.iter().zip(&grid.x) {
        if m > 0.0 && sag.is_none_or(|s| m > s.0) {
            sag = Some((m, xi));
        }
        if m < 0.0 && hog.is_none_or(|h| m < h.0) {
            hog = Some((m, xi));
        }
    }
    let dead = dead_factor(mat);
    let wt = trapz(&sec.area, &grid.x) * rho / IN3_PER_FT3;
    Ok(StandsCalc {
        orientation: sp.orientation,
        pad_in: sp.pad_in,
        step_in: sp.step_in,
        dead_factor: dead,
        capacity_factor: capacity_factors(mat, pu).0,
        hull_weight_lb: wt,
        factored_weight_lb: wt * dead,
        length_in: l,
        x_stand_in: x,
        x_over_l: x / l,
        spacing_in: l - 2.0 * x,
        reactions_lb: d.reactions,
        sag_max_lbin: sag.map(|s| s.0),
        x_sag_in: sag.map(|s| s.1),
        hog_max_lbin: hog.map(|h| h.0),
        x_hog_in: hog.map(|h| h.1),
        x_in: grid.x.clone(),
        shear_lb: d.shear,
        moment_lbin: d.moment,
        verdict,
        local,
        beam_dc: beam,
        sweep_x_in: xs,
        sweep_dc: sweep,
        sweep_beam_dc: sweep_beam,
    })
}

/// Knee punching D/C of the heaviest paddler at one knee split.
#[derive(Debug, Clone, Serialize)]
pub struct SplitDc {
    pub knee_split: f64,
    /// Load on the critical perimeter, lb.
    pub load_lb: f64,
    pub demand_psi: f64,
    pub capacity_psi: f64,
    pub dc: f64,
}

/// Punching D/C at knee splits 0.5, 0.6 and 1.0 (two knees; 1.0 = all on one knee), for a
/// factored paddler load `paddler_lb` and the winner's punching capacity `capacity_psi`.
pub fn knee_split_sensitivity(
    paddler_lb: f64,
    t: f64,
    knee_in: f64,
    pu: &Punching,
    capacity_psi: f64,
) -> Vec<SplitDc> {
    [0.5, 0.6, 1.0]
        .into_iter()
        .map(|s| {
            let p = Punching { knees: 2, knee_split: s, ..pu.clone() };
            // fc only sets the capacity, which is given: any positive value.
            let c = punching_calc(paddler_lb, t, 1.0, 1.0, knee_in, &p);
            let dc = if capacity_psi > 0.0 && capacity_psi.is_finite() {
                c.v_u / capacity_psi
            } else {
                f64::INFINITY
            };
            SplitDc { knee_split: s, load_lb: c.load_lb, demand_psi: c.v_u, capacity_psi, dc }
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::hull::linspace;
    use crate::params::Params;
    use crate::section;
    use std::path::Path;

    fn box_grid(l: f64, b: f64, depth: f64, n: usize) -> Grid {
        let x = linspace(0.0, l, n);
        let z = linspace(0.0, depth, 41);
        let y = vec![b / 2.0; n * z.len()];
        Grid { x, z, y, depth }
    }

    fn setup() -> (Params, StrengthFit) {
        let p = Params::load(Path::new("examples/canoe.toml")).unwrap();
        let fit = crate::optimize::strength(&p).unwrap();
        (p, fit)
    }

    #[test]
    fn reactions_balance_an_asymmetric_weight() {
        let x = linspace(0.0, 200.0, 401);
        // Heavier towards the stern: w = 1 + x/200 lb/in, W = 300 lb, xg = 111.11 in.
        let w: Vec<f64> = x.iter().map(|x| 1.0 + x / 200.0).collect();
        let d = stand_diagram(&x, &w, 40.0, 6.0).unwrap();
        // W = int w = 300 lb; W xg = int w x = 20000 + 200^3/600.
        let (wt, xg) = (300.0, (20000.0 + 8.0e6 / 600.0) / 300.0);
        // Statics by hand: R1 + R2 = W, R1 (40) + R2 (160) = W xg.
        let r2 = wt * (xg - 40.0) / 120.0;
        assert!((d.reactions[0] + d.reactions[1] - wt).abs() < 1e-6);
        assert!((d.reactions[1] - r2).abs() < 0.01, "{:?} {r2}", d.reactions);
        assert!(d.reactions[1] > d.reactions[0]);
        assert!(d.shear[400].abs() < 1e-9 && d.moment[400].abs() < 0.5);
        // Free ends: no shear or moment at the bow.
        assert_eq!((d.shear[0], d.moment[0]), (0.0, 0.0));
    }

    /// Uniform weight: equal sagging and hogging peaks at x_s = 0.2071 L (narrow pads).
    #[test]
    fn uniform_weight_balances_moments_near_0207_l() {
        let l = 216.0;
        let x = linspace(0.0, l, 433);
        let w = vec![0.5; x.len()];
        let peak = |xs: f64| {
            let d = stand_diagram(&x, &w, xs, 1.0).unwrap();
            d.moment.iter().fold(0.0f64, |m, v| m.max(v.abs()))
        };
        let best = (2..=200).map(|k| k as f64 * 0.5).min_by(|a, b| peak(*a).total_cmp(&peak(*b))).unwrap();
        assert!((best / l - 0.2071).abs() < 0.005, "{best} = {} L", best / l);
    }

    /// A narrow box (neutral axis near mid-depth, long enough that bending, not shear, governs)
    /// on stands: the lowest-D/C position is near 0.207 L (0.203 L balances sag and hog tension
    /// with this box's zbar), and the hand formula sigma = M c / I holds there. A deep box is
    /// shear-governed instead, with its optimum at L/4 (shear w x_s = W/2 - w x_s).
    #[test]
    fn box_on_stands_optimum_and_hand_check() {
        let (p, fit) = setup();
        let g = box_grid(216.0, 2.0, 10.0, 433);
        let sec = section::sections(&g, 0.375);
        let sp = Stands { pad_in: 2.0, orientation: StandOrientation::Upright, step_in: 0.5 };
        let r = optimize_position(&g, &sec, 60.0, &p.material, &fit, &p.punching, &sp).unwrap();
        assert!((r.x_over_l - 0.2071).abs() < 0.01, "{}", r.x_over_l);
        let gov = r.verdict.checks.iter().filter(|c| !c.name.starts_with("stand_")).fold(
            None,
            |g: Option<&CheckResult>, c| {
                if g.is_none_or(|g| c.dc > g.dc) { Some(c) } else { g }
            },
        );
        assert!(matches!(gov.unwrap().name, "bending_tension" | "biaxial"), "{:?}", gov);
        assert!((r.spacing_in - (216.0 - 2.0 * r.x_stand_in)).abs() < 1e-12);
        assert!((r.reactions_lb[0] - r.reactions_lb[1]).abs() < 1e-6 * r.factored_weight_lb);
        assert!(r.sag_max_lbin.unwrap() > 0.0 && r.hog_max_lbin.unwrap() < 0.0);
        let bt = r.verdict.checks.iter().find(|c| c.name == "bending_tension").unwrap();
        let i = g.x.iter().position(|&x| Some(x) == bt.x_in).unwrap();
        let m = r.moment_lbin[i];
        let c = if m >= 0.0 { sec.zbar[i] } else { g.depth - sec.zbar[i] };
        assert!((bt.demand - m.abs() * c / sec.inertia[i]).abs() < 1e-9);
        assert_eq!(r.verdict.checks.len(), 5);
        assert_eq!(r.verdict.checks[3].name, "stand_punching");
        // The reported position is the beam-D/C minimum of the sweep; the local pad check does
        // not move with position here (symmetric box), so it cannot pick the position.
        let min = r.sweep_beam_dc.iter().cloned().fold(f64::INFINITY, f64::min);
        assert_eq!(r.beam_dc, min);
        assert!(r.verdict.dc >= r.beam_dc);
    }

    #[test]
    fn inverted_puts_the_sagging_tension_at_the_gunwale() {
        let (p, fit) = setup();
        let g = box_grid(216.0, 24.0, 12.0, 217);
        let sec = section::sections(&g, 0.375);
        let up = Stands { pad_in: 6.0, orientation: StandOrientation::Upright, step_in: 0.5 };
        let inv = Stands { orientation: StandOrientation::Inverted, ..up.clone() };
        let (du, vu, _) = evaluate(&g, &sec, 60.0, &p.material, &fit, &p.punching, &up, 30.0).unwrap();
        let (di, vi, li) = evaluate(&g, &sec, 60.0, &p.material, &fit, &p.punching, &inv, 30.0).unwrap();
        assert_eq!(du.moment, di.moment);
        // Midship sags; upright the keel (c = zbar) is in tension, inverted the gunwale.
        let i = 108;
        assert!(du.moment[i] > 0.0);
        let st = |v: &Verdict| v.checks.iter().find(|c| c.name == "bending_tension").unwrap().demand;
        assert!(st(&vu) != st(&vi));
        assert_eq!(li.name, "stand_bearing");
        assert!(vi.checks.iter().any(|c| c.name == "stand_bearing"));
    }

    /// Local checks by hand: ACI 22.6 square pad (upright) and bearing on the gunwales (inverted).
    #[test]
    fn local_checks_hand_calc() {
        let (mut p, _) = setup();
        p.material.method = DesignMethod::Lrfd;
        let pu = &p.punching;
        let sp = Stands { pad_in: 6.0, orientation: StandOrientation::Upright, step_in: 0.5 };
        let (t, fc, r) = (0.375, 1600.0, 80.0);
        let d = pu.d_over_t().unwrap() * t;
        let lc = local_check(r, t, fc, &p.material, pu, &sp, 0.75).unwrap();
        let b0 = 4.0 * (6.0 + d);
        let vc = (2.0 + 40.0 * d / b0).min(4.0) * fc.sqrt();
        assert!((lc.demand_psi - r / (b0 * d)).abs() < 1e-12);
        assert!((lc.capacity_psi - 0.75 * vc).abs() < 1e-9);
        let inv = Stands { orientation: StandOrientation::Inverted, ..sp };
        let lb = local_check(r, t, fc, &p.material, pu, &inv, 0.75).unwrap();
        assert!((lb.demand_psi - r / (2.0 * t * 6.0)).abs() < 1e-12);
        assert!((lb.capacity_psi - 0.65 * 0.85 * fc).abs() < 1e-9);
        // No strength: the check fails, never passes.
        assert!(local_check(r, t, 0.0, &p.material, pu, &sp, 0.75).unwrap().dc.is_infinite());
    }

    #[test]
    fn knee_split_sensitivity_scales_with_the_split() {
        let (p, _) = setup();
        let s = knee_split_sensitivity(336.0, 0.375, 3.0, &p.punching, 90.0);
        assert_eq!(s.iter().map(|x| x.knee_split).collect::<Vec<_>>(), [0.5, 0.6, 1.0]);
        assert!((s[1].dc / s[0].dc - 1.2).abs() < 1e-12 && (s[2].dc / s[0].dc - 2.0).abs() < 1e-12);
        assert!((s[2].load_lb - 336.0).abs() < 1e-12);
    }
}
