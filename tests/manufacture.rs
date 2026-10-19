//! Hardboard for the example winner's foam mold: tracing templates (default) and boards in the
//! mold, stack, pieces, packing and DXF.

use jaguar::manufacture::{
    Boards, Layout, MoldSpec, Orientation, Packing, Pt, dxf_name, from_result, to_dxf,
};

/// The examples/canoe.toml winner as a result.json (run once for all tests).
fn example() -> String {
    static RESULT: std::sync::OnceLock<String> = std::sync::OnceLock::new();
    RESULT
        .get_or_init(|| {
            let p = jaguar::params::Params::load(std::path::Path::new("examples/canoe.toml")).unwrap();
            serde_json::to_string(&jaguar::run(&p, &|_| {}).unwrap()).unwrap()
        })
        .clone()
}

/// Its flat tracing templates with the default settings (built once: nesting is slow in debug).
fn flat() -> &'static Layout {
    static FLAT: std::sync::OnceLock<Layout> = std::sync::OnceLock::new();
    FLAT.get_or_init(|| from_result(&example(), &MoldSpec::default()).unwrap())
}

/// Upright slabs with thick foam (few slabs, quick to pack).
fn ribs_spec() -> MoldSpec {
    MoldSpec { orientation: Orientation::Vertical, foam_in: vec![12.0], ..MoldSpec::default() }
}

fn bbox(p: &[Pt]) -> (Pt, Pt) {
    p.iter().fold(([f64::INFINITY; 2], [f64::NEG_INFINITY; 2]), |(lo, hi), q| {
        ([lo[0].min(q[0]), lo[1].min(q[1])], [hi[0].max(q[0]), hi[1].max(q[1])])
    })
}

fn shoelace(p: &[Pt]) -> f64 {
    let n = p.len();
    0.5 * (0..n).map(|i| p[i][0] * p[(i + 1) % n][1] - p[(i + 1) % n][0] * p[i][1]).sum::<f64>()
}

/// Every placed piece on its sheet, inside the gap margin, and every piece placed once.
fn check_sheets(l: &Layout) {
    let s = &l.spec;
    let mut seen = vec![0; l.pieces.len()];
    for sh in &l.sheets {
        for p in &sh.parts {
            seen[p.piece] += 1;
            let (lo, hi) = bbox(&p.outline);
            assert!(lo[0] >= s.gap_in - 1e-9 && lo[1] >= s.gap_in - 1e-9, "{} off the sheet", p.piece);
            assert!(hi[0] <= sh.length_in - s.gap_in + 1e-9 && hi[1] <= sh.width_in - s.gap_in + 1e-9);
            let a = l.pieces[p.piece].area_in2;
            assert!((shoelace(&p.outline) - a).abs() < 1e-6 * a.max(1.0), "turning keeps the area");
        }
    }
    assert!(seen.iter().all(|&k| k == 1), "each piece placed exactly once");
}

/// Does the outline fit an l x w rectangle in a quarter turn?
fn fits(p: &[Pt], l: f64, w: f64) -> bool {
    let (lo, hi) = bbox(p);
    let (a, b) = (hi[0] - lo[0], hi[1] - lo[1]);
    (a <= l + 1e-9 && b <= w + 1e-9) || (b <= l + 1e-9 && a <= w + 1e-9)
}

