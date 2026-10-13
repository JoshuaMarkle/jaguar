//! The search.
//!
//! 1. Expand `search.{thickness,density,depth}` into a grid of candidates.
//! 2. Evaluate every candidate against every load case in parallel (rayon).
//!    A candidate's D/C is the max over load cases. An `Err` from
//!    equilibrium marks that candidate failed with its reason; it does not
//!    abort the run.
//! 3. Winner = lightest candidate with dc <= gates.dc_search and
//!    freeboard >= gates.freeboard_min_in.
//! 4. Re-evaluate the winner at 2n-1 and 4n-3 stations and Richardson-
//!    extrapolate its D/C ([`convergence`]); `verified` = extrapolated (or,
//!    when irregular, the 2n-1) D/C <= dc_verify.
//! 5. Describe every searched variable (`search_space`: range, step, count,
//!    winner value, bound it sits on) and the winner density's lab-data
//!    provenance. `on_grid_edge` = a wall variable on a bound that is not a
//!    real limit (buildable floor, mix data, tested mixes).
//!
//! Must stay deterministic: same canoe.toml => byte-identical result.json.

use crate::check::{Verdict, check, first_moment_below, freeboard};
use crate::equilibrium::{CrewLoad, Loads, solve};
use crate::hull::{Grid, Hull};
use crate::hullgen::ShapeParams;
use crate::mix::{MixProvenance, StrengthFit};
use crate::params::{LoadCase, ObjectiveKind, Params, StrengthModel, range};
use crate::resistance::{resistance, weighted_resistance};
use crate::seats::{self, CaseSeats, SeatSource};
use crate::section::{Sections, sections};
use anyhow::Result;
use rayon::prelude::*;
use serde::Serialize;
use std::sync::atomic::{AtomicUsize, Ordering};

