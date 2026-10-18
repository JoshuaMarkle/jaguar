//! STEP/STL export: watertight solids, correct size and units, volume consistent with the
//! analysis section model, deterministic files.

use jaguar::export::{ExportSpec, Mesh, build_mesh, to_step, to_stl};
use jaguar::hull::{FORMAT, Grid, Hull, linspace};
use jaguar::section::sections;
use std::collections::HashMap;
use std::path::Path;

fn off1() -> Hull {
    Hull::load(Path::new("tests/golden/off1_hull.json")).unwrap()
}

/// Wall-sided box with closed (zero) end stations.
fn boxy(l: f64, half: f64, draft: f64) -> Hull {
    let x = linspace(0.0, l, 21);
    let z = linspace(0.0, draft, 5);
    let half_breadth = x
        .iter()
        .enumerate()
        .map(|(i, _)| vec![if i == 0 || i == 20 { 0.0 } else { half }; z.len()])
        .collect();
    Hull {
        format: FORMAT.into(),
        units: "in".into(),
        name: "box".into(),
        source: "test".into(),
        length: l,
        draft_design: draft,
        x,
        z,
        half_breadth,
        stem: None,
    }
}

fn spec(depth: f64, t: Option<f64>) -> ExportSpec {
    ExportSpec {
        depth_in: depth,
        thickness_in: t,
        flare_deg: 0.0,
        gunwale_radius_in: None,
        stations: 81,
        girth: 41,
        pocket_in: None,
        layers: None,
        filled: false,
    }
}

/// Every undirected edge used by exactly two facets, once in each direction.
fn watertight(m: &Mesh) -> bool {
    let mut e: HashMap<(u32, u32), i32> = HashMap::new();
    for t in &m.tris {
        for (a, b) in [(t[0], t[1]), (t[1], t[2]), (t[2], t[0])] {
            *e.entry((a.min(b), a.max(b))).or_default() += if a < b { 1 } else { -1 };
            *e.entry((a.min(b) + u32::MAX / 2, a.max(b))).or_default() += 1;
        }
    }
    e.iter().all(|(k, v)| if k.0 >= u32::MAX / 2 { *v == 2 } else { *v == 0 })
}

/// Analysis wall volume (trapz of section area along x), in^3.
fn analysis_volume(h: &Hull, s: &ExportSpec) -> f64 {
    let g = Grid::build_flared(h, s.depth_in, s.stations, s.flare_deg);
    let a = sections(&g, s.thickness_in.unwrap()).area;
    (1..g.nx()).map(|i| 0.5 * (a[i] + a[i - 1]) * (g.x[i] - g.x[i - 1])).sum()
}

#[test]
fn box_solid_volume_is_exact() {
    // Section 20 wide x 12 deep, t = 0.5: exact wall area = outer U minus inner U
    // = (20*12) - (19*11.5) = 21.5 in^2 (the analysis thin-shell model gives t*girth = 22).
    let h = boxy(100.0, 10.0, 6.0);
    let m = build_mesh(&h, &spec(12.0, Some(0.5))).unwrap();
    assert!(m.closed && watertight(&m));
    let solid_ends = 2.0 * 0.5 * (100.0 / 80.0) * 240.0; // closed stems: one station spacing each, ~solid
    let want = 21.5 * (100.0 - 2.0 * 100.0 / 20.0);
    let v = m.volume();
    assert!(v > want && v < want + solid_ends, "volume {v}, want ~{want}");
}

#[test]
fn off1_solid_is_watertight_sized_and_matches_analysis() {
    let h = off1();
    let s = spec(12.0, Some(0.375));
    let m = build_mesh(&h, &s).unwrap();
    assert!(watertight(&m), "solid must be watertight");
    let (lo, hi) = m.bbox();
    assert!((hi[0] - lo[0] - h.length).abs() < 0.01, "length {}", hi[0] - lo[0]);
    let beam = 2.0 * h.half_breadth.iter().flatten().cloned().fold(0.0, f64::max);
    assert!((hi[1] - lo[1] - beam).abs() < 0.01, "beam {}", hi[1] - lo[1]);
    assert!(lo[2].abs() < 1e-9 && (hi[2] - 12.0).abs() < 1e-9);
    // The analysis is a thin shell t x mold-line girth (about 1.6% over the true offset wall
    // amidships) and the export fills its narrow ends solid; on off1 the two nearly cancel
    // (2926 vs 2928 in^3). Counting the empty waterlines below the rockered keel as wall, as the
    // analysis once did, put it 7% over the solid.
    let (v, va) = (m.volume(), analysis_volume(&h, &s));
    assert!((v / va - 1.0).abs() < 0.02, "solid {v:.0} vs analysis {va:.0} in^3");
}

