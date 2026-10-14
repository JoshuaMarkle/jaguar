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

/// A side-view rectangle of dense mix: x from the bow, z above the table keel (z = 0), in.
/// It covers the whole girth between those heights, both sides.
#[derive(Debug, Clone, Copy, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct Rect {
    pub x0_in: f64,
    pub x1_in: f64,
    pub z0_in: f64,
    pub z1_in: f64,
}

impl Rect {
    pub fn has_x(&self, x: f64) -> bool {
        x >= self.x0_in - 1e-9 && x <= self.x1_in + 1e-9
    }
}

/// Two-mix wall (docs/SPEC.md): the dense mix inside the side-view rectangles `dense`
/// (non-overlapping), the light mix everywhere else.
#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct ZoneLayout {
    pub light_pcf: f64,
    pub dense_pcf: f64,
    pub dense: Vec<Rect>,
}

impl ZoneLayout {
    /// Is the wall at (x, z) dense?
    pub fn dense_at(&self, x: f64, z: f64) -> bool {
        self.dense.iter().any(|r| r.has_x(x) && z >= r.z0_in - 1e-12 && z <= r.z1_in + 1e-12)
    }
    /// Is the whole of x, z0..z1 dense?
    pub fn covers(&self, x: f64, z0: f64, z1: f64) -> bool {
        let mut iv: Vec<(f64, f64)> =
            self.dense.iter().filter(|r| r.has_x(x)).map(|r| (r.z0_in, r.z1_in)).collect();
        iv.sort_by(|a, b| a.0.total_cmp(&b.0));
        let mut reach = z0;
        for (a, b) in iv {
            if a > reach + 1e-9 {
                break;
            }
            reach = reach.max(b);
        }
        reach >= z1 - 1e-9
    }
}

/// An x slice (x0, x1) and its dense z-intervals.
type Slice = (f64, f64, Vec<(f64, f64)>);

/// `rects` as non-overlapping rectangles covering the same union: cut at every x edge, take
/// the z-union in each slice, and join neighbouring slices with the same z-intervals.
pub fn normalize(rects: &[Rect]) -> Vec<Rect> {
    let mut xs: Vec<f64> = rects.iter().flat_map(|r| [r.x0_in, r.x1_in]).collect();
    xs.sort_by(f64::total_cmp);
    xs.dedup_by(|a, b| (*a - *b).abs() < 1e-9);
    // (x0, x1, z-intervals) per slice
    let mut slices: Vec<Slice> = Vec::new();
    for w in xs.windows(2) {
        let xm = 0.5 * (w[0] + w[1]);
        let mut iv: Vec<(f64, f64)> =
            rects.iter().filter(|r| r.x0_in < xm && r.x1_in > xm).map(|r| (r.z0_in, r.z1_in)).collect();
        iv.sort_by(|a, b| a.0.total_cmp(&b.0).then(a.1.total_cmp(&b.1)));
        let mut u: Vec<(f64, f64)> = Vec::new();
        for (a, b) in iv {
            match u.last_mut() {
                Some(l) if a <= l.1 + 1e-9 => l.1 = l.1.max(b),
                _ => u.push((a, b)),
            }
        }
        match slices.last_mut() {
            Some(l) if (l.1 - w[0]).abs() < 1e-9 && l.2 == u => l.1 = w[1],
            _ => slices.push((w[0], w[1], u)),
        }
    }
    let mut out: Vec<Rect> = slices
        .into_iter()
        .flat_map(|(x0, x1, u)| {
            u.into_iter().map(move |(z0, z1)| Rect { x0_in: x0, x1_in: x1, z0_in: z0, z1_in: z1 })
        })
        .collect();
    out.sort_by(|a, b| a.x0_in.total_cmp(&b.x0_in).then(a.z0_in.total_cmp(&b.z0_in)));
    out
}

/// Section of a zoned wall per station, with the light mix as the reference material.
#[derive(Debug, Clone)]
pub struct ZonedSec {
    /// Self weight, lb per inch of length.
    pub weight: Vec<f64>,
    /// Height of the wall's mass centroid (0 where there is no wall).
    pub zg: Vec<f64>,
    /// Transformed section (dense areas x `n`): centroid and inertia, light units.
    pub zbar: Vec<f64>,
    pub inertia: Vec<f64>,
    /// Transformed first moment of the wall below the neutral axis about it.
    pub q_na: Vec<f64>,
    /// The mix at the neutral axis is the dense one.
    pub dense_at_na: Vec<bool>,
    /// (lowest, highest) z of each mix's wall at the station; None where it has none.
    pub light_z: Vec<Option<(f64, f64)>>,
    pub dense_z: Vec<Option<(f64, f64)>>,
}

/// One piece of wall: area, z range, dense?
struct Piece {
    a: f64,
    lo: f64,
    hi: f64,
    dense: bool,
}

