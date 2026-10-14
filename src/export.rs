//! Canoe geometry export for CAD and the mold shop: STEP (ISO 10303-21, AP214 B-rep of planar faces) and
//! binary STL. Below `report` in the layout: reads a [`Hull`], never feeds physics.
//!
//! Geometry is the same surface the analysis uses ([`Grid::build_flared`]: offsets resampled
//! along x, topsides extruded to the mold depth, flared by `flare_deg`), both sides mirrored.
//!
//! * Each station's half girth (sheer -> keel centreline) is resampled by arc length to a fixed
//!   number of points, so the surface is a regular grid even where rocker lifts the keel.
//!   Leading/trailing stations with no offsets are dropped, except one at each end which becomes
//!   the stem line. The exported length is therefore the extent of the non-zero offsets.
//! * `thickness_in = Some(t)`: a closed SOLID. The inner face is the outer girth offset inward by
//!   t in the section plane (mitred, capped at 2t). Near the bow and stern, where the offset wall
//!   would close on itself, the end is solid concrete: the inner surface stops at the first/last
//!   station with a proper hollow, a planar bulkhead closes it there, and a flat top covers the
//!   solid end. A rim closes the sheer. `None`: the outer mold surface only, as an open shell.
//! * `pocket_in = Some(l)` (with a wall): the sealed bow/stern air pockets of the swamped-flotation
//!   gate (`flotation.rs`: from each tip of the analysis grid, x0 and xn, inward l, interior up to
//!   the mold depth). A solid bulkhead one casting layer thick (b = t / `layers`, or t when the
//!   pass count is unknown) closes the interior from x0 + l to x0 + l + b and from xn - l - b to
//!   xn - l, fused into the wall, and a top cover as thick closes each pocket at the mold depth
//!   (interior from D - b up), from the solid end to the bulkhead: each pocket is a sealed void
//!   inside the one solid (STEP: BREP_WITH_VOIDS, one inner shell per pocket). Rows are inserted at those four x,
//!   linear between the neighbouring resampled stations. Err if a bulkhead would fall in the
//!   solid end or the pockets overlap.
//! * Coincident vertices are welded, zero-area and mirrored-duplicate facets removed, so a solid
//!   is watertight (every edge used by exactly two facets, in opposite directions).
//!
//! Units: inches, declared in the STEP file. Output is deterministic (fixed timestamp).

use crate::hull::{Grid, Hull};
use anyhow::{Context, Result, bail, ensure};
use std::collections::HashMap;
use std::fmt::Write as _;

/// What to export.
#[derive(Debug, Clone, PartialEq)]
pub struct ExportSpec {
    /// Mold depth (top of the topsides), in.
    pub depth_in: f64,
    /// Wall thickness, in. `None` = outer mold surface only.
    pub thickness_in: Option<f64>,
    /// Topside flare, deg from vertical (as `hull.flare_deg`).
    pub flare_deg: f64,
    /// Line-arc-line gunwale radius, in (as `hull.gunwale_radius_in`); None = flared extrusion.
    pub gunwale_radius_in: Option<f64>,
    /// Longitudinal stations (resampled evenly, as the analysis grid).
    pub stations: usize,
    /// Points around the full girth, sheer to sheer (odd, so one lies on the keel centreline).
    pub girth: usize,
    /// Swamped-flotation air pocket length at EACH end, in (result `air_pocket_in`). With a
    /// wall, a bulkhead one casting layer thick closes the hull interior inboard of each pocket.
    /// `None` or 0: none.
    pub pocket_in: Option<f64>,
    /// Casting passes of the wall (`punching.layers`): the bulkheads are t / layers thick, one
    /// pass. None = the whole wall thickness t.
    pub layers: Option<u32>,
}

impl ExportSpec {
    /// Air pocket bulkhead thickness, in: one casting layer of the wall (None without a wall).
    pub fn bulkhead_in(&self) -> Option<f64> {
        self.thickness_in.map(|t| t / f64::from(self.layers.unwrap_or(1).max(1)))
    }
}

/// Triangle mesh, inches. `closed` = watertight solid.
#[derive(Debug, Clone)]
pub struct Mesh {
    pub verts: Vec<[f64; 3]>,
    pub tris: Vec<[u32; 3]>,
    pub closed: bool,
}

impl Mesh {
    /// Enclosed volume, in^3 (divergence theorem; meaningful when `closed`).
    pub fn volume(&self) -> f64 {
        self.tris
            .iter()
            .map(|t| {
                let [a, b, c] = t.map(|i| self.verts[i as usize]);
                dot(a, cross(b, c)) / 6.0
            })
            .sum()
    }

    /// (min, max) corners, in.
    pub fn bbox(&self) -> ([f64; 3], [f64; 3]) {
        let mut lo = [f64::INFINITY; 3];
        let mut hi = [f64::NEG_INFINITY; 3];
        for v in &self.verts {
            for a in 0..3 {
                lo[a] = lo[a].min(v[a]);
                hi[a] = hi[a].max(v[a]);
            }
        }
        (lo, hi)
    }
}