#[test]
fn surface_is_open_and_stl_matches() {
    let h = off1();
    let m = build_mesh(&h, &spec(12.0, None)).unwrap();
    assert!(!m.closed && !watertight(&m));
    let stl = to_stl(&m, "off1");
    assert_eq!(stl.len(), 84 + 50 * m.tris.len());
    assert_eq!(u32::from_le_bytes(stl[80..84].try_into().unwrap()) as usize, m.tris.len());
}

/// Volume inside the analysis grid's mold line up to the mold depth (trapz in z, then x), in^3.
fn grid_volume(h: &Hull, s: &ExportSpec) -> f64 {
    let g = Grid::build_flared(h, s.depth_in, s.stations, s.flare_deg);
    let area: Vec<f64> = (0..g.nx())
        .map(|i| {
            let y = g.row(i);
            (1..g.z.len()).map(|k| (y[k] + y[k - 1]) * (g.z[k] - g.z[k - 1])).sum()
        })
        .collect();
    (1..g.nx()).map(|i| 0.5 * (area[i] + area[i - 1]) * (g.x[i] - g.x[i - 1])).sum()
}

#[test]
fn filled_box_is_the_hull_volume_to_the_deck() {
    // 20 wide x 12 deep, ends tapering to zero over one 5 in table spacing each: 240 in^2 over
    // 90 in plus two wedges of half that over 5 in each.
    let h = boxy(100.0, 10.0, 6.0);
    let m = build_mesh(&h, &ExportSpec { filled: true, ..spec(12.0, None) }).unwrap();
    assert!(m.closed && watertight(&m));
    let v = m.volume();
    assert!((v / (240.0 * 95.0) - 1.0).abs() < 0.01, "filled volume {v:.0} in^3");
    let (lo, hi) = m.bbox();
    assert!(lo[2].abs() < 1e-9 && (hi[2] - 12.0).abs() < 1e-9);
}

#[test]
fn off1_filled_is_a_smooth_solid_of_the_hull_volume() {
    let h = off1();
    let s = ExportSpec { filled: true, ..spec(12.0, None) };
    let m = build_mesh(&h, &s).unwrap();
    assert!(m.closed && watertight(&m), "filled hull must be a watertight solid");
    // Same outer surface as the shelled export, so the same size.
    let shell = build_mesh(&h, &spec(12.0, Some(0.375))).unwrap();
    assert_eq!(m.bbox(), shell.bbox());
    // Its volume is what the analysis grid holds to the mold depth (the solid is the smooth
    // loft resampled by girth, the grid its waterline offsets).
    let (v, vg) = (m.volume(), grid_volume(&h, &s));
    assert!((v / vg - 1.0).abs() < 0.01, "filled {v:.0} vs grid {vg:.0} in^3");
    assert!(v > shell.volume() * 5.0);
    // STEP: one B-spline solid closed by the deck plane, no voids, no open shell.
    let step = to_step(&m, "off1");
    assert!(step.contains("MANIFOLD_SOLID_BREP") && step.contains("B_SPLINE_SURFACE"), "smooth solid");
    assert!(!step.contains("OPEN_SHELL") && !step.contains("BREP_WITH_VOIDS"));
    assert!(step.contains("PLANE("));
    // A wall and filled at once is a contradiction.
    assert!(build_mesh(&h, &ExportSpec { filled: true, ..spec(12.0, Some(0.375)) }).is_err());
}

