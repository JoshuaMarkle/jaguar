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
//! winner on two stands (empty) --stands--> post-search verification (`[stands]`)
//! winner hull + wall + crews --curves--> power vs speed, righting lever vs heel (pictures)
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
pub mod curves;
pub mod equilibrium;
pub mod export;
pub mod flotation;
pub mod hull;
pub mod hullgen;
pub mod launch;
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
pub mod stands;
pub mod stepimport;
pub mod units;

use anyhow::Result;
use optimize::{Progress, RunResult};
use params::Params;

/// Run one canoe.toml: the wall search on `hull.file`, or the hull-shape
/// search when `[shape_search]` is present instead.
/// The winner then gets its post-search checks once (`optimize::verify_stands`: `[stands]`
/// and the knee-split sensitivity) and its power and GZ curves (`optimize::winner_curves`),
/// never per candidate or per shape.
pub fn run(params: &Params, on_progress: &(dyn Fn(Progress) + Sync)) -> Result<RunResult> {
    run_controlled(params, on_progress, &optimize::RunControl::default())
}

/// [`run`] that the web UI can pause and end (`optimize::RunControl`): a fixed-hull search
/// ends with Err(`optimize::RunEnded`), a shape search with the best shape so far.
pub fn run_controlled(
    params: &Params,
    on_progress: &(dyn Fn(Progress) + Sync),
    ctl: &optimize::RunControl,
) -> Result<RunResult> {
    let mut r = match (&params.hull.file, &params.shape_search) {
        (Some(f), _) => {
            let h = hull::Hull::load(f)?.prepared(&params.hull)?;
            optimize::run_controlled(params, &h, on_progress, ctl)?
        }
        (None, Some(_)) => shapeopt::run_controlled(params, on_progress, ctl)?,
        (None, None) => anyhow::bail!("give either hull.file or [shape_search]"),
    };
    optimize::verify_stands(params, &mut r);
    optimize::winner_case_calcs(params, &mut r);
    optimize::winner_curves(params, &mut r);
    // The closest failing design also lists a failed verification (stations, stands).
    if let Some(cl) = &mut r.closest
        && let Some(f) = &cl.design.verify_failure
    {
        cl.fails.push(format!("verification: {f}"));
    }
    Ok(r)
}