fn sub(a: [f64; 3], b: [f64; 3]) -> [f64; 3] {
    [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
}
fn cross(a: [f64; 3], b: [f64; 3]) -> [f64; 3] {
    [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]
}
fn dot(a: [f64; 3], b: [f64; 3]) -> f64 {
    a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
}
fn norm(a: [f64; 3]) -> f64 {
    dot(a, a).sqrt()
}

/// Resample a 2D polyline to `n` points evenly spaced by arc length (ends kept exactly).
fn resample(p: &[(f64, f64)], n: usize) -> Vec<(f64, f64)> {
    let mut s = vec![0.0];
    for w in p.windows(2) {
        s.push(s.last().unwrap() + (w[1].0 - w[0].0).hypot(w[1].1 - w[0].1));
    }
    let total = *s.last().unwrap();
    (0..n)
        .map(|j| {
            if j == n - 1 {
                return *p.last().unwrap();
            }
            let target = total * j as f64 / (n - 1) as f64;
            let k = s.partition_point(|&v| v <= target).clamp(1, p.len() - 1);
            let seg = s[k] - s[k - 1];
            let t = if seg > 0.0 { (target - s[k - 1]) / seg } else { 0.0 };
            (p[k - 1].0 + t * (p[k].0 - p[k - 1].0), p[k - 1].1 + t * (p[k].1 - p[k - 1].1))
        })
        .collect()
}

/// Keel height of one offsets column (half-breadths `y` at waterlines `z`), not snapped to a
/// waterline: below the lowest non-zero offset the section is taken as `z = zk + c y^2` through
/// the two lowest non-zero offsets, clamped between the last zero waterline and the first non-zero
/// one. A flat bottom (offset at the first waterline) keeps `z[0]`. `None` if all zero.
fn keel_z(y: &[f64], z: &[f64]) -> Option<f64> {
    let first = y.iter().position(|&v| v > 1e-9)?;
    if first == 0 {
        return Some(z[0]);
    }
    let (y1, z1) = (y[first], z[first]);
    let fit = y.get(first + 1).filter(|&&y2| y2 > y1 + 1e-9).map(|&y2| {
        let z2 = z[first + 1];
        (z1 * y2 * y2 - z2 * y1 * y1) / (y2 * y2 - y1 * y1)
    });
    Some(fit.unwrap_or(z[first - 1]).clamp(z[first - 1], z1))
}

/// Monotone cubic (Fritsch-Carlson PCHIP) through `(xs, ys)`, evaluated at `x` (clamped). Smooth
/// keel line along x with no overshoot below the table keel.
fn pchip(xs: &[f64], ys: &[f64], x: f64) -> f64 {
    let n = xs.len();
    if n == 1 {
        return ys[0];
    }
    let h: Vec<f64> = xs.windows(2).map(|w| w[1] - w[0]).collect();
    let d: Vec<f64> = (0..n - 1).map(|i| (ys[i + 1] - ys[i]) / h[i]).collect();
    let mut m = vec![0.0; n];
    m[0] = d[0];
    m[n - 1] = d[n - 2];
    for i in 1..n - 1 {
        if d[i - 1] * d[i] > 0.0 {
            let (w1, w2) = (2.0 * h[i] + h[i - 1], h[i] + 2.0 * h[i - 1]);
            m[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i]);
        }
    }
    let x = x.clamp(xs[0], xs[n - 1]);
    let i = xs.partition_point(|&v| v <= x).clamp(1, n - 1) - 1;
    let t = (x - xs[i]) / h[i];
    let (t2, t3) = (t * t, t * t * t);
    ys[i] * (2.0 * t3 - 3.0 * t2 + 1.0)
        + h[i] * m[i] * (t3 - 2.0 * t2 + t)
        + ys[i + 1] * (-2.0 * t3 + 3.0 * t2)
        + h[i] * m[i + 1] * (t3 - t2)
}

/// Starboard half girth of one station, sheer -> centreline keel point at height `zk`, consecutive duplicates removed. `None` if the
/// station has no offsets (all zero).
fn half_girth(y: &[f64], z: &[f64], zk: f64) -> Option<Vec<(f64, f64)>> {
    let first = y.iter().position(|&v| v > 1e-9)?;
    // Between table stations the grid blends offsets linearly, so a small offset can appear
    // below the smooth keel; waterlines below the keel are dropped. The lowest offset exactly at
    // the keel height is a flat bottom: kept, so its chine is not cut into a V.
    let zk = zk.max(z[0]);
    let mut p: Vec<(f64, f64)> = (first..y.len())
        .rev()
        .filter(|&k| z[k] > zk + 1e-6 || k + 1 == y.len() || (k == first && (z[k] - zk).abs() <= 1e-6))
        .map(|k| (y[k], z[k]))
        .collect();
    p.push((0.0, zk.min(p.last().unwrap().1)));
    p.dedup_by(|a, b| (a.0 - b.0).abs() < 1e-12 && (a.1 - b.1).abs() < 1e-12);
    Some(p)
}

/// Inward offset by `t` of a resampled starboard half girth (last point on the centreline).
/// `scale[j]` stretches the in-plane offset at point j (1 / cos of the surface normal's tilt out
/// of the section plane) so the 3D normal wall thickness is t where the hull tapers along x.
fn offset_inward(p: &[(f64, f64)], t: f64, z_top: f64, scale: &[f64]) -> Vec<(f64, f64)> {
    let n = p.len();
    let seg_n = |a: (f64, f64), b: (f64, f64)| -> Option<(f64, f64)> {
        let (dy, dz) = (b.0 - a.0, b.1 - a.1);
        let l = dy.hypot(dz);
        (l > 1e-12).then(|| (dz / l, -dy / l))
    };
    (0..n)
        .map(|j| {
            let mut ns: Vec<(f64, f64)> = vec![];
            if j > 0
                && let Some(v) = seg_n(p[j - 1], p[j])
            {
                ns.push(v);
            }
            if j + 1 < n
                && let Some(v) = seg_n(p[j], p[j + 1])
            {
                ns.push(v);
            }
            if j == n - 1
                && let Some(&(ny, nz)) = ns.first()
            {
                ns.push((-ny, nz)); // mirrored port segment at the centreline
            }
            let (sy, sz) = ns.iter().fold((0.0, 0.0), |a, v| (a.0 + v.0, a.1 + v.1));
            let l = sy.hypot(sz);
            let (y, z) = p[j];
            if l < 1e-12 {
                return (y, z);
            }
            let (uy, uz) = (sy / l, sz / l);
            let c = ns.iter().map(|v| v.0 * uy + v.1 * uz).fold(1.0, f64::min);
            let m = (t * scale[j] / c.max(1e-9)).min(2.0 * t);
            ((y + m * uy).max(0.0), (z + m * uz).min(z_top))
        })
        .collect()
}