#[test]
fn step_is_wellformed_inch_and_deterministic() {
    let h = off1();
    let m = build_mesh(&h, &spec(12.0, Some(0.375))).unwrap();
    let a = to_step(&m, "off1");
    assert_eq!(a, to_step(&build_mesh(&h, &spec(12.0, Some(0.375))).unwrap(), "off1"));
    assert!(a.starts_with("ISO-10303-21;") && a.trim_end().ends_with("END-ISO-10303-21;"));
    assert!(a.contains("AUTOMOTIVE_DESIGN") && a.contains("CONVERSION_BASED_UNIT('INCH'"));
    assert!(a.contains("MANIFOLD_SOLID_BREP") && a.contains("CLOSED_SHELL"));
    // Smooth faces: the outer hull and inner wall are cubic B-spline surfaces (two halves each),
    // the top and the two bulkheads closing the hollow are planes; no facets.
    assert_eq!(a.matches("B_SPLINE_SURFACE_WITH_KNOTS(").count(), 4);
    assert_eq!(a.matches("ADVANCED_FACE(").count(), 7);
    assert_eq!(a.matches("PLANE(").count(), 3);
    assert!(a.contains("B_SPLINE_CURVE_WITH_KNOTS('',3,") && !a.contains("LINE("));
    // Every edge is shared by exactly two faces (once each way): watertight by construction.
    let ids = |l: &str| -> Vec<String> {
        l.split('#').skip(1).map(|r| r.chars().take_while(char::is_ascii_digit).collect()).collect()
    };
    let mut uses: HashMap<String, (usize, usize)> = HashMap::new();
    for l in a.lines().filter(|l| l.contains("=ORIENTED_EDGE(")) {
        let e = ids(l.split_once('=').unwrap().1).pop().unwrap();
        let u = uses.entry(e).or_default();
        if l.contains(".T.)") { u.0 += 1 } else { u.1 += 1 }
    }
    assert_eq!(uses.len(), a.matches("=EDGE_CURVE(").count());
    assert!(uses.values().all(|&u| u == (1, 1)), "{uses:?}");
    // Without the structured nets the facets are written, one face each.
    let facets = to_step(&Mesh { brep: None, ..m.clone() }, "off1");
    assert_eq!(facets.matches("ADVANCED_FACE(").count(), m.tris.len());
    // every #ref points to a defined entity
    let defined: std::collections::HashSet<&str> =
        a.lines().filter_map(|l| l.strip_prefix('#')?.split('=').next()).collect();
    for l in a.lines().filter(|l| l.starts_with('#')) {
        let body = l.split_once('=').unwrap().1;
        for r in body.split('#').skip(1) {
            let id: String = r.chars().take_while(char::is_ascii_digit).collect();
            assert!(defined.contains(id.as_str()), "dangling #{id} in {l}");
        }
    }
    let open = to_step(&build_mesh(&h, &spec(12.0, None)).unwrap(), "off1");
    assert!(open.contains("OPEN_SHELL") && open.contains("SHELL_BASED_SURFACE_MODEL"));
    assert_eq!(open.matches("B_SPLINE_SURFACE_WITH_KNOTS(").count(), 2);
}

#[test]
fn flare_widens_the_gunwale() {
    let h = off1();
    let a = build_mesh(&h, &spec(12.0, None)).unwrap().bbox();
    let b = build_mesh(&h, &ExportSpec { flare_deg: 10.0, ..spec(12.0, None) }).unwrap().bbox();
    assert!(b.1[1] > a.1[1] + 1.0);
}

