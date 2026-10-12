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

use crate::check::Verdict;
use crate::hull::Hull;
use crate::params::Params;
use anyhow::Result;
use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
pub struct Candidate {
    pub thickness_in: f64,
    pub density_pcf: f64,
    pub depth_in: f64,
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

pub fn run(params: &Params, hull: &Hull) -> Result<RunResult> {
    let _ = (params, hull);
    todo!("T5: grid search + verify; see module docs")
}