/// Build the export mesh for `hull`.
pub fn build_mesh(hull: &Hull, spec: &ExportSpec) -> Result<Mesh> {
    ensure!(spec.depth_in.is_finite() && spec.depth_in > 0.0, "depth must be > 0, got {}", spec.depth_in);
    ensure!(spec.stations >= 3, "need >= 3 stations");
    ensure!(spec.girth >= 5 && spec.girth % 2 == 1, "girth points must be odd and >= 5, got {}", spec.girth);
    if let Some(t) = spec.thickness_in {
        ensure!(t.is_finite() && t > 0.0, "thickness must be > 0, got {t}");
    }
    // The export draws what it is given; buildability limits were the run's to apply.
    let ts = crate::hull::Topsides {
        flare_deg: spec.flare_deg,
        gunwale_radius_in: spec.gunwale_radius_in,
        gunwale_max_flare_deg: 89.9,
    };
    // Table stations closer together than half the even spacing (the refined noses of a
    // filleted hull) are kept as well, so the noses survive; other tables export as before.
    let even = hull.length / (spec.stations - 1) as f64;
    let fine: Vec<f64> = (0..hull.x.len())
        .filter(|&i| {
            let gap = |a: usize, b: usize| (hull.x[b] - hull.x[a]) < 0.5 * even;
            (i > 0 && gap(i - 1, i)) || (i + 1 < hull.x.len() && gap(i, i + 1))
        })
        .map(|i| hull.x[i])
        .collect();
    let (grid, unbuildable) = if !fine.is_empty() {
        let mut xs = crate::hull::linspace(0.0, hull.length, spec.stations);
        xs.extend(&fine);
        xs.sort_by(f64::total_cmp);
        xs.dedup_by(|a, b| (*a - *b).abs() < 1e-6);
        Grid::build_topsides_at(hull, spec.depth_in, &xs, ts)
    } else {
        Grid::build_topsides(hull, spec.depth_in, spec.stations, ts)
    };
    if let Some(why) = unbuildable {
        anyhow::bail!("export: {why}");
    }
    let z_top = *grid.z.last().unwrap();
    // Keel line: fitted at the table stations that have offsets, then a monotone cubic along x.
    let (kx, kz): (Vec<f64>, Vec<f64>) =
        hull.x.iter().zip(&hull.half_breadth).filter_map(|(&x, y)| Some((x, keel_z(y, &hull.z)?))).unzip();
    ensure!(!kx.is_empty(), "hull has no offsets");
    let halves: Vec<Option<Vec<(f64, f64)>>> =
        (0..grid.nx()).map(|i| half_girth(grid.row(i), &grid.z, pchip(&kx, &kz, grid.x[i]))).collect();
    let Some(first) = halves.iter().position(Option::is_some) else { bail!("hull has no offsets") };
    let last = halves.iter().rposition(Option::is_some).unwrap();
    ensure!(halves[first..=last].iter().all(Option::is_some), "hull has an empty station mid-length");
    ensure!(last > first, "hull has offsets at only one station");
    let h = spec.girth.div_ceil(2);
    // Station range: one empty station at each end (the stem line) when there is one.
    let (i0, i1) = (first.saturating_sub(1), (last + 1).min(grid.nx() - 1));
    let mut rows: Vec<(f64, Vec<(f64, f64)>)> = vec![];
    for i in i0..=i1 {
        let half = match &halves[i] {
            Some(p) => resample(p, h),
            None => {
                // Stem line: from the sheer down to the keel height of the neighbouring station.
                let nb = halves[if i < first { first } else { last }].as_ref().unwrap();
                let zk = nb.last().unwrap().1;
                resample(&[(0.0, z_top), (0.0, zk)], h)
            }
        };
        rows.push((grid.x[i], half));
    }
    // Air pocket bulkheads: rows at the four bulkhead faces (bow inner/outer, stern inner/outer).
    let mut cuts: Option<[usize; 4]> = None;
    if let (Some(b), Some(l)) = (spec.bulkhead_in(), spec.pocket_in) {
        ensure!(l.is_finite() && l >= 0.0, "air pocket length must be >= 0, got {l}");
        if l > 0.0 {
            let (x0, xn) = (grid.x[0], *grid.x.last().unwrap());
            let xs = [x0 + l, x0 + l + b, xn - l - b, xn - l];
            ensure!(
                xs[1] < xs[2],
                "air pockets of {l} in plus {b} in bulkheads overlap on a {:.1} in hull",
                xn - x0
            );
            for x in xs {
                let k = rows.partition_point(|r| r.0 < x - 1e-6);
                if k < rows.len() && (rows[k].0 - x).abs() <= 1e-6 {
                    continue;
                }
                ensure!(k > 0 && k < rows.len(), "air pocket bulkhead at x = {x:.2} in is outside the hull");
                let (a, b) = (&rows[k - 1], &rows[k]);
                let s = (x - a.0) / (b.0 - a.0);
                let half =
                    a.1.iter()
                        .zip(&b.1)
                        .map(|(p, q)| (p.0 + s * (q.0 - p.0), p.1 + s * (q.1 - p.1)))
                        .collect();
                rows.insert(k, (x, half));
            }
            cuts = Some(xs.map(|x| rows.iter().position(|r| (r.0 - x).abs() <= 1e-6).unwrap()));
        }
    }
    let full = |half: &[(f64, f64)]| -> Vec<(f64, f64)> {
        half.iter().copied().chain(half.iter().rev().skip(1).map(|&(y, z)| (-y, z))).collect()
    };
    let nr = rows.len();
    let m = spec.girth;
    let mut verts: Vec<[f64; 3]> = vec![];
    let mut push_row = |x: f64, pts: &[(f64, f64)]| -> usize {
        let start = verts.len();
        verts.extend(pts.iter().map(|&(y, z)| [x, y, z]));
        start
    };
    let outer: Vec<usize> = rows.iter().map(|(x, half)| push_row(*x, &full(half))).collect();
    let mut tris: Vec<[u32; 3]> = vec![];
    let quad = |tris: &mut Vec<[u32; 3]>, a: usize, b: usize, c: usize, d: usize| {
        tris.push([a as u32, b as u32, c as u32]);
        tris.push([a as u32, c as u32, d as u32]);
    };
    for r in 0..nr - 1 {
        for j in 0..m - 1 {
            let (a, b) = (outer[r] + j, outer[r + 1] + j);
            quad(&mut tris, a, b, b + 1, a + 1);
        }
    }
    let closed = spec.thickness_in.is_some();
    if let Some(t) = spec.thickness_in {
        // Inner rows only where the offset wall is a proper U (no point clamped onto the
        // centreline); the narrow ends before/after are solid concrete.
        let inner_half: Vec<Option<Vec<(f64, f64)>>> = rows
            .iter()
            .enumerate()
            .map(|(r, (_, half))| {
                if r == 0 || r == nr - 1 {
                    return None;
                }
                let scale: Vec<f64> = (0..half.len())
                    .map(|j| {
                        let (ja, jb) = (j.saturating_sub(1), (j + 1).min(half.len() - 1));
                        let (gy, gz) = (half[jb].0 - half[ja].0, half[jb].1 - half[ja].1);
                        let (pa, pb) = (&rows[r - 1], &rows[r + 1]);
                        let tx = [pb.0 - pa.0, pb.1[j].0 - pa.1[j].0, pb.1[j].1 - pa.1[j].1];
                        let n = cross([0.0, gy, gz], tx);
                        let l = norm(n);
                        if l < 1e-12 { 1.0 } else { (1.0 / (1.0 - (n[0] / l).powi(2)).sqrt()).min(1.5) }
                    })
                    .collect();
                let q = offset_inward(half, t, z_top, &scale);
                // Points pushed onto/through the centreline (V keel narrower than the wall) are
                // dropped; the inner girth then runs straight to its centreline point.
                // Every inner point is the mitred offset of its own outer point, so each inner
                // facet is parallel to its outer one at distance t (re-resampling the inner girth
                // by arc length would cut its corners and thicken the wall at the bilge).
                // Only the tail pushed onto the centreline is respread along its last segment.
                let (keel, sides) = q.split_last().unwrap();
                let kept = sides.iter().take_while(|p| p.0 > 1e-6).count();
                let ok = kept >= 2 && keel.1 < z_top - 1e-6;
                ok.then(|| {
                    let mut inner = q[..kept].to_vec();
                    let tail = resample(&[q[kept - 1], *keel], q.len() - kept + 1);
                    inner.extend_from_slice(&tail[1..]);
                    inner
                })
            })
            .collect();
        // The hollow part is the longest run of stations with a valid offset U (ties: first);
        // everything fore and aft of it is solid concrete.
        let (mut ra, mut rb, mut best) = (0, 0, 0);
        let mut r = 0;
        while r < nr {
            if inner_half[r].is_none() {
                r += 1;
                continue;
            }
            let s = r;
            while r < nr && inner_half[r].is_some() {
                r += 1;
            }
            if r - s > best {
                (ra, rb, best) = (s, r - 1, r - s);
            }
        }
        ensure!(best >= 2, "wall thickness {t} in leaves no hollow section: the hull would be solid");
        // Cell r (rows r..r+1) is hollow inside [ra, rb] except across a pocket and its bulkhead:
        // a covered pocket is solid from outside, its void an inner shell (below).
        let solid_cell = |r: usize| cuts.is_some_and(|c| r < c[1] || r >= c[2]);
        if let Some(c) = cuts {
            ensure!(
                ra < c[0] && c[3] < rb,
                "air pocket bulkhead at x = {:.2} / {:.2} in falls in the solid concrete end (hollow from \
                 x = {:.2} to {:.2} in); export with more --stations or --no-pockets",
                rows[c[0]].0,
                rows[c[3]].0,
                rows[ra].0,
                rows[rb].0
            );
        }
        let hollow_cell: Vec<bool> = (0..nr - 1).map(|r| r >= ra && r < rb && !solid_cell(r)).collect();
        let hollow_row = |r: usize| (r > 0 && hollow_cell[r - 1]) || (r < nr - 1 && hollow_cell[r]);
        let mut inner = vec![usize::MAX; nr];
        for r in (0..nr).filter(|&r| hollow_row(r)) {
            inner[r] = push_row(rows[r].0, &full(inner_half[r].as_ref().unwrap()));
        }
        // Bulkheads: a planar face closing the inner U where a hollow run ends (fanned from its
        // top centre c). Then each row's top edge, starboard -> port, with its y for the strip.
        let mut side: Vec<Vec<(usize, f64)>> = vec![];
        for r in 0..nr {
            let (yo, mut s) = (rows[r].1[0].0, vec![]);
            s.push((outer[r], yo));
            if hollow_row(r) && !(r > 0 && hollow_cell[r - 1] && r < nr - 1 && hollow_cell[r]) {
                let c = push_row(rows[r].0, &[(0.0, z_top)]);
                for j in 0..m - 1 {
                    tris.push([c as u32, (inner[r] + j) as u32, (inner[r] + j + 1) as u32]);
                }
                let yi = inner_half[r].as_ref().unwrap()[0].0;
                s.extend([(inner[r], yi), (c, 0.0), (inner[r] + m - 1, -yi)]);
            }
            s.push((outer[r] + m - 1, -yo));
            side.push(s);
        }
        // Pocket voids: the inner U of each pocket's rows (solid end to bulkhead) below the cover,
        // a flat lid at D - b (through a centre point per row) and a cap at each end.
        if let (Some(c), Some(b)) = (cuts, spec.bulkhead_in()) {
            let zc = z_top - b;
            for (p0, p1) in [(ra, c[0]), (c[3], rb)] {
                if p1 <= p0 {
                    continue;
                }
                let mut ring = vec![];
                for r in p0..=p1 {
                    let half = inner_half[r].as_ref().unwrap();
                    let k = half.iter().position(|q| q.1 <= zc).unwrap_or(half.len());
                    ensure!(
                        k < half.len() && half[half.len() - 1].1 < zc - 1e-6,
                        "air pocket at x = {:.2} in is shallower than its {b:.3} in cover",
                        rows[r].0
                    );
                    // The inner points themselves (the void wall stays t from the outer one, corners
                    // kept); those above the cover move down onto it where the girth crosses D - b.
                    let top = if k > 0 && half[k].1 < zc {
                        let (a, q) = (half[k - 1], half[k]);
                        let s = (a.1 - zc) / (a.1 - q.1);
                        (a.0 + s * (q.0 - a.0), zc)
                    } else {
                        half[k]
                    };
                    let clip: Vec<(f64, f64)> =
                        half.iter().enumerate().map(|(j, q)| if j < k { top } else { *q }).collect();
                    let g = push_row(rows[r].0, &full(&clip));
                    let cen = push_row(rows[r].0, &[(0.0, zc)]);
                    ring.push((g, cen));
                }
                for w in ring.windows(2) {
                    let ((ga, ca), (gb, cb)) = (w[0], w[1]);
                    for j in 0..m - 1 {
                        quad(&mut tris, ga + j, ga + j + 1, gb + j + 1, gb + j);
                    }
                    quad(&mut tris, ga, gb, cb, ca);
                    quad(&mut tris, ca, cb, gb + m - 1, ga + m - 1);
                }
                for &(g, cen) in [ring[0], ring[ring.len() - 1]].iter() {
                    for j in 0..m - 1 {
                        tris.push([cen as u32, (g + j) as u32, (g + j + 1) as u32]);
                    }
                }
            }
        }
        for r in 0..nr - 1 {
            if hollow_cell[r] {
                for j in 0..m - 1 {
                    let (a, b) = (inner[r] + j, inner[r + 1] + j);
                    quad(&mut tris, a, a + 1, b + 1, b);
                }
                for j in [0, m - 1] {
                    let (oa, ob, ia, ib) = (outer[r] + j, outer[r + 1] + j, inner[r] + j, inner[r + 1] + j);
                    quad(&mut tris, oa, ob, ib, ia);
                }
            } else {
                // Flat top over solid concrete (an end or a pocket bulkhead): zip the two rows' top
                // edges together by y. Every facet has points on both rows, so none is degenerate.
                let (a, b) = (&side[r], &side[r + 1]);
                let (mut i, mut j) = (0, 0);
                while i + 1 < a.len() || j + 1 < b.len() {
                    if j + 1 == b.len() || (i + 1 < a.len() && a[i + 1].1 >= b[j + 1].1) {
                        tris.push([a[i].0 as u32, a[i + 1].0 as u32, b[j].0 as u32]);
                        i += 1;
                    } else {
                        tris.push([a[i].0 as u32, b[j + 1].0 as u32, b[j].0 as u32]);
                        j += 1;
                    }
                }
            }
        }
    }
    let mut mesh = clean(Mesh { verts, tris, closed });
    orient(&mut mesh)?;
    if closed {
        // Each shell on its own: the outer one (largest volume) encloses positive volume, the
        // pocket voids negative (facing into the void, out of the concrete).
        let comp = components(&mesh);
        let n = comp.iter().max().map_or(0, |c| c + 1);
        let mut vol = vec![0.0; n];
        for (t, &k) in mesh.tris.iter().zip(&comp) {
            vol[k] += tri_volume(&mesh, t);
        }
        let outer = (0..n).max_by(|&a, &b| vol[a].abs().total_cmp(&vol[b].abs())).unwrap_or(0);
        for (t, &k) in mesh.tris.iter_mut().zip(&comp) {
            if (k == outer) == (vol[k] < 0.0) {
                t.swap(1, 2);
            }
        }
    } else if outward_flip(&mesh) {
        for t in &mut mesh.tris {
            t.swap(1, 2);
        }
    }
    Ok(mesh)
}

