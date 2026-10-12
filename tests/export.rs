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
    ExportSpec { depth_in: depth, thickness_in: t, flare_deg: 0.0, stations: 81, girth: 41 }
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
    // The analysis takes t x the full table girth, which includes zero-breadth waterlines in the
    // rockered ends (a vertical "wall" on the centreline that does not exist: ~250 in^3 on off1)
    // and a thin-shell t x girth on the mold line. The real solid is lighter, by under 15%.
    let (v, va) = (m.volume(), analysis_volume(&h, &s));
    assert!(v < va && v > 0.85 * va, "solid {v:.0} vs analysis {va:.0} in^3");
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