/// A generated hull whose section is blunter than a parabola at the keel: the mold (the inner
/// surface) is the mold depth less the wall high, keel to gunwale.
#[test]
fn mold_height_is_depth_less_wall_on_a_generated_hull() {
    let s = jaguar::hullgen::ShapeParams {
        length_in: 216.0,
        beam_in: 28.64,
        draft_in: 7.0,
        fore_wp: 0.818,
        section: 0.706,
        fore_rocker: 0.221,
        aft_wp: 0.818,
        aft_rocker: 0.221,
        fore_len_frac: 0.5,
        aft_len_frac: 0.5,
        gunwale_radius_in: None,
        metrics: None,
    };
    let hull = jaguar::hullgen::generate(&s, 41, 21, "blunt").unwrap();
    let es = jaguar::export::ExportSpec {
        depth_in: 14.875,
        thickness_in: Some(0.375),
        flare_deg: 3.0,
        gunwale_radius_in: None,
        stations: 241,
        girth: 41,
        pocket_in: None,
        layers: None,
        filled: false,
    };
    let spec = MoldSpec { foam_in: vec![8.0], ..MoldSpec::default() };
    let st = jaguar::manufacture::layout(&hull, &es, &spec).unwrap().stack;
    // 14.5 in less ~0.001: the lofted keel is a 4.6 deg V, where a wall t thick normal to the
    // surface is t / cos 4.6 deg deep at the centreline. (Before the keel lift fix: 14.37 in.)
    assert!((st.height_in - 14.5).abs() < 0.003, "mold {} in high", st.height_in);
}

#[test]
fn flat_templates_in_blocks_from_the_gunwale_up() {
    let l = flat();
    let st = &l.stack;
    // Foam layer k sits k foam thicknesses above the floor, until the keel.
    for (k, ly) in l.layers.iter().enumerate() {
        assert!((ly.pos_in - k as f64 * 2.0).abs() < 1e-9);
        assert_eq!(ly.thick_in, 2.0);
        assert!(!ly.foam.is_empty());
        // Each foam piece fits a foam sheet and has exactly one template.
        assert_eq!(ly.foam.len(), ly.pieces.len());
        for f in &ly.foam {
            assert!(fits(f, 96.0, 48.0), "{} foam piece fits the foam sheet", ly.label);
        }
        // Blocks: every joint between foam pieces is a block joint, the same in every layer.
        let (j, x0, x1) = (&st.joints_in, bbox(&ly.foam[0]).0[0], bbox(&ly.foam[ly.foam.len() - 1]).1[0]);
        for w in ly.foam.windows(2) {
            let x = bbox(&w[0]).1[0];
            assert!((x - bbox(&w[1]).0[0]).abs() < 1e-9, "{} pieces end to end", ly.label);
            assert!(
                j.iter().any(|&k| (k - x).abs() < 1e-9),
                "{} joint at {x} is not a block joint",
                ly.label
            );
        }
        let inside = j.iter().filter(|&&k| k > x0 + 0.01 && k < x1 - 0.01).count();
        assert_eq!(ly.foam.len(), inside + 1, "{}: one piece per block it reaches", ly.label);
    }
    assert_eq!(st.layers, l.layers.len());
    assert_eq!(st.foam_layers, l.layers.len());
    assert_eq!(st.foam_pieces, l.pieces.len());
    assert!((st.layers - 1) as f64 * 2.0 < st.height_in && st.height_in <= st.layers as f64 * 2.0 + 1e-9);
    // The floor layer's template is the inner gunwale outline: as wide as the inner beam,
    // symmetric, pieces end to end over the hollow run.
    let l1 = &l.layers[0].foam;
    let w = l1.iter().map(|f| 2.0 * bbox(f).1[1]).fold(0.0, f64::max);
    assert!((w - st.beam_in).abs() < 0.05, "floor layer {w} vs inner beam {}", st.beam_in);
    assert!(st.beam_in < 30.0 && st.beam_in > 15.0, "inner beam {}", st.beam_in);
    for f in l1 {
        let (lo, hi) = bbox(f);
        assert!((lo[1] + hi[1]).abs() < 1e-9, "symmetric about the centreline");
    }
    for w in l1.windows(2) {
        assert!((bbox(&w[0]).1[0] - bbox(&w[1]).0[0]).abs() < 1e-9, "joint");
    }
    let span = bbox(&l1[l1.len() - 1]).1[0] - bbox(&l1[0]).0[0];
    assert!((span - st.length_in).abs() < 0.5, "floor layer spans the hollow run");
    // Layers do not grow toward the keel (off1's sides bulge a hair below the gunwale, so
    // allow 1%), and every template below the top carries a sanding line.
    let area = |k: usize| l.layers[k].foam.iter().map(|f| shoelace(f)).sum::<f64>();
    for k in 1..l.layers.len() {
        assert!(area(k) < 1.01 * area(k - 1), "layer {} bigger than the one below", k + 1);
    }
    assert!(area(l.layers.len() - 1) < 0.8 * area(0), "the top layer is the keel's");
    assert!(
        l.pieces.iter().filter(|p| p.layer + 1 < l.layers.len()).all(|p| p.marks.len() > 10),
        "sanding line"
    );
    check_sheets(l);
    assert!(l.utilization > 0.3 && l.utilization <= 1.0);
}

