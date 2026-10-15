//! hull.doubly_symmetric: bow and stern identical (as well as port and starboard) all the way
//! from the hull table to the analysis grid and the exported solid.
use jaguar::hull::{Grid, Hull, Topsides};
use jaguar::params::Params;
use std::collections::HashSet;
use std::path::Path;

/// Largest |half-breadth(i) - half-breadth(mirror of i)| over a table whose stations are mirrored.
fn table_asymmetry(h: &Hull) -> f64 {
    let (n, l) = (h.x.len(), h.x[0] + h.x[h.x.len() - 1]);
    let mut worst: f64 = 0.0;
    for i in 0..n {
        assert!((h.x[i] + h.x[n - 1 - i] - l).abs() < 1e-9, "stations are mirrored");
        for k in 0..h.z.len() {
            worst = worst.max((h.half_breadth[i][k] - h.half_breadth[n - 1 - i][k]).abs());
        }
    }
    worst
}

fn grid_asymmetry(g: &Grid) -> f64 {
    let n = g.nx();
    let mut worst: f64 = 0.0;
    for i in 0..n {
        for k in 0..g.nz() {
            worst = worst.max((g.y(i, k) - g.y(n - 1 - i, k)).abs());
        }
    }
    worst
}

#[test]
fn an_asymmetric_hull_file_is_made_doubly_symmetric() {
    let h = Hull::load(Path::new("examples/beamy_hull.json")).unwrap();
    let s = h.symmetrized();
    assert_eq!(table_asymmetry(&s), 0.0);
    // Each station is the mean of the hull at x and L - x.
    let mid = s.x.len() / 2;
    let x = s.x[mid / 2];
    let near = |hh: &Hull, xx: f64| {
        let j = hh.x.partition_point(|&v| v <= xx).clamp(1, hh.x.len() - 1);
        let t = (xx - hh.x[j - 1]) / (hh.x[j] - hh.x[j - 1]);
        hh.half_breadth[j - 1][3] * (1.0 - t) + hh.half_breadth[j][3] * t
    };
    let want = 0.5 * (near(&h, x) + near(&h, h.length - x));
    assert!((s.half_breadth[mid / 2][3] - want).abs() < 1e-12);
    assert_eq!((s.length, s.draft_design), (h.length, h.draft_design));
    // An already symmetric hull (off1) keeps its shape.
    let o = Hull::load(Path::new("examples/off1_hull.json")).unwrap();
    let os = o.symmetrized();
    for (i, &xx) in o.x.iter().enumerate() {
        let j = os.x.iter().position(|v| (v - xx).abs() < 1e-6).unwrap();
        for k in 0..o.z.len() {
            assert!((os.half_breadth[j][k] - o.half_breadth[i][k]).abs() < 1e-9);
        }
    }
}

#[test]
fn the_whole_build_chain_stays_symmetric() {
    // Symmetrize, then 11 templates and a forefoot radius; flared topsides and a line-arc-line
    // gunwale on the analysis grid; the exported solid with air pockets.
    let mut p = Params::load(Path::new("examples/beamy.toml")).unwrap();
    p.hull.doubly_symmetric = true;
    p.hull.templates = Some(11);
    p.hull.keel_fillet_radius_in = Some(0.5);
    let h = Hull::load(p.hull.file.as_ref().unwrap()).unwrap().prepared(&p.hull).unwrap();
    assert!(table_asymmetry(&h) < 1e-9, "{}", table_asymmetry(&h));
    let (g, why) = Grid::build_topsides(&h, 13.0, 161, Topsides { flare_deg: 3.0, ..p.hull.topsides() });
    assert!(why.is_none(), "{why:?}");
    assert!(grid_asymmetry(&g) < 1e-6, "flared: {}", grid_asymmetry(&g));
    // A line-arc-line gunwale needs straight ends (beamy's full ends take none): a generated
    // doubly symmetric shape with one, through the same build.
    let shape = jaguar::hullgen::ShapeParams {
        length_in: 240.0,
        beam_in: 24.0,
        draft_in: 6.0,
        fore_wp: 1.3,
        section: 0.5,
        fore_rocker: 0.3,
        aft_wp: 1.3,
        aft_rocker: 0.3,
        fore_len_frac: 0.4,
        aft_len_frac: 0.4,
        gunwale_radius_in: Some(400.0),
        metrics: None,
    };
    let only_sym =
        jaguar::params::HullParams { templates: None, keel_fillet_radius_in: None, ..p.hull.clone() };
    let gh = jaguar::hullgen::generate(&shape, 41, 21, "sym").unwrap().prepared(&only_sym).unwrap();
    let ts = Topsides { flare_deg: 3.0, gunwale_radius_in: Some(400.0), gunwale_max_flare_deg: 89.0 };
    let (g, why) = Grid::build_topsides(&gh, 12.0, 161, ts);
    assert!(why.is_none(), "{why:?}");
    assert!(grid_asymmetry(&g) < 1e-6, "gunwale: {}", grid_asymmetry(&g));
    let spec = jaguar::export::ExportSpec {
        depth_in: 13.0,
        thickness_in: Some(0.375),
        flare_deg: 3.0,
        gunwale_radius_in: None,
        stations: 161,
        girth: 41,
        pocket_in: Some(12.0),
        layers: Some(2),
    };
    let m = jaguar::export::build_mesh(&h, &spec).unwrap();
    let key =
        |v: [f64; 3]| [(v[0] * 1e4).round() as i64, (v[1] * 1e4).round() as i64, (v[2] * 1e4).round() as i64];
    let set: HashSet<[i64; 3]> = m.verts.iter().map(|&v| key(v)).collect();
    let (lo, hi) = m.bbox();
    let missing = m.verts.iter().filter(|v| !set.contains(&key([lo[0] + hi[0] - v[0], v[1], v[2]]))).count();
    assert_eq!(missing, 0, "every vertex has its bow-stern mirror");
}

#[test]
fn doubly_symmetric_and_shape_search_symmetric_are_one_switch() {
    let base = std::fs::read_to_string("examples/shape_opt.toml").unwrap();
    let dir = Path::new("examples");
    let on = base.replacen("[hull]", "[hull]\ndoubly_symmetric = true", 1);
    let p = Params::parse(&on, dir).unwrap();
    assert!(p.hull.doubly_symmetric && p.shape_search.as_ref().unwrap().symmetric);
    let legacy = base.replace("# symmetric = false", "symmetric = true");
    let p = Params::parse(&legacy, dir).unwrap();
    assert!(p.hull.doubly_symmetric && p.shape_search.as_ref().unwrap().symmetric);
    let off = Params::parse(&base, dir).unwrap();
    assert!(!off.hull.doubly_symmetric);
}