/// Make facet winding consistent across shared edges (each edge traversed once each way).
/// Err if the surface is non-orientable or (for a solid) not closed and manifold.
fn orient(m: &mut Mesh) -> Result<()> {
    let mut by_edge: HashMap<(u32, u32), Vec<usize>> = HashMap::new();
    for (f, t) in m.tris.iter().enumerate() {
        for (a, b) in [(t[0], t[1]), (t[1], t[2]), (t[2], t[0])] {
            by_edge.entry((a.min(b), a.max(b))).or_default().push(f);
        }
    }
    if let Some((e, fs)) = by_edge.iter().find(|(_, f)| f.len() > 2 || (m.closed && f.len() != 2)) {
        bail!(
            "export mesh is not a closed manifold: edge {:?}-{:?} has {} facets",
            m.verts[e.0 as usize],
            m.verts[e.1 as usize],
            fs.len()
        );
    }
    let has = |t: &[u32; 3], a: u32, b: u32| (0..3).any(|k| t[k] == a && t[(k + 1) % 3] == b);
    let mut done = vec![false; m.tris.len()];
    for seed in 0..m.tris.len() {
        if done[seed] {
            continue;
        }
        done[seed] = true;
        let mut stack = vec![seed];
        while let Some(f) = stack.pop() {
            let t = m.tris[f];
            for (a, b) in [(t[0], t[1]), (t[1], t[2]), (t[2], t[0])] {
                for &g in &by_edge[&(a.min(b), a.max(b))] {
                    if g == f {
                        continue;
                    }
                    let same = has(&m.tris[g], a, b);
                    if done[g] {
                        ensure!(!same, "export mesh is not orientable");
                        continue;
                    }
                    if same {
                        m.tris[g].swap(1, 2);
                    }
                    done[g] = true;
                    stack.push(g);
                }
            }
        }
    }
    Ok(())
}