#[test]
fn packings_trade_pieces_for_sheets() {
    let ex = example();
    let cont = from_result(&ex, &MoldSpec { packing: Packing::Continuous, ..MoldSpec::default() }).unwrap();
    // Continuous: fewest pieces per layer (one fewer would be longer than the foam sheet).
    for ly in &cont.layers {
        let total: f64 = ly.foam.iter().map(|f| bbox(f).1[0] - bbox(f).0[0]).sum();
        let n = ly.foam.len();
        assert!(n == 1 || total / (n - 1) as f64 > 96.0, "{}: {n} pieces over {total:.1} in", ly.label);
    }
    // Blocks never needs fewer pieces than continuous, but its joints line up.
    let blocks = flat();
    assert!(blocks.pieces.len() >= cont.pieces.len());
    assert!(!blocks.stack.joints_in.is_empty() && cont.stack.joints_in.is_empty());
    let tight = from_result(&ex, &MoldSpec { packing: Packing::Tight, ..MoldSpec::default() }).unwrap();
    let few = from_result(&ex, &MoldSpec { packing: Packing::FewestShapes, ..MoldSpec::default() }).unwrap();
    // Tight never uses more sheets; continuous never more pieces.
    assert!(tight.sheets.len() <= cont.sheets.len());
    check_sheets(&cont);
    assert!(tight.pieces.len() >= cont.pieces.len());
    // Fewest shapes: half templates trace every foam piece twice (both sides), fewer shapes.
    let traces: usize = few.pieces.iter().map(|p| p.traces.len()).sum();
    assert_eq!(traces, 2 * few.stack.foam_pieces);
    assert!(few.pieces.len() < cont.pieces.len());
    assert!(few.sheets.len() < cont.sheets.len());
    check_sheets(&tight);
    check_sheets(&few);
}

#[test]
fn mixed_foam_thicknesses_and_sheet_sizes() {
    let spec = MoldSpec {
        foam_in: vec![1.5, 2.0],
        foam_sheets: vec![[96.0, 48.0], [72.0, 36.0]],
        sheets: vec![[96.0, 48.0], [48.0, 48.0]],
        ..MoldSpec::default()
    };
    let l = from_result(&example(), &spec).unwrap();
    let st = &l.stack;
    // The order covers the mold, by less than the thinnest foam, thick layers on the floor.
    let sum: f64 = st.foam_order_in.iter().sum();
    assert!(sum >= st.height_in - 1e-9 && st.over_in < 1.5 && (sum - st.height_in - st.over_in).abs() < 1e-9);
    assert!(st.foam_order_in.windows(2).all(|w| w[0] >= w[1]), "{:?}", st.foam_order_in);
    assert!(st.foam_order_in.iter().all(|t| *t == 1.5 || *t == 2.0));
    // Each layer starts where the ones below end, and is as thick as its foam.
    let mut h = 0.0;
    for (ly, t) in l.layers.iter().zip(&st.foam_order_in) {
        assert!((ly.pos_in - h).abs() < 1e-9);
        assert_eq!((ly.thick_in, ly.foam_in), (*t, *t));
        h += t;
    }
    // Every sheet is one of the sizes on hand.
    assert!(l.sheets.iter().all(|s| spec.sheets.contains(&[s.length_in, s.width_in])));
    check_sheets(&l);
}

