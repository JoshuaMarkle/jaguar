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
    /// Longitudinal stations (resampled evenly, as the analysis grid).
    pub stations: usize,
    /// Points around the full girth, sheer to sheer (odd, so one lies on the keel centreline).
    pub girth: usize,
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

/// Starboard half girth of one station, sheer -> centreline keel point, consecutive duplicates
/// removed. `None` if the station has no offsets (all zero).
fn half_girth(y: &[f64], z: &[f64]) -> Option<Vec<(f64, f64)>> {
    let first = y.iter().position(|&v| v > 1e-9)?;
    let k0 = first.saturating_sub(1);
    let mut p: Vec<(f64, f64)> =
        (k0..y.len()).rev().map(|k| (if k == k0 && y[k] <= 1e-9 { 0.0 } else { y[k] }, z[k])).collect();
    if y[k0] > 1e-9 {
        p.push((0.0, z[k0]));
    }
    p.dedup_by(|a, b| (a.0 - b.0).abs() < 1e-12 && (a.1 - b.1).abs() < 1e-12);
    Some(p)
}

/// Inward offset by `t` of a resampled starboard half girth (last point on the centreline).
fn offset_inward(p: &[(f64, f64)], t: f64, z_top: f64) -> Vec<(f64, f64)> {
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
            let m = (t / c.max(1e-9)).min(2.0 * t);
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
    let grid = Grid::build_flared(hull, spec.depth_in, spec.stations, spec.flare_deg);
    let z_top = *grid.z.last().unwrap();
    let halves: Vec<Option<Vec<(f64, f64)>>> =
        (0..grid.nx()).map(|i| half_girth(grid.row(i), &grid.z)).collect();
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
                let q = offset_inward(half, t, z_top);
                // Points pushed onto/through the centreline (V keel narrower than the wall) are
                // dropped; the inner girth then runs straight to its centreline point.
                let (keel, sides) = q.split_last().unwrap();
                let mut inner: Vec<(f64, f64)> = sides.iter().copied().filter(|p| p.0 > 1e-6).collect();
                inner.push(*keel);
                let ok = inner.len() >= 3 && keel.1 < z_top - 1e-6 && inner[0].0 > 1e-6;
                ok.then(|| resample(&inner, half.len()))
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
        let mut inner = vec![usize::MAX; nr];
        for r in ra..=rb {
            inner[r] = push_row(rows[r].0, &full(inner_half[r].as_ref().unwrap()));
        }
        for r in ra..rb {
            for j in 0..m - 1 {
                let (a, b) = (inner[r] + j, inner[r + 1] + j);
                quad(&mut tris, a, a + 1, b + 1, b);
            }
            for j in [0, m - 1] {
                let (oa, ob, ia, ib) = (outer[r] + j, outer[r + 1] + j, inner[r] + j, inner[r + 1] + j);
                quad(&mut tris, oa, ob, ib, ia);
            }
        }
        // Solid ends: a planar bulkhead closing the inner U at ra / rb (fanned from its top
        // centre c), and a flat top over the solid end, as a strip between the two sheer lines.
        for (rc, step) in [(ra, -1i64), (rb, 1i64)] {
            let c = push_row(rows[rc].0, &[(0.0, z_top)]);
            for j in 0..m - 1 {
                tris.push([c as u32, (inner[rc] + j) as u32, (inner[rc] + j + 1) as u32]);
            }
            let next = (rc as i64 + step) as usize;
            // First strip cell: its row-rc side is split at the inner sheers and c.
            let apex = outer[next] as u32;
            let chain = [outer[rc], inner[rc], c, inner[rc] + m - 1, outer[rc] + m - 1, outer[next] + m - 1];
            for k in 0..chain.len() - 1 {
                tris.push([apex, chain[k] as u32, chain[k + 1] as u32]);
            }
            let mut r = next;
            while (step < 0 && r > 0) || (step > 0 && r < nr - 1) {
                let s = (r as i64 + step) as usize;
                quad(&mut tris, outer[r], outer[s], outer[s] + m - 1, outer[r] + m - 1);
                r = s;
            }
        }
    }
    let mut mesh = clean(Mesh { verts, tris, closed });
    orient(&mut mesh)?;
    if mesh.volume() < 0.0 || (!closed && outward_flip(&mesh)) {
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
    let mut faces = Vec::with_capacity(mesh.tris.len());
    for t in &mesh.tris {
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
        faces.push(w.e(format!("ADVANCED_FACE('',(#{fb}),#{pl},.T.)")));
    }
    let list = faces.iter().map(|f| format!("#{f}")).collect::<Vec<_>>().join(",");
    let rep = if mesh.closed {
        let sh = w.e(format!("CLOSED_SHELL('',({list}))"));
        let br = w.e(format!("MANIFOLD_SOLID_BREP('{name}',#{sh})"));
        w.e(format!("ADVANCED_BREP_SHAPE_REPRESENTATION('{name}',(#{ax},#{br}),#{ctx})"))
    } else {
        let sh = w.e(format!("OPEN_SHELL('',({list}))"));
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
/// params.hull.file), thickness, depth and the run's flare. Err if there is no winner.
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
        flare_deg: v["params"]["hull"]["flare_deg"].as_f64().unwrap_or(0.0),
        stations,
        girth,
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
        let q = offset_inward(&p, 0.5, 10.0);
        assert!((q[0].0 - 4.5).abs() < 1e-9 && (q[0].1 - 10.0).abs() < 1e-9);
        assert!((q[30].0).abs() < 1e-9 && (q[30].1 - 0.5).abs() < 1e-9);
    }

    #[test]
    fn surface_only_is_open() {
        let spec = ExportSpec { depth_in: 12.0, thickness_in: None, flare_deg: 0.0, stations: 41, girth: 21 };
        let m = build_mesh(&off1(), &spec).unwrap();
        assert!(!m.closed);
        let (lo, hi) = m.bbox();
        assert!((hi[2] - 12.0).abs() < 1e-9 && lo[2].abs() < 1e-9);
        assert!((hi[1] + lo[1]).abs() < 1e-9, "symmetric");
    }

    #[test]
    fn bad_specs_error() {
        let h = off1();
        let ok =
            ExportSpec { depth_in: 12.0, thickness_in: Some(0.375), flare_deg: 0.0, stations: 41, girth: 21 };
        assert!(build_mesh(&h, &ExportSpec { girth: 20, ..ok.clone() }).is_err());
        assert!(build_mesh(&h, &ExportSpec { depth_in: 0.0, ..ok.clone() }).is_err());
        assert!(build_mesh(&h, &ExportSpec { thickness_in: Some(-1.0), ..ok }).is_err());
    }
}
