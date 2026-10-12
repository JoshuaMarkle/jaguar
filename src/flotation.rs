//! Swamped flotation (ASCE: the canoe must float when swamped).
//!
//! Swamped, the concrete wall (fully submerged) and sealed bow/stern air pockets
//! provide all buoyancy. Need (V_wall + V_pockets) * 62.4/1728 >= swamp_factor * W,
//! W = canoe self weight (no crew). Each pocket runs from a tip inward a length l
//! (same l both ends) and fills the hull interior up to the mold depth. The interior
//! area per station is 2 * integral of max(y - t, 0) dz (inner half-breadth = outer - t)
//! minus the bottom-shell strip 2 * (y_keel - t) * t.
//! Pure: no I/O.

use crate::hull::Grid;
use crate::section::Sections;
use crate::units::WATER_PCF;
use anyhow::{Result, bail};

#[derive(Debug, Clone, Copy)]
pub struct Flotation {
    /// Concrete wall volume, in^3.
    pub wall_in3: f64,
    /// Canoe self weight, lb.
    pub weight_lb: f64,
    /// Total pocket volume needed (both ends), in^3. 0 if the wall alone floats.
    pub pocket_in3: f64,
    /// Pocket length needed at EACH end, in.
    pub pocket_len_in: f64,
}

fn trapz(x: &[f64], f: &[f64]) -> f64 {
    x.windows(2).zip(f.windows(2)).map(|(x, a)| 0.5 * (a[0] + a[1]) * (x[1] - x[0])).sum()
}

/// Interior (air-tight) area of each station, in^2.
fn interior_area(grid: &Grid, t: f64) -> Vec<f64> {
    (0..grid.nx())
        .map(|i| {
            let w: Vec<f64> = grid.row(i).iter().map(|&y| 2.0 * (y - t).max(0.0)).collect();
            // the bottom shell occupies z in [0, t]: remove that strip
            trapz(&grid.z, &w) - w[0] * t
        })
        .collect()
}

/// Integral of piecewise-linear `a` over [x0, x0 + l] (forward) where x is increasing.
fn integrate_to(x: &[f64], a: &[f64], from: f64, to: f64) -> f64 {
    let at = |s: f64| -> f64 {
        let j = x.partition_point(|&v| v <= s).clamp(1, x.len() - 1);
        let r = (s - x[j - 1]) / (x[j] - x[j - 1]);
        a[j - 1] + r * (a[j] - a[j - 1])
    };
    let mut pts = vec![from];
    pts.extend(x.iter().cloned().filter(|&v| v > from && v < to));
    pts.push(to);
    pts.windows(2).map(|p| 0.5 * (at(p[0]) + at(p[1])) * (p[1] - p[0])).sum()
}

/// Pocket length needed at each end. Errors (a FAIL reason) if no pocket up to
/// half the hull suffices or the length exceeds `max_len_in`.
pub fn flotation(
    grid: &Grid,
    sec: &Sections,
    t: f64,
    rho: f64,
    swamp_factor: f64,
    max_len_in: f64,
) -> Result<Flotation> {
    let wall = trapz(&grid.x, &sec.area);
    let weight = wall * rho / 1728.0;
    let need = (swamp_factor * weight * 1728.0 / WATER_PCF - wall).max(0.0);
    let (x0, xn) = (grid.x[0], grid.x[grid.nx() - 1]);
    let ai = interior_area(grid, t);
    let vol = |l: f64| integrate_to(&grid.x, &ai, x0, x0 + l) + integrate_to(&grid.x, &ai, xn - l, xn);
    let mut len = 0.0;
    if need > 0.0 {
        let half = 0.5 * (xn - x0);
        if vol(half) < need {
            bail!("swamped: pockets cannot float the canoe even filling the whole hull");
        }
        let (mut lo, mut hi) = (0.0, half);
        for _ in 0..100 {
            let mid = 0.5 * (lo + hi);
            if vol(mid) < need { lo = mid } else { hi = mid }
        }
        len = hi;
    }
    if len > max_len_in {
        bail!("swamped: needs {len:.1} in air pocket each end > air_pocket_max_in {max_len_in}");
    }
    Ok(Flotation { wall_in3: wall, weight_lb: weight, pocket_in3: need, pocket_len_in: len })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::section::sections;

    /// Box hull: half-breadth 10, depth 10, length 200, t = 0.5, rho = 100.
    fn box_grid() -> Grid {
        let x: Vec<f64> = (0..=40).map(|i| i as f64 * 5.0).collect();
        let z: Vec<f64> = (0..=10).map(|k| k as f64).collect();
        let y = vec![10.0; x.len() * z.len()];
        Grid { x, z, y, depth: 10.0 }
    }

    #[test]
    fn box_pocket_length_hand_computed() {
        let g = box_grid();
        let s = sections(&g, 0.5);
        let f = flotation(&g, &s, 0.5, 100.0, 1.1, 60.0).unwrap();
        // section area = 2*10*0.5 (keel) + 2*0.5*10 (sides) = 20 in^2; wall = 4000 in^3
        // weight = 4000*100/1728; need = 1.1*4000*100/62.4 - 4000 = 3051.28 in^3
        // interior area = 2*9.5*10 - 2*9.5*0.5 = 180.5 in^2, two ends: l = need / 361
        let need = 1.1 * 4000.0 * 100.0 / 62.4 - 4000.0;
        assert!((f.wall_in3 - 4000.0).abs() < 1e-9);
        assert!((f.pocket_in3 - need).abs() < 1e-6);
        assert!((f.pocket_len_in - need / 361.0).abs() < 1e-6, "{}", f.pocket_len_in);
    }

    #[test]
    fn too_long_pocket_fails_with_reason() {
        let g = box_grid();
        let s = sections(&g, 0.5);
        let e = flotation(&g, &s, 0.5, 100.0, 1.1, 5.0).unwrap_err().to_string();
        assert!(e.contains("air_pocket_max_in"), "{e}");
        let e = flotation(&g, &s, 0.5, 100.0, 20.0, 1e9).unwrap_err().to_string();
        assert!(e.contains("whole hull"), "{e}");
    }

    #[test]
    fn light_mix_needs_no_pocket() {
        let g = box_grid();
        let s = sections(&g, 0.5);
        let f = flotation(&g, &s, 0.5, 50.0, 1.1, 0.0).unwrap();
        assert_eq!(f.pocket_len_in, 0.0);
    }
}