#[test]
fn nesting_inside_never_costs_sheets() {
    let ex = example();
    let base = MoldSpec { packing: Packing::FewestShapes, ..MoldSpec::default() };
    let plain = from_result(&ex, &base).unwrap();
    let nested = from_result(&ex, &MoldSpec { nest_inside: true, ..base }).unwrap();
    assert!(nested.sheets.len() <= plain.sheets.len());
    check_sheets(&nested);
    // A part cut out of another lies inside it.
    for sh in &nested.sheets {
        for p in &sh.parts {
            if let Some(h) = p.inside {
                let (hl, hh) = bbox(&sh.parts[h].outline);
                let (pl, ph) = bbox(&p.outline);
                assert!(pl[0] > hl[0] && pl[1] > hl[1] && ph[0] < hh[0] && ph[1] < hh[1]);
            }
        }
    }
}

#[test]
fn alignment_holes_line_up_through_the_stack_and_are_cut_first() {
    let l = flat();
    // Blocks: the holes are at the same stations in every layer (12 in from the bow end of the
    // hull), so a dowel goes straight down through the stack.
    let xs = |k: usize| -> Vec<i64> {
        let mut v: Vec<i64> = l.layers[k]
            .pieces
            .iter()
            .flat_map(|&i| &l.pieces[i].holes)
            .map(|h| (h[0] * 1e6).round() as i64)
            .collect();
        v.sort();
        v
    };
    let floor = xs(0);
    assert!(floor.len() >= 10, "{} holes in the floor layer", floor.len());
    for k in 1..l.layers.len() {
        assert!(xs(k).iter().all(|x| floor.contains(x)), "layer {} holes off the floor layer's", k + 1);
    }
    // DXF: a circle per hole, each piece's holes before its outline.
    let dxf = to_dxf(l, 0).unwrap();
    let holes: usize = l.sheets[0].parts.iter().map(|p| p.holes.len()).sum();
    assert_eq!(dxf.matches("\nCIRCLE\n").count(), holes);
    let first_circle = dxf.find("\nCIRCLE\n").unwrap();
    let first_cut = dxf.find("POLYLINE\n8\nCUT\n").unwrap();
    assert!(first_circle < first_cut, "holes of the first piece before its outline");
    // A cut width moves the holes in by half of it.
    let k = from_result(&example(), &MoldSpec { kerf_in: 0.05, ..MoldSpec::default() }).unwrap();
    assert!(to_dxf(&k, 0).unwrap().contains("40\n0.1000\n"), "hole radius 0.125 - 0.025");
    assert!(
        from_result(&example(), &MoldSpec { kerf_in: 0.3, ..MoldSpec::default() }).is_err(),
        "cut width over the gap"
    );
}

/// From each tip a template outline tapers to its point: moving away from the tip the half
/// breadth grows ever more slowly (no bulb, neck or spike).
fn tips_taper(l: &Layout) {
    for ly in &l.layers {
        for (k, f) in ly.foam.iter().enumerate() {
            let mut top: Vec<Pt> = f.iter().filter(|p| p[1] > 1e-9).copied().collect();
            top.sort_by(|a, b| a[0].total_cmp(&b[0]));
            for stern in [false, true] {
                let pts: Vec<Pt> =
                    if stern { top.iter().rev().map(|p| [-p[0], p[1]]).collect() } else { top.clone() };
                if pts.len() < 8 || pts[0][1] > 0.6 {
                    continue; // not a tip (a joint or a square end)
                }
                let x0 = pts[0][0];
                let near: Vec<&Pt> = pts.iter().filter(|p| p[0] <= x0 + 12.0).collect();
                let (mut lo, mut i) = (f64::INFINITY, 0);
                while i + 1 < near.len() {
                    let mut j = i + 1;
                    while j + 1 < near.len() && near[j][0] - near[i][0] < 0.5 {
                        j += 1;
                    }
                    if near[j][0] - near[i][0] > 1e-6 {
                        let s = (near[j][1] - near[i][1]) / (near[j][0] - near[i][0]);
                        let end = if stern { "stern" } else { "bow" };
                        assert!(
                            s <= lo + 0.15,
                            "{} {end} tip bulges: slope {s:.2} after {lo:.2}",
                            ly.foam_labels[k]
                        );
                        lo = lo.min(s);
                    }
                    i = j;
                }
            }
        }
    }
}

