//! Hull-shape optimization (outer loop). Owner: T13. Plan: docs/SHAPE_PLAN.md.
//!
//! For each candidate shape (a `hullgen::ShapeParams` inside the
//! `[shape_search]` bounds), generate the hull and run the existing wall search
//! (`optimize::run_with_progress`, the inner loop) on it. A shape's score is its
//! inner winner's objective value (weight, or drag); shapes with no passing wall
//! design score +inf. Deterministic for a given seed.

use crate::optimize::{Progress, RunResult};
use crate::params::Params;
use anyhow::Result;

pub fn run(params: &Params, on_progress: &(dyn Fn(Progress) + Sync)) -> Result<RunResult> {
    let _ = (params, on_progress);
    anyhow::bail!("shape search not implemented yet (T13)")
}