fn closest_on_tri(p: [f64; 3], a: [f64; 3], b: [f64; 3], c: [f64; 3]) -> f64 {
    // Ericson, Real-Time Collision Detection 5.1.5
    let s = |u: [f64; 3], v: [f64; 3]| [u[0] - v[0], u[1] - v[1], u[2] - v[2]];
    let d = |u: [f64; 3], v: [f64; 3]| u[0] * v[0] + u[1] * v[1] + u[2] * v[2];
    let at = |u: [f64; 3], v: [f64; 3], w: f64| [u[0] + w * v[0], u[1] + w * v[1], u[2] + w * v[2]];
    let dist = |q: [f64; 3]| d(s(p, q), s(p, q)).sqrt();
    let (ab, ac, ap) = (s(b, a), s(c, a), s(p, a));
    let (d1, d2) = (d(ab, ap), d(ac, ap));
    if d1 <= 0.0 && d2 <= 0.0 {
        return dist(a);
    }
    let bp = s(p, b);
    let (d3, d4) = (d(ab, bp), d(ac, bp));
    if d3 >= 0.0 && d4 <= d3 {
        return dist(b);
    }
    let vc = d1 * d4 - d3 * d2;
    if vc <= 0.0 && d1 >= 0.0 && d3 <= 0.0 {
        return dist(at(a, ab, d1 / (d1 - d3)));
    }
    let cp = s(p, c);
    let (d5, d6) = (d(ab, cp), d(ac, cp));
    if d6 >= 0.0 && d5 <= d6 {
        return dist(c);
    }
    let vb = d5 * d2 - d1 * d6;
    if vb <= 0.0 && d2 >= 0.0 && d6 <= 0.0 {
        return dist(at(a, ac, d2 / (d2 - d6)));
    }
    let va = d3 * d6 - d5 * d4;
    if va <= 0.0 && (d4 - d3) >= 0.0 && (d5 - d6) >= 0.0 {
        return dist(at(b, s(c, b), (d4 - d3) / ((d4 - d3) + (d5 - d6))));
    }
    let den = 1.0 / (va + vb + vc);
    at(at(a, ab, vb * den), ac, vc * den).iter().zip(p).map(|(q, p)| (q - p).powi(2)).sum::<f64>().sqrt()
}

/// Normal wall thickness at every inner-surface vertex of the solid in the middle `frac` of
/// the length and below `z_max`: distance to the nearest point of the outer mold surface.
/// Returns (min, max, keel samples).
fn wall_thickness(h: &Hull, s: &ExportSpec, frac: f64, z_max: f64) -> (f64, f64, usize) {
    let solid = build_mesh(h, s).unwrap();
    let outer = build_mesh(h, &ExportSpec { thickness_in: None, ..s.clone() }).unwrap();
    let key = |v: [f64; 3]| v.map(|c| (c * 1e6).round() as i64);
    let on_outer: std::collections::HashSet<[i64; 3]> = outer.verts.iter().map(|&v| key(v)).collect();
    let (x0, x1) = (h.length * (0.5 - frac / 2.0), h.length * (0.5 + frac / 2.0));
    let (mut lo, mut hi, mut keel) = (f64::INFINITY, 0.0f64, 0);
    for &p in solid.verts.iter().filter(|v| !on_outer.contains(&key(**v))) {
        if p[0] < x0 || p[0] > x1 || p[2] > z_max {
            continue;
        }
        let d = outer
            .tris
            .iter()
            .map(|t| {
                let [a, b, c] = t.map(|i| outer.verts[i as usize]);
                closest_on_tri(p, a, b, c)
            })
            .fold(f64::INFINITY, f64::min);
        lo = lo.min(d);
        hi = hi.max(d);
        keel += (p[1].abs() < 1e-9) as usize;
    }
    // reverse: outer vertices to the inner surface (catches a wall that is too thick)
    let inner_tris: Vec<[[f64; 3]; 3]> = solid
        .tris
        .iter()
        .map(|t| t.map(|i| solid.verts[i as usize]))
        .filter(|t| t.iter().all(|v| !on_outer.contains(&key(*v))))
        .collect();
    for &p in &outer.verts {
        if p[0] < x0 || p[0] > x1 || p[2] > z_max {
            continue;
        }
        let d =
            inner_tris.iter().map(|[a, b, c]| closest_on_tri(p, *a, *b, *c)).fold(f64::INFINITY, f64::min);
        lo = lo.min(d);
        hi = hi.max(d);
    }
    (lo, hi, keel)
}

/// Keel line of the outer surface: lowest centreline z at each station x.
fn keel_line(m: &Mesh) -> Vec<(f64, f64)> {
    let mut k: std::collections::BTreeMap<i64, f64> = Default::default();
    for v in m.verts.iter().filter(|v| v[1].abs() < 1e-9) {
        let e = k.entry((v[0] * 1e6).round() as i64).or_insert(f64::INFINITY);
        *e = e.min(v[2]);
    }
    k.into_iter().map(|(x, z)| (x as f64 / 1e6, z)).collect()
}

