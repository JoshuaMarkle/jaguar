//! Pass/fail checks on one analysed design. Owner: strength agent (T9).
//!
//! Every check produces a [`CheckResult`] with demand, capacity and D/C in the
//! same units, so the report and the web UI can list them uniformly. The
//! candidate's governing D/C (`Verdict::dc`) is the MAX over all checks.
//!
//! Checks (T9 implements the ones marked TODO; bending is the golden-tested one
//! and must keep matching `tests/golden.rs`):
//! * `bending_tension`: sigma = |M| c / I, c = max(zbar, D - zbar), stations
//!   with I > 1e-3 max I. capacity = (ft_slope*rho + ft_intercept) / FS.
//! * `bending_compression` (TODO T9): same sigma at the compression fibre vs
//!   0.85 f'c / FS (or a documented elastic allowable). Rarely governs.
//! * `shear` (TODO T9): tau = V Q / (I b) for the thin shell, b = 2t (two
//!   walls), Q at the neutral axis, vs 2 lambda sqrt(f'c) / FS (ACI one-way).
//! * `punching` (TODO T9): one knee on the hull bottom, ACI 318-19 22.6.5.2:
//!   b0 = 4 (patch + d), d = t/2, v_u = P/(b0 t) with P = heaviest paddler *
//!   dynamic factor; capacity = min of the three ACI v_c / FS. Port intent from
//!   old/structural.py::punching_shear_check (keep the min-of-three rule).
//! * Freeboard: depth - max local draft (not a D/C; gated separately).
//!
//! A check that cannot be computed FAILS (dc = +inf via Err in optimize). Never
//! default, never skip.

use crate::equilibrium::Loads;
use crate::hull::Grid;
use crate::params::Material;
use crate::section::Sections;
use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
pub struct CheckResult {
    /// Stable id: "bending_tension" | "bending_compression" | "shear" | "punching".
    pub name: &'static str,
    /// Demand and capacity in psi (stresses), capacity already divided by FS.
    pub demand: f64,
    pub capacity: f64,
    pub dc: f64,
    /// Station of the governing value, in from bow (None for local checks).
    pub x_in: Option<f64>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Verdict {
    /// Governing D/C = max over `checks`.
    pub dc: f64,
    /// Name of the governing check.
    pub governing_check: &'static str,
    /// Station of the governing bending D/C, in from bow.
    pub x_governing_in: f64,
    /// Minimum loaded freeboard, in.
    pub freeboard_in: f64,
    /// Every check that ran, in a fixed order.
    pub checks: Vec<CheckResult>,
}

pub fn check(grid: &Grid, sec: &Sections, loads: &Loads, rho: f64, mat: &Material) -> Verdict {
    let imax = sec.inertia.iter().cloned().fold(0.0, f64::max);
    let (mut smax, mut xg) = (f64::NEG_INFINITY, f64::NAN);
    for i in 0..grid.nx() {
        if sec.inertia[i] > 1e-3 * imax {
            let c = sec.zbar[i].max(grid.depth - sec.zbar[i]);
            let s = loads.moment[i].abs() * c / sec.inertia[i];
            if s > smax {
                smax = s;
                xg = grid.x[i];
            }
        }
    }
    let cap = (mat.ft_slope_psi_per_pcf * rho + mat.ft_intercept_psi) / mat.factor_of_safety;
    let dc = if cap <= 0.0 || !smax.is_finite() { f64::INFINITY } else { smax / cap };
    let l = grid.x[grid.nx() - 1];
    let fb = grid
        .x
        .iter()
        .map(|xi| grid.depth - (loads.draft_in + loads.trim_in * (xi - l / 2.0) / l))
        .fold(f64::INFINITY, f64::min);
    let bending = CheckResult { name: "bending_tension", demand: smax, capacity: cap, dc, x_in: Some(xg) };
    Verdict {
        dc,
        governing_check: "bending_tension",
        x_governing_in: xg,
        freeboard_in: fb,
        checks: vec![bending],
    }
}