#[derive(Debug, Clone, Serialize)]
pub struct Candidate {
    /// Hull length overall, in (from the hull table).
    pub length_in: f64,
    /// Maximum beam (2 x max half-breadth), in.
    pub beam_in: f64,
    /// Generator parameters when the hull was produced by the shape search;
    /// None for a hull loaded from file.
    pub shape: Option<ShapeParams>,
    pub thickness_in: f64,
    /// Mix density. With strength_model = "tested_mixes" this is the tested
    /// mix's density and `mix` names it.
    pub density_pcf: f64,
    /// Name of the tested mix used (strength_model = "tested_mixes"), else None.
    pub mix: Option<String>,
    pub depth_in: f64,
    /// Self weight from the section areas alone (trapz of A*rho/1728 over x).
    /// Computable even when equilibrium fails, so it is ALWAYS a real number:
    /// never NaN (serde would write NaN as `null`, a sentinel).
    pub hull_weight_lb: f64,
    /// Worst verdict over all load cases, or None if analysis failed.
    pub verdict: Option<Verdict>,
    /// Name of the governing load case.
    pub governing_case: Option<String>,
    /// Why the analysis failed, if it did.
    pub error: Option<String>,
    pub passes: bool,
    /// Loaded transverse metacentric height GM_T, in, worst load case.
    /// None until computed or if analysis failed. Gate: gm >= stability.gm_min_in.
    pub gm_in: Option<f64>,
    /// Total resistance at objective.speed_mps, N (wave + ITTC friction), worst
    /// (heaviest) load case. None unless objective.kind = drag.
    pub drag_n: Option<f64>,
    /// Swamped-flotation air pocket length needed at EACH end, in. None when
    /// `[flotation]` is absent or the pocket cannot be sized (then `error` says why).
    pub air_pocket_in: Option<f64>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Winner {
    pub candidate: Candidate,
    pub verify_stations: usize,
    pub verify_dc: f64,
    /// Gate: max(dc_extrapolated, D/C at 4n-3) <= dc_verify when the
    /// extrapolation is well behaved, else verify_dc <= dc_verify.
    pub verified: bool,
    /// Richardson-extrapolated D/C at infinite station resolution (max over
    /// load cases and checks), None when the series is not well behaved.
    pub dc_extrapolated: Option<f64>,
    /// Observed convergence order of the series that sets dc_extrapolated
    /// (None when that series had already converged).
    pub dc_order: Option<f64>,
    pub convergence: Convergence,
    pub on_grid_edge: bool,
    /// Winner density is at the edge of the range the mix data supports
    /// (lowest/highest tested density -/+ density_extrapolation_pcf). Not a
    /// search-range problem: the team needs lab data for lighter/heavier mixes.
    pub at_mix_data_edge: bool,
    /// Lab data behind the winner density: tested or predicted, nearest
    /// tested mixes and the distance to them ("reach").
    pub mix_provenance: MixProvenance,
    /// Every searched variable: thickness, density, depth, then the free
    /// `[shape_search]` factors (shape runs). See [`search_space`].
    pub search_space: Vec<SearchAxis>,
    /// Along-hull diagrams for the winner at verification resolution, governing
    /// load case, for plotting.
    pub diagrams: Diagrams,
    /// Every load case at verification resolution, in `load_cases` order.
    pub cases: Vec<CaseDiagrams>,
    /// The bare hull floating with no crew, at verification resolution.
    pub empty: EmptyFloat,
    /// Every intermediate value behind the governing load case's checks
    /// (the report's calculation appendix).
    pub calc: CalcTrace,
}

/// Worked calculation of the winner's governing load case at the seats its
/// verdict comes from and the strength combination that governs, at
/// verification resolution. Every number is recomputed by the same functions
/// the checks use, so each demand and capacity equals that case's verdict.
#[derive(Debug, Clone, Serialize, Default)]
pub struct CalcTrace {
    /// Set when the trace could not be computed; nothing else is then present.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    #[serde(flatten)]
    pub calc: Option<Calc>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Calc {
    pub case: String,
    /// Knee x per paddler (`weights_lb` order) of the governing seat sample.
    pub seats_in: Vec<f64>,
    pub weights_lb: Vec<f64>,
    pub method: crate::params::DesignMethod,
    pub factor_of_safety: f64,
    pub dynamic_load_factor: f64,
    /// (dead, live) load factors of the governing combination.
    pub dead_factor: f64,
    pub live_factor: f64,
    /// Capacity multiplier: 1/FS (asd) or phi (lrfd); punching may differ.
    pub capacity_factor: f64,
    pub punching_factor: f64,
    pub stations: usize,
    pub length_in: f64,
    pub thickness_in: f64,
    pub density_pcf: f64,
    pub depth_in: f64,
    /// Integral of the shell area A(x) dx, in^3.
    pub wall_volume_in3: f64,
    /// Service equilibrium (unfactored, dynamic factor on the crew).
    pub service: CalcFloat,
    /// Equilibrium the stress checks use (factored for lrfd).
    pub strength: CalcFloat,
    pub split_slope_psi_per_pcf: f64,
    pub split_intercept_psi: f64,
    pub split_psi: f64,
    /// "measured" or "derived" (see `StrengthFit::source`).
    pub flexural_source: &'static str,
    pub flex_over_split: f64,
    pub flexural_psi: f64,
    pub fc_source: &'static str,
    pub fc_psi: f64,
    pub lambda: f64,
    pub bending_tension: Option<CalcFibre>,
    pub bending_compression: Option<CalcFibre>,
    pub shear: Option<CalcShear>,
    pub punching: CalcPunching,
    pub biaxial: Option<CalcBiaxial>,
    /// Swamped flotation of the bare hull (with `[flotation]`).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub flotation: Option<crate::flotation::Flotation>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub swamp_factor: Option<f64>,
}

/// One equilibrium: weights, draft, trim, freeboard and GM parts.
#[derive(Debug, Clone, Serialize)]
pub struct CalcFloat {
    pub hull_weight_lb: f64,
    /// Crew as floated (weights x dynamic factor x live factor), lb.
    pub crew_lb: f64,
    pub weight_lb: f64,
    pub draft_in: f64,
    pub trim_in: f64,
    /// Highest local waterline (an end of the trimmed waterline), in.
    pub waterline_max_in: f64,
    pub freeboard_in: f64,
    pub kb_in: f64,
    pub bm_in: f64,
    pub kg_in: f64,
    pub gm_in: f64,
    pub crew_cg_in: f64,
}

/// Bending stress at one fibre of one station.
#[derive(Debug, Clone, Serialize)]
pub struct CalcFibre {
    pub x_in: f64,
    pub moment_lbin: f64,
    pub area_in2: f64,
    pub zbar_in: f64,
    pub inertia_in4: f64,
    /// Fibre distance from the neutral axis, in.
    pub c_in: f64,
    /// "bottom" or "top".
    pub fibre: &'static str,
    pub stress_psi: f64,
    pub capacity_psi: f64,
    pub dc: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct CalcShear {
    pub x_in: f64,
    pub shear_lb: f64,
    pub zbar_in: f64,
    pub inertia_in4: f64,
    /// First moment of the shell below the axis, in^3.
    pub q_in3: f64,
    /// Web width, two walls: 2 t, in.
    pub b_in: f64,
    pub stress_psi: f64,
    pub capacity_psi: f64,
    pub dc: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct CalcPunching {
    pub method: crate::params::PunchingMethod,
    /// Heaviest paddler x dynamic factor x live factor, lb.
    pub paddler_lb: f64,
    pub knees: u32,
    pub knee_split: f64,
    pub knee_spacing_in: f64,
    pub knee_patch_in: f64,
    pub pad_in: f64,
    /// d / t (aci_22_6), 1 for plain_14_5.
    pub depth_over_t: f64,
    #[serde(flatten)]
    pub calc: crate::check::PunchingCalc,
    pub capacity_psi: f64,
    pub dc: f64,
}

/// Kupfer-Gerstle at one station: the governing of the four sign combinations.
#[derive(Debug, Clone, Serialize)]
pub struct CalcBiaxial {
    pub x_in: f64,
    pub tension_psi: f64,
    pub compression_psi: f64,
    /// Transverse turning stress (0 without `[turning]`), psi.
    pub transverse_psi: f64,
    pub shear_psi: f64,
    /// sigma_x and sigma_y of the governing combination.
    pub sigma_x_psi: f64,
    pub sigma_y_psi: f64,
    pub sigma_1_psi: f64,
    pub sigma_2_psi: f64,
    /// f_t (1 - 0.8 |sigma_2| / f'c) with f_t, f'c already x capacity factor.
    pub ft_psi: f64,
    pub fc_psi: f64,
    pub capacity_psi: f64,
    pub dc: f64,
}

/// Build the calculation trace of `case` (the winner's governing case).
#[allow(clippy::too_many_arguments)]
pub fn calc_trace(
    params: &Params,
    fit: &StrengthFit,
    grid: &Grid,
    sec: &Sections,
    t: f64,
    rho: f64,
    case_name: &str,
    seats: &CaseSeats,
) -> CalcTrace {
    match calc(params, fit, grid, sec, t, rho, case_name, seats) {
        Ok(c) => CalcTrace { error: None, calc: Some(c) },
        Err(e) => CalcTrace { error: Some(format!("{e:#}")), calc: None },
    }
}

#[allow(clippy::too_many_arguments)]
fn calc(
    params: &Params,
    fit: &StrengthFit,
    grid: &Grid,
    sec: &Sections,
    t: f64,
    rho: f64,
    case_name: &str,
    seats: &CaseSeats,
) -> Result<Calc> {
    use crate::check::{first_moment_below, hoop_psi, punching_calc};
    use crate::params::{DesignMethod, PunchingMethod};
    let case = params
        .load_cases
        .iter()
        .find(|c| c.name == case_name)
        .ok_or_else(|| anyhow::anyhow!("unknown load case {case_name}"))?;
    let mat = &params.material;
    // The checks use the thickness recovered from the section (equal to t up to rounding).
    let t = crate::check::thickness(grid, sec).unwrap_or(t);
    let env = eval_envelope(params, fit, grid, sec, rho, case, &seats.seats_in, false)?;
    let e = eval_case(params, fit, grid, sec, rho, case, &env.verdict_seats_in)?;
    let (dead, live) = e.combo;
    let pu = &params.punching;
    let (k, k_punch) = match mat.method {
        DesignMethod::Asd => (1.0 / mat.factor_of_safety, 1.0 / mat.factor_of_safety),
        DesignMethod::Lrfd => (0.60, if pu.method == PunchingMethod::Aci22_6 { 0.75 } else { 0.60 }),
    };
    let crew_total: f64 = case.weights_lb.iter().sum::<f64>() * mat.dynamic_load_factor;
    let float = |l: &Loads, lf: f64| CalcFloat {
        hull_weight_lb: l.hull_weight_lb,
        crew_lb: crew_total * lf,
        weight_lb: l.weight_lb,
        draft_in: l.draft_in,
        trim_in: l.trim_in,
        waterline_max_in: waterline(grid, l).iter().cloned().fold(f64::NEG_INFINITY, f64::max),
        freeboard_in: freeboard(grid, l),
        kb_in: l.kb_in,
        bm_in: l.bm_in,
        kg_in: l.kg_in,
        gm_in: l.gm_in,
        crew_cg_in: params.stability.crew_cg_in,
    };
    let loads = &e.strength;
    let v = &e.verdict;
    let (ft_cap, fc) = (fit.flexural_psi(rho) * k, fit.fc_psi(rho));
    let lambda = 1.0;
    let station = |name: &str| -> Option<usize> {
        let x = v.checks.iter().find(|c| c.name == name)?.x_in?;
        grid.x.iter().position(|&xi| xi == x)
    };
    let fibre = |name: &str, tension: bool| -> Option<CalcFibre> {
        let i = station(name)?;
        let (m, zb, ii) = (loads.moment[i], sec.zbar[i], sec.inertia[i]);
        let (c_top, c_bot) = (grid.depth - zb, zb);
        let (ct, cc) = crate::check::fibres(m, c_top, c_bot);
        let c = if tension { ct } else { cc };
        // Sagging (M >= 0) puts the bottom in tension, hogging the top.
        let bottom = (m >= 0.0) == tension;
        let cap = if tension {
            ft_cap
        } else if fc > 0.0 {
            0.85 * fc * k
        } else {
            f64::NAN
        };
        let s = m.abs() * c / ii;
        Some(CalcFibre {
            x_in: grid.x[i],
            moment_lbin: m,
            area_in2: sec.area[i],
            zbar_in: zb,
            inertia_in4: ii,
            c_in: c,
            fibre: if bottom { "bottom" } else { "top" },
            stress_psi: s,
            capacity_psi: cap,
            dc: s / cap,
        })
    };
    let shear = station("shear").map(|i| {
        let q = first_moment_below(grid.row(i), &grid.z, t, sec.zbar[i]);
        let tau = loads.shear[i].abs() * q / (sec.inertia[i] * 2.0 * t);
        let cap = if fc > 0.0 { 2.0 * lambda * fc.sqrt() * k } else { f64::NAN };
        CalcShear {
            x_in: grid.x[i],
            shear_lb: loads.shear[i],
            zbar_in: sec.zbar[i],
            inertia_in4: sec.inertia[i],
            q_in3: q,
            b_in: 2.0 * t,
            stress_psi: tau,
            capacity_psi: cap,
            dc: tau / cap,
        }
    });
    let max_paddler =
        case.weights_lb.iter().cloned().fold(f64::NEG_INFINITY, f64::max) * mat.dynamic_load_factor * live;
    let pc = punching_calc(max_paddler, t, fc, lambda, mat.knee_patch_in, pu);
    let p_cap = pc.v_nominal * k_punch;
    let punching = CalcPunching {
        method: pu.method,
        paddler_lb: max_paddler,
        knees: pu.knees,
        knee_split: pu.knee_split,
        knee_spacing_in: pu.knee_spacing_in,
        knee_patch_in: mat.knee_patch_in,
        pad_in: pu.pad_in,
        depth_over_t: pc.depth_in / t,
        dc: pc.v_u / p_cap,
        capacity_psi: p_cap,
        calc: pc,
    };
    let fc_k = if fc > 0.0 { fc * k } else { f64::NAN };
    let hoop = hoop_psi(grid, loads, t, params.turning.as_ref());
    let biaxial = station("biaxial").map(|i| {
        let (m, zb, ii) = (loads.moment[i], sec.zbar[i], sec.inertia[i]);
        let (ct, cc) = crate::check::fibres(m, grid.depth - zb, zb);
        let (st, sc) = (m.abs() * ct / ii, m.abs() * cc / ii);
        let q = first_moment_below(grid.row(i), &grid.z, t, zb);
        let tau = loads.shear[i].abs() * q / (ii * 2.0 * t);
        let sy = hoop[i];
        // Same four sign combinations as check::kupfer_dc; keep the worst.
        let mut best: Option<CalcBiaxial> = None;
        for (sx, sy) in [(st, sy), (-sc, sy), (st, -sy), (-sc, -sy)] {
            let r = ((0.5 * (sx - sy)).powi(2) + tau * tau).sqrt();
            let c = 0.5 * (sx + sy);
            let (s1, s2) = (c + r, c - r);
            let cap = ft_cap * (1.0 - 0.8 * s2.min(0.0).abs() / fc_k);
            let dc = if cap.is_nan() || cap <= 0.0 || !cap.is_finite() {
                f64::INFINITY
            } else {
                s1.max(0.0) / cap
            };
            if best.as_ref().is_none_or(|b| dc > b.dc) {
                best = Some(CalcBiaxial {
                    x_in: grid.x[i],
                    tension_psi: st,
                    compression_psi: sc,
                    transverse_psi: hoop[i],
                    shear_psi: tau,
                    sigma_x_psi: sx,
                    sigma_y_psi: sy,
                    sigma_1_psi: s1,
                    sigma_2_psi: s2,
                    ft_psi: ft_cap,
                    fc_psi: fc_k,
                    capacity_psi: cap,
                    dc,
                });
            }
        }
        best
    });
    let n = grid.nx();
    let flotation = match &params.flotation {
        Some(fp) => Some(crate::flotation::flotation(grid, sec, rho, fp.swamp_factor, fp.air_pocket_max_in)?),
        None => None,
    };
    Ok(Calc {
        case: case.name.clone(),
        seats_in: env.verdict_seats_in.clone(),
        weights_lb: case.weights_lb.clone(),
        method: mat.method,
        factor_of_safety: mat.factor_of_safety,
        dynamic_load_factor: mat.dynamic_load_factor,
        dead_factor: dead,
        live_factor: live,
        capacity_factor: k,
        punching_factor: k_punch,
        stations: n,
        length_in: grid.x[n - 1] - grid.x[0],
        thickness_in: t,
        density_pcf: rho,
        depth_in: grid.depth,
        wall_volume_in3: (1..n)
            .map(|i| 0.5 * (sec.area[i] + sec.area[i - 1]) * (grid.x[i] - grid.x[i - 1]))
            .sum(),
        service: float(&e.service, 1.0),
        strength: float(loads, live),
        split_slope_psi_per_pcf: fit.split_slope_psi_per_pcf,
        split_intercept_psi: fit.split_intercept_psi,
        split_psi: fit.split_psi(rho),
        flexural_source: fit.source(crate::mix::Property::Flexural),
        flex_over_split: fit.flex_over_split,
        flexural_psi: fit.flexural_psi(rho),
        fc_source: fit.source(crate::mix::Property::Compressive),
        fc_psi: fc,
        lambda,
        bending_tension: fibre("bending_tension", true),
        bending_compression: fibre("bending_compression", false),
        shear,
        punching,
        biaxial: biaxial.flatten(),
        flotation,
        swamp_factor: params.flotation.as_ref().map(|f| f.swamp_factor),
    })
}

/// Unloaded (no crew) equilibrium of the winner: hull self weight only.
#[derive(Debug, Clone, Serialize, Default)]
pub struct EmptyFloat {
    /// Set when equilibrium failed; the numbers below are then absent/empty.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    pub draft_in: Option<f64>,
    pub trim_in: Option<f64>,
    /// Minimum freeboard over the stations, in.
    pub freeboard_in: Option<f64>,
    pub x_in: Vec<f64>,
    /// Local waterline height above keel, in.
    pub waterline_in: Vec<f64>,
}

/// Float the bare hull (no crew) and report draft, trim, freeboard and waterline.
pub fn empty_float(params: &Params, grid: &Grid, sec: &Sections, rho: f64) -> EmptyFloat {
    let (patch, cg) = (params.material.knee_patch_in, params.stability.crew_cg_in);
    match solve(grid, sec, rho, &[], patch, cg) {
        Ok(loads) => EmptyFloat {
            error: None,
            draft_in: Some(loads.draft_in),
            trim_in: Some(loads.trim_in),
            freeboard_in: Some(freeboard(grid, &loads)),
            x_in: grid.x.clone(),
            waterline_in: waterline(grid, &loads),
        },
        Err(e) => EmptyFloat { error: Some(format!("{e:#}")), ..Default::default() },
    }
}

/// Local waterline height above keel per station, clamped to [0, depth].
fn waterline(grid: &Grid, loads: &Loads) -> Vec<f64> {
    let l = grid.x[grid.nx() - 1];
    grid.x
        .iter()
        .map(|&x| (loads.draft_in + loads.trim_in * (x - l / 2.0) / l).clamp(0.0, grid.depth))
        .collect()
}

/// One load case of the winner: resolved seats, equilibrium, stresses and
/// per-station D/C (0 at masked tip stations; same formulas as check.rs).
#[derive(Debug, Clone, Serialize, Default)]
pub struct CaseDiagrams {
    pub name: String,
    /// Nominal knee x per paddler (`weights_lb` order), in from bow.
    pub seats_in: Vec<f64>,
    /// Paddler indices bow to stern.
    pub crew_order: Vec<usize>,
    /// given | even | auto (None only in hand-built values).
    pub seat_source: Option<SeatSource>,
    /// Knees checked at +/- this (`seats.variance_in`).
    pub seat_variance_in: f64,
    /// Seats of the sample `verdict` comes from (= seats_in when variance is 0).
    pub verdict_seats_in: Vec<f64>,
    pub weights_lb: Vec<f64>,
    /// Set when equilibrium failed for this case; all numbers below are then
    /// absent/empty (never a sentinel). With an error, verdict is None.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    /// Draft and trim at the nominal seats; GM and freeboard the worst over
    /// the seat samples (what the gates see). Diagrams and per-station D/C are
    /// at the nominal seats, `verdict` is the worst sample.
    pub draft_in: Option<f64>,
    pub trim_in: Option<f64>,
    pub gm_in: Option<f64>,
    pub freeboard_in: Option<f64>,
    /// x, V, M, tension, waterline etc. (same shape as `Winner.diagrams`).
    #[serde(flatten)]
    pub diagrams: Diagrams,
    pub compression_psi: Vec<f64>,
    pub shear_psi: Vec<f64>,
    pub compression_allow_psi: Option<f64>,
    pub shear_allow_psi: Option<f64>,
    pub dc_bending_tension: Vec<f64>,
    pub dc_bending_compression: Vec<f64>,
    pub dc_shear: Vec<f64>,
    /// Kupfer-Gerstle biaxial D/C per station (`check::kupfer_dc` on this
    /// station's tension/compression fibre stress and shear stress).
    pub dc_biaxial: Vec<f64>,
    pub verdict: Option<Verdict>,
}

/// Per-station arrays (same length as `x_in`) for the web plot tabs.
#[derive(Debug, Clone, Serialize, Default)]
pub struct Diagrams {
    pub x_in: Vec<f64>,
    /// Shear, lb.
    pub shear_lb: Vec<f64>,
    /// Bending moment, lb*in (positive = sagging).
    pub moment_lbin: Vec<f64>,
    /// Bending tension stress |M| c / I, psi (0 at masked tip stations).
    pub tension_psi: Vec<f64>,
    /// Allowable bending tension (capacity / FS), psi, constant along the hull.
    pub tension_allow_psi: f64,
    /// Wall cross-section area, in^2, and second moment, in^4.
    pub area_in2: Vec<f64>,
    pub inertia_in4: Vec<f64>,
    /// Local waterline height above keel, in.
    pub waterline_in: Vec<f64>,
}

/// End of a searched range.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Bound {
    Min,
    Max,
}

/// One searched variable and where the winner landed on it.
#[derive(Debug, Clone, Serialize)]
pub struct SearchAxis {
    /// `search.*` or `shape_search.*` key.
    pub name: &'static str,
    /// Lowest and highest value tried (wall axes after the buildable floor and
    /// the mix-data clip; stepped shape axes snapped to their step).
    pub min: f64,
    pub max: f64,
    /// Grid step. None = continuous (shape factors) or the tested-mix list.
    pub step: Option<f64>,
    /// Values on the axis. None = continuous.
    pub count: Option<usize>,
    pub winner: f64,
    /// Range end the winner sits on. None = inside (or a one-value axis).
    pub at_bound: Option<Bound>,
    /// Why that end is a real limit, not a range to widen: "buildable floor",
    /// "mix data" (no lab data further out), "tested mixes". None = widen it.
    pub limit: Option<&'static str>,
}

/// Searched thicknesses: `search.thickness_in` from the buildable floor up.
fn thicknesses(params: &Params) -> Vec<f64> {
    let floor = params.gates.thickness_min_buildable_in;
    range(params.search.thickness_in).into_iter().filter(|&t| t >= floor - 1e-9).collect()
}

/// The search space around candidate `c`: thickness, density, depth (the
/// values [`run`] actually tried), then for a shape run (`c.shape` set) each
/// free `[shape_search]` factor (fixed ones and symmetric-tied aft ones are
/// left out). Wall bounds compare exactly (the winner is a grid value); shape
/// bounds within 1e-9 relative.
pub fn search_space(params: &Params, fit: &StrengthFit, c: &Candidate) -> Result<Vec<SearchAxis>> {
    let at = |x: f64, r: &[f64]| match r {
        [lo, .., hi] if x == *lo => Some(Bound::Min),
        [_, .., hi] if x == *hi => Some(Bound::Max),
        _ => None,
    };
    let axis = |name, r: &[f64], step, x| SearchAxis {
        name,
        min: r[0],
        max: r[r.len() - 1],
        step,
        count: Some(r.len()),
        winner: x,
        at_bound: at(x, r),
        limit: None,
    };
    let s = &params.search;
    let ts = thicknesses(params);
    anyhow::ensure!(!ts.is_empty(), "no searched thickness at or above thickness_min_buildable_in");
    let mut t = axis("thickness_in", &ts, Some(s.thickness_in[2]), c.thickness_in);
    // Thickness at the buildable floor is a real constraint, not a range problem.
    if (c.thickness_in - params.gates.thickness_min_buildable_in).abs() <= 1e-9 {
        t.limit = t.at_bound.map(|_| "buildable floor");
    }
    let rhos: Vec<f64> = density_axis(params, fit)?.iter().map(|a| a.0).collect();
    let rho = match params.material.strength_model {
        StrengthModel::TestedMixes => {
            let mut a = axis("density_pcf", &rhos, None, c.density_pcf);
            a.limit = a.at_bound.map(|_| "tested mixes");
            a
        }
        StrengthModel::Fit => {
            // Density at the end of the trusted fit range, where the requested
            // range extended further: the lab data, not the search range, is the limit.
            let full = range(s.density_pcf);
            let (lo, hi) = (rhos[0], rhos[rhos.len() - 1]);
            let mut a = axis("density_pcf", &rhos, Some(s.density_pcf[2]), c.density_pcf);
            if c.density_pcf == lo && full[0] < lo - 1e-9 {
                (a.at_bound, a.limit) = (Some(Bound::Min), Some("mix data"));
            } else if c.density_pcf == hi && full[full.len() - 1] > hi + 1e-9 {
                (a.at_bound, a.limit) = (Some(Bound::Max), Some("mix data"));
            }
            a
        }
    };
    let ds = range(s.depth_in);
    let mut out = vec![t, rho, axis("depth_in", &ds, Some(s.depth_in[2]), c.depth_in)];
    if let (Some(ss), Some(sh)) = (&params.shape_search, &c.shape) {
        let w = [
            sh.length_in,
            sh.beam_in,
            sh.draft_in,
            sh.fore_wp,
            sh.section,
            sh.fore_rocker,
            sh.aft_wp,
            sh.aft_rocker,
            sh.fore_len_frac,
            sh.aft_len_frac,
        ];
        let steps = [ss.length_step_in, ss.beam_step_in, ss.draft_step_in];
        for (d, (name, [lo, hi])) in ss.bounds().into_iter().enumerate() {
            if hi <= lo || (ss.symmetric && matches!(d, 6 | 7 | 9)) {
                continue;
            }
            let (min, max, step, count) = match steps.get(d).copied().flatten() {
                // Same snapping as shapeopt::normalize.
                Some(st) => {
                    let (kmin, kmax) = ((lo / st - 1e-9).ceil(), (hi / st + 1e-9).floor());
                    (kmin * st, kmax * st, Some(st), Some((kmax - kmin) as usize + 1))
                }
                None => (lo, hi, None, None),
            };
            let near = |a: f64, b: f64| (a - b).abs() <= 1e-9 * a.abs().max(b.abs()).max(1.0);
            let at_bound = if near(w[d], min) {
                Some(Bound::Min)
            } else if near(w[d], max) {
                Some(Bound::Max)
            } else {
                None
            };
            out.push(SearchAxis { name, min, max, step, count, winner: w[d], at_bound, limit: None });
        }
    }
    Ok(out)
}

#[derive(Debug, Clone, Serialize)]
pub struct RunResult {
    pub format: &'static str,
    pub params: Params,
    /// The strength fit actually used (from material.mix_data).
    pub strength: StrengthFit,
    /// Winning hull geometry (jaguar-hull/1), so viewers need not regenerate it.
    pub winner_hull: Option<Hull>,
    /// Fixed hull: every grid candidate. Shape search: one representative per
    /// evaluated shape (its inner winner, or its lowest-D/C failing candidate),
    /// in evaluation order, each with `shape` set.
    pub candidates: Vec<Candidate>,
    /// None = no design passed. Never a fallback "best failing" design.
    pub winner: Option<Winner>,
}

pub const RESULT_FORMAT: &str = "jaguar-result/1";

/// Progress events, emitted in order. The web server streams these to the
/// browser; the CLI ignores them. Must be cheap and thread-safe to call.
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "event", rename_all = "snake_case")]
#[allow(clippy::large_enum_variant)] // short-lived, passed by value to a callback
pub enum Progress {
    /// Search started with `total` candidates (for a shape search: total over
    /// all shapes known so far; may grow as refinement rounds are added).
    Started { total: usize },
    /// Shape search: shape `index` of `shapes` is being evaluated.
    Shape { index: usize, shapes: usize, shape: ShapeParams },
    /// One candidate finished (any order, from any thread). `done` counts up to `total`.
    Candidate { done: usize, total: usize, candidate: Candidate },
    /// Winner is being re-checked at `stations`.
    Verifying { stations: usize },
}

/// CLI entry: no progress reporting.
pub fn run(params: &Params, hull: &Hull) -> Result<RunResult> {
    run_with_progress(params, hull, &|_| {})
}

/// Load and fit the mix data, and the density range the fit may be used on.
pub fn strength(params: &Params) -> Result<StrengthFit> {
    StrengthFit::load(&params.material.mix_data, params.material.flex_over_split)
}

/// Densities from search.density_pcf clipped to the trusted fit range
/// ([`StrengthFit::trusted_range`]: every active strength line's tested range
/// +/- extrapolation, intersected). Err if empty.
pub fn densities(params: &Params, fit: &StrengthFit) -> Result<Vec<f64>> {
    let e = params.material.density_extrapolation_pcf;
    let (lo, hi) = fit.trusted_range(e);
    let (tlo, thi) = fit.tested_range();
    let v: Vec<f64> =
        range(params.search.density_pcf).into_iter().filter(|&r| r >= lo - 1e-9 && r <= hi + 1e-9).collect();
    anyhow::ensure!(
        !v.is_empty(),
        "no searched density lies within the mix data range {lo:.1}..{hi:.1} pcf (tested by every fitted \
         strength line: {tlo:.1}..{thi:.1}, +/- density_extrapolation_pcf {e}, none below the lightest \
         test of a negative-slope line); widen search.density_pcf or add lab data"
    );
    Ok(v)
}

/// The density axis: (density, strength model, tested-mix name) per entry.
/// Fit mode: [`densities`] with the global fit, no names. Tested-mixes mode:
/// one entry per tested mix (by density then name) with that mix's own
/// strength; search.density_pcf and the extrapolation limit are ignored.
pub fn density_axis(params: &Params, fit: &StrengthFit) -> Result<Vec<(f64, StrengthFit, Option<String>)>> {
    match params.material.strength_model {
        StrengthModel::Fit => {
            Ok(densities(params, fit)?.into_iter().map(|r| (r, fit.clone(), None)).collect())
        }
        StrengthModel::TestedMixes => {
            let m = fit.tested_mixes();
            anyhow::ensure!(!m.is_empty(), "no tested mixes in the mix data");
            Ok(m.iter().map(|x| (x.density_pcf, fit.for_mix(x), Some(x.name.clone()))).collect())
        }
    }
}

/// Same result as [`run`] (byte-identical JSON), plus progress events.
///
/// If the winner's analysis fails at verification resolution, it is reported
/// as not verified with `verify_dc = f64::MAX` (a finite, obviously failing
/// number, so result.json stays numeric).
pub fn run_with_progress(
    params: &Params,
    hull: &Hull,
    on_progress: &(dyn Fn(Progress) + Sync),
) -> Result<RunResult> {
    run_inner(params, hull, true, on_progress)
}

/// [`run_with_progress`] with a choice of parallelism. `parallel = false` runs
/// the whole search on one thread (a private 1-thread rayon pool) so the shape
/// search can parallelize over shapes instead. Output is identical either way.
pub fn run_inner(
    params: &Params,
    hull: &Hull,
    parallel: bool,
    on_progress: &(dyn Fn(Progress) + Sync),
) -> Result<RunResult> {
    if parallel {
        return run_search(params, hull, on_progress);
    }
    let pool = rayon::ThreadPoolBuilder::new().num_threads(1).build()?;
    pool.install(|| run_search(params, hull, on_progress))
}

fn run_search(params: &Params, hull: &Hull, on_progress: &(dyn Fn(Progress) + Sync)) -> Result<RunResult> {
    let ts = thicknesses(params);
    let fit = strength(params)?;
    let axis = density_axis(params, &fit)?;
    let rhos: Vec<f64> = axis.iter().map(|a| a.0).collect();
    let ds = range(params.search.depth_in);
    let total = ts.len() * rhos.len() * ds.len();
    on_progress(Progress::Started { total });
    let n = params.hull.stations;
    let done = AtomicUsize::new(0);

    let grids: Vec<Grid> =
        ds.par_iter().map(|&d| Grid::build_flared(hull, d, n, params.hull.flare_deg)).collect();
    let pairs: Vec<(usize, usize)> =
        (0..grids.len()).flat_map(|g| (0..ts.len()).map(move |t| (g, t))).collect();
    // Seats per wall geometry (t, depth), solved at the middle density of the
    // axis and shared by every density (src/seats.rs); verify reuses them.
    let mid = &axis[axis.len() / 2];
    let plans: Vec<Result<Vec<CaseSeats>, String>> = pairs
        .par_iter()
        .map(|&(gi, ti)| {
            let sec = sections(&grids[gi], ts[ti]);
            plan_seats(params, &mid.1, &grids[gi], &sec, ts[ti], mid.0).map_err(|e| format!("{e:#}"))
        })
        .collect();
    let mut candidates: Vec<(usize, usize, usize, Candidate)> = pairs
        .par_iter()
        .zip(&plans)
        .flat_map_iter(|(&(gi, ti), plan)| {
            let grid = &grids[gi];
            let t = ts[ti];
            let sec = sections(grid, t);
            let done = &done;
            let axis = &axis;
            rhos.iter()
                .enumerate()
                .map(move |(ri, &rho)| {
                    let mut c = evaluate(params, &axis[ri].1, grid, &sec, t, rho, plan, true);
                    c.mix = axis[ri].2.clone();
                    let k = done.fetch_add(1, Ordering::SeqCst) + 1;
                    on_progress(Progress::Candidate { done: k, total, candidate: c.clone() });
                    (ti, ri, gi, c)
                })
                .collect::<Vec<_>>()
        })
        .collect();
    candidates.sort_by_key(|&(t, r, d, _)| (t, r, d));
    let candidates: Vec<Candidate> = candidates.into_iter().map(|x| x.3).collect();

    let best = candidates.iter().filter(|c| c.passes).reduce(|a, b| if better(params, b, a) { b } else { a });

    let winner = match best {
        None => None,
        Some(c) => {
            let stations = 2 * n - 1;
            on_progress(Progress::Verifying { stations });
            let grid = Grid::build_flared(hull, c.depth_in, stations, params.hull.flare_deg);
            let sec = sections(&grid, c.thickness_in);
            let ri = axis.iter().position(|a| a.0 == c.density_pcf && a.2 == c.mix).unwrap_or(0);
            let pi = pairs
                .iter()
                .position(|&(gi, ti)| grids[gi].depth == c.depth_in && ts[ti] == c.thickness_in)
                .ok_or_else(|| anyhow::anyhow!("winner wall not on the search grid"))?;
            let plan = &plans[pi];
            let v = evaluate(params, &axis[ri].1, &grid, &sec, c.thickness_in, c.density_pcf, plan, false);
            // A winner passed, so its seats resolved.
            let seats = plan.as_ref().map_err(|e| anyhow::anyhow!("winner seats: {e}"))?;
            let cases =
                all_case_diagrams(params, &axis[ri].1, &grid, &sec, c.thickness_in, c.density_pcf, seats);
            let empty = empty_float(params, &grid, &sec, c.density_pcf);
            let calc = match (&c.governing_case, &axis[ri].1) {
                (Some(g), f) => match params.load_cases.iter().position(|lc| &lc.name == g) {
                    Some(gi) => {
                        calc_trace(params, f, &grid, &sec, c.thickness_in, c.density_pcf, g, &seats[gi])
                    }
                    None => CalcTrace { error: Some(format!("unknown governing case {g}")), calc: None },
                },
                (None, _) => CalcTrace { error: Some("no governing case".into()), calc: None },
            };
            let verify_dc = v.verdict.as_ref().map_or(f64::MAX, |v| v.dc);
            let conv = convergence(params, &axis[ri].1, hull, c, seats);
            let verified = v.verdict.is_some()
                && match conv.extrapolated {
                    Some(x) => x.max(conv.dc[2]) <= params.gates.dc_verify,
                    None => verify_dc <= params.gates.dc_verify,
                };
            let search_space = search_space(params, &fit, c)?;
            let wall = &search_space[..3];
            Some(Winner {
                candidate: c.clone(),
                verify_stations: stations,
                verify_dc,
                verified,
                dc_extrapolated: conv.extrapolated,
                dc_order: conv.order,
                convergence: conv,
                on_grid_edge: wall.iter().any(|a| a.at_bound.is_some() && a.limit.is_none()),
                at_mix_data_edge: wall[1].limit == Some("mix data"),
                mix_provenance: fit.provenance(c.density_pcf, c.mix.as_deref())?,
                search_space,
                diagrams: cases
                    .iter()
                    .find(|d| v.governing_case.as_deref() == Some(d.name.as_str()) && d.error.is_none())
                    .map(|d| d.diagrams.clone())
                    .unwrap_or_default(),
                cases,
                empty,
                calc,
            })
        }
    };

    let winner_hull = winner.as_ref().map(|_| hull.clone());
    Ok(RunResult {
        format: RESULT_FORMAT,
        params: params.clone(),
        strength: fit,
        winner_hull,
        candidates,
        winner,
    })
}

/// Knee x per paddler for `case` without auto seats: `seats_in`, or evenly spaced.
pub fn resolved_seats(case: &crate::params::LoadCase, grid: &Grid) -> Vec<f64> {
    let n = grid.nx();
    let length = grid.x[n - 1] - grid.x[0];
    let k = case.weights_lb.len();
    match &case.seats_in {
        Some(s) => s.clone(),
        None => (0..k).map(|i| grid.x[0] + length * (i + 1) as f64 / (k + 1) as f64).collect(),
    }
}

/// Every load case of the winner, in `params.load_cases` order.
pub fn all_case_diagrams(
    params: &Params,
    fit: &StrengthFit,
    grid: &Grid,
    sec: &Sections,
    t: f64,
    rho: f64,
    seats: &[CaseSeats],
) -> Vec<CaseDiagrams> {
    params
        .load_cases
        .iter()
        .zip(seats)
        .map(|(lc, s)| match case_diagrams(params, fit, grid, sec, t, rho, &lc.name, s) {
            Ok(c) => c,
            Err(e) => CaseDiagrams {
                name: lc.name.clone(),
                seats_in: s.seats_in.clone(),
                crew_order: s.crew_order.clone(),
                seat_source: Some(s.source),
                seat_variance_in: params.seats.variance_in,
                weights_lb: lc.weights_lb.clone(),
                error: Some(format!("{e:#}")),
                ..Default::default()
            },
        })
        .collect()
}

/// One load case at fixed seats: service equilibrium (draft, trim, GM,
/// freeboard) and the governing strength combination (`material.method`).
pub struct CaseEval {
    pub service: Loads,
    /// Loads of the combination with the highest D/C (= service for asd).
    pub strength: Loads,
    /// Verdict of that combination; freeboard from the service loads.
    pub verdict: Verdict,
    /// (dead, live) load factors of that combination ((1, 1) for asd).
    pub combo: (f64, f64),
}

/// Evaluate `case` with paddlers at `seats`. LRFD: each combination
/// (dead, live) re-solves equilibrium with self weight x dead and crew
/// (incl. dynamic factor) x live; buoyancy balances the factored weight.
pub fn eval_case(
    params: &Params,
    fit: &StrengthFit,
    grid: &Grid,
    sec: &Sections,
    rho: f64,
    case: &LoadCase,
    seats: &[f64],
) -> Result<CaseEval> {
    let mat = &params.material;
    let crew = |lf: f64| -> Vec<CrewLoad> {
        seats
            .iter()
            .zip(&case.weights_lb)
            .map(|(&x_in, &w)| CrewLoad { x_in, lb: w * mat.dynamic_load_factor * lf })
            .collect()
    };
    let (patch, cg) = (mat.knee_patch_in, params.stability.crew_cg_in);
    let service = solve(grid, sec, rho, &crew(1.0), patch, cg)?;
    let fb = freeboard(grid, &service);
    let max_paddler =
        case.weights_lb.iter().cloned().fold(f64::NEG_INFINITY, f64::max) * mat.dynamic_load_factor;
    let mut best: Option<(Loads, Verdict, (f64, f64))> = None;
    for &(dead, live) in mat.method.combos() {
        let loads = if (dead, live) == (1.0, 1.0) {
            service.clone()
        } else {
            solve(grid, sec, rho * dead, &crew(live), patch, cg)?
        };
        let mut v = check(
            grid,
            sec,
            &loads,
            rho,
            mat,
            fit,
            max_paddler * live,
            &params.punching,
            params.turning.as_ref(),
        );
        v.freeboard_in = fb;
        if best.as_ref().is_none_or(|(_, b, _)| v.dc.is_nan() || v.dc > b.dc) {
            best = Some((loads, v, (dead, live)));
        }
    }
    let (strength, verdict, combo) = best.ok_or_else(|| anyhow::anyhow!("no load combinations"))?;
    Ok(CaseEval { service, strength, verdict, combo })
}

/// Seats of every load case on this wall (`params.load_cases` order): given,
/// even, or auto (src/seats.rs, scored with [`eval_case`]).
pub fn plan_seats(
    params: &Params,
    fit: &StrengthFit,
    grid: &Grid,
    sec: &Sections,
    t: f64,
    rho: f64,
) -> Result<Vec<CaseSeats>> {
    params
        .load_cases
        .iter()
        .map(|case| match seats::fixed(params, case, grid) {
            Some(s) => Ok(s),
            None => {
                let exact = |s: &[f64]| {
                    let e = eval_case(params, fit, grid, sec, rho, case, s)?;
                    Ok(seats::score(params, &e.verdict, e.service.gm_in))
                };
                seats::auto(params, fit, grid, sec, t, rho, case, &exact)
                    .map_err(|e| anyhow::anyhow!("{}: {e:#}", case.name))
            }
        })
        .collect()
}

/// A load case checked over its seat samples (`seats::samples`: nominal,
/// then the +/- `seats.variance_in` corners).
pub struct CaseEnvelope {
    /// At the nominal seats.
    pub nominal: CaseEval,
    /// Highest-D/C sample's verdict, freeboard = min over the samples.
    pub verdict: Verdict,
    /// Min GM_T over the samples.
    pub gm_in: f64,
    /// Seats of the sample `verdict` comes from.
    pub verdict_seats_in: Vec<f64>,
}

/// Fails a gate (search D/C, freeboard, GM)?
fn misses_gate(params: &Params, v: &Verdict, gm: f64) -> bool {
    !(v.dc <= params.gates.dc_search
        && v.freeboard_in >= params.gates.freeboard_min_in
        && gm >= params.stability.gm_min_in)
}

/// [`eval_case`] over the seat samples. `early_exit`: stop at the first
/// sample that misses a gate (the case fails either way, so the verdict is
/// then the worst sample evaluated, not necessarily the worst of all).
#[allow(clippy::too_many_arguments)]
pub fn eval_envelope(
    params: &Params,
    fit: &StrengthFit,
    grid: &Grid,
    sec: &Sections,
    rho: f64,
    case: &LoadCase,
    seats: &[f64],
    early_exit: bool,
) -> Result<CaseEnvelope> {
    let mut all = seats::samples(seats, params.seats.variance_in, grid).into_iter();
    let first = all.next().unwrap_or_default();
    let nominal = eval_case(params, fit, grid, sec, rho, case, &first)?;
    let mut env = CaseEnvelope {
        verdict: nominal.verdict.clone(),
        gm_in: nominal.service.gm_in,
        verdict_seats_in: first,
        nominal,
    };
    for s in all {
        if early_exit && misses_gate(params, &env.verdict, env.gm_in) {
            break;
        }
        let e = eval_case(params, fit, grid, sec, rho, case, &s)?;
        let fb = env.verdict.freeboard_in.min(e.verdict.freeboard_in);
        env.gm_in = env.gm_in.min(e.service.gm_in);
        if e.verdict.dc.is_nan() || e.verdict.dc > env.verdict.dc {
            (env.verdict, env.verdict_seats_in) = (e.verdict, s);
        }
        env.verdict.freeboard_in = fb;
    }
    Ok(env)
}

/// Full per-case diagrams and per-station D/C (same masks and formulas as check.rs).
#[allow(clippy::too_many_arguments)]
pub fn case_diagrams(
    params: &Params,
    fit: &StrengthFit,
    grid: &Grid,
    sec: &Sections,
    t: f64,
    rho: f64,
    case_name: &str,
    seats: &CaseSeats,
) -> Result<CaseDiagrams> {
    let case = params
        .load_cases
        .iter()
        .find(|c| c.name == case_name)
        .ok_or_else(|| anyhow::anyhow!("unknown load case {case_name}"))?;
    let n = grid.nx();
    let env = eval_envelope(params, fit, grid, sec, rho, case, &seats.seats_in, false)?;
    let CaseEval { service, strength: loads, .. } = env.nominal;
    let verdict = env.verdict;
    let cap = |name: &str| verdict.checks.iter().find(|c| c.name == name).map_or(f64::NAN, |c| c.capacity);
    let (ft_cap, fc_cap, v_cap) = (cap("bending_tension"), cap("bending_compression"), cap("shear"));
    let dc = |s: f64, c: f64| if c.is_finite() && c > 0.0 { s / c } else { f64::INFINITY };
    // check.rs gives kupfer_dc f'c times the capacity factor; compression's
    // capacity is 0.85 of that (NaN when f'c <= 0, which kupfer_dc fails).
    let fc_kupfer = fc_cap / 0.85;
    let imax = sec.inertia.iter().cloned().fold(0.0, f64::max);
    let mut out = CaseDiagrams {
        name: case.name.clone(),
        seats_in: seats.seats_in.clone(),
        crew_order: seats.crew_order.clone(),
        seat_source: Some(seats.source),
        seat_variance_in: params.seats.variance_in,
        verdict_seats_in: env.verdict_seats_in,
        weights_lb: case.weights_lb.clone(),
        draft_in: Some(service.draft_in),
        trim_in: Some(service.trim_in),
        gm_in: Some(env.gm_in),
        freeboard_in: Some(verdict.freeboard_in),
        compression_allow_psi: Some(fc_cap),
        shear_allow_psi: Some(v_cap),
        ..Default::default()
    };
    let mut tension_psi = Vec::with_capacity(n);
    let hoop = crate::check::hoop_psi(grid, &loads, t, params.turning.as_ref());
    for (i, &hoop) in hoop.iter().enumerate() {
        let ii = sec.inertia[i];
        if ii <= 1e-3 * imax {
            for v in [&mut tension_psi, &mut out.compression_psi, &mut out.shear_psi] {
                v.push(0.0);
            }
            for v in [
                &mut out.dc_bending_tension,
                &mut out.dc_bending_compression,
                &mut out.dc_shear,
                &mut out.dc_biaxial,
            ] {
                v.push(0.0);
            }
            continue;
        }
        let m = loads.moment[i];
        let (c_top, c_bot) = (grid.depth - sec.zbar[i], sec.zbar[i]);
        let (ct, cc) = crate::check::fibres(m, c_top, c_bot);
        let st = m.abs() * ct / ii;
        let sc = m.abs() * cc / ii;
        let q = first_moment_below(grid.row(i), &grid.z, t, sec.zbar[i]);
        let tau = loads.shear[i].abs() * q / (ii * 2.0 * t);
        tension_psi.push(st);
        out.compression_psi.push(sc);
        out.shear_psi.push(tau);
        out.dc_bending_tension.push(dc(st, ft_cap));
        out.dc_bending_compression.push(dc(sc, fc_cap));
        out.dc_shear.push(dc(tau, v_cap));
        out.dc_biaxial.push(crate::check::kupfer_dc(st, sc, hoop, tau, ft_cap, fc_kupfer));
    }
    let waterline_in = waterline(grid, &service);
    out.diagrams = Diagrams {
        x_in: grid.x.clone(),
        shear_lb: loads.shear,
        moment_lbin: loads.moment,
        tension_psi,
        tension_allow_psi: ft_cap,
        area_in2: sec.area.clone(),
        inertia_in4: sec.inertia.clone(),
        waterline_in,
    };
    out.verdict = Some(verdict);
    Ok(out)
}

/// One D/C series over station refinement (spacing h, h/2, h/4).
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Series {
    /// Changes below 1e-6 relative: the value at the finest grid.
    Converged(f64),
    /// Monotone and contracting with observed order p in [0.5, 4]:
    /// f3 + (f3 - f2) / (2^p - 1).
    Extrapolated { value: f64, order: f64 },
    /// Anything else (oscillating, diverging, order out of range, non-finite).
    Irregular,
}

/// Richardson extrapolation of `f` at spacings h, h/2, h/4.
pub fn richardson(f: [f64; 3]) -> Series {
    if f.iter().any(|v| !v.is_finite()) {
        return Series::Irregular;
    }
    let (d1, d2) = (f[1] - f[0], f[2] - f[1]);
    let tol = 1e-6 * f[2].abs().max(1e-3);
    if d2.abs() <= tol && (d1.abs() <= tol || d1.abs() > 4.0 * d2.abs()) {
        return Series::Converged(f[2]);
    }
    if d1 * d2 <= 0.0 || d2.abs() >= d1.abs() {
        return Series::Irregular;
    }
    let order = (d1 / d2).log2();
    if !(0.5..=4.0).contains(&order) {
        return Series::Irregular;
    }
    Series::Extrapolated { value: f[2] + d2 / (order.exp2() - 1.0), order }
}

/// Winner D/C over station refinement (`Winner.convergence`).
#[derive(Debug, Clone, Serialize)]
pub struct Convergence {
    /// n, 2n-1, 4n-3: station spacing halves each time.
    pub stations: [usize; 3],
    /// D/C (max over load cases, checks and seat samples) at each.
    pub dc: [f64; 3],
    /// max over (case, check) series of the extrapolated (or converged)
    /// value. None = an irregular series could reach it (bound: its max + 2x
    /// its largest step); then `note` says which and the gate falls back.
    /// Irregular series below that bound are ignored and listed in `note`.
    pub extrapolated: Option<f64>,
    /// Order of the series that sets `extrapolated` (None = converged).
    pub order: Option<f64>,
    /// "case/check" of that series, or of the first irregular one.
    pub governing: Option<String>,
    pub note: Option<String>,
}

/// Every (case, check) D/C of the winner wall at n, 2n-1 and 4n-3 stations,
/// each the max over the seat samples, then [`richardson`] per series.
fn convergence(
    params: &Params,
    fit: &StrengthFit,
    hull: &Hull,
    c: &Candidate,
    seats: &[CaseSeats],
) -> Convergence {
    let n = params.hull.stations;
    let stations = [n, 2 * n - 1, 4 * n - 3];
    let mut out = Convergence {
        stations,
        dc: [f64::NAN; 3],
        extrapolated: None,
        order: None,
        governing: None,
        note: None,
    };
    // series[case][check][resolution]
    let mut series: Vec<Vec<(&'static str, [f64; 3])>> = vec![Vec::new(); params.load_cases.len()];
    for (r, &ns) in stations.iter().enumerate() {
        let grid = Grid::build_flared(hull, c.depth_in, ns, params.hull.flare_deg);
        let sec = sections(&grid, c.thickness_in);
        for (k, (case, s)) in params.load_cases.iter().zip(seats).enumerate() {
            for sample in seats::samples(&s.seats_in, params.seats.variance_in, &grid) {
                let v = match eval_case(params, fit, &grid, &sec, c.density_pcf, case, &sample) {
                    Ok(e) => e.verdict,
                    Err(e) => {
                        out.note =
                            Some(format!("{} at {ns} stations: {e:#}; verify_dc gate used", case.name));
                        return out;
                    }
                };
                for (j, ch) in v.checks.iter().enumerate() {
                    if series[k].len() <= j {
                        series[k].push((ch.name, [f64::NEG_INFINITY; 3]));
                    }
                    let e = &mut series[k][j].1[r];
                    *e = e.max(ch.dc);
                }
            }
        }
    }
    let tagged: Vec<(String, [f64; 3])> = params
        .load_cases
        .iter()
        .zip(&series)
        .flat_map(|(case, ss)| ss.iter().map(move |&(name, f)| (format!("{}/{name}", case.name), f)))
        .collect();
    extrapolate(stations, &tagged)
}

/// [`Convergence`] from tagged ("case/check") D/C series at `stations`.
pub fn extrapolate(stations: [usize; 3], series: &[(String, [f64; 3])]) -> Convergence {
    let mut out = Convergence {
        stations,
        dc: [f64::NAN; 3],
        extrapolated: None,
        order: None,
        governing: None,
        note: None,
    };
    for r in 0..3 {
        out.dc[r] = series.iter().map(|s| s.1[r]).fold(f64::NEG_INFINITY, f64::max);
    }
    let mut best: Option<(f64, Option<f64>, String)> = None;
    // Irregular series: (tag, values, bound = max + 2 max|step|).
    let mut irregular: Vec<(String, [f64; 3], f64)> = Vec::new();
    for (tag, f) in series {
        let (tag, f) = (tag.clone(), *f);
        let (value, order) = match richardson(f) {
            Series::Converged(v) => (v, None),
            Series::Extrapolated { value, order } => (value, Some(order)),
            Series::Irregular => {
                let step = (f[1] - f[0]).abs().max((f[2] - f[1]).abs());
                irregular.push((tag, f, f[0].max(f[1]).max(f[2]) + 2.0 * step));
                continue;
            }
        };
        if best.as_ref().is_none_or(|b| value > b.0) {
            best = Some((value, order, tag));
        }
    }
    let top = best.as_ref().map_or(f64::NEG_INFINITY, |b| b.0);
    // An irregular series that could reach the governing value makes the
    // whole extrapolation untrustworthy; one far below it cannot govern.
    if let Some((tag, f, _)) = irregular.iter().find(|i| i.2.is_nan() || i.2 >= top) {
        out.governing = Some(tag.clone());
        out.note = Some(format!(
            "{tag} D/C {:.4} / {:.4} / {:.4} is not monotone with an order in [0.5, 4]; verify_dc gate used",
            f[0], f[1], f[2]
        ));
        return out;
    }
    if !irregular.is_empty() {
        let tags: Vec<&str> = irregular.iter().map(|i| i.0.as_str()).collect();
        out.note = Some(format!("irregular but far below the governing D/C: {}", tags.join(", ")));
    }
    if let Some((v, o, tag)) = best {
        (out.extrapolated, out.order, out.governing) = (Some(v), o, Some(tag));
    }
    out
}

/// Is `b` a better winner than `a` (both passing) under the objective?
/// Weight: lighter. Drag: less drag, ties broken by weight.
fn better(params: &Params, b: &Candidate, a: &Candidate) -> bool {
    match (params.objective.kind, b.drag_n, a.drag_n) {
        (ObjectiveKind::Drag, Some(db), Some(da)) if db != da => db < da,
        _ => b.hull_weight_lb < a.hull_weight_lb,
    }
}

/// The analysis grid as a hull table (all rows; `resistance` clips at the draft).
fn grid_hull(grid: &Grid) -> Hull {
    let nz = grid.nz();
    Hull {
        format: crate::hull::FORMAT.into(),
        units: "in".into(),
        name: "grid".into(),
        source: String::new(),
        length: grid.x[grid.nx() - 1],
        draft_design: grid.z[nz - 1],
        x: grid.x.clone(),
        z: grid.z.clone(),
        half_breadth: (0..grid.nx()).map(|i| grid.row(i).to_vec()).collect(),
    }
}

/// Self weight in lb: trapezoid of A*rho/1728 over x.
fn hull_weight(grid: &Grid, sec: &Sections, rho: f64) -> f64 {
    let w: f64 =
        grid.x.windows(2).zip(sec.area.windows(2)).map(|(x, a)| 0.5 * (a[0] + a[1]) * (x[1] - x[0])).sum();
    w * rho / 1728.0
}

/// One candidate over every load case at its planned seats (`plan`, Err = the
/// seats could not be resolved: the candidate fails with that reason).
#[allow(clippy::too_many_arguments)]
fn evaluate(
    params: &Params,
    fit: &StrengthFit,
    grid: &Grid,
    sec: &Sections,
    t: f64,
    rho: f64,
    plan: &Result<Vec<CaseSeats>, String>,
    early_exit: bool,
) -> Candidate {
    let beam = 2.0 * grid.y.iter().cloned().fold(0.0, f64::max);
    let mut c = Candidate {
        length_in: grid.x[grid.x.len() - 1] - grid.x[0],
        beam_in: beam,
        shape: None,
        gm_in: None,
        drag_n: None,
        air_pocket_in: None,
        thickness_in: t,
        density_pcf: rho,
        depth_in: grid.depth,
        hull_weight_lb: hull_weight(grid, sec, rho),
        mix: None,
        verdict: None,
        governing_case: None,
        error: None,
        passes: false,
    };
    let mut worst: Option<(Verdict, String)> = None;
    let (mut gm, mut fb) = (f64::INFINITY, f64::INFINITY);
    // (crew lb, midship draft) of the heaviest load case, for drag
    let mut heaviest: Option<(f64, f64)> = None;
    let plan = match plan {
        Ok(p) => p,
        Err(e) => {
            c.error = Some(format!("seats: {e}"));
            return c;
        }
    };
    for (case, seats) in params.load_cases.iter().zip(plan) {
        let v = match eval_envelope(params, fit, grid, sec, rho, case, &seats.seats_in, early_exit) {
            Ok(env) => {
                gm = gm.min(env.gm_in);
                let lb: f64 = case.weights_lb.iter().sum();
                if heaviest.is_none_or(|(h, _)| lb > h) {
                    heaviest = Some((lb, env.nominal.service.draft_in));
                }
                env.verdict
            }
            Err(e) => {
                c.error = Some(format!("{}: {e:#}", case.name));
                return c;
            }
        };
        // +inf D/C is a legitimate FAIL (a check had no capacity); NaN is a bug.
        if v.dc.is_nan() || !v.freeboard_in.is_finite() {
            c.error = Some(format!("{}: non-finite check result", case.name));
            return c;
        }
        fb = fb.min(v.freeboard_in);
        if worst.as_ref().is_none_or(|(w, _)| v.dc > w.dc) {
            worst = Some((v, case.name.clone()));
        }
    }
    if let Some((mut v, name)) = worst {
        // The freeboard gate covers every case, not just the governing one.
        v.freeboard_in = fb;
        c.gm_in = Some(gm);
        if params.objective.kind == ObjectiveKind::Drag {
            let draft = heaviest.map(|h| h.1).unwrap_or(f64::NAN);
            let hull = grid_hull(grid);
            let speed = params.objective.speed_mps;
            let drag = match &params.objective.displacements {
                None => resistance(&hull, draft, speed).map(|r| r.total_n),
                Some(ds) => weighted_resistance(&hull, c.hull_weight_lb, ds, speed),
            };
            match drag {
                Ok(r) => c.drag_n = Some(r),
                Err(e) => {
                    c.error = Some(format!("drag: {e:#}"));
                    c.verdict = Some(v);
                    c.governing_case = Some(name);
                    return c;
                }
            }
        }
        if let Some(fp) = &params.flotation {
            match crate::flotation::flotation(grid, sec, rho, fp.swamp_factor, fp.air_pocket_max_in) {
                Ok(f) => c.air_pocket_in = Some(f.pocket_len_in),
                Err(e) => {
                    c.error = Some(format!("{e:#}"));
                    c.verdict = Some(v);
                    c.governing_case = Some(name);
                    return c;
                }
            }
        }
        c.passes = v.dc <= params.gates.dc_search
            && v.freeboard_in >= params.gates.freeboard_min_in
            && gm >= params.stability.gm_min_in;
        c.verdict = Some(v);
        c.governing_case = Some(name);
    } else {
        c.error = Some("no load cases".into());
    }
    c
}