#[test]
fn wall_is_t_thick_normal_to_the_surface() {
    // Before T42 the inner girth was re-resampled by arc length, cutting the bilge corners:
    // off1 at t = 0.375 measured 0.365..0.423 in. Now within 1% over the middle 80%.
    let h = off1();
    for t in [0.25, 0.375, 0.5] {
        let (lo, hi, keel) = wall_thickness(&h, &spec(12.0, Some(t)), 0.8, 11.0);
        assert!(keel > 20, "keel inner points sampled: {keel}");
        assert!(lo > 0.99 * t && hi < 1.015 * t, "t {t}: wall {lo:.4}..{hi:.4}");
    }
    let b = boxy(100.0, 10.0, 6.0);
    let (lo, hi, _) = wall_thickness(&b, &spec(12.0, Some(0.5)), 0.8, 11.0);
    // hi: the outer chine corner is t*sqrt(2) from the inner one (a real corner, not an error)
    assert!((lo - 0.5).abs() < 1e-6 && hi < 0.5 * 2f64.sqrt() + 1e-6, "box wall {lo}..{hi}");
}

#[test]
fn keel_line_is_smooth() {
    // Before T42 the keel snapped to table waterlines: a staircase with |d2 z| up to 0.74 in.
    let h = off1();
    for s in [spec(12.0, None), spec(12.0, Some(0.375)), ExportSpec { stations: 161, ..spec(12.0, None) }] {
        let k = keel_line(&build_mesh(&h, &s).unwrap());
        // Away from the forefoot/heel (outer 5% of length) the keel curvature d2z/dx2 is small.
        let inner: Vec<(f64, f64)> =
            k.iter().copied().filter(|p| p.0 > 0.05 * h.length && p.0 < 0.95 * h.length).collect();
        let dx = inner[1].0 - inner[0].0;
        let d2 = inner.windows(3).map(|w| (w[0].1 - 2.0 * w[1].1 + w[2].1).abs()).fold(0.0, f64::max);
        let kap = d2 / (dx * dx);
        assert!(kap < 0.01, "max keel curvature {kap} /in (|d2 z| {d2} in)");
        // monotone down to the low point and up after it (no sawtooth)
        let lo = inner.iter().enumerate().min_by(|a, b| a.1.1.total_cmp(&b.1.1)).unwrap().0;
        assert!(inner[..=lo].windows(2).all(|w| w[1].1 <= w[0].1 + 1e-9));
        assert!(inner[lo..].windows(2).all(|w| w[1].1 >= w[0].1 - 1e-9));
    }
}

/// off1 as the run builds it with a stem radius (the default 4 in, or `r`).
fn off1_stem(r: f64) -> Hull {
    let mut hp = jaguar::params::Params::load(Path::new("examples/canoe.toml")).unwrap().hull;
    hp.stem_radius_in = r;
    hp.doubly_symmetric = false;
    hp.templates = None;
    off1().prepared(&hp).unwrap()
}

#[test]
fn keel_is_smooth_to_the_stems() {
    // Before the smooth-keel loft the table was blended at fixed heights between stations: near
    // the ends (where the keel rises) the keel sat flat for a station spacing, then stepped up
    // to 0.4 in (0.8 in on a shape-search hull); a 4 in stem radius then folded the sections
    // near the keel. Now the keel is the monotone cubic through the station keels, the stem arc
    // tangent to it, over the whole length.
    for r in [0.0, 4.0] {
        let h = if r > 0.0 { off1_stem(r) } else { off1() };
        let xt = h.stem.as_ref().map_or(0.0, |s| s.bow.unwrap()[2]);
        let k = keel_line(&build_mesh(&h, &spec(12.0, Some(0.375))).unwrap());
        // One low point, monotone down to it from the bow and up after it to the stern.
        let lo = k.iter().enumerate().min_by(|a, b| a.1.1.total_cmp(&b.1.1)).unwrap().0;
        assert!(k[..=lo].windows(2).all(|w| w[1].1 <= w[0].1 + 1e-9), "r {r}: sawtooth before the low point");
        assert!(k[lo..].windows(2).all(|w| w[1].1 >= w[0].1 - 1e-9), "r {r}: sawtooth after the low point");
        // No steps: away from the stem arc the slope changes by little from one station to
        // the next (a step is a flat run then a jump).
        let worst = k
            .windows(3)
            .filter(|w| w[0].0 > xt + 1.0 && w[2].0 < h.length - xt - 1.0)
            .map(|w| ((w[2].1 - w[1].1) / (w[2].0 - w[1].0) - (w[1].1 - w[0].1) / (w[1].0 - w[0].0)).abs())
            .fold(0.0, f64::max);
        assert!(worst < 0.1, "r {r}: keel slope jumps by {worst} between stations");
    }
}

