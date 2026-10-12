//! Thin-shell cross-section properties per station. PLAN task T2.
//!
//! Model (same as the prototype, golden-tested): the hull wall is a shell of
//! uniform thickness `t` following the half-breadth polyline on both sides,
//! plus a flat keel strip of width `2*y[0]`. Per station:
//! * `area`  = sum over segments of 2*t*ds  + 2*y0*t
//! * `zbar`  = area-weighted centroid height above the keel
//! * `inertia` = sum 2*t*ds*((zm - zbar)^2 + dz^2/12) + keel_strip*zbar^2
//!
//! Hot path: called once per (thickness, depth) per candidate. No allocation
//! per station beyond the output vectors.

use crate::hull::Grid;

#[derive(Debug, Clone)]
pub struct Sections {
    /// Wall cross-section area, in^2, per station.
    pub area: Vec<f64>,
    /// Neutral axis height above keel, in, per station.
    pub zbar: Vec<f64>,
    /// Second moment of area about the neutral axis, in^4, per station.
    pub inertia: Vec<f64>,
}

pub fn sections(grid: &Grid, thickness_in: f64) -> Sections {
    let _ = (grid, thickness_in);
    todo!("T2: port section() from the prototype; see docs/PORTING.md")
}