/// Wall pieces of station `i`: the keel strip and each girth segment, cut at the zone lines.
fn pieces(grid: &Grid, i: usize, t: f64, zl: &ZoneLayout) -> Vec<Piece> {
    let (y, z, x) = (grid.row(i), &grid.z, grid.x[i]);
    let mut out = Vec::new();
    if y[0] > 0.0 {
        out.push(Piece { a: 2.0 * y[0] * t, lo: z[0], hi: z[0], dense: zl.dense_at(x, z[0]) });
    }
    let cuts: Vec<f64> = zl.dense.iter().filter(|r| r.has_x(x)).flat_map(|r| [r.z0_in, r.z1_in]).collect();
    for k in 0..z.len() - 1 {
        let len = segment_len(y, z, k);
        if len <= 0.0 {
            continue;
        }
        let (z0, z1) = (z[k], z[k + 1]);
        let mut b: Vec<f64> = vec![z0];
        b.extend(cuts.iter().copied().filter(|c| *c > z0 && *c < z1));
        b.push(z1);
        b.sort_by(f64::total_cmp);
        for w in b.windows(2) {
            let (lo, hi) = (w[0], w[1]);
            let a = 2.0 * t * len * (hi - lo) / (z1 - z0);
            out.push(Piece { a, lo, hi, dense: zl.dense_at(x, 0.5 * (lo + hi)) });
        }
    }
    out
}

/// Zoned section of every station: wall t thick, light / dense mixes per `zl`, the dense mix
/// `n` times as stiff as the light (E ratio).
pub fn zoned_sections(grid: &Grid, t: f64, zl: &ZoneLayout, n: f64) -> ZonedSec {
    let nx = grid.nx();
    let mut s = ZonedSec {
        weight: vec![0.0; nx],
        zg: vec![0.0; nx],
        zbar: vec![0.0; nx],
        inertia: vec![0.0; nx],
        q_na: vec![0.0; nx],
        dense_at_na: vec![false; nx],
        light_z: vec![None; nx],
        dense_z: vec![None; nx],
    };
    for i in 0..nx {
        let p = pieces(grid, i, t, zl);
        let rho = |q: &Piece| if q.dense { zl.dense_pcf } else { zl.light_pcf };
        let nf = |q: &Piece| if q.dense { n } else { 1.0 };
        let ea: f64 = p.iter().map(|q| nf(q) * q.a).sum();
        if ea <= 0.0 {
            continue;
        }
        let zc = |q: &Piece| 0.5 * (q.lo + q.hi);
        let zb = p.iter().map(|q| nf(q) * q.a * zc(q)).sum::<f64>() / ea;
        let mass: f64 = p.iter().map(|q| q.a * rho(q)).sum();
        s.weight[i] = mass / 1728.0;
        s.zg[i] = p.iter().map(|q| q.a * rho(q) * zc(q)).sum::<f64>() / mass;
        s.zbar[i] = zb;
        s.inertia[i] =
            p.iter().map(|q| nf(q) * q.a * ((zc(q) - zb).powi(2) + (q.hi - q.lo).powi(2) / 12.0)).sum();
        s.q_na[i] = p
            .iter()
            .map(|q| {
                if q.hi <= zb {
                    nf(q) * q.a * (zb - zc(q))
                } else if q.lo < zb {
                    let f = (zb - q.lo) / (q.hi - q.lo);
                    nf(q) * q.a * f * (zb - 0.5 * (q.lo + zb))
                } else {
                    0.0
                }
            })
            .sum();
        s.dense_at_na[i] = zl.dense_at(grid.x[i], zb);
        for q in &p {
            let slot = if q.dense { &mut s.dense_z[i] } else { &mut s.light_z[i] };
            *slot = Some(slot.map_or((q.lo, q.hi), |(a, b)| (a.min(q.lo), b.max(q.hi))));
        }
    }
    s
}