#[test]
fn sections_never_fold() {
    // Every exported girth (outer hull and inner wall) narrows going down from the sheer to the
    // keel: no wing sticking out near the keel. off1 with and without a stem radius, beamy, and
    // a generated (shape search) hull with rocker.
    let shape = jaguar::hullgen::generate(
        &jaguar::hullgen::ShapeParams {
            length_in: 216.0,
            beam_in: 19.6,
            draft_in: 6.6,
            fore_wp: 0.48,
            section: 0.55,
            fore_rocker: 0.56,
            aft_wp: 0.38,
            aft_rocker: 0.59,
            fore_len_frac: 0.43,
            aft_len_frac: 0.43,
            gunwale_radius_in: None,
            metrics: None,
        },
        41,
        21,
        "gen",
    )
    .unwrap();
    let beamy = Hull::load(Path::new("examples/beamy_hull.json")).unwrap();
    let mut hp = jaguar::params::Params::load(Path::new("examples/canoe.toml")).unwrap().hull;
    hp.templates = None;
    hp.doubly_symmetric = false;
    for (name, h) in [
        ("off1", off1()),
        ("off1 r4", off1_stem(4.0)),
        ("beamy r4", beamy.prepared(&hp).unwrap()),
        ("shape r4", shape.prepared(&hp).unwrap()),
    ] {
        let b = build_mesh(&h, &spec(12.0, Some(0.375))).unwrap().brep.expect("B-spline nets");
        // The outer girths strictly; the inner wall within 0.05 in (its offset is scaled for the
        // surface's tilt along x, point by point, so near a tapering tip it can bulge a little
        // at a knuckle; the wing a fold makes was about 1 in).
        for (side, rows, tol) in [("outer", &b.outer, 1e-9), ("inner", b.inner.as_ref().unwrap(), 0.05)] {
            for (x, g) in rows.iter() {
                for w in g.windows(2) {
                    assert!(w[1].0 <= w[0].0 + tol, "{name} {side} folds at x {x}: {:?}", g);
                }
            }
        }
    }
}

fn pockets(s: ExportSpec, l: f64) -> ExportSpec {
    ExportSpec { pocket_in: Some(l), ..s }
}

/// Points of the mesh at x (within 1e-6).
fn at_x(m: &Mesh, x: f64) -> Vec<[f64; 3]> {
    m.verts.iter().copied().filter(|v| (v[0] - x).abs() < 1e-6).collect()
}

