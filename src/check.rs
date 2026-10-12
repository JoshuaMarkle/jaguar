use crate::equilibrium::Loads;
use crate::hull::Grid;
use crate::params::Material;
use crate::section::Sections;
use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
pub struct Verdict {
    pub dc: f64,
    pub x_governing_in: f64,
    pub freeboard_in: f64,
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
    Verdict { dc, x_governing_in: xg, freeboard_in: fb }
}