/// Connected shell of each facet (facets sharing an edge), numbered in first-facet order.
pub fn components(m: &Mesh) -> Vec<usize> {
    let mut by_edge: HashMap<(u32, u32), Vec<usize>> = HashMap::new();
    for (f, t) in m.tris.iter().enumerate() {
        for (a, b) in [(t[0], t[1]), (t[1], t[2]), (t[2], t[0])] {
            by_edge.entry((a.min(b), a.max(b))).or_default().push(f);
        }
    }
    let mut comp = vec![usize::MAX; m.tris.len()];
    let mut next = 0;
    for seed in 0..m.tris.len() {
        if comp[seed] != usize::MAX {
            continue;
        }
        comp[seed] = next;
        let mut stack = vec![seed];
        while let Some(f) = stack.pop() {
            let t = m.tris[f];
            for (a, b) in [(t[0], t[1]), (t[1], t[2]), (t[2], t[0])] {
                for &g in &by_edge[&(a.min(b), a.max(b))] {
                    if comp[g] == usize::MAX {
                        comp[g] = next;
                        stack.push(g);
                    }
                }
            }
        }
        next += 1;
    }
    comp
}

/// Signed volume of the tetrahedron (origin, facet).
fn tri_volume(m: &Mesh, t: &[u32; 3]) -> f64 {
    let [a, b, c] = t.map(|i| m.verts[i as usize]);
    dot(a, cross(b, c)) / 6.0
}

