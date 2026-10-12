//! Free-floating beam equilibrium: hull weight + crew vs buoyancy. PLAN task T3.
//!
//! Solve for the waterline (draft `d0` at midship and trim `theta`, the
//! change in draft over the length) such that total buoyancy equals total
//! weight AND the buoyancy moment about midship equals the weight moment.
//! Then integrate the net load q(x) = buoyancy - self weight - crew twice to
//! get shear V(x) and moment M(x). A free-free beam must close: V(L) ~ 0 and
//! M(L) ~ 0, otherwise return `Err` (never a plausible wrong number).
//!
//! Crew load: each paddler is `weight * dynamic_load_factor` spread uniformly
//! over a `knee_patch_in` patch centred on the seat, NEVER a point load.
//!
//! Errors (all must be `Err`, never a sentinel value):
//! * hull sinks (weight > buoyancy at full depth)
//! * trim balance not bracketed
//! * equilibrium does not close

use crate::hull::Grid;
use crate::section::Sections;
use anyhow::Result;

/// One crew load: weight already multiplied by the dynamic factor.
#[derive(Debug, Clone, Copy)]
pub struct CrewLoad {
    pub x_in: f64,
    pub lb: f64,
}

#[derive(Debug, Clone)]
pub struct Loads {
    /// Hull self weight, lb.
    pub hull_weight_lb: f64,
    /// Midship draft, in.
    pub draft_in: f64,
    /// Draft change bow->stern over the length, in.
    pub trim_in: f64,
    /// Shear, lb, per station.
    pub shear: Vec<f64>,
    /// Bending moment, lb*in, per station. Positive = sagging.
    pub moment: Vec<f64>,
}

pub fn solve(
    grid: &Grid,
    sec: &Sections,
    density_pcf: f64,
    crew: &[CrewLoad],
    knee_patch_in: f64,
) -> Result<Loads> {
    let _ = (grid, sec, density_pcf, crew, knee_patch_in);
    todo!("T3: port moments() from the prototype; see docs/PORTING.md")
}