#[test]
fn template_tips_taper_to_a_point() {
    tips_taper(flat());
    // Thin foam puts layers right at the keel, where the slices' noses differ most.
    let spec = MoldSpec { foam_in: vec![1.0], packing: Packing::Continuous, ..MoldSpec::default() };
    tips_taper(&from_result(&example(), &spec).unwrap());
}

#[test]
fn half_canoe_traces_every_foam_piece_from_bow_half_templates() {
    let ex = example();
    for packing in [Packing::Blocks, Packing::Continuous] {
        let base = MoldSpec { packing, ..MoldSpec::default() };
        let full = from_result(&ex, &base).unwrap();
        let half = from_result(&ex, &MoldSpec { half_canoe: true, ..base.clone() }).unwrap();
        assert!(half.pieces.len() < full.pieces.len() && half.sheets.len() <= full.sheets.len());
        // Every foam piece is traced exactly once, and no template lies wholly aft of midships.
        let mut traced: Vec<String> = half
            .pieces
            .iter()
            .flat_map(|p| &p.traces)
            .map(|t| t.split(' ').next().unwrap().to_string())
            .collect();
        traced.sort();
        let mut foam: Vec<String> = half.layers.iter().flat_map(|l| l.foam_labels.clone()).collect();
        foam.sort();
        assert_eq!(traced, foam, "{packing:?}");
        let mid = 0.5 * (bbox(&half.layers[0].foam[0]).0[0] + bbox(half.layers[0].foam.last().unwrap()).1[0]);
        for p in &half.pieces {
            assert!(bbox(&p.outline).0[0] < mid - 0.01, "{} is aft of midships", p.label);
        }
        check_sheets(&half);
    }
    // Only for flat tracing templates.
    let bad = MoldSpec { half_canoe: true, boards: Boards::Mold, ..MoldSpec::default() };
    assert!(from_result(&ex, &bad).is_err());
    let bad = MoldSpec { half_canoe: true, ..ribs_spec() };
    assert!(from_result(&ex, &bad).is_err());
}

#[test]
fn boards_in_the_mold_sit_between_the_foam_layers() {
    let spec = MoldSpec { boards: Boards::Mold, ..MoldSpec::default() };
    let l = from_result(&example(), &spec).unwrap();
    let pitch = 2.0 + spec.board_in;
    for (k, ly) in l.layers.iter().enumerate() {
        assert!((ly.pos_in - k as f64 * pitch).abs() < 1e-9);
        assert_eq!(ly.thick_in, spec.board_in);
    }
    assert!(l.stack.foam_layers >= l.stack.layers - 1);
    check_sheets(&l);
}