/// Open surface: true if its facets point inward (toward the centreline / up into the canoe).
fn outward_flip(m: &Mesh) -> bool {
    let s: f64 = m
        .tris
        .iter()
        .map(|t| {
            let [a, b, c] = t.map(|i| m.verts[i as usize]);
            let n = cross(sub(b, a), sub(c, a));
            let cen = [(a[0] + b[0] + c[0]) / 3.0, (a[1] + b[1] + c[1]) / 3.0, 0.0];
            n[1] * cen[1] - n[2] // outward = away from centreline and downward
        })
        .sum();
    s < 0.0
}

/// Weld coincident vertices, drop degenerate facets and cancel mirrored duplicate pairs.
fn clean(m: Mesh) -> Mesh {
    let key = |v: [f64; 3]| v.map(|c| (c * 1e7).round() as i64);
    let mut map: HashMap<[i64; 3], u32> = HashMap::new();
    let mut verts = vec![];
    let remap: Vec<u32> = m
        .verts
        .iter()
        .map(|&v| {
            *map.entry(key(v)).or_insert_with(|| {
                verts.push(v);
                (verts.len() - 1) as u32
            })
        })
        .collect();
    let tris: Vec<[u32; 3]> = m
        .tris
        .iter()
        .map(|t| t.map(|i| remap[i as usize]))
        .filter(|t| {
            t[0] != t[1] && t[1] != t[2] && t[0] != t[2] && {
                let [a, b, c] = t.map(|i| verts[i as usize]);
                norm(cross(sub(b, a), sub(c, a))) > 1e-12
            }
        })
        .collect();
    let mut count: HashMap<[u32; 3], usize> = HashMap::new();
    let sorted = |t: &[u32; 3]| {
        let mut s = *t;
        s.sort_unstable();
        s
    };
    for t in &tris {
        *count.entry(sorted(t)).or_default() += 1;
    }
    let mut kept: HashMap<[u32; 3], bool> = HashMap::new();
    let tris: Vec<[u32; 3]> = tris
        .into_iter()
        .filter(|t| {
            let s = sorted(t);
            count[&s] % 2 == 1 && !kept.insert(s, true).unwrap_or(false)
        })
        .collect();
    // Drop vertices no facet uses (keeps the files lean), preserving order.
    let mut used = vec![u32::MAX; verts.len()];
    let mut out = vec![];
    let tris = tris
        .into_iter()
        .map(|t| {
            t.map(|i| {
                if used[i as usize] == u32::MAX {
                    used[i as usize] = out.len() as u32;
                    out.push(verts[i as usize]);
                }
                used[i as usize]
            })
        })
        .collect();
    Mesh { verts: out, tris, closed: m.closed }
}

/// Binary STL (80-byte header, little endian).
pub fn to_stl(mesh: &Mesh, name: &str) -> Vec<u8> {
    let mut b = Vec::with_capacity(84 + 50 * mesh.tris.len());
    let mut header = format!("JAGUAR {name} (inches)").into_bytes();
    header.resize(80, b' ');
    b.extend_from_slice(&header);
    b.extend_from_slice(&(mesh.tris.len() as u32).to_le_bytes());
    for t in &mesh.tris {
        let [p0, p1, p2] = t.map(|i| mesh.verts[i as usize]);
        let n = cross(sub(p1, p0), sub(p2, p0));
        let l = norm(n).max(1e-30);
        for v in [n.map(|c| c / l), p0, p1, p2] {
            for c in v {
                b.extend_from_slice(&(c as f32).to_le_bytes());
            }
        }
        b.extend_from_slice(&[0, 0]);
    }
    b
}

fn step_str(s: &str) -> String {
    s.chars().filter(|c| c.is_ascii() && !c.is_ascii_control()).collect::<String>().replace('\'', "''")
}

/// Sequential STEP entity writer.
struct StepW {
    d: String,
    id: usize,
}

impl StepW {
    fn e(&mut self, body: impl AsRef<str>) -> usize {
        self.id += 1;
        let _ = writeln!(self.d, "#{}={};", self.id, body.as_ref());
        self.id
    }
}

