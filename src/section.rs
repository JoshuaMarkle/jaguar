//! Thin-shell wall section per station: t times the mold-line girth (both sides) plus a flat
//! keel strip `2 y[0] t`. The girth follows the hull as the grid gives it: a waterline segment
//! with zero half-breadth at both ends lies below the keel (rockered bow/stern, empty stem
//! station) or is the closed stem tip, so it is not wall and counts nothing.

use crate::hull::Grid;

#[derive(Debug, Clone)]
pub struct Sections {
    pub area: Vec<f64>,
    /// Centroid height above the table keel, in. A station with no wall (area 0, e.g. the
    /// stem tips) has inertia 0 and is skipped by every check; its zbar of 0 only ever
    /// multiplies its zero weight.
    pub zbar: Vec<f64>,
    pub inertia: Vec<f64>,
}

/// Length of girth segment `k` (waterline `k` to `k + 1`) of one side, or 0 when it is not
/// wall: both ends on the centreline (below the keel, or the closed stem).
pub fn segment_len(y: &[f64], z: &[f64], k: usize) -> f64 {
    if y[k] <= 0.0 && y[k + 1] <= 0.0 {
        return 0.0;
    }
    (y[k + 1] - y[k]).hypot(z[k + 1] - z[k])
}

/// Shell area per unit thickness of one station: `2 y[0]` (keel strip) + 2 x girth.
pub fn perimeter(y: &[f64], z: &[f64]) -> f64 {
    2.0 * y[0] + (0..z.len() - 1).map(|k| 2.0 * segment_len(y, z, k)).sum::<f64>()
}

pub fn sections(grid: &Grid, t: f64) -> Sections {
    let n = grid.nx();
    let z = &grid.z;
    let (mut area, mut zbar, mut inertia) = (vec![0.0; n], vec![0.0; n], vec![0.0; n]);
    for i in 0..n {
        let y = grid.row(i);
        let a0 = 2.0 * y[0] * t;
        let (mut a, mut az) = (a0, 0.0);
        for k in 0..z.len() - 1 {
            let ai = 2.0 * t * segment_len(y, z, k);
            a += ai;
            az += ai * 0.5 * (z[k] + z[k + 1]);
        }
        let zb = if a > 0.0 { az / a } else { 0.0 };
        let mut ii = a0 * zb * zb;
        for k in 0..z.len() - 1 {
            let dz = z[k + 1] - z[k];
            let ai = 2.0 * t * segment_len(y, z, k);
            let zm = 0.5 * (z[k] + z[k + 1]);
            ii += ai * ((zm - zb).powi(2) + dz * dz / 12.0);
        }
        area[i] = a;
        zbar[i] = zb;
        inertia[i] = ii;
    }
    Sections { area, zbar, inertia }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn grid(rows: &[Vec<f64>], z: Vec<f64>) -> Grid {
        let x = (0..rows.len()).map(|i| i as f64).collect();
        Grid { x, depth: *z.last().unwrap(), z, y: rows.concat() }
    }

    #[test]
    fn rockered_station_counts_only_the_wall_above_the_keel() {
        // Box 4 wide from z = 2 to 6, nothing below z = 2 (keel lifted by rocker), t = 0.5.
        let z = vec![0.0, 1.0, 2.0, 4.0, 6.0];
        let lifted = vec![0.0, 0.0, 2.0, 2.0, 2.0];
        let g = grid(&[lifted, vec![0.0; 5]], z);
        let s = sections(&g, 0.5);
        // Segment 1 -> 2 runs from the centreline at z = 1 to y = 2 at z = 2 (the keel taken
        // linearly between waterlines), then two vertical sides of 4.
        let bottom = 2f64.hypot(1.0);
        let want = 2.0 * 0.5 * (bottom + 4.0);
        assert!((s.area[0] - want).abs() < 1e-12, "{} vs {want}", s.area[0]);
        let zb = (bottom * 1.5 + 4.0 * 4.0) / (bottom + 4.0);
        assert!((s.zbar[0] - zb).abs() < 1e-12);
        // Closed stem station: no wall at all.
        assert_eq!((s.area[1], s.inertia[1]), (0.0, 0.0));
        assert_eq!(perimeter(g.row(0), &g.z) * 0.5, s.area[0]);
    }
}