#[test]
fn upright_slabs_every_foam_thickness_along_the_hollow_run() {
    let spec = ribs_spec();
    let l = from_result(&example(), &spec).unwrap();
    let n = (l.stack.length_in / 12.0 - 1e-6).ceil() as usize;
    assert_eq!(l.layers.len(), n);
    assert_eq!(l.pieces.len(), n);
    for w in l.layers.windows(2) {
        assert!((w[1].pos_in - w[0].pos_in - 12.0).abs() < 1e-9);
    }
    // Each slab stands on the floor (y from -half gunwale to +half) and reaches the inner keel.
    let mid = &l.pieces[n / 2];
    let (lo, hi) = bbox(&mid.outline);
    assert!(lo[1].abs() < 1e-9 && (lo[0] + hi[0]).abs() < 1e-9);
    assert!(
        hi[1] > 8.0 && hi[1] < l.stack.depth_in - l.stack.wall_in + 1e-6,
        "midships slab {} in high",
        hi[1]
    );
    check_sheets(&l);
    // Fewest shapes: half slabs, traced on both sides.
    let half = from_result(&example(), &MoldSpec { packing: Packing::FewestShapes, ..spec }).unwrap();
    assert!(half.pieces.iter().all(|p| p.traces.len() == 2 && bbox(&p.outline).0[0].abs() < 1e-9));
}

#[test]
fn dxf_is_r12_with_one_outline_per_piece_and_deterministic() {
    let l = flat();
    let a = to_dxf(l, 0).unwrap();
    let ribs = to_dxf(&from_result(&example(), &ribs_spec()).unwrap(), 0).unwrap();
    assert_eq!(ribs, to_dxf(&from_result(&example(), &ribs_spec()).unwrap(), 0).unwrap(), "deterministic");
    assert!(a.contains("AC1009") && a.ends_with("EOF\n"));
    // Cut lines only by default: no reference layers in the entities.
    let polylines = a.matches("\nPOLYLINE\n").count();
    assert_eq!(polylines, l.sheets[0].parts.len(), "one outline per piece");
    assert!(!a.contains("\nTEXT\n") && !a.contains("\nLINE\n") && !a.contains("POLYLINE\n8\nSHEET"));
    let marked = from_result(&example(), &MoldSpec { dxf_marks: true, ..MoldSpec::default() }).unwrap();
    let b = to_dxf(&marked, 0).unwrap();
    assert_eq!(b.matches("\nPOLYLINE\n").count(), marked.sheets[0].parts.len() + 1, "plus the sheet edge");
    assert!(b.contains("\nTEXT\n") && b.contains("\nLINE\n"));
    let cut = a.matches("POLYLINE\n8\nCUT\n").count();
    assert_eq!(cut, l.sheets[0].parts.len());
    assert!(a.is_ascii());
    assert!(to_dxf(l, l.sheets.len()).is_err());
    let stem: String =
        l.hull.chars().map(|c| if c.is_ascii_alphanumeric() || c == '-' { c } else { '_' }).collect();
    assert_eq!(dxf_name(l, 0), format!("{stem}_sheet_01.dxf"));
}

#[test]
fn bad_settings_and_tiny_sheets_fail() {
    let ex = example();
    assert!(from_result(&ex, &MoldSpec { foam_in: vec![0.0], ..MoldSpec::default() }).is_err());
    assert!(from_result(&ex, &MoldSpec { foam_sheets: vec![[96.0, 0.0]], ..MoldSpec::default() }).is_err());
    assert!(from_result(&ex, &MoldSpec { gap_in: -1.0, ..MoldSpec::default() }).is_err());
    let tiny = MoldSpec { sheets: vec![[12.0, 12.0]], ..MoldSpec::default() };
    let e = from_result(&ex, &tiny).unwrap_err().to_string();
    assert!(e.contains("does not split into blocks that fit"), "{e}");
    let cont = MoldSpec { packing: Packing::Continuous, ..tiny };
    let e = from_result(&ex, &cont).unwrap_err().to_string();
    assert!(e.contains("does not fit"), "{e}");
    // Foam sheets smaller than the canoe is wide, both ways: no number of lengths fits.
    let narrow = MoldSpec { foam_sheets: vec![[10.0, 10.0]], ..MoldSpec::default() };
    assert!(from_result(&ex, &narrow).unwrap_err().to_string().contains("foam sheets (10 x 10 in)"));
}