/// STEP AP214 (AUTOMOTIVE_DESIGN): MANIFOLD_SOLID_BREP of planar triangles (closed solid) or SHELL_BASED_SURFACE_MODEL
/// (open surface), length unit inch.
pub fn to_step(mesh: &Mesh, name: &str) -> String {
    let name = step_str(name);
    let mut s = String::new();
    let mut w = StepW { d: String::new(), id: 0 };
    let app = w.e("APPLICATION_CONTEXT('automotive design')");
    w.e(format!("APPLICATION_PROTOCOL_DEFINITION('international standard','automotive_design',2000,#{app})"));
    let pc = w.e(format!("PRODUCT_CONTEXT('',#{app},'mechanical')"));
    let prod = w.e(format!("PRODUCT('{name}','{name}','JAGUAR concrete canoe',(#{pc}))"));
    w.e(format!("PRODUCT_RELATED_PRODUCT_CATEGORY('part',$,(#{prod}))"));
    let pdf = w.e(format!("PRODUCT_DEFINITION_FORMATION('','',#{prod})"));
    let pdc = w.e(format!("PRODUCT_DEFINITION_CONTEXT('part definition',#{app},'design')"));
    let pd = w.e(format!("PRODUCT_DEFINITION('design','',#{pdf},#{pdc})"));
    let pds = w.e(format!("PRODUCT_DEFINITION_SHAPE('','',#{pd})"));
    let dim = w.e("DIMENSIONAL_EXPONENTS(1.,0.,0.,0.,0.,0.,0.)");
    let mm = w.e("(LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.))");
    let lmu = w.e(format!("LENGTH_MEASURE_WITH_UNIT(LENGTH_MEASURE(25.4),#{mm})"));
    let inch = w.e(format!("(CONVERSION_BASED_UNIT('INCH',#{lmu}) LENGTH_UNIT() NAMED_UNIT(#{dim}))"));
    let rad = w.e("(NAMED_UNIT(*) PLANE_ANGLE_UNIT() SI_UNIT($,.RADIAN.))");
    let sr = w.e("(NAMED_UNIT(*) SI_UNIT($,.STERADIAN.) SOLID_ANGLE_UNIT())");
    let unc = w.e(format!(
        "UNCERTAINTY_MEASURE_WITH_UNIT(LENGTH_MEASURE(1.E-05),#{inch},'distance_accuracy_value','')"
    ));
    let ctx = w.e(format!(
        "(GEOMETRIC_REPRESENTATION_CONTEXT(3) GLOBAL_UNCERTAINTY_ASSIGNED_CONTEXT((#{unc})) \
         GLOBAL_UNIT_ASSIGNED_CONTEXT((#{inch},#{rad},#{sr})) REPRESENTATION_CONTEXT('',''))"
    ));
    let o = w.e("CARTESIAN_POINT('',(0.,0.,0.))");
    let zd = w.e("DIRECTION('',(0.,0.,1.))");
    let xd = w.e("DIRECTION('',(1.,0.,0.))");
    let ax = w.e(format!("AXIS2_PLACEMENT_3D('',#{o},#{zd},#{xd})"));
    let fnum = |v: f64| {
        let r = format!("{v:.6}");
        r.trim_end_matches('0').to_string()
    };
    let pt = |v: [f64; 3]| format!("CARTESIAN_POINT('',({},{},{}))", fnum(v[0]), fnum(v[1]), fnum(v[2]));
    let dir = |v: [f64; 3]| {
        let l = norm(v).max(1e-300);
        format!("DIRECTION('',({},{},{}))", fnum(v[0] / l), fnum(v[1] / l), fnum(v[2] / l))
    };
    // Vertices.
    let vp: Vec<usize> = mesh
        .verts
        .iter()
        .map(|&v| {
            let p = w.e(pt(v));
            w.e(format!("VERTEX_POINT('',#{p})"))
        })
        .collect();
    // Edges: one EDGE_CURVE per undirected mesh edge (sorted order => deterministic), LINE geometry.
    let mut edges: Vec<(u32, u32)> = mesh
        .tris
        .iter()
        .flat_map(|t| [(t[0], t[1]), (t[1], t[2]), (t[2], t[0])])
        .map(|(a, b)| (a.min(b), a.max(b)))
        .collect();
    edges.sort_unstable();
    edges.dedup();
    let mut edge_id: HashMap<(u32, u32), usize> = HashMap::with_capacity(edges.len());
    for &(a, b) in &edges {
        let (pa, pb) = (mesh.verts[a as usize], mesh.verts[b as usize]);
        let p = w.e(pt(pa));
        let dv = w.e(dir(sub(pb, pa)));
        let vec = w.e(format!("VECTOR('',#{dv},{})", fnum(norm(sub(pb, pa)))));
        let line = w.e(format!("LINE('',#{p},#{vec})"));
        let ec = w.e(format!("EDGE_CURVE('',#{},#{},#{line},.T.)", vp[a as usize], vp[b as usize]));
        edge_id.insert((a, b), ec);
    }
    // Shells: the outer one, then each void (written facing out of the void, as a closed shell
    // is; BREP_WITH_VOIDS turns it round).
    let comp = if mesh.closed { components(mesh) } else { vec![0; mesh.tris.len()] };
    let nshell = comp.iter().max().map_or(1, |c| c + 1);
    let shell_vol: Vec<f64> = (0..nshell)
        .map(|k| {
            mesh.tris.iter().zip(&comp).filter(|(_, c)| **c == k).map(|(t, _)| tri_volume(mesh, t)).sum()
        })
        .collect();
    let mut faces: Vec<Vec<usize>> = vec![vec![]; nshell];
    for (t, &k) in mesh.tris.iter().zip(&comp) {
        let t = &if shell_vol[k] < 0.0 { [t[0], t[2], t[1]] } else { *t };
        let [pa, pb, pc] = t.map(|i| mesh.verts[i as usize]);
        let oe: Vec<String> = [(t[0], t[1]), (t[1], t[2]), (t[2], t[0])]
            .iter()
            .map(|&(a, b)| {
                let ec = edge_id[&(a.min(b), a.max(b))];
                format!(
                    "#{}",
                    w.e(format!("ORIENTED_EDGE('',*,*,#{ec},{})", if a < b { ".T." } else { ".F." }))
                )
            })
            .collect();
        let lp = w.e(format!("EDGE_LOOP('',({}))", oe.join(",")));
        let fb = w.e(format!("FACE_OUTER_BOUND('',#{lp},.T.)"));
        let n = cross(sub(pb, pa), sub(pc, pa));
        let loc = w.e(pt(pa));
        let nd = w.e(dir(n));
        let rd = w.e(dir(sub(pb, pa)));
        let ap = w.e(format!("AXIS2_PLACEMENT_3D('',#{loc},#{nd},#{rd})"));
        let pl = w.e(format!("PLANE('',#{ap})"));
        faces[k].push(w.e(format!("ADVANCED_FACE('',(#{fb}),#{pl},.T.)")));
    }
    let list = |k: usize| faces[k].iter().map(|f| format!("#{f}")).collect::<Vec<_>>().join(",");
    let rep = if mesh.closed {
        let outer = (0..nshell).find(|&k| shell_vol[k] > 0.0).unwrap_or(0);
        let sh = w.e(format!("CLOSED_SHELL('',({}))", list(outer)));
        let voids: Vec<String> = (0..nshell)
            .filter(|&k| k != outer)
            .map(|k| {
                let v = w.e(format!("CLOSED_SHELL('',({}))", list(k)));
                format!("#{}", w.e(format!("ORIENTED_CLOSED_SHELL('',*,#{v},.F.)")))
            })
            .collect();
        let br = if voids.is_empty() {
            w.e(format!("MANIFOLD_SOLID_BREP('{name}',#{sh})"))
        } else {
            w.e(format!("BREP_WITH_VOIDS('{name}',#{sh},({}))", voids.join(",")))
        };
        w.e(format!("ADVANCED_BREP_SHAPE_REPRESENTATION('{name}',(#{ax},#{br}),#{ctx})"))
    } else {
        let sh = w.e(format!("OPEN_SHELL('',({}))", list(0)));
        let sm = w.e(format!("SHELL_BASED_SURFACE_MODEL('{name}',(#{sh}))"));
        w.e(format!("MANIFOLD_SURFACE_SHAPE_REPRESENTATION('{name}',(#{ax},#{sm}),#{ctx})"))
    };
    w.e(format!("SHAPE_DEFINITION_REPRESENTATION(#{pds},#{rep})"));
    let d = w.d;
    s.push_str("ISO-10303-21;\nHEADER;\n");
    let _ = writeln!(s, "FILE_DESCRIPTION(('JAGUAR concrete canoe, units inch'),'2;1');");
    let _ = writeln!(s, "FILE_NAME('{name}.step','2000-01-01T00:00:00',(''),(''),'JAGUAR','JAGUAR','');");
    s.push_str("FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));\nENDSEC;\nDATA;\n");
    s.push_str(&d);
    s.push_str("ENDSEC;\nEND-ISO-10303-21;\n");
    s
}