#[test]
fn box_pockets_are_sealed_voids_under_a_cover_and_behind_a_bulkhead() {
    // Box section 20 x 12, t = 0.5, no layer count: bulkhead and cover 0.5 thick. Stations on
    // the box's own 5 in spacing, so the hull is hollow from x = 5 with the full inner U
    // 19 x 11.5 = 218.5 in^2. Pockets of 20 in from each tip: bulkheads fill x in [20, 20.5] and
    // [79.5, 80]; covers fill the top 0.5 in of the interior (19 x 0.5) from x 5 to 20 and 80 to
    // 95, over a sealed void 19 x 11 x 15. 23 girth points land one on the chine corner.
    let h = boxy(100.0, 10.0, 6.0);
    let s = ExportSpec { girth: 23, stations: 21, ..spec(12.0, Some(0.5)) };
    let plain = build_mesh(&h, &s).unwrap();
    let m = build_mesh(&h, &pockets(s.clone(), 20.0)).unwrap();
    assert!(m.closed && watertight(&m), "watertight with the bulkheads, covers and voids");
    // At x = 5 (next to the closed tip) the inner wall is the mitred 45 deg offset, 10 - t sqrt 2
    // from the centreline, as in the open hull; the cover tapers from there to 19 wide at x = 10.
    let w5 = 2.0 * (10.0 - 0.5 * 2f64.sqrt());
    let cover = 0.5 * (5.0 * 0.5 * (w5 + 19.0) + 10.0 * 19.0);
    let dv = m.volume() - plain.volume();
    let want = 2.0 * 218.5 * 0.5 + 2.0 * cover;
    assert!((dv - want).abs() < 1e-6, "bulkheads + covers {dv} vs {want}");
    assert_eq!(m.bbox(), plain.bbox());
    // Three shells: the canoe and one void per pocket, each void enclosing the air (negative).
    let comp = jaguar::export::components(&m);
    assert_eq!(comp.iter().max(), Some(&2));
    let mut vols = [0.0; 3];
    for (t, &k) in m.tris.iter().zip(&comp) {
        let one = Mesh { verts: m.verts.clone(), tris: vec![*t], closed: false, brep: None };
        vols[k] += one.volume();
    }
    let mut v = vols.to_vec();
    v.sort_by(f64::total_cmp);
    // (x = 5 has a mitred inner bottom too, so the void is a little under 11 x the cover width.)
    let void = 11.0 * (5.0 * 0.5 * (w5 + 19.0) + 10.0 * 19.0);
    assert!((v[0] - v[1]).abs() < 1e-6 && v[2] > 0.0, "{vols:?}");
    assert!(v[0] < 0.0 && (-v[0] / void - 1.0).abs() < 0.01, "{vols:?} vs {void}");
    // The void ends at the pocket face of each bulkhead, under the cover; the face toward the
    // middle of the canoe reaches the top (open interior).
    for x in [20.0, 80.0] {
        let p = at_x(&m, x);
        assert!(p.iter().any(|v| v[1].abs() < 1e-9 && (v[2] - 11.5).abs() < 1e-9), "void lid at {x}");
        assert!(
            p.iter().any(|v| (v[1] - 9.5).abs() < 1e-9 && (v[2] - 0.5).abs() < 1e-9),
            "void bilge at {x}"
        );
    }
    for x in [20.5, 79.5] {
        let p = at_x(&m, x);
        assert!(p.iter().any(|v| v[1].abs() < 1e-9 && (v[2] - 12.0).abs() < 1e-9), "bulkhead top at {x}");
    }
    // STEP: one solid with two voids.
    let st = to_step(&m, "b");
    assert!(st.contains("BREP_WITH_VOIDS") && st.matches("ORIENTED_CLOSED_SHELL").count() == 2);
    assert!(!to_step(&plain, "b").contains("BREP_WITH_VOIDS"));
    // zero length = no pockets: the same file as without
    let z = build_mesh(&h, &pockets(s, 0.0)).unwrap();
    assert_eq!(to_stl(&z, "b"), to_stl(&plain, "b"));
}

#[test]
fn bulkheads_are_one_casting_layer_thick() {
    // 3 layers of a 3/8 in wall: 1/8 in bulkheads, at [20, 20.125] and [79.875, 80].
    let h = boxy(100.0, 10.0, 6.0);
    let s = ExportSpec { girth: 23, stations: 21, layers: Some(3), ..spec(12.0, Some(0.375)) };
    assert_eq!(s.bulkhead_in(), Some(0.125));
    let plain = build_mesh(&h, &s).unwrap();
    let m = build_mesh(&h, &pockets(s, 20.0)).unwrap();
    assert!(m.closed && watertight(&m));
    // Bulkheads (inner U 19.25 x 11.625) and covers (19.25 wide, x 5 to 20) all 1/8 in thick.
    let (w, d) = (2.0 * (10.0 - 0.375), 12.0 - 0.375);
    let w5 = 2.0 * (10.0 - 0.375 * 2f64.sqrt());
    let want = 2.0 * w * d * 0.125 + 2.0 * 0.125 * (5.0 * 0.5 * (w5 + w) + 10.0 * w);
    let dv = m.volume() - plain.volume();
    assert!((dv - want).abs() < 1e-6, "{dv} vs {want}");
    for x in [20.0, 80.0] {
        assert!(at_x(&m, x).iter().any(|v| v[1].abs() < 1e-9 && (v[2] - 11.875).abs() < 1e-9), "lid at {x}");
    }
    for x in [20.125, 79.875] {
        assert!(at_x(&m, x).iter().any(|v| v[1].abs() < 1e-9 && (v[2] - 12.0).abs() < 1e-9), "face at {x}");
    }
    assert!(at_x(&m, 20.375).is_empty(), "not a full wall thick");
}

