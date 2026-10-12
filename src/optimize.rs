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
use crate::params::{Params, range};
use crate::section::{Sections, sections};
use anyhow::Result;
use rayon::prelude::*;
use serde::Serialize;
use std::sync::atomic::{AtomicUsize, Ordering};

#[derive(Debug, Clone, Serialize)]
pub struct Candidate {
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
}

#[derive(Debug, Clone, Serialize)]
pub struct Winner {
    pub candidate: Candidate,
    pub verify_stations: usize,
    pub verify_dc: f64,
    pub verified: bool,
    pub on_grid_edge: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct RunResult {
    pub format: &'static str,
    pub params: Params,
    pub candidates: Vec<Candidate>,
    /// None = no design passed. Never a fallback "best failing" design.
    pub winner: Option<Winner>,
}

pub const RESULT_FORMAT: &str = "jaguar-result/1";

/// Progress events, emitted in order. The web server streams these to the
/// browser; the CLI ignores them. Must be cheap and thread-safe to call.
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "event", rename_all = "snake_case")]
pub enum Progress {
    /// Search started with `total` candidates.
    Started { total: usize },
    /// One candidate finished (any order, from any thread). `done` counts up to `total`.
    Candidate { done: usize, total: usize, candidate: Candidate },
    /// Winner is being re-checked at `stations`.
    Verifying { stations: usize },
}

/// CLI entry: no progress reporting.
pub fn run(params: &Params, hull: &Hull) -> Result<RunResult> {
    run_with_progress(params, hull, &|_| {})
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
    let rhos = range(params.search.density_pcf);
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
                    let c = evaluate(params, grid, &sec, t, rho);
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
        let v = evaluate(params, &grid, &sec, c.thickness_in, c.density_pcf);
        let verify_dc = v.verdict.as_ref().map_or(f64::MAX, |v| v.dc);
        let edge = |x: f64, r: &[f64]| r.len() > 1 && (x == r[0] || x == r[r.len() - 1]);
        Winner {
            candidate: c.clone(),
            verify_stations: stations,
            verify_dc,
            verified: v.verdict.is_some() && verify_dc <= params.gates.dc_verify,
            // Thickness at the buildable floor is a real constraint, not a range problem.
            on_grid_edge: (edge(c.thickness_in, &ts) && (c.thickness_in - floor).abs() > 1e-9)
                || edge(c.density_pcf, &rhos)
                || edge(c.depth_in, &ds),
        }
    });

    Ok(RunResult { format: RESULT_FORMAT, params: params.clone(), candidates, winner })
}

/// Self weight in lb: trapezoid of A*rho/1728 over x.
fn hull_weight(grid: &Grid, sec: &Sections, rho: f64) -> f64 {
    let w: f64 =
        grid.x.windows(2).zip(sec.area.windows(2)).map(|(x, a)| 0.5 * (a[0] + a[1]) * (x[1] - x[0])).sum();
    w * rho / 1728.0
}

fn evaluate(params: &Params, grid: &Grid, sec: &Sections, t: f64, rho: f64) -> Candidate {
    let mut c = Candidate {
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
            Ok(loads) => check(grid, sec, &loads, rho, mat),
            Err(e) => {
                c.error = Some(format!("{}: {e:#}", case.name));
                return c;
            }
        };
        if !v.dc.is_finite() || !v.freeboard_in.is_finite() {
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