/// Hull and export spec from a `jaguar-result/1` JSON: the winner's hull (generated shape, or
/// params.hull.file), thickness, depth, the run's flare, the winner's air pocket length (if
/// `[flotation]` sized one) and the run's casting passes (`punching.layers`, for the bulkheads).
/// Err if there is no winner.
pub fn from_result(text: &str, stations: usize, girth: usize) -> Result<(Hull, ExportSpec)> {
    let v: serde_json::Value = serde_json::from_str(text).context("parsing result JSON")?;
    ensure!(v["format"] == crate::optimize::RESULT_FORMAT, "not a jaguar-result/1 file");
    let c = &v["winner"]["candidate"];
    ensure!(c.is_object(), "the result has no winner: nothing to export");
    let num = |f: &str| c[f].as_f64().with_context(|| format!("winner has no {f}"));
    let hull: Hull = if v["winner_hull"].is_object() {
        serde_json::from_value(v["winner_hull"].clone()).context("winner_hull")?
    } else {
        let f =
            v["params"]["hull"]["file"].as_str().context("result has neither winner_hull nor hull.file")?;
        Hull::load(std::path::Path::new(f))?
    };
    hull.validate()?;
    let spec = ExportSpec {
        depth_in: num("depth_in")?,
        thickness_in: Some(num("thickness_in")?),
        flare_deg: crate::report::result_topsides(&v).flare_deg,
        gunwale_radius_in: crate::report::result_topsides(&v).gunwale_radius_in,
        stations,
        girth,
        pocket_in: c["air_pocket_in"].as_f64(),
        layers: serde_json::from_value::<crate::params::Punching>(v["params"]["punching"].clone())
            .ok()
            .and_then(|p| p.layer_count()),
    };
    Ok((hull, spec))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    fn off1() -> Hull {
        Hull::load(Path::new("tests/golden/off1_hull.json")).unwrap()
    }

    #[test]
    fn resample_keeps_ends_and_spacing() {
        let r = resample(&[(0.0, 0.0), (0.0, 2.0), (2.0, 2.0)], 5);
        assert_eq!(r[0], (0.0, 0.0));
        assert_eq!(r[4], (2.0, 2.0));
        assert!((r[2].1 - 2.0).abs() < 1e-12 && r[2].0.abs() < 1e-12);
    }

    #[test]
    fn offset_of_box_section_is_t_inside() {
        // half box: side x=5 from z=10 to 0, bottom to centreline
        let p = resample(&[(5.0, 10.0), (5.0, 0.0), (0.0, 0.0)], 31);
        let q = offset_inward(&p, 0.5, 10.0, &[1.0; 31]);
        assert!((q[0].0 - 4.5).abs() < 1e-9 && (q[0].1 - 10.0).abs() < 1e-9);
        assert!((q[30].0).abs() < 1e-9 && (q[30].1 - 0.5).abs() < 1e-9);
    }

    #[test]
    fn surface_only_is_open() {
        let spec = ExportSpec {
            depth_in: 12.0,
            thickness_in: None,
            flare_deg: 0.0,
            gunwale_radius_in: None,
            stations: 41,
            girth: 21,
            pocket_in: None,
            layers: None,
        };
        let m = build_mesh(&off1(), &spec).unwrap();
        assert!(!m.closed);
        let (lo, hi) = m.bbox();
        assert!((hi[2] - 12.0).abs() < 1e-9 && lo[2].abs() < 1e-9);
        assert!((hi[1] + lo[1]).abs() < 1e-9, "symmetric");
    }

    #[test]
    fn bad_specs_error() {
        let h = off1();
        let ok = ExportSpec {
            depth_in: 12.0,
            thickness_in: Some(0.375),
            flare_deg: 0.0,
            gunwale_radius_in: None,
            stations: 41,
            girth: 21,
            pocket_in: None,
            layers: None,
        };
        assert!(build_mesh(&h, &ExportSpec { girth: 20, ..ok.clone() }).is_err());
        assert!(build_mesh(&h, &ExportSpec { depth_in: 0.0, ..ok.clone() }).is_err());
        assert!(build_mesh(&h, &ExportSpec { thickness_in: Some(-1.0), ..ok }).is_err());
    }
}