#[test]
fn off1_pockets_stay_a_valid_solid() {
    let h = off1();
    let s = spec(12.0, Some(0.375));
    let v0 = build_mesh(&h, &s).unwrap().volume();
    let mut last = 0.0;
    // 3.6 in is the examples/canoe.toml winner's pocket; longer ones reach fuller sections.
    for l in [3.6, 12.0, 30.0] {
        let m = build_mesh(&h, &pockets(s.clone(), l)).unwrap();
        assert!(watertight(&m), "l {l}: watertight");
        let dv = m.volume() - v0;
        assert!(dv > last, "l {l}: bulkheads add {dv} in^3");
        last = dv;
        let a = to_step(&m, "off1");
        assert!(a.contains("BREP_WITH_VOIDS") && a.matches("ORIENTED_CLOSED_SHELL").count() == 2, "l {l}");
    }
}

#[test]
fn bad_pockets_error() {
    let h = boxy(100.0, 10.0, 6.0);
    let s = spec(12.0, Some(0.5));
    let e = build_mesh(&h, &pockets(s.clone(), 49.9)).unwrap_err().to_string();
    assert!(e.contains("overlap"), "{e}");
    let e = build_mesh(&h, &pockets(s.clone(), 0.1)).unwrap_err().to_string();
    assert!(e.contains("solid concrete end") && e.contains("--no-pockets"), "{e}");
    assert!(build_mesh(&h, &pockets(s.clone(), -1.0)).is_err());
    assert!(build_mesh(&h, &pockets(s, f64::NAN)).is_err());
    // the mold surface has no wall to put a bulkhead in: pockets ignored
    let open = build_mesh(&h, &pockets(spec(12.0, None), 20.0)).unwrap();
    assert!(!open.closed);
}

#[test]
fn from_result_takes_the_winner_air_pocket() {
    let h = boxy(100.0, 10.0, 6.0);
    let doc = |pocket: serde_json::Value| {
        serde_json::json!({
            "format": jaguar::optimize::RESULT_FORMAT,
            "params": {"hull": {"flare_deg": 0.0}},
            "winner_hull": h,
            "winner": {"candidate": {"thickness_in": 0.5, "depth_in": 12.0, "air_pocket_in": pocket}},
        })
        .to_string()
    };
    let (_, s) = jaguar::export::from_result(&doc(20.0.into()), 41, 21).unwrap();
    assert_eq!(s.pocket_in, Some(20.0));
    // No [punching] in this bare document: whole-wall bulkheads.
    assert_eq!((s.layers, s.bulkhead_in()), (None, Some(0.5)));
    // A real run's punching passes: examples/canoe.toml casts in 2 layers.
    let p = jaguar::params::Params::load(std::path::Path::new("examples/canoe.toml")).unwrap();
    let mut v: serde_json::Value = serde_json::from_str(&doc(20.0.into())).unwrap();
    v["params"]["punching"] = serde_json::to_value(&p.punching).unwrap();
    v["params"]["punching"]["layers"] = 3.into();
    v["params"]["punching"]["mesh_after_layer"] = 1.into();
    let (_, s) = jaguar::export::from_result(&v.to_string(), 41, 21).unwrap();
    assert_eq!((s.layers, s.bulkhead_in()), (Some(3), Some(0.5 / 3.0)));
    let (_, s) = jaguar::export::from_result(&doc(serde_json::Value::Null), 41, 21).unwrap();
    assert_eq!(s.pocket_in, None);
}
