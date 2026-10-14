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

#[test]
fn step_is_wellformed_inch_and_deterministic() {
    let h = off1();
    let m = build_mesh(&h, &spec(12.0, Some(0.375))).unwrap();
    let a = to_step(&m, "off1");
    assert_eq!(a, to_step(&build_mesh(&h, &spec(12.0, Some(0.375))).unwrap(), "off1"));
    assert!(a.starts_with("ISO-10303-21;") && a.trim_end().ends_with("END-ISO-10303-21;"));
    assert!(a.contains("AUTOMOTIVE_DESIGN") && a.contains("CONVERSION_BASED_UNIT('INCH'"));
    assert!(a.contains("MANIFOLD_SOLID_BREP") && a.contains("CLOSED_SHELL"));
    assert_eq!(a.matches("ADVANCED_FACE(").count(), m.tris.len());
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

fn pockets(s: ExportSpec, l: f64) -> ExportSpec {
    ExportSpec { pocket_in: Some(l), ..s }
}

/// Points of the mesh at x (within 1e-6).
fn at_x(m: &Mesh, x: f64) -> Vec<[f64; 3]> {
    m.verts.iter().copied().filter(|v| (v[0] - x).abs() < 1e-6).collect()
}

#[test]
fn box_pocket_bulkheads_are_fused_t_thick_at_the_pocket_ends() {
    // Box section 20 x 12, t = 0.5: inner U = 19 x 11.5 = 218.5 in^2. Pockets of 20 in from
    // each tip: bulkheads fill x in [20, 20.5] and [79.5, 80], each exactly 218.5 * 0.5 in^3.
    // 23 girth points: 2 in apart on the 22 in half girth, so one lands on the chine corner.
    let h = boxy(100.0, 10.0, 6.0);
    let s = ExportSpec { girth: 23, ..spec(12.0, Some(0.5)) };
    let plain = build_mesh(&h, &s).unwrap();
    let m = build_mesh(&h, &pockets(s.clone(), 20.0)).unwrap();
    assert!(m.closed && watertight(&m), "one watertight solid with the bulkheads");
    let dv = m.volume() - plain.volume();
    assert!((dv - 2.0 * 218.5 * 0.5).abs() < 1e-6, "bulkhead volume {dv}");
    assert_eq!(m.bbox(), plain.bbox());
    for x in [20.0, 20.5, 79.5, 80.0] {
        let p = at_x(&m, x);
        // bulkhead face: inner U corner and its top centre
        assert!(p.iter().any(|v| v[1].abs() < 1e-9 && (v[2] - 12.0).abs() < 1e-9), "top centre at {x}");
        assert!(
            p.iter().any(|v| (v[1] - 9.5).abs() < 1e-9 && (v[2] - 0.5).abs() < 1e-9),
            "inner bilge at {x}"
        );
    }
    // zero length = no bulkheads: the same file as without
    let z = build_mesh(&h, &pockets(s, 0.0)).unwrap();
    assert_eq!(to_stl(&z, "b"), to_stl(&plain, "b"));
}

#[test]
fn bulkheads_are_one_casting_layer_thick() {
    // 3 layers of a 3/8 in wall: 1/8 in bulkheads, at [20, 20.125] and [79.875, 80].
    let h = boxy(100.0, 10.0, 6.0);
    let s = ExportSpec { girth: 23, layers: Some(3), ..spec(12.0, Some(0.375)) };
    assert_eq!(s.bulkhead_in(), Some(0.125));
    let plain = build_mesh(&h, &s).unwrap();
    let m = build_mesh(&h, &pockets(s, 20.0)).unwrap();
    assert!(m.closed && watertight(&m));
    // Inner U 19.25 x 11.625 = 223.78 in^2, two bulkheads 1/8 in thick.
    let inner = 2.0 * (10.0 - 0.375) * (12.0 - 0.375);
    assert!(
        (m.volume() - plain.volume() - 2.0 * inner * 0.125).abs() < 1e-6,
        "{}",
        m.volume() - plain.volume()
    );
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
        assert!(a.contains("MANIFOLD_SOLID_BREP") && a.matches("CLOSED_SHELL").count() == 1);
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
