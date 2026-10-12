//! JAGUAR: lightest concrete canoe wall that survives the crew.
//!
//! Pipeline (one direction only, each stage a pure function of the previous):
//!
//! ```text
//! canoe.toml --params--> Params
//! hull.json  --hull----> Hull --(resample, extrude to depth)--> Grid
//! Grid + thickness  --section--> Sections (A, zbar, I per station)
//! Sections + density + crew --equilibrium--> Loads (draft, trim, V(x), M(x))
//! Loads + Sections + material --check--> Verdict (D/C bending, freeboard)
//! search over (thickness, density, depth) --optimize--> RunResult
//! RunResult --report--> terminal table + result.json --serve--> localhost viz
//! ```
//!
//! Units everywhere inside the crate: inches, pounds, psi, pcf. Convert at the
//! edges (hull import) and nowhere else. See `docs/SPEC.md`.

pub mod check;
pub mod equilibrium;
pub mod hull;
pub mod hullgen;
pub mod mix;
pub mod optimize;
pub mod params;
pub mod report;
pub mod resistance;
pub mod section;
pub mod serve;
pub mod shapeopt;
pub mod units;
