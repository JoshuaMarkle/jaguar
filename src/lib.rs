//! JAGUAR: lightest concrete canoe wall that survives the crew.
//!
//! Pipeline (one direction only, each stage a pure function of the previous):
//!
//! ```text
//! canoe.toml --params--> Params            lab CSV --mix--> StrengthFit
//! hull.json  --hull----> Hull  (or hullgen: shape factors -> Hull)
//! Hull --(resample, extrude to depth)--> Grid --section--> Sections (A, zbar, I)
//! Sections + density + crew --equilibrium--> Loads (draft, trim, GM, V(x), M(x))
//! Loads + Sections + StrengthFit --check--> Verdict (bending, shear, punching, freeboard)
//! grid over (thickness, density, depth) --optimize--> RunResult   (+ resistance for drag)
//! optional outer loop over hull shapes --shapeopt--> RunResult
//! RunResult --report--> terminal + result.json       --serve--> localhost web UI
//! winner hull + wall --export--> STEP / STL (CAD, mold shop)
//! winner hull + weight + [race] --race--> race times, straight/turn weighting (jaguar race)
//! ```
//!
//! [`run`] is the entry point for the CLI and the web UI.
//! Units everywhere inside the crate: inches, pounds, psi, pcf (see [`units`]).

pub mod check;
pub mod equilibrium;
pub mod export;
pub mod flotation;
pub mod hull;
pub mod hullgen;
pub mod meshimport;
pub mod mix;
pub mod optimize;
pub mod params;
pub mod race;
pub mod report;
pub mod resistance;
pub mod seats;
pub mod section;
pub mod serve;
pub mod shapeopt;
pub mod units;

use anyhow::Result;
use optimize::{Progress, RunResult};
use params::Params;

/// Run one canoe.toml: the wall search on `hull.file`, or the hull-shape
/// search when `[shape_search]` is present instead.
pub fn run(params: &Params, on_progress: &(dyn Fn(Progress) + Sync)) -> Result<RunResult> {
    match (&params.hull.file, &params.shape_search) {
        (Some(f), _) => {
            let h = hull::Hull::load(f)?.prepared(&params.hull)?;
            optimize::run_with_progress(params, &h, on_progress)
        }
        (None, Some(_)) => shapeopt::run(params, on_progress),
        (None, None) => anyhow::bail!("give either hull.file or [shape_search]"),
    }
}
