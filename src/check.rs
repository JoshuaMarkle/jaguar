//! Pass/fail checks on one analysed design. PLAN task T4.
//!
//! * Bending: sigma = |M| * c / I with c = max(zbar, depth - zbar), at
//!   stations where I > 1e-3 * max(I) (the pointed tips carry no moment).
//!   Capacity = (ft_slope * density + ft_intercept) / factor_of_safety.
//!   D/C = max sigma / capacity. Capacity <= 0 => D/C = +inf (fails).
//! * Freeboard: depth - (local draft) at the worst station >= freeboard_min.
//!
//! A check that cannot be computed FAILS. Never default, never skip.

use crate::equilibrium::Loads;
use crate::hull::Grid;
use crate::params::Material;
use crate::section::Sections;
use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
pub struct Verdict {
    /// Governing bending demand / capacity.
    pub dc: f64,
    /// Station of the governing D/C, in from bow.
    pub x_governing_in: f64,
    /// Minimum loaded freeboard, in.
    pub freeboard_in: f64,
}

pub fn check(grid: &Grid, sec: &Sections, loads: &Loads, density_pcf: f64, mat: &Material) -> Verdict {
    let _ = (grid, sec, loads, density_pcf, mat);
    todo!("T4: port the stress check from evaluate() in the prototype")
}
