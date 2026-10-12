//! The search. PLAN task T5.
//!
//! 1. Expand `search.{thickness,density,depth}` into a grid of candidates.
//! 2. Evaluate every candidate against every load case in parallel (rayon).
//!    A candidate's D/C is the max over load cases. An `Err` from
//!    equilibrium marks that candidate failed with its reason; it does not
//!    abort the run.
//! 3. Winner = lightest candidate with dc <= gates.dc_search and
//!    freeboard >= gates.freeboard_min_in.
//! 4. Re-evaluate the winner at 2n-1 stations. `verified` = dc <= dc_verify.
//! 5. Flag `on_grid_edge` if any winning coordinate is a search bound.
//!
//! Must stay deterministic: same canoe.toml => byte-identical result.json.

use crate::check::{Verdict, check};
use crate::equilibrium::{CrewLoad, solve};
use crate::hull::{Grid, Hull};
use crate::hullgen::ShapeParams;
use crate::mix::StrengthFit;
use crate::params::{Params, range};
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
    pub density_pcf: f64,
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
    /// None until computed (T12) or if analysis failed. Gate: gm >= stability.gm_min_in.
    pub gm_in: Option<f64>,
    /// Total resistance at objective.speed_mps, N (wave + ITTC friction), worst
    /// (heaviest) load case. None unless objective.kind = drag (T12).
    pub drag_n: Option<f64>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Winner {
    pub candidate: Candidate,
    pub verify_stations: usize,
    pub verify_dc: f64,
    pub verified: bool,
    pub on_grid_edge: bool,
    /// Winner density is at the edge of the range the mix data supports
    /// (lowest/highest tested density -/+ density_extrapolation_pcf). Not a
    /// search-range problem: the team needs lab data for lighter/heavier mixes.
    pub at_mix_data_edge: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct RunResult {
    pub format: &'static str,
    pub params: Params,
    /// The strength fit actually used (from material.mix_data).
    pub strength: StrengthFit,
    /// Winning hull geometry (jaguar-hull/1), so viewers need not regenerate it.
    pub winner_hull: Option<Hull>,
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
    /// Shape search: shape `index` of `shapes` is being evaluated (T13).
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

/// Entry point for callers holding only Params: loads `hull.file`, or runs the
/// shape search when `[shape_search]` is present (T13: `crate::shapeopt::run`).
pub fn run_params(params: &Params, on_progress: &(dyn Fn(Progress) + Sync)) -> Result<RunResult> {
    match (&params.hull.file, &params.shape_search) {
        (Some(f), _) => run_with_progress(params, &Hull::load(f)?, on_progress),
        (None, Some(_)) => anyhow::bail!("shape search not implemented yet (T13)"),
        (None, None) => anyhow::bail!("give either hull.file or [shape_search]"),
    }
}

/// Load and fit the mix data, and the density range the fit may be used on.
pub fn strength(params: &Params) -> Result<StrengthFit> {
    StrengthFit::load(&params.material.mix_data, params.material.flex_over_split)
}

/// Densities from search.density_pcf clipped to the trusted fit range
/// [min tested - extrapolation, max tested + extrapolation]. Err if empty.
pub fn densities(params: &Params, fit: &StrengthFit) -> Result<Vec<f64>> {
    let e = params.material.density_extrapolation_pcf;
    let (lo, hi) = (fit.density_min_pcf - e, fit.density_max_pcf + e);
    let v: Vec<f64> =
        range(params.search.density_pcf).into_iter().filter(|&r| r >= lo - 1e-9 && r <= hi + 1e-9).collect();
    anyhow::ensure!(
        !v.is_empty(),
        "no searched density lies within the mix data range {lo:.1}..{hi:.1} pcf (tested {:.1}..{:.1} \
         +/- density_extrapolation_pcf {e}); widen search.density_pcf or add lab data",
        fit.density_min_pcf,
        fit.density_max_pcf
    );
    Ok(v)
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
    let floor = params.gates.thickness_min_buildable_in;
    let ts: Vec<f64> = range(params.search.thickness_in).into_iter().filter(|&t| t >= floor - 1e-9).collect();
    let fit = strength(params)?;
    let rhos = densities(params, &fit)?;
    let ds = range(params.search.depth_in);
    let total = ts.len() * rhos.len() * ds.len();
    on_progress(Progress::Started { total });
    let n = params.hull.stations;
    let done = AtomicUsize::new(0);

    let grids: Vec<Grid> = ds.par_iter().map(|&d| Grid::build(hull, d, n)).collect();
    let pairs: Vec<(usize, usize)> =
        (0..grids.len()).flat_map(|g| (0..ts.len()).map(move |t| (g, t))).collect();
    let mut candidates: Vec<(usize, usize, usize, Candidate)> = pairs
        .par_iter()
        .flat_map_iter(|&(gi, ti)| {
            let grid = &grids[gi];
            let t = ts[ti];
            let sec = sections(grid, t);
            let done = &done;
            rhos.iter()
                .enumerate()
                .map(|(ri, &rho)| {
                    let c = evaluate(params, &fit, grid, &sec, t, rho);
                    let k = done.fetch_add(1, Ordering::SeqCst) + 1;
                    on_progress(Progress::Candidate { done: k, total, candidate: c.clone() });
                    (ti, ri, gi, c)
                })
                .collect::<Vec<_>>()
        })
        .collect();
    candidates.sort_by_key(|&(t, r, d, _)| (t, r, d));
    let candidates: Vec<Candidate> = candidates.into_iter().map(|x| x.3).collect();

    let best = candidates
        .iter()
        .filter(|c| c.passes)
        .reduce(|a, b| if b.hull_weight_lb < a.hull_weight_lb { b } else { a });

    let winner = best.map(|c| {
        let stations = 2 * n - 1;
        on_progress(Progress::Verifying { stations });
        let grid = Grid::build(hull, c.depth_in, stations);
        let sec = sections(&grid, c.thickness_in);
        let v = evaluate(params, &fit, &grid, &sec, c.thickness_in, c.density_pcf);
        let verify_dc = v.verdict.as_ref().map_or(f64::MAX, |v| v.dc);
        let edge = |x: f64, r: &[f64]| r.len() > 1 && (x == r[0] || x == r[r.len() - 1]);
        // Density at the end of the trusted fit range, where the full search range
        // extended further: the lab data, not the search range, is the limit.
        let full = range(params.search.density_pcf);
        let mix_edge = |x: f64| {
            (x == rhos[0] && full[0] < rhos[0] - 1e-9)
                || (x == rhos[rhos.len() - 1] && full[full.len() - 1] > rhos[rhos.len() - 1] + 1e-9)
        };
        Winner {
            candidate: c.clone(),
            verify_stations: stations,
            verify_dc,
            verified: v.verdict.is_some() && verify_dc <= params.gates.dc_verify,
            // Thickness at the buildable floor is a real constraint, not a range problem.
            on_grid_edge: (edge(c.thickness_in, &ts) && (c.thickness_in - floor).abs() > 1e-9)
                || (edge(c.density_pcf, &rhos) && !mix_edge(c.density_pcf))
                || edge(c.depth_in, &ds),
            at_mix_data_edge: mix_edge(c.density_pcf),
        }
    });

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

/// Self weight in lb: trapezoid of A*rho/1728 over x.
fn hull_weight(grid: &Grid, sec: &Sections, rho: f64) -> f64 {
    let w: f64 =
        grid.x.windows(2).zip(sec.area.windows(2)).map(|(x, a)| 0.5 * (a[0] + a[1]) * (x[1] - x[0])).sum();
    w * rho / 1728.0
}

fn evaluate(params: &Params, fit: &StrengthFit, grid: &Grid, sec: &Sections, t: f64, rho: f64) -> Candidate {
    let beam = 2.0 * grid.y.iter().cloned().fold(0.0, f64::max);
    let mut c = Candidate {
        length_in: grid.x[grid.x.len() - 1] - grid.x[0],
        beam_in: beam,
        shape: None,
        gm_in: None,
        drag_n: None,
        thickness_in: t,
        density_pcf: rho,
        depth_in: grid.depth,
        hull_weight_lb: hull_weight(grid, sec, rho),
        verdict: None,
        governing_case: None,
        error: None,
        passes: false,
    };
    let mat = &params.material;
    let length = grid.x[grid.x.len() - 1] - grid.x[0];
    let mut worst: Option<(Verdict, String)> = None;
    for case in &params.load_cases {
        let max_paddler =
            case.weights_lb.iter().cloned().fold(f64::NEG_INFINITY, f64::max) * mat.dynamic_load_factor;
        let k = case.weights_lb.len();
        let crew: Vec<CrewLoad> = case
            .weights_lb
            .iter()
            .enumerate()
            .map(|(i, &w)| CrewLoad {
                x_in: match &case.seats_in {
                    Some(s) => s[i],
                    None => grid.x[0] + length * (i + 1) as f64 / (k + 1) as f64,
                },
                lb: w * mat.dynamic_load_factor,
            })
            .collect();
        let v = match solve(grid, sec, rho, &crew, mat.knee_patch_in) {
            Ok(loads) => check(grid, sec, &loads, rho, mat, fit, max_paddler),
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
        if worst.as_ref().is_none_or(|(w, _)| v.dc > w.dc) {
            worst = Some((v, case.name.clone()));
        }
    }
    if let Some((v, name)) = worst {
        c.passes = v.dc <= params.gates.dc_search && v.freeboard_in >= params.gates.freeboard_min_in;
        c.verdict = Some(v);
        c.governing_case = Some(name);
    } else {
        c.error = Some("no load cases".into());
    }
    c
}