/// Elastic modulus of a mix, psi: ACI 318-19 19.2.2.1, E = 33 w^1.5 sqrt(f'c) (w pcf, f'c psi).
pub fn modulus_psi(w_pcf: f64, fc_psi: f64) -> f64 {
    33.0 * w_pcf.powf(1.5) * fc_psi.max(0.0).sqrt()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn grid(rows: &[Vec<f64>], z: Vec<f64>) -> Grid {
        let x = (0..rows.len()).map(|i| i as f64).collect();
        Grid { x, depth: *z.last().unwrap(), z, y: rows.concat() }
    }

    fn box_grid() -> Grid {
        // Flat bottom half-width 6, vertical sides to z = 10, 3 stations at x = 0, 50, 100.
        let z: Vec<f64> = (0..=20).map(|k| k as f64 * 0.5).collect();
        let row = vec![6.0; z.len()];
        Grid { x: vec![0.0, 50.0, 100.0], z, y: [row.clone(), row.clone(), row].concat(), depth: 10.0 }
    }

    #[test]
    fn normalize_makes_disjoint_rectangles_of_the_same_union() {
        let r = |x0, x1, z0, z1| Rect { x0_in: x0, x1_in: x1, z0_in: z0, z1_in: z1 };
        // Two overlapping knee patches and a band under them.
        let n = normalize(&[r(10.0, 20.0, 0.0, 2.0), r(15.0, 30.0, 0.0, 2.0), r(0.0, 40.0, 0.0, 1.0)]);
        assert_eq!(n, vec![r(0.0, 10.0, 0.0, 1.0), r(10.0, 30.0, 0.0, 2.0), r(30.0, 40.0, 0.0, 1.0)]);
        let zl = ZoneLayout { light_pcf: 50.0, dense_pcf: 60.0, dense: n };
        assert!(zl.covers(12.0, 0.0, 2.0) && !zl.covers(5.0, 0.0, 2.0) && zl.covers(5.0, 0.0, 1.0));
        assert!(zl.dense_at(35.0, 0.5) && !zl.dense_at(35.0, 1.5) && !zl.dense_at(41.0, 0.5));
    }

    #[test]
    fn zoned_with_equal_mixes_is_the_plain_section() {
        let h = crate::hull::Hull::load(std::path::Path::new("tests/golden/off1_hull.json")).unwrap();
        let g = Grid::build(&h, 12.0, 61);
        let plain = sections(&g, 0.375);
        let zl = ZoneLayout {
            light_pcf: 58.0,
            dense_pcf: 58.0,
            dense: vec![
                Rect { x0_in: 60.0, x1_in: 180.0, z0_in: 0.0, z1_in: 2.0 },
                Rect { x0_in: 90.0, x1_in: 120.0, z0_in: 5.0, z1_in: 12.0 },
            ],
        };
        let z = zoned_sections(&g, 0.375, &zl, 1.0);
        for i in 0..g.nx() {
            assert!((z.weight[i] - plain.area[i] * 58.0 / 1728.0).abs() < 1e-12);
            if plain.area[i] > 0.0 {
                assert!((z.zbar[i] - plain.zbar[i]).abs() < 1e-9, "zbar at {i}");
                assert!((z.inertia[i] / plain.inertia[i] - 1.0).abs() < 1e-9, "I at {i}");
                let q = crate::check::first_moment_below(g.row(i), &g.z, 0.375, plain.zbar[i]);
                assert!((z.q_na[i] / q - 1.0).abs() < 1e-6, "Q at {i}: {} vs {q}", z.q_na[i]);
            }
        }
    }

    #[test]
    fn zoned_box_matches_strip_integration() {
        // Box section, t = 0.5, dense (n = 2, 1.5x density) bottom flange to z = 2 and top flange
        // from z = 8, in the span x 25..75: the middle station is zoned, the end ones light.
        let (g, t, n) = (box_grid(), 0.5, 2.0);
        let zl = ZoneLayout {
            light_pcf: 50.0,
            dense_pcf: 75.0,
            dense: vec![
                Rect { x0_in: 25.0, x1_in: 75.0, z0_in: 0.0, z1_in: 2.0 },
                Rect { x0_in: 25.0, x1_in: 75.0, z0_in: 8.0, z1_in: 10.0 },
            ],
        };
        let s = zoned_sections(&g, t, &zl, n);
        // Strips: bottom plate (z = 0, area 12 t, dense) + two walls of 1e5 strips each.
        let m = 100_000;
        let (mut ea, mut eaz, mut w, mut wz) = (12.0 * t * n, 0.0, 12.0 * t * 75.0, 0.0);
        let mut strips = vec![(12.0 * t, 0.0, true)];
        for j in 0..m {
            let zc = (j as f64 + 0.5) * 10.0 / m as f64;
            let dense = zc <= 2.0 || zc >= 8.0;
            strips.push((2.0 * t * 10.0 / m as f64, zc, dense));
        }
        for &(a, zc, d) in &strips[1..] {
            ea += if d { n } else { 1.0 } * a;
            eaz += if d { n } else { 1.0 } * a * zc;
            w += a * if d { 75.0 } else { 50.0 };
            wz += a * if d { 75.0 } else { 50.0 } * zc;
        }
        let zb = eaz / ea;
        let inertia: f64 =
            strips.iter().map(|&(a, zc, d)| if d { n } else { 1.0 } * a * (zc - zb).powi(2)).sum();
        assert!((s.zbar[1] - zb).abs() < 1e-6, "{} vs {zb}", s.zbar[1]);
        assert!((s.inertia[1] / inertia - 1.0).abs() < 1e-6, "{} vs {inertia}", s.inertia[1]);
        assert!((s.weight[1] - w / 1728.0).abs() < 1e-9 && (s.zg[1] - wz / w).abs() < 1e-6);
        assert_eq!((s.dense_z[1], s.light_z[1]), (Some((0.0, 10.0)), Some((2.0, 8.0))));
        assert!(!s.dense_at_na[1], "the NA is in the light web");
        // Outside the span: all light.
        assert_eq!(s.dense_z[0], None);
        assert!((s.weight[0] - (12.0 + 20.0) * t * 50.0 / 1728.0).abs() < 1e-9);
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
